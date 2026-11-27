"""Exercise queued email replies and SMTP threading without a live mailbox."""

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "codex_qq_inbox.py"
SPEC = importlib.util.spec_from_file_location("qq_inbox_under_test", SOURCE)
inbox = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inbox
SPEC.loader.exec_module(inbox)
mail = inbox.mail

THREAD = "00000000-0000-4000-8000-000000000001"
SENDER = "author@example.com"
MAILBOX = "bot@example.com"
ROOT_ID = "<original@example.com>"
PARENT_ID = "<reply@example.com>"
SUBJECT = "[完成通知][测试项目][测试会话][续聊码 0123456789abcdef01234567]"


class ReplyNotificationTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.multiple(
            mail, CODEX_PROFILE=root, INBOX_STATE=root / "inbox-state.sqlite3",
            SETTINGS=root / "settings.json", TURN_NOTICES=root / "turn-notices.sqlite3",
            REPLY_ROUTES=root / "reply-routes.sqlite3", ROUTES=root / "routes",
        ))
        self.db = self.enterContext(contextlib.closing(sqlite3.connect(mail.INBOX_STATE)))
        inbox.init_state(self.db)
        self.transcript = root / f"rollout-2026-01-01-{THREAD}.jsonl"
        with contextlib.closing(sqlite3.connect(root / "state_5.sqlite")) as state, state:
            state.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
            state.execute("INSERT INTO threads VALUES (?, ?)", (THREAD, str(self.transcript)))
        mail.remember_reply_route(ROOT_ID, MAILBOX, SENDER, SUBJECT, THREAD)
        mail.write_json(mail.SETTINGS, {"sender": MAILBOX, "recipient": MAILBOX, "protected_code": "YQ=="})
        self.enterContext(patch.object(mail, "windows_protect", side_effect=lambda data, **_: data))
        self.config = {"mailbox": MAILBOX, "project_root": str(root)}

    def command(self):
        # The input keeps its own Message-ID, plus a reference to the original notice.
        message = EmailMessage()
        message["From"] = SENDER
        message["To"] = MAILBOX
        message["Subject"] = "回复：" + SUBJECT
        message["Message-ID"] = PARENT_ID
        message["In-Reply-To"] = ROOT_ID
        message.set_content("请继续。\n> 上一轮正文")
        command, _ = inbox.parse_mail(message.as_bytes(), MAILBOX)
        self.assertEqual(command.text, "请继续。")
        return command

    def queue_reply(self, *, busy=False):
        command = self.command()
        with (patch.object(inbox, "connect_codex") as connect,
              patch.object(inbox, "resolve_chat") as resolve,
              contextlib.redirect_stdout(io.StringIO())):
            server = connect.return_value.__enter__.return_value
            resolve.return_value = THREAD
            if busy:
                resolve.side_effect = inbox.BusyThread()
            inbox.submit_command(self.db, self.config, command, 1)
            inbox.submit_command(self.db, self.config, command, 1)
            server.call.assert_called_once()
        method, args = server.call.call_args.args
        self.assertEqual(method, "thread/queue/add")
        self.assertEqual(args["input"], [{"type": "text", "text": "请继续。"}])
        return args["clientUserMessageId"]

    def append(self, **event):
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": event}) + "\n")

    def record_turn(self, client_id):
        self.append(type="item_completed", turn_id="reply-turn", item={"type": "UserMessage", "client_id": client_id})

    def test_completion_replies_in_thread_once(self):
        client_id = self.queue_reply(busy=True)
        self.append(type="task_complete", turn_id="previous-turn", last_agent_message="其他轮的结果")
        self.record_turn(client_id)
        with patch.object(mail.smtplib, "SMTP_SSL") as connection, contextlib.redirect_stdout(io.StringIO()):
            smtp = connection.return_value.__enter__.return_value
            inbox.deliver_completed_replies(self.db, MAILBOX)
            smtp.send_message.assert_not_called()
            self.append(type="task_complete", turn_id="reply-turn", last_agent_message="本轮已完成。")
            inbox.deliver_completed_replies(self.db, MAILBOX)
            inbox.deliver_completed_replies(self.db, MAILBOX)
            smtp.send_message.assert_called_once()
        response = smtp.send_message.call_args.args[0]
        self.assertEqual(str(response["To"]), SENDER)
        self.assertEqual(str(response["In-Reply-To"]), PARENT_ID)
        self.assertEqual(str(response["References"]), f"{ROOT_ID} {PARENT_ID}")
        self.assertEqual(str(response["Subject"]), "Re: " + SUBJECT)
        self.assertIn("本轮已完成。", response.get_content())
        self.assertEqual(inbox.parse_mail(response.as_bytes(), MAILBOX)[0], None)
        for headers in ((str(response["Message-ID"]),), ()):
            self.assertEqual(mail.find_reply_chat(MAILBOX, SENDER, "Re: " + SUBJECT, headers, ()), THREAD)

    def test_stop_only_suppresses_matching_turn_and_recipient(self):
        self.record_turn(self.queue_reply())
        event = {"hook_event_name": "Stop", "session_id": THREAD, "transcript_path": str(self.transcript)}
        outcomes = (("reply-turn", SENDER, False), ("reply-turn", "other@example.com", True),
                    ("desktop-turn", SENDER, True))
        # Each case represents a separate hook invocation with its own configured recipient.
        for turn_id, recipient, expected in outcomes:
            payload = {**event, "turn_id": turn_id}
            stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")))
            with (patch.object(mail.sys, "stdin", stdin), contextlib.redirect_stdout(io.StringIO()),
                  patch.object(mail, "take_turn_notice", return_value=(recipient, "问题")),
                  patch.object(mail, "send_notice") as send):
                mail.run_hook()
                self.assertEqual(send.called, expected)

    def test_smtp_rejection_retries_existing_completion(self):
        self.record_turn(self.queue_reply())
        self.append(type="task_complete", turn_id="reply-turn", last_agent_message="已完成。")
        with patch.object(mail.smtplib, "SMTP_SSL") as connection, contextlib.redirect_stdout(io.StringIO()):
            smtp = connection.return_value.__enter__.return_value
            smtp.send_message.side_effect = [mail.smtplib.SMTPDataError(451, b"Try again"), {}]
            inbox.deliver_completed_replies(self.db, MAILBOX)
            inbox.deliver_completed_replies(self.db, MAILBOX)
            inbox.deliver_completed_replies(self.db, MAILBOX)
            deliveries = smtp.send_message.call_args_list
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(deliveries[0].args[0]["Message-ID"], deliveries[1].args[0]["Message-ID"])


if __name__ == "__main__":
    unittest.main()

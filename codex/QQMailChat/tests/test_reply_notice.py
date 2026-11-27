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
from unittest.mock import Mock, call, patch


SOURCE = Path(__file__).resolve().parents[1] / "codex_qq_inbox.py"
SPEC = importlib.util.spec_from_file_location("qq_inbox_under_test", SOURCE)
inbox = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inbox
SPEC.loader.exec_module(inbox)
mail = inbox.mail

THREAD = "01234567-89ab-4def-8123-456789abcdef"
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

    def raw_email(self, subject, body, *, in_reply_to=None, message_id=PARENT_ID, sender=SENDER):
        message = EmailMessage()
        message["From"] = sender
        message["To"] = MAILBOX
        message["Subject"] = subject
        message["Message-ID"] = message_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        message.set_content(body)
        return message.as_bytes()

    def command_email(self, subject, body, *, in_reply_to=None):
        return inbox.parse_mail(self.raw_email(subject, body, in_reply_to=in_reply_to), MAILBOX)

    def command(self):
        command, _ = self.command_email("回复：" + SUBJECT, "请继续。\n> 上一轮正文", in_reply_to=ROOT_ID)
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

    def test_explicit_id_mail_resumes_idle_chat_and_waits_for_completion(self):
        command, _ = self.command_email(f"[AI会话][{THREAD.upper()}]", "请继续。\n> 这是指令的一部分")
        self.assertEqual(command.thread_id, THREAD)
        self.assertFalse(command.is_reply)
        self.assertIn("> 这是指令的一部分", command.text)
        with patch.object(inbox, "connect_codex") as connect, contextlib.redirect_stdout(io.StringIO()):
            server = connect.return_value.__enter__.return_value
            server.call.side_effect = [
                {"thread": {"id": THREAD}}, {"thread": {"id": THREAD}}, {"turn": {"id": "test-turn"}},
            ]
            server.wait_turn.return_value = "completed"
            inbox.submit_command(self.db, self.config, command, 1)
        self.assertEqual(server.call.call_args_list[:2], [
            call("thread/read", {"threadId": THREAD, "includeTurns": False}),
            call("thread/resume", {"threadId": THREAD, "excludeTurns": True}),
        ])
        method, params = server.call.call_args.args
        self.assertEqual((method, params["threadId"]), ("turn/start", THREAD))
        server.wait_turn.assert_called_once_with(THREAD, "test-turn")
        server.pages.assert_not_called()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reply_notifications").fetchone(), (0,))

    def test_explicit_id_mail_queues_when_target_chat_is_busy(self):
        command, _ = self.command_email(f"[AI会话][{THREAD}]", "请继续。")
        with patch.object(inbox, "connect_codex") as connect, contextlib.redirect_stdout(io.StringIO()):
            server = connect.return_value.__enter__.return_value
            server.call.side_effect = [{"thread": {"id": THREAD, "status": {"type": "active"}}}, {}]
            inbox.submit_command(self.db, self.config, command, 1)
        self.assertEqual([request.args[0] for request in server.call.call_args_list], ["thread/read", "thread/queue/add"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reply_notifications").fetchone(), (0,))

    def test_explicit_id_does_not_inherit_reply_permissions(self):
        # A reply header on an explicit-ID command cannot bypass its normal sender checks.
        command, message = self.command_email(f"[AI会话][{THREAD}]", "请继续。", in_reply_to=ROOT_ID)
        with (patch.object(inbox, "qq_dmarc_passes", return_value=True) as dmarc,
              patch.object(inbox, "qq_spf_passes", return_value=True) as spf):
            disallowed = {**self.config, "senders": [MAILBOX], "spf_senders": [SENDER]}
            self.assertFalse(inbox.authenticated(None, disallowed, command, message))
            allowed = {**self.config, "senders": [SENDER]}
            self.assertTrue(inbox.authenticated(None, allowed, command, message))
            dmarc.return_value = False
            self.assertFalse(inbox.authenticated(None, allowed, command, message))
            spf.assert_not_called()
            self.assertTrue(inbox.authenticated(None, {**allowed, "spf_senders": [SENDER]}, command, message))
            spf.assert_called_once_with(message, SENDER)

    def test_name_format_and_invalid_id(self):
        command, _ = self.command_email("[AI会话][测试项目][测试会话]", "请继续。")
        self.assertEqual((command.project, command.chat, command.thread_id), ("测试项目", "测试会话", None))
        for bad_id in ("", "01234567", "not-an-id"):
            with self.subTest(bad_id=bad_id):
                invalid, _ = self.command_email(f"[AI会话][{bad_id}]", "请继续。")
                self.assertIn("完整的本地会话 ID", invalid.rejection_reason)
                self.assertIsNone(invalid.thread_id)

    def test_missing_id_is_rejected_without_attempting_to_resume_or_create(self):
        command, _ = self.command_email(f"[AI会话][{THREAD}]", "请继续。")
        server = Mock()
        server.call.side_effect = inbox.RpcError("thread/read", {"code": -32600, "message": f"thread not loaded: {THREAD}"})
        with self.assertRaisesRegex(inbox.RejectedMail, "未找到该本地 Codex 会话"):
            inbox.resolve_chat(server, Path(self.config["project_root"]), command)
        server.call.assert_called_once_with("thread/read", {"threadId": THREAD, "includeTurns": False})
        server.pages.assert_not_called()

    def test_failed_commands_reply_in_original_threads_and_advance_the_inbox(self):
        # The fifth UID repeats the first Message-ID. The fourth sender is unauthorized.
        by_id = f"[AI会话][{THREAD}]"
        by_name = "[AI会话][测试项目][测试会话]"
        named_id, malformed_id = "<named@example.com>", "<malformed@example.com>"
        messages = {
            1: self.raw_email(by_id, "测试找不到的会话。", in_reply_to=ROOT_ID),
            2: self.raw_email(by_name, "测试重名的会话。", message_id=named_id),
            3: self.raw_email("[AI会话][123]", "测试无效的会话 ID。", message_id=malformed_id),
            4: self.raw_email(by_id, "未授权。", sender="unknown@example.com", message_id="<unknown@example.com>"),
        }
        messages[5] = messages[1]
        box = Mock()
        box.response.return_value = ("UIDVALIDITY", [b"42"])
        box.uid.return_value = ("OK", [b"1 2 3 4 5"])
        inbox.checkpoint(self.db, MAILBOX, 42, 0)
        config = {**self.config, "senders": [SENDER]}
        with (patch.object(inbox, "connect_codex") as connect,
              patch.object(inbox, "inbox_connection", return_value=contextlib.nullcontext(box)),
              patch.object(inbox, "read_message", side_effect=lambda _, uid: messages[uid]),
              patch.object(inbox, "qq_dmarc_passes", return_value=True),
              contextlib.redirect_stdout(io.StringIO())):
            server = connect.return_value.__enter__.return_value
            server.call.side_effect = inbox.RpcError("thread/read", {"code": -32600, "message": f"thread not loaded: {THREAD}"})
            server.pages.side_effect = [
                [{"id": "project", "name": "测试项目"}],
                [{"id": "first", "name": "测试会话"}, {"id": "second", "name": "测试会话"}],
            ]
            inbox.poll(self.db, config)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(self.db.execute("SELECT last_uid FROM checkpoint").fetchone(), (5,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM failed_replies").fetchone(), (3,))

        with patch.object(mail.smtplib, "SMTP_SSL") as connection, contextlib.redirect_stdout(io.StringIO()):
            smtp = connection.return_value.__enter__.return_value
            smtp.send_message.side_effect = [mail.smtplib.SMTPDataError(451, b"Try again"), {}, {}, {}]
            for _ in range(3):
                inbox.deliver_failed_replies(self.db, MAILBOX)
            attempts = [attempt.args[0] for attempt in smtp.send_message.call_args_list]
        self.assertEqual(len(attempts), 4)
        self.assertEqual(attempts[0]["Message-ID"], attempts[3]["Message-ID"])
        replies = {str(message["In-Reply-To"]): message for message in attempts}
        self.assertEqual(set(replies), {PARENT_ID, named_id, malformed_id})
        for parent_id, subject, explanation in (
            (PARENT_ID, by_id, "未找到该本地 Codex 会话"),
            (named_id, by_name, "有多个同名会话"),
            (malformed_id, "[AI会话][123]", "完整的本地会话 ID"),
        ):
            response = replies[parent_id]
            self.assertEqual(str(response["To"]), SENDER)
            self.assertEqual(str(response["Subject"]), "Re: " + subject)
            self.assertEqual(str(response["Auto-Submitted"]), "auto-replied")
            self.assertIn(explanation, response.get_content())
            self.assertIsNone(inbox.parse_mail(response.as_bytes(), MAILBOX)[0])
        self.assertEqual(str(replies[PARENT_ID]["References"]), f"{ROOT_ID} {PARENT_ID}")
        self.assertFalse(mail.find_reply_chat(MAILBOX, SENDER, "", (str(replies[PARENT_ID]["Message-ID"]),), ()))

    def test_busy_rpc_is_retried_without_a_failure_receipt(self):
        command, _ = self.command_email("[AI会话][测试项目][测试会话]", "请继续。")
        error = inbox.RpcError("thread/resume", {"code": -32600, "message": "thread already has an active writer"})
        with patch.object(inbox, "submit_command", side_effect=error), self.assertRaises(inbox.RpcError):
            inbox.submit_or_record_failure(self.db, self.config, command, 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM failed_replies").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()

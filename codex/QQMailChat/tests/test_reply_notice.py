"""Exercise QQMailChat routing and notifications without a live mailbox."""

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
            NOTICE_PREFERENCES=root / "notice-preferences.sqlite3",
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
                resolve.side_effect = inbox.BusyThread(THREAD)
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

    def submit_prompt(self, text, turn_id):
        event = {"hook_event_name": "UserPromptSubmit", "session_id": THREAD, "turn_id": turn_id, "prompt": text}
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(event).encode("utf-8")))
        output = io.StringIO()
        with patch.object(mail.sys, "stdin", stdin), contextlib.redirect_stdout(output):
            mail.run_prompt_hook()
        return mail.turn_key(event), json.loads(output.getvalue())

    def test_default_recipient_is_saved_and_reused_only_when_requested(self):
        with patch.object(mail, "prompt_default_recipient", return_value=SENDER) as dialog:
            first, result = self.submit_prompt("请完成任务。\n完成后邮件通知", "first")
            self.assertIn("hookSpecificOutput", result)
            self.assertEqual(mail.take_turn_notice(first), (SENDER, "请完成任务。"))
            self.assertEqual(mail.take_turn_notice(first), (None, ""))
            later, _ = self.submit_prompt("完成后邮件通知。", "later")
            ordinary, _ = self.submit_prompt("继续下一项任务。", "ordinary")
            self.assertEqual(mail.take_turn_notice(later), (SENDER, ""))
            self.assertEqual(mail.take_turn_notice(ordinary), (None, ""))
            dialog.assert_called_once()

    def test_explicit_recipients_take_priority_without_replacing_the_default(self):
        explicit = "override@example.com"
        with patch.object(mail, "prompt_default_recipient", return_value=SENDER) as dialog:
            first, _ = self.submit_prompt(f"完成后邮件通知{explicit}\n完成后邮件通知", "explicit")
            dialog.assert_not_called()
            self.assertFalse(mail.NOTICE_PREFERENCES.exists())
            self.submit_prompt("完成后邮件通知", "explicit")
            self.assertEqual(mail.take_turn_notice(first), (explicit, ""))
            later, _ = self.submit_prompt("完成后邮件通知", "default")
            self.assertEqual(mail.take_turn_notice(later), (SENDER, ""))
            dialog.assert_called_once()

    def test_cancel_leaves_the_default_unset_for_the_next_request(self):
        with patch.object(mail, "prompt_default_recipient", side_effect=[None, SENDER]):
            first, result = self.submit_prompt("完成后邮件通知", "cancel")
            self.assertIn("systemMessage", result)
            self.assertEqual(mail.take_turn_notice(first), (None, ""))
            later, _ = self.submit_prompt("完成后邮件通知", "retry")
            self.assertEqual(mail.take_turn_notice(later), (SENDER, ""))

    def test_edit_default_prefills_and_updates_only_future_notifications(self):
        updated = "updated@example.com"
        with patch.object(mail, "prompt_default_recipient", side_effect=[SENDER, updated, None]) as dialog:
            first, _ = self.submit_prompt("完成后邮件通知", "first")
            edit, result = self.submit_prompt("修改默认接收通知邮件地址", "edit")
            self.assertIn("已保存", result["systemMessage"])
            self.assertEqual(dialog.call_args.kwargs, {"edit": True, "current": SENDER})
            self.assertEqual(mail.take_turn_notice(edit), (None, ""))
            self.assertEqual(mail.take_turn_notice(first), (SENDER, ""))
            self.submit_prompt("修改默认接收通知邮件地址", "cancel")
            self.assertEqual(dialog.call_args.kwargs, {"edit": True, "current": updated})
            later, _ = self.submit_prompt("完成后邮件通知", "later")
            self.assertEqual(mail.take_turn_notice(later), (updated, ""))
            self.assertEqual(dialog.call_count, 3)

    def test_examples_and_invalid_addresses_do_not_open_the_dialog(self):
        samples = (
            "“完成后邮件通知”", "`完成后邮件通知`", "> 完成后邮件通知", "```\n完成后邮件通知\n```",
            "例如：\n完成后邮件通知", "完成后邮件通知 xxx", "完成后邮件通知 `invalid`",
            "完成后邮件通知a@example.com\n完成后邮件通知b@example.com\n完成后邮件通知",
            "「修改默认接收通知邮件地址」", "```\n修改默认接收通知邮件地址\n```",
        )
        with patch.object(mail, "prompt_default_recipient") as dialog:
            for index, prompt in enumerate(samples):
                with self.subTest(prompt=prompt):
                    turn, _ = self.submit_prompt(prompt, str(index))
                    self.assertEqual(mail.take_turn_notice(turn), (None, ""))
            dialog.assert_not_called()

    def test_incoming_email_with_bare_notice_still_only_replies_to_sender(self):
        command, _ = self.command_email("回复：" + SUBJECT, "请继续。\n完成后邮件通知", in_reply_to=ROOT_ID)
        with (patch.object(inbox, "connect_codex") as connect,
              patch.object(inbox, "resolve_chat", return_value=THREAD), contextlib.redirect_stdout(io.StringIO())):
            inbox.submit_command(self.db, self.config, command, 1)
        method, args = connect.return_value.__enter__.return_value.call.call_args.args
        self.assertEqual(method, "thread/queue/add")
        self.assertEqual(args["input"], [{"type": "text", "text": "请继续。"}])
        self.assertEqual(self.db.execute("SELECT recipient FROM reply_notifications").fetchone(), (SENDER,))

    def test_edit_command_from_email_stays_out_of_the_local_hook(self):
        command, _ = self.command_email("回复：" + SUBJECT, "修改默认接收通知邮件地址", in_reply_to=ROOT_ID)
        with (patch.object(inbox, "connect_codex") as connect,
              patch.object(inbox, "resolve_chat", return_value=THREAD), contextlib.redirect_stdout(io.StringIO())):
            inbox.submit_command(self.db, self.config, command, 1)
        _, args = connect.return_value.__enter__.return_value.call.call_args.args
        with patch.object(mail, "prompt_default_recipient") as dialog:
            self.submit_prompt(args["input"][0]["text"], "inbox")
            dialog.assert_not_called()
        self.assertEqual(self.db.execute("SELECT recipient FROM reply_notifications").fetchone(), (SENDER,))

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
        self.assertEqual(self.db.execute("SELECT recipient, thread_id FROM reply_notifications").fetchone(), (SENDER, THREAD))

    def test_explicit_id_mail_queues_when_target_chat_is_busy(self):
        command, _ = self.command_email(f"[AI会话][{THREAD}]", "请继续。")
        with patch.object(inbox, "connect_codex") as connect, contextlib.redirect_stdout(io.StringIO()):
            server = connect.return_value.__enter__.return_value
            server.call.side_effect = [{"thread": {"id": THREAD, "status": {"type": "active"}}}, {}]
            inbox.submit_command(self.db, self.config, command, 1)
        self.assertEqual([request.args[0] for request in server.call.call_args_list], ["thread/read", "thread/queue/add"])
        self.assertEqual(self.db.execute("SELECT recipient, thread_id FROM reply_notifications").fetchone(), (SENDER, THREAD))

    def test_named_mail_queues_once_when_target_chat_is_busy(self):
        command, _ = self.command_email("[AI会话][测试项目][测试会话]", "请继续。")
        project = {"id": "tools", "name": "测试项目", "roots": [{"path": self.config["project_root"]}]}
        chat = {"id": THREAD, "name": "测试会话", "cwd": self.config["project_root"]}
        busy_resume = inbox.RpcError("thread/resume", {"code": -32600, "message": "thread already has an active writer"})
        busy_start = inbox.RpcError("turn/start", {"code": -32600, "message": "thread has an active or pending turn"})
        cases = (
            ({**chat, "status": {"type": "active"}}, [{}], ["thread/queue/add"]),
            (chat, [busy_resume, {}], ["thread/resume", "thread/queue/add"]),
            (chat, [{"thread": chat}, busy_start, {}], ["thread/resume", "turn/start", "thread/queue/add"]),
        )
        # Run each busy path through name resolution and the same deduplication as the listener.
        for listed_chat, responses, methods in cases:
            with self.subTest(methods=methods), contextlib.closing(sqlite3.connect(":memory:")) as db:
                inbox.init_state(db)
                with patch.object(inbox, "connect_codex") as connect, contextlib.redirect_stdout(io.StringIO()):
                    server = connect.return_value.__enter__.return_value
                    server.pages.side_effect = [[project], [listed_chat]]
                    server.call.side_effect = responses
                    inbox.submit_or_record_failure(db, self.config, command, 1)
                    inbox.submit_or_record_failure(db, self.config, command, 1)
                requests = server.call.call_args_list
                self.assertEqual([request.args[0] for request in requests], methods)
                self.assertEqual(requests[-1].args[1]["threadId"], THREAD)
                self.assertEqual(db.execute("SELECT thread_id, status FROM submissions").fetchall(), [(THREAD, "queued")])
                self.assertEqual(db.execute("SELECT client_id, parent_id FROM reply_notifications").fetchall(), [
                    (requests[-1].args[1]["clientUserMessageId"], PARENT_ID),
                ])

    def test_new_email_chat_replies_on_completion_and_accepts_followup(self):
        subject = "[AI会话][测试目录][新会话]"
        command, _ = self.command_email(subject, "请开始。")
        project = {"id": "tools", "name": "测试目录", "roots": [{"path": self.config["project_root"]}]}
        with patch.object(inbox, "connect_codex") as connect, contextlib.redirect_stdout(io.StringIO()):
            server = connect.return_value.__enter__.return_value
            server.pages.side_effect = [[project], [], []]
            server.call.side_effect = [{"thread": {"id": THREAD}}, {}, {"turn": {"id": "reply-turn"}}]
            server.wait_turn.return_value = "completed"
            inbox.submit_or_record_failure(self.db, self.config, command, 1)
            inbox.submit_or_record_failure(self.db, self.config, command, 1)
            connect.assert_called_once()
        requests = server.call.call_args_list
        self.assertEqual([request.args[0] for request in requests], ["thread/start", "thread/name/set", "turn/start"])
        self.record_turn(requests[-1].args[1]["clientUserMessageId"])
        self.append(type="task_complete", turn_id="reply-turn", last_agent_message="已经完成。")
        with patch.object(mail.smtplib, "SMTP_SSL") as connection, contextlib.redirect_stdout(io.StringIO()):
            smtp = connection.return_value.__enter__.return_value
            inbox.deliver_completed_replies(self.db, MAILBOX)
            inbox.deliver_completed_replies(self.db, MAILBOX)
            smtp.send_message.assert_called_once()
        response = smtp.send_message.call_args.args[0]
        self.assertEqual(str(response["To"]), SENDER)
        self.assertEqual(str(response["Subject"]), "Re: " + subject)
        self.assertEqual(str(response["In-Reply-To"]), PARENT_ID)
        self.assertIn("已经完成。", response.get_content())
        followup, _ = inbox.parse_mail(self.raw_email(
            str(response["Subject"]), "请继续。\n> 已经完成。", in_reply_to=str(response["Message-ID"]),
            message_id="<followup@example.com>",
        ), MAILBOX)
        self.assertEqual((followup.thread_id, followup.is_reply, followup.text), (THREAD, True, "请继续。"))

    def test_rejected_turn_start_only_enqueues_failure_receipt(self):
        command, _ = self.command_email(f"[AI会话][{THREAD}]", "请继续。")
        with patch.object(inbox, "connect_codex") as connect, contextlib.redirect_stdout(io.StringIO()):
            server = connect.return_value.__enter__.return_value
            server.call.side_effect = [
                {"thread": {"id": THREAD}}, {"thread": {"id": THREAD}},
                inbox.RpcError("turn/start", {"code": -32602, "message": "invalid params"}),
            ]
            inbox.submit_or_record_failure(self.db, self.config, command, 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reply_notifications").fetchone(), (0,))
        self.assertEqual(self.db.execute("SELECT status FROM failed_replies").fetchone(), ("pending",))

    def test_poll_queues_verified_replies_from_recipient_outside_allowlist(self):
        # Cover both registered reply routes and the mail server's authentication paths.
        received = b"Received: from outbound.example.com by mx.qq.com\n"
        dmarc = b"Authentication-Results: mx.qq.com; dmarc=pass header.from=example.com\n" + received
        spf = (received + b"X-QQ-MID: trace\nX-QQ-XMAILINFO: trace\n"
               b"Authentication-Results: mx.qq.com; spf=pass smtp.mailfrom=author@example.com\n")
        messages = {
            1: dmarc + self.raw_email("Re: " + SUBJECT, "按回复头继续。", in_reply_to=ROOT_ID),
            2: spf + self.raw_email("回复：" + SUBJECT, "按主题继续。", message_id="<topic@example.com>"),
            3: dmarc + self.raw_email("Re: " + SUBJECT, "其他发件人。", in_reply_to=ROOT_ID,
                                      message_id="<other@example.com>", sender="other@example.com"),
            4: self.raw_email("Re: " + SUBJECT, "未验证的发件人。", in_reply_to=ROOT_ID,
                              message_id="<unverified@example.com>"),
        }
        box = Mock()
        box.response.return_value = ("UIDVALIDITY", [b"42"])
        box.uid.return_value = ("OK", [b"1 2 3 4"])
        inbox.checkpoint(self.db, MAILBOX, 42, 0)
        config = {**self.config, "senders": [MAILBOX]}
        with (patch.object(inbox, "connect_codex") as connect,
              patch.object(inbox, "inbox_connection", return_value=contextlib.nullcontext(box)),
              patch.object(inbox, "read_message", side_effect=lambda _, uid: messages[uid]),
              contextlib.redirect_stdout(io.StringIO())):
            server = connect.return_value.__enter__.return_value
            server.call.return_value = {"thread": {"id": THREAD}}
            inbox.poll(self.db, config)
        queued = [args[1] for args, _ in server.call.call_args_list if args[0] == "thread/queue/add"]
        self.assertEqual([(item["threadId"], item["input"][0]["text"]) for item in queued],
                         [(THREAD, "按回复头继续。"), (THREAD, "按主题继续。")])
        self.assertEqual(connect.call_count, 2)

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

    def test_subject_formats_default_names_to_global_and_keep_full_ids(self):
        command, _ = self.command_email("[AI会话][测试项目][测试会话]", "请继续。")
        self.assertEqual((command.workspace, command.chat, command.thread_id), ("测试项目", "测试会话", None))
        for prefix in ("AI会话", "AIChat", "aichat", "AiChAt"):
            with self.subTest(prefix=prefix):
                for target in ("[测试会话]", "[Global][测试会话]"):
                    named, _ = self.command_email(f"[{prefix}]{target}", "请继续。")
                    self.assertEqual((named.workspace, named.chat, named.thread_id), ("Global", "测试会话", None))
                    self.assertIsNone(named.rejection_reason)
                by_id, _ = self.command_email(f"[{prefix}][{THREAD.upper()}]", "请继续。")
                self.assertEqual((by_id.workspace, by_id.chat, by_id.thread_id), ("", "", THREAD))
                self.assertIsNone(by_id.rejection_reason)
        numeric, _ = self.command_email("[AIChat][123]", "请继续。")
        self.assertEqual((numeric.workspace, numeric.chat, numeric.thread_id), ("Global", "123", None))
        invalid, _ = self.command_email("[AIChat][]", "请继续。")
        self.assertIn("缺少有效的会话名", invalid.rejection_reason)

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
            3: self.raw_email("[AIChat][]", "测试空会话名。", message_id=malformed_id),
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
            workspace = r"X:\Work\测试项目"
            server.pages.side_effect = [
                [{"id": "project", "name": "测试项目", "roots": [{"path": workspace}]}],
                [{"id": "first", "name": "测试会话", "cwd": workspace},
                 {"id": "second", "name": "测试会话", "cwd": workspace}],
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
            (malformed_id, "[AIChat][]", "缺少有效的会话名"),
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

    def test_workspace_display_name_and_basename_beat_a_legacy_project_match(self):
        project = {"id": "tools", "name": "工具工程", "roots": [{"path": "X:/Projects/Tools"}]}
        wanted = {"id": THREAD, "name": "测试会话", "cwd": r"\\?\X:\Projects\Tools"}
        older = {"id": "legacy", "name": wanted["name"], "cwd": r"X:\Elsewhere", "projectId": project["id"]}
        for workspace in ("工具工程", "TOOLS"):
            with self.subTest(workspace=workspace):
                command, _ = self.command_email(f"[AI会话][{workspace}][测试会话]", "请继续。")
                server = Mock()
                server.pages.side_effect = [[project], [older, wanted]]
                server.call.return_value = {"thread": wanted}
                self.assertEqual(inbox.resolve_chat(server, Path(self.config["project_root"]), command), THREAD)
                server.call.assert_called_once_with("thread/resume", {"threadId": THREAD, "excludeTurns": True})

    def test_archived_workspace_match_beats_active_legacy_project(self):
        command, _ = self.command_email("[AI会话][工具工程][测试会话]", "请继续。")
        project = {"id": "tools", "name": "工具工程", "roots": [{"path": "X:/Projects/Tools"}]}
        wanted = {"id": THREAD, "name": "测试会话", "cwd": r"X:\Projects\Tools"}
        older = {"id": "legacy", "name": wanted["name"], "cwd": r"X:\Elsewhere", "projectId": project["id"]}
        server = Mock()
        server.pages.side_effect = [[project], [older], [wanted]]
        server.call.side_effect = [{"thread": wanted}, {"thread": wanted}]
        self.assertEqual(inbox.resolve_chat(server, Path(self.config["project_root"]), command), THREAD)
        self.assertEqual([request.args[0] for request in server.call.call_args_list], ["thread/unarchive", "thread/resume"])

    def test_unregistered_folder_and_legacy_project_still_resolve(self):
        project = {"id": "tools", "name": "旧项目名", "roots": [{"path": "X:/Projects/Tools"}]}
        wanted = {"id": THREAD, "name": "测试会话", "cwd": r"X:\Projects\Standalone", "projectId": project["id"]}
        for workspace, projects in (("Standalone", []), ("旧项目名", [project])):
            with self.subTest(workspace=workspace):
                command, _ = self.command_email(f"[AI会话][{workspace}][测试会话]", "请继续。")
                server = Mock()
                server.pages.side_effect = [projects, [wanted], []]
                server.call.return_value = {"thread": wanted}
                self.assertEqual(inbox.resolve_chat(server, Path(self.config["project_root"]), command), THREAD)
                server.call.assert_called_once_with("thread/resume", {"threadId": THREAD, "excludeTurns": True})

    def test_new_chat_uses_the_requested_secondary_folder(self):
        root = Path(self.config["project_root"])
        primary, secondary = root / "Primary", root / "Secondary"
        primary.mkdir()
        secondary.mkdir()
        project = {"id": "tools", "name": "工具工程", "roots": [{"path": str(primary)}, {"path": str(secondary)}]}
        command, _ = self.command_email("[AI会话][Secondary][新会话]", "请开始。")
        server = Mock()
        server.pages.side_effect = [[project], [], []]
        server.call.side_effect = [{"thread": {"id": THREAD}}, {}]
        self.assertEqual(inbox.resolve_chat(server, root, command), THREAD)
        method, params = server.call.call_args_list[0].args
        self.assertEqual((method, params["cwd"], params["projectId"]), ("thread/start", str(secondary), "tools"))


class NoticeProjectNameTest(unittest.TestCase):
    def setUp(self):
        profile = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.object(mail, "CODEX_PROFILE", profile))
        self.db = self.enterContext(contextlib.closing(sqlite3.connect(profile / "state_5.sqlite")))
        self.db.executescript("""
            CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, project_id TEXT, cwd TEXT);
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE project_roots (project_id TEXT, path TEXT);
        """)
        with self.db:
            self.db.execute("INSERT INTO threads VALUES (?, ?, NULL, ?)", (
                THREAD, "测试会话", r"\\?\X:\Projects\Tools",
            ))
            self.db.execute("INSERT INTO projects VALUES ('tools', '工具工程')")
            self.db.execute("INSERT INTO project_roots VALUES ('tools', 'x:/projects/tools/')")
        self.event = {"session_id": THREAD}

    def test_subject_uses_unique_workspace_project_for_legacy_chat(self):
        subject, _ = mail.notice_content(self.event, "测试问题")
        self.assertEqual(subject, "[完成通知][工具工程][测试会话]")

    def test_registered_workspace_name_beats_explicit_assignment(self):
        with self.db:
            self.db.execute("INSERT INTO projects VALUES ('explicit', '指定工程')")
            self.db.execute("UPDATE threads SET project_id='explicit'")
        self.assertEqual(mail.project_and_chat(self.event), ("工具工程", "测试会话"))

    def test_ambiguous_workspace_uses_the_actual_folder_name(self):
        with self.db:
            self.db.execute("INSERT INTO projects VALUES ('other', '另一工程')")
            self.db.execute("INSERT INTO project_roots VALUES ('other', 'X:/Projects/Tools')")
        self.assertEqual(mail.project_and_chat(self.event), ("Tools", "测试会话"))

    def test_unavailable_workspace_lookup_uses_the_actual_folder_name(self):
        with self.db:
            self.db.execute("DROP TABLE project_roots")
        self.assertEqual(mail.project_and_chat(self.event), ("Tools", "测试会话"))

    def test_extended_unc_root_matches_standard_unc(self):
        self.assertEqual(mail.workspace_key(r"\\?\UNC\Server\Share\Tools"), mail.workspace_key(r"\\server\share\tools"))


if __name__ == "__main__":
    unittest.main()

"""Route authenticated QQ Mail messages to persistent local Codex chats."""

import argparse
import base64
import getpass
import hashlib
import imaplib
import importlib.util
import json
import msvcrt
import os
import queue
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


DIRECTORY = Path(__file__).resolve().parent
CONFIG = DIRECTORY / "inbox-settings.json"
# A missing project is created under the current user's home unless configured otherwise.
PROJECT_ROOT = Path.home() / "CodexMailProjects"
POLL_SECONDS = 30
MAIL_SIZE = 1024 * 1024
SUBJECT = re.compile(r"\[AI会话\]\s*\[([^\[\]\r\n]*)\](?:\s*\[([^\[\]\r\n]*)\])?")
CLIENT_ID = re.compile(r"<[!-~]{1,254}>")
REPLY_ID = re.compile(r"<[^<>\s]{1,254}>")
QUOTE_START = re.compile(
    r"^\s*(?:>|[-_\s]*(?:Original Message|原始邮件|原邮件|转发的邮件)\s*[-_：:]*$|"
    r"On .{1,300} wrote:\s*$|.{0,220}(?:在|于)\s*\d{4}年.{0,90}写(?:道|到)\s*[:：]?\s*$)", re.IGNORECASE,
)

# Load the adjacent installed module by path so Python isolated mode can reuse DPAPI.
sys.dont_write_bytecode = True
mail_spec = importlib.util.spec_from_file_location("codex_qq_mail", DIRECTORY / "codex_qq_mail.py")
mail = importlib.util.module_from_spec(mail_spec)
mail_spec.loader.exec_module(mail)


class RejectedMail(ValueError):
    """The mail cannot be routed, even on retry."""


class RpcError(RuntimeError):
    def __init__(self, method, error):
        self.method = method
        self.detail = str(error.get("message", ""))
        self.code = error.get("code")
        super().__init__(f"{method} returned RPC error {self.code}")


class BusyThread(RuntimeError):
    """The destination chat already has an active turn."""


class AppServer:
    """Own one JSON-RPC connection and drain notifications while work is active."""

    def __init__(self, command):
        self.command = command
        self.messages = queue.Queue()
        self.request_id = 0
        self.completed = {}

    def __enter__(self):
        self.proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, encoding="utf-8", text=True,
        )
        threading.Thread(target=self.read_stdout, daemon=True).start()
        try:
            self.call("initialize", {
                "clientInfo": {"name": "codex_qq_inbox", "title": "QQ Mail Inbox", "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            }, timeout=12)
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)

    def __exit__(self, *exc):
        self.close()

    def read_stdout(self):
        try:
            # The reader owns stdout. Closing it on the main thread can block on a pipe read.
            with self.proc.stdout as stream:
                for line in stream:
                    try:
                        self.messages.put(json.loads(line))
                    except ValueError:
                        continue
        finally:
            self.messages.put(None)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout=None):
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("Codex App Server response timed out") from error
        if message is None:
            raise ConnectionError("Codex App Server closed its connection")
        if "id" in message and "method" in message:
            # An unattended process has no user who can answer an approval or elicitation.
            self.send({"id": message["id"], "error": {"code": -32601, "message": "No interactive approval client"}})
        if message.get("method") == "turn/completed":
            params = message["params"]
            turn = params["turn"]
            self.completed[(params["threadId"], turn["id"])] = turn["status"]
        return message

    def call(self, method, params, *, timeout=35):
        self.request_id += 1
        self.send({"method": method, "id": self.request_id, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            result = self.receive(max(0, deadline - time.monotonic()))
            if result.get("id") == self.request_id and "method" not in result:
                if "error" in result:
                    raise RpcError(method, result["error"])
                return result["result"]
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{method} timed out")

    def pages(self, method, **params):
        cursor = None
        while True:
            page = self.call(method, {**params, "cursor": cursor, "limit": 100})
            yield from page["data"]
            cursor = page.get("nextCursor")
            if not cursor:
                return

    def wait_turn(self, thread_id, turn_id):
        key = (thread_id, turn_id)
        while key not in self.completed:
            self.receive()
        return self.completed.pop(key)


@contextmanager
def connect_codex():
    executable = shutil.which("codex.exe") or shutil.which("codex")
    if not executable:
        raise FileNotFoundError("Codex CLI is missing from PATH")
    # Reuse the running local daemon when present. An isolated stdio server can
    # operate on the same saved chats when the daemon is unavailable.
    candidate = AppServer([executable, "app-server", "proxy"])
    try:
        server = candidate.__enter__()
    except (OSError, RpcError):
        with AppServer([executable, "app-server"]) as server:
            yield server
    else:
        try:
            yield server
        finally:
            server.close()


class PlainText(HTMLParser):
    def __init__(self, *, stop_at_quote=False):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.hidden = []
        self.stop_at_quote = stop_at_quote
        self.reached_quote = False

    def handle_starttag(self, tag, attrs):
        if self.reached_quote:
            return
        markers = " ".join(value or "" for key, value in attrs if key in {"id", "class"}).casefold()
        if self.stop_at_quote and (tag == "blockquote" or re.search(r"\b(?:gmail_quote|yahoo_quoted|divrplyfwdmsg|replyforwardmsg)\b", markers)):
            self.reached_quote = True
            return
        if tag in {"head", "script", "style"}:
            self.hidden.append(tag)
        elif not self.hidden and tag in {"br", "p", "div", "li", "tr"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if self.reached_quote:
            return
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        elif not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden and not self.reached_quote:
            self.text.append(data)


@dataclass(frozen=True)
class CommandMail:
    sender: str
    message_id: str
    project: str
    chat: str
    text: str
    thread_id: str | None = None
    is_reply: bool = False
    subject: str = ""
    references: tuple[str, ...] = ()
    rejection_reason: str | None = None


def reply_ids(message, field):
    ids = [value for header in message.get_all(field, []) for value in REPLY_ID.findall(str(header))]
    return tuple(dict.fromkeys(value for value in ids if value.isascii()))[-50:]


def reply_text(text):
    # Strip the previous message at the quote boundary, including HTML entities in plain text.
    lines = []
    for line in text.splitlines():
        if QUOTE_START.match(unescape(line)):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def parse_mail(raw, mailbox=None):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(message.get("Subject", "")).rstrip()
    match = SUBJECT.fullmatch(subject)
    could_be_reply = bool(mailbox and (mail.REPLY_PREFIX.match(subject) or message.get("In-Reply-To") or message.get("References")))
    if not (match or could_be_reply) or str(message.get("Auto-Submitted", "no")).strip().casefold() != "no":
        return None, message
    from_headers = message.get_all("From", [])
    addresses = from_headers[0].addresses if len(from_headers) == 1 else ()
    message_ids = message.get_all("Message-ID", [])
    message_id = str(message_ids[0]).strip() if len(message_ids) == 1 else ""
    if len(addresses) != 1 or not CLIENT_ID.fullmatch(message_id):
        raise RejectedMail("邮件缺少有效的发件邮箱或 Message-ID。")
    if any(char in message_id[1:-1] for char in '<>\\"'):
        raise RejectedMail("邮件 Message-ID 格式无效。")
    sender = addresses[0].addr_spec.casefold()
    project = chat = ""
    thread_id = None
    is_reply = match is None
    rejection_reason = None
    in_reply_to = reply_ids(message, "In-Reply-To")
    references = reply_ids(message, "References")
    if match:
        target, chat_name = match.groups()
        target = target.strip()
        if chat_name is None:
            if re.fullmatch(mail.UUID_TEXT, target, re.IGNORECASE):
                thread_id = mail.chat_id(target)
            else:
                rejection_reason = "请在 [AI会话][会话ID] 中填写完整的本地会话 ID。"
        else:
            project, chat = target, chat_name.strip()
            if not (project and chat):
                rejection_reason = "邮件缺少有效的项目名或会话名。"
    else:
        try:
            thread_id = mail.find_reply_chat(mailbox, sender, subject.strip(), in_reply_to, references)
        except ValueError as error:
            raise RejectedMail(str(error)) from error
        if thread_id is None:
            return None, message
    references = tuple(dict.fromkeys((*references, *in_reply_to)))[-49:]
    body = message.get_body(preferencelist=("plain", "html"))
    text = body.get_content(errors="replace") if body is not None else ""
    if body is not None and body.get_content_subtype() == "html":
        parser = PlainText(stop_at_quote=is_reply)
        parser.feed(text)
        parser.close()
        text = "".join(parser.text)
    text = text.replace("\r\n", "\n").strip()
    if is_reply:
        text = reply_text(text)
    if not text:
        if is_reply:
            raise RejectedMail("邮件正文为空。")
        rejection_reason = rejection_reason or "请在邮件正文中填写要交给 Codex 的指令。"
    # Retain a valid envelope across routing errors so poll can authenticate it before replying.
    return CommandMail(
        sender, message_id, project, chat, text,
        thread_id=thread_id, is_reply=is_reply, subject=subject, references=references,
        rejection_reason=rejection_reason,
    ), message


def qq_dmarc_passes(message, sender):
    # Trust only QQ's authentication result above its first Received trace.
    # An original sender's headers appear below that transport boundary.
    results = []
    received_by_qq = False
    for name, value in message.raw_items():
        if name.casefold() == "received":
            received_by_qq = bool(re.search(r"\bby\s+(?:[\w-]+\.)*qq\.com\b(?![\w.-])", value, re.IGNORECASE))
            break
        if name.casefold() == "authentication-results":
            results.append(value)
    if not received_by_qq or len(results) != 1:
        return False
    parts = results[0].split(";")
    authority = parts[0].strip().casefold().split()
    if not authority or authority[0] not in {"mx.qq.com", "qq.com"}:
        return False
    domain = sender.rsplit("@", 1)[-1]
    return any(
        re.match(r"\s*dmarc=pass\b", part, re.IGNORECASE)
        and re.search(rf'\bheader\.from\s*=\s*"?{re.escape(domain)}"?(?=\s|$)', part, re.IGNORECASE)
        for part in parts[1:]
    )


def qq_spf_passes(message, sender):
    # QQ's inbound header block follows its Received trace on some enterprise mail paths.
    first_received = False
    qq_trace = set()
    results = []
    for name, value in message.raw_items():
        name = name.casefold()
        if name == "received":
            if first_received:
                break
            first_received = bool(re.search(r"\bby\s+(?:[\w-]+\.)*qq\.com\b(?![\w.-])", value, re.IGNORECASE))
            if not first_received:
                return False
        elif name in {"from", "to", "subject"}:
            break
        elif first_received and name in {"x-qq-mid", "x-qq-xmailinfo", "x-qq-xmrinfo"}:
            qq_trace.add(name)
        elif first_received and name == "authentication-results":
            results.append(value)
    if len(qq_trace) < 2 or len(results) != 1:
        return False
    authority, _, methods = results[0].partition(";")
    if authority.strip().casefold() != "mx.qq.com":
        return False
    for method in methods.split(";"):
        match = re.match(r"\s*spf\s*=\s*pass\b(?:\s*\([^)]*\))?\s+smtp\.mailfrom\s*=\s*(?:<([^>]+)>|(\S+))", method, re.IGNORECASE)
        if match and "".join((match[1] or match[2]).split()).casefold() == sender:
            return True
    return False


def read_message(box, uid):
    status, data = box.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{MAIL_SIZE + 1}>)")
    if status != "OK":
        raise imaplib.IMAP4.error("Cannot fetch mail")
    raw = next((item[1] for item in data if isinstance(item, tuple)), None)
    if raw is not None and len(raw) > MAIL_SIZE:
        raise RejectedMail("邮件超过 1 MiB。")
    return raw


def select_inbox(box):
    if box.select("INBOX", readonly=True)[0] != "OK":
        raise imaplib.IMAP4.error("Cannot open INBOX")


def sent_folders(box):
    status, folders = box.list()
    if status != "OK":
        raise imaplib.IMAP4.error("Cannot list mail folders")
    sent_names = {b"sent", b"sent messages"}
    for label in ("已发送", "已发送邮件", "发件箱"):
        encoded = base64.b64encode(label.encode("utf-16-be")).rstrip(b"=").replace(b"/", b",")
        sent_names.add((b"&" + encoded + b"-").lower())
    names = []
    for item in folders:
        if not isinstance(item, bytes):
            continue
        match = re.fullmatch(rb'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)', item)
        if match and (b"\\sent" in match[1].lower().split() or match[2].strip(b'"').lower() in sent_names):
            names.append(match[2])
    return names


def sent_copy_matches(box, command, mailbox):
    # A matching copy written to the authenticated account's Sent folder verifies self-mail.
    for name in sent_folders(box):
        if box.select(name, readonly=True)[0] != "OK":
            continue
        try:
            status, data = box.uid("SEARCH", None, "HEADER", "Message-ID", f'"{command.message_id}"')
            if status != "OK":
                raise imaplib.IMAP4.error("Cannot search Sent")
            for uid in reversed((data[0] or b"").split()[-8:]):
                raw = read_message(box, int(uid))
                if raw and parse_mail(raw, mailbox)[0] == command:
                    return True
        finally:
            select_inbox(box)
    return False


def authenticated(box, config, command, message):
    # Only a reply to a registered notice can use the recipient's reply permissions.
    if not command.is_reply and command.sender not in config["senders"]:
        return False
    if command.sender == config["mailbox"]:
        return sent_copy_matches(box, command, config["mailbox"])
    # Standalone commands may use SPF only when that allowlisted address opted in.
    allow_spf = command.is_reply or command.sender in config.get("spf_senders", ())
    return qq_dmarc_passes(message, command.sender) or (allow_spf and qq_spf_passes(message, command.sender))


def exactly_one(items, name, kind):
    matches = [item for item in items if item.get("name") == name]
    if len(matches) > 1:
        raise RejectedMail(f"有多个同名{kind}，请给目标改成唯一名称。")
    return next(iter(matches), None)


def new_project_folder(root, name):
    if name in {".", ".."} or os.path.isreserved(name) or Path(name).name != name:
        raise RejectedMail("新项目名不能作为 Windows 文件夹名称使用。")
    root = root.resolve()
    path = root / name
    if path.is_symlink() or path.is_junction() or path.resolve().parent != root:
        raise RejectedMail("新项目文件夹超出了配置的根目录。")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileExistsError, NotADirectoryError) as error:
        raise RejectedMail("无法创建项目目录，请检查本机项目根目录的访问权限和同名文件。") from error
    return path


def resume_chat(server, thread):
    if thread.get("status", {}).get("type") == "active":
        raise BusyThread
    thread = server.call("thread/resume", {"threadId": thread["id"], "excludeTurns": True})["thread"]
    if thread.get("status", {}).get("type") == "active":
        raise BusyThread
    return thread["id"]


def resolve_chat(server, project_root, command):
    if command.thread_id:
        try:
            thread = server.call("thread/read", {"threadId": command.thread_id, "includeTurns": False})["thread"]
        except RpcError as error:
            if error.code == -32600 and error.detail in (
                f"thread not loaded: {command.thread_id}", f"thread not found: {command.thread_id}",
            ):
                raise RejectedMail("未找到该本地 Codex 会话。请核对会话 ID。") from error
            raise
        return resume_chat(server, thread)
    project = exactly_one(server.pages("project/list"), command.project, "项目")
    if project is None:
        path = new_project_folder(project_root, command.project)
        project = server.call("project/import", {
            "name": command.project,
            "roots": [{"path": str(path)}],
            "idempotencyKey": str(uuid5(NAMESPACE_URL, f"codex-qq-mail-project:{path}")),
        })["project"]

    filters = {"projectId": project["id"], "currentAccountOnly": True,
               "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"]}
    thread = exactly_one(server.pages("thread/list", **filters), command.chat, "会话")
    if thread is None:
        archived = exactly_one(server.pages("thread/list", **filters, archived=True), command.chat, "归档会话")
        if archived:
            thread = server.call("thread/unarchive", {"threadId": archived["id"]})["thread"]
    if thread is None:
        roots = [item["path"] for item in project["roots"]]
        if not roots or not Path(roots[0]).is_dir():
            raise RejectedMail("项目主目录在这台 Windows 电脑上不可用。")
        thread = server.call("thread/start", {
            "cwd": roots[0], "runtimeWorkspaceRoots": roots,
            "projectId": project["id"], "ephemeral": False,
            "sandbox": "workspace-write", "approvalPolicy": "never",
        })["thread"]
        server.call("thread/name/set", {"threadId": thread["id"], "name": command.chat})
        return thread["id"]
    return resume_chat(server, thread)


def submission_key(mailbox, message_id):
    return hashlib.sha256(f"{mailbox}\0{message_id}".encode()).hexdigest()


def submit_command(db, config, command, uid):
    key = submission_key(config["mailbox"], command.message_id)
    previous = db.execute("SELECT status FROM submissions WHERE message_key=?", (key,)).fetchone()
    if previous:
        if previous[0] == "submitting":
            print(f"UID {uid} 上次提交结果未知，已跳过重复提交。", flush=True)
        return

    with connect_codex() as server:
        queued = command.is_reply
        try:
            thread_id = resolve_chat(server, Path(config["project_root"]), command)
        except (BusyThread, RpcError) as error:
            occupied = isinstance(error, BusyThread) or (
                error.method == "thread/resume" and "already has an active writer" in error.detail.casefold()
            )
            if not command.thread_id or not occupied:
                raise
            thread_id = command.thread_id
            queued = True
        client_id = str(uuid5(NAMESPACE_URL, key))
        params = {
            "threadId": thread_id, "clientUserMessageId": client_id,
            "input": [{"type": "text", "text": command.text}],
        }
        # Persist the claim before the write RPC. A lost connection may hide a successful submit.
        with db:
            db.execute("INSERT INTO submissions VALUES (?, ?, NULL, ?)", (key, thread_id, "submitting"))
            if command.is_reply:
                _, question = mail.split_mail_request(command.text)
                protected = mail.windows_protect(question.encode("utf-8"), encrypt=True) if question else None
                db.execute(
                    "INSERT INTO reply_notifications (client_id, mailbox, thread_id, recipient, subject, "
                    "parent_id, refs, question) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (client_id, config["mailbox"], thread_id, command.sender, command.subject,
                     command.message_id, json.dumps(command.references), protected),
                )
        try:
            if queued:
                # The current writer can dispatch this after the active turn.
                server.call("thread/queue/add", params)
            else:
                # Apply the workspace sandbox even when an existing chat used a broader policy.
                started = server.call("turn/start", {
                    **params, "approvalPolicy": "never", "sandboxPolicy": {"type": "workspaceWrite"},
                })
        except RpcError:
            with db:
                db.execute("DELETE FROM submissions WHERE message_key=?", (key,))
                if command.is_reply:
                    db.execute("DELETE FROM reply_notifications WHERE client_id=?", (client_id,))
            raise
        if queued:
            with db:
                db.execute("UPDATE submissions SET status='queued' WHERE message_key=?", (key,))
            print(f"UID {uid} 已排队到 codex://threads/{thread_id}", flush=True)
            return
        turn_id = started["turn"]["id"]
        with db:
            db.execute("UPDATE submissions SET status='submitted', turn_id=? WHERE message_key=?", (turn_id, key))
        print(f"UID {uid} 已提交到 codex://threads/{thread_id}", flush=True)
        status = server.wait_turn(thread_id, turn_id)
        with db:
            db.execute("UPDATE submissions SET status=? WHERE message_key=?", (status, key))
        print(f"UID {uid} 执行状态 {status}", flush=True)


def rejection_reason(error):
    if isinstance(error, RejectedMail):
        return str(error)
    if error.code == -32601:
        return "当前 Codex 版本不支持所需的会话操作，请更新 Codex 后重试。"
    if error.code not in {-32600, -32602} or any(hint in error.detail.casefold() for hint in (
        "already has an active writer", "active or pending turn", "thread is busy",
    )):
        return None  # Busy chats and transport or server failures can still recover.
    # The raw RPC error may include local paths or account details.
    return {
        "thread/read": "Codex 无法读取该会话，请核对本机会话 ID。",
        "thread/resume": "Codex 无法重新打开该会话，请确认能在本机打开它。",
        "thread/unarchive": "Codex 无法恢复该归档会话，请先在本机恢复它。",
        "project/import": "Codex 无法添加该项目，请检查项目名称和本机项目目录。",
        "thread/start": "Codex 无法在该项目中创建会话，请检查项目是否可用。",
    }.get(error.method, "Codex 拒绝了这封邮件的会话操作，请核对项目或会话后重试。")


def submit_or_record_failure(db, config, command, uid):
    if command.is_reply:
        submit_command(db, config, command, uid)
        return
    try:
        if command.rejection_reason:
            raise RejectedMail(command.rejection_reason)
        submit_command(db, config, command, uid)
    except (RejectedMail, RpcError) as error:
        reason = rejection_reason(error)
        if reason is None:
            raise
        key = submission_key(config["mailbox"], command.message_id)
        # Record the receipt before advancing the inbox cursor. Duplicate UIDs with the same
        # Message-ID see this claim and cannot resubmit the command or enqueue another receipt.
        with db:
            if not db.execute("INSERT OR IGNORE INTO submissions VALUES (?, NULL, NULL, 'rejected')", (key,)).rowcount:
                return
            db.execute(
                "INSERT INTO failed_replies (client_id, mailbox, recipient, subject, parent_id, refs, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid5(NAMESPACE_URL, key)), config["mailbox"], command.sender, command.subject,
                 command.message_id, json.dumps(command.references), reason),
            )
        print(f"UID {uid} 已登记失败回执。{reason}", flush=True)


def send_queued_reply(client_id, recipient, subject, parent_id, refs, body, *, thread_id=None):
    try:
        mail.send_notice(
            test=False, recipient=recipient, subject=subject, thread_id=thread_id,
            body=body, reply=(parent_id, json.loads(refs), client_id),
        )
    except mail.ReplyDeliveryUncertain:
        # A disconnect during SMTP DATA may have hidden a successful delivery.
        print("回信投递结果未知，已跳过自动重发。", flush=True)
        return "uncertain"
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        print(f"回信暂时失败（{type(error).__name__}），下一次轮询重试。", flush=True)
        return "pending"
    return "sent"


def deliver_failed_replies(db, mailbox):
    rows = db.execute(
        "SELECT client_id, recipient, subject, parent_id, refs, reason FROM failed_replies "
        "WHERE mailbox=? AND status='pending' ORDER BY rowid", (mailbox,),
    ).fetchall()
    for client_id, recipient, subject, parent_id, refs, reason in rows:
        with db:
            db.execute("UPDATE failed_replies SET status='sending' WHERE client_id=?", (client_id,))
        body = f"这封邮件中的指令未能提交给 Codex。\n\n原因\n{reason}\n"
        status = send_queued_reply(client_id, recipient, subject, parent_id, refs, body)
        with db:
            db.execute("UPDATE failed_replies SET status=? WHERE client_id=?", (status, client_id))
        if status == "sent":
            print("已发送失败回执。", flush=True)


def deliver_reply(db, entry, conclusion):
    client_id, thread_id, recipient, subject, parent_id, refs, protected_question = entry
    try:
        question = mail.windows_protect(protected_question, encrypt=False).decode("utf-8") if protected_question else ""
    except (OSError, UnicodeError) as error:
        print(f"暂时无法读取完成回信正文（{type(error).__name__}）。", flush=True)
        return
    with db:
        db.execute("UPDATE reply_notifications SET status='sending' WHERE client_id=?", (client_id,))
    body = mail.notice_body(question, conclusion)
    status = send_queued_reply(client_id, recipient, subject, parent_id, refs, body, thread_id=thread_id)
    with db:
        db.execute("UPDATE reply_notifications SET status=?, question=? WHERE client_id=?", (
            status, protected_question if status == "pending" else None, client_id,
        ))
    if status == "sent":
        print("已发送完成回信。", flush=True)


def deliver_completed_replies(db, mailbox):
    rows = db.execute(
        "SELECT client_id, thread_id, recipient, subject, parent_id, refs, question "
        "FROM reply_notifications WHERE mailbox=? AND status='pending' ORDER BY rowid", (mailbox,),
    ).fetchall()
    state = mail.latest_state_db() if rows else None
    if state is None:
        return
    by_thread = {}
    for row in rows:
        by_thread.setdefault(row[1], {})[row[0]] = row
    with closing(sqlite3.connect(state.as_uri() + "?mode=ro", uri=True, timeout=2)) as codex:
        for thread_id, entries in by_thread.items():
            rollout = codex.execute("SELECT rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()
            if not rollout or not rollout[0]:
                continue
            started = {}
            for event in mail.reply_turn_events(rollout[0]):
                turn_id = event.get("turn_id")
                client_id = mail.user_item(event).get("client_id")
                if turn_id and client_id in entries:
                    started[turn_id] = entries[client_id]
                elif event.get("type") == "task_complete" and turn_id in started:
                    # The actual completion can happen long after queueing, even after a restart.
                    deliver_reply(db, started.pop(turn_id), event.get("last_agent_message"))


@contextmanager
def inbox_connection(config):
    code = mail.windows_protect(base64.b64decode(config["protected_code"], validate=True), encrypt=False)
    with imaplib.IMAP4_SSL("imap.qq.com", 993, ssl_context=ssl.create_default_context(), timeout=20) as box:
        box.login(config["mailbox"], code.decode("ascii"))
        if "ID" in box.capabilities:
            box.xatom("ID", '("name" "Codex QQ Inbox" "version" "1.0")')
        select_inbox(box)
        yield box


def checkpoint(db, mailbox, validity, uid):
    with db:
        db.execute("INSERT INTO checkpoint VALUES (?, ?, ?) ON CONFLICT(mailbox) DO UPDATE "
                   "SET uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid", (mailbox, validity, uid))


def poll(db, config):
    with inbox_connection(config) as box:
        raw_validity = box.response("UIDVALIDITY")[1][0]
        if raw_validity is None:
            raise imaplib.IMAP4.error("INBOX did not provide UIDVALIDITY")
        validity = int(raw_validity)
        baseline = db.execute("SELECT uidvalidity,last_uid FROM checkpoint WHERE mailbox=?", (config["mailbox"],)).fetchone()
        if not baseline or baseline[0] != validity:
            raw_next_uid = box.response("UIDNEXT")[1][0]
            if raw_next_uid is None:
                status, data = box.uid("SEARCH", None, "ALL")
                if status != "OK":
                    raise imaplib.IMAP4.error("Cannot read the current INBOX baseline")
                last_uid = max(map(int, (data[0] or b"").split()), default=0)
            else:
                last_uid = int(raw_next_uid) - 1
            checkpoint(db, config["mailbox"], validity, last_uid)
            print("已记录收件起点。现在可以发送新指令邮件。", flush=True)
            return

        first_uid = baseline[1] + 1
        status, data = box.uid("SEARCH", None, "UID", f"{first_uid}:*")
        if status != "OK":
            raise imaplib.IMAP4.error("Cannot search INBOX")
        for uid in sorted(int(value) for value in (data[0] or b"").split() if int(value) >= first_uid):
            try:
                raw = read_message(box, uid)
                if raw:
                    command, message = parse_mail(raw, config["mailbox"])
                    if command:
                        if not authenticated(box, config, command, message):
                            raise RejectedMail("发件身份验证未通过。")
                        submit_or_record_failure(db, config, command, uid)
            except RejectedMail as error:
                print(f"UID {uid} 已跳过。{error}", flush=True)
            checkpoint(db, config["mailbox"], validity, uid)


@contextmanager
def listener_lock():
    # The OS releases this lock if the process exits or the computer restarts.
    with (DIRECTORY / "inbox.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("收信程序已经在运行。") from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def setup():
    if not sys.stdin.isatty():
        raise ValueError("请在本机 PowerShell 或 Windows 终端运行 setup。")
    outgoing = json.loads(mail.SETTINGS.read_text(encoding="utf-8-sig")) if mail.SETTINGS.is_file() else {}
    default_mailbox = outgoing.get("sender", "")
    mailbox = mail.email_address(input(f"接收指令的 QQ 邮箱 [{default_mailbox}]：").strip() or default_mailbox).casefold()
    source = input("允许发指令的邮箱（逗号分隔，回车仅允许此 QQ 邮箱本人）：").strip()
    senders = sorted({mail.email_address(address).casefold() for address in source.split(",")}) if source else [mailbox]
    root = Path(input(f"新项目存放目录 [{PROJECT_ROOT}]：").strip().strip('"') or PROJECT_ROOT)
    if not root.is_absolute():
        raise ValueError("新项目存放目录需要是绝对路径。")
    reusable = outgoing.get("protected_code") if mailbox == default_mailbox.casefold() else None
    suffix = "，回车复用发信授权码" if reusable else ""
    code = "".join(getpass.getpass(f"QQ 邮箱 IMAP 授权码（输入不可见{suffix}）：").split())
    if not code and reusable:
        protected = reusable
    elif code and code.isascii():
        protected = base64.b64encode(mail.windows_protect(code.encode("ascii"), encrypt=True)).decode("ascii")
    else:
        raise ValueError("请输入 QQ 邮箱 IMAP 授权码。")
    previous = json.loads(CONFIG.read_text(encoding="utf-8-sig")) if CONFIG.is_file() else {}
    settings = {"mailbox": mailbox, "senders": senders, "project_root": str(root), "protected_code": protected}
    # Retain SPF opt-ins only for addresses still allowed on this same mailbox.
    if previous.get("mailbox", "").casefold() == mailbox:
        retained = sorted(set(previous.get("spf_senders", ())).intersection(senders))
        if retained:
            settings["spf_senders"] = retained
    mail.write_json(CONFIG, settings)
    if not outgoing:
        # A fresh installation can share one QQ credential for SMTP and IMAP setup.
        mail.write_json(mail.SETTINGS, {"sender": mailbox, "recipient": mailbox, "protected_code": protected})
        print("已同时保存同一邮箱的发信凭据。")
    print("收信授权码已加密保存。运行 check 验证连接，再运行 run。")


def run(config):
    with listener_lock(), closing(sqlite3.connect(mail.INBOX_STATE)) as db:
        init_state(db)
        with db:
            db.execute("UPDATE reply_notifications SET status='uncertain', question=NULL WHERE status='sending'")
            db.execute("UPDATE failed_replies SET status='uncertain' WHERE status='sending'")
        print("QQ 收信程序正在运行。按 Ctrl+C 停止。", flush=True)
        while True:
            try:
                deliver_failed_replies(db, config["mailbox"])
            except sqlite3.Error as error:
                print(f"暂时无法查询失败回执（{type(error).__name__}）。", flush=True)
            try:
                deliver_completed_replies(db, config["mailbox"])
            except (OSError, sqlite3.Error) as error:
                print(f"暂时无法查询完成回信（{type(error).__name__}）。", flush=True)
            try:
                poll(db, config)
            except BusyThread:
                print("目标会话正在运行，下一次轮询重试。", flush=True)
            except (OSError, sqlite3.Error, imaplib.IMAP4.error, RpcError) as error:
                detail = str(error) if isinstance(error, RpcError) else type(error).__name__
                print(f"收信或提交暂时失败（{detail}），下一次轮询重试。", flush=True)
            time.sleep(POLL_SECONDS)


def init_state(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS checkpoint (mailbox TEXT PRIMARY KEY, uidvalidity INTEGER, last_uid INTEGER);
        CREATE TABLE IF NOT EXISTS submissions (message_key TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS reply_notifications (
            client_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, thread_id TEXT NOT NULL, recipient TEXT NOT NULL,
            subject TEXT NOT NULL, parent_id TEXT NOT NULL, refs TEXT NOT NULL, question BLOB,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS inbox_reply_recipient ON reply_notifications(thread_id, recipient);
        CREATE INDEX IF NOT EXISTS inbox_pending_reply ON reply_notifications(mailbox, status);
        CREATE TABLE IF NOT EXISTS failed_replies (
            client_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, recipient TEXT NOT NULL,
            subject TEXT NOT NULL, parent_id TEXT NOT NULL, refs TEXT NOT NULL, reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS inbox_pending_failure ON failed_replies(mailbox, status);
    """)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "check", "run"))
    action = parser.parse_args().action
    try:
        if action == "setup":
            setup()
        else:
            config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
            if action == "check":
                with inbox_connection(config) as box:
                    print("QQ IMAP 登录成功。")
                    if config["mailbox"] in config["senders"] and not sent_folders(box):
                        print("未找到已发送文件夹。发给自己的指令邮件需要一份已发送副本。")
                with connect_codex() as server:
                    server.call("project/list", {"limit": 1})
                    print("本地 Codex App Server 连接成功。")
            else:
                run(config)
    except KeyboardInterrupt:
        print("已停止收信程序。")
    except FileNotFoundError:
        print("请先运行 setup，并确认 Codex 在 PATH 中。", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, imaplib.IMAP4.error) as error:
        show_details = isinstance(error, (RpcError, ValueError)) or type(error) is RuntimeError
        detail = str(error) if show_details else type(error).__name__
        print(f"操作失败（{detail}）。请检查 IMAP 服务和授权码。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

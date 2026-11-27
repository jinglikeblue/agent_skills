"""Send Codex completion notices with the current turn's question and conclusion."""

import argparse
import base64
import ctypes
import getpass
import hashlib
import importlib.util
import json
import ntpath
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from ctypes import wintypes
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, urlsplit
from uuid import UUID


SETTINGS = Path(__file__).resolve().with_name("settings.json")
# Load the installed sibling by path because the hook runs Python in isolated mode.
format_spec = importlib.util.spec_from_file_location("qq_mail_format", SETTINGS.with_name("qq_mail_format.py"))
mail_format = importlib.util.module_from_spec(format_spec)
format_spec.loader.exec_module(mail_format)
CODEX_PROFILE = SETTINGS.parent.parent
ROUTES = SETTINGS.with_name("routes")
TURN_NOTICES = SETTINGS.with_name("turn-notices.sqlite3")
NOTICE_PREFERENCES = SETTINGS.with_name("notice-preferences.sqlite3")
REPLY_ROUTES = SETTINGS.with_name("reply-routes.sqlite3")
INBOX_STATE = SETTINGS.with_name("inbox-state.sqlite3")
UUID_TEXT = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ROLLOUT_NAME = re.compile(rf"rollout-.+?-({UUID_TEXT})(?:_{UUID_TEXT})?\.jsonl", re.IGNORECASE)
REPLY_PREFIX = re.compile(r"^(?:(?:re|回复)\s*[:：]\s*)+", re.IGNORECASE)

# Match a direct instruction at the start of a clause. Wrapper punctuation is optional.
MAILBOX = r"[a-z0-9_%+\-]+(?:\.[a-z0-9_%+\-]+)*@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+"
RECIPIENT = rf"[`（(]?\s*(?P<recipient>{MAILBOX})\s*[`）)]?"
WHEN_DONE = (
    r"(?:^|(?<=[，,。；;！？!?]))\s*(?:[-*+]\s+)?(?:\$qq-mail-notify\s+)?(?:请\s*)?(?:等|在)?\s*"
    r"(?:本轮|本次|这次|当前)?\s*(?:任务|工作|测试|构建|运行)?\s*(?:处理|执行)?\s*"
    r"(?:完成|结束|做完|处理完|执行完|跑完|搞定)(?:之后|以后|后|时)\s*[,，:：]?\s*"
    r"(?:(?:请|麻烦|记得)\s*)?(?:帮我\s*)?"
)
MAIL_ACTION = (
    r"(?:(?:用|通过)?\s*(?:QQ\s*)?(?:邮件|邮箱)\s*(?:通知|告知)(?:我)?"
    r"|发(?:送)?\s*(?:一封|个)?\s*(?:邮件通知|邮件)(?:\s*通知(?:我)?)?)"
)
CLAUSE_END = r"\s*(?:(?:即可|就行|就好|谢谢)\s*)?(?=$|[，,。；;！？!?.])"
NOTICE_PATTERNS = (
    re.compile(WHEN_DONE + MAIL_ACTION + r"\s*(?:给|到|至)?\s*[:：]?\s*" + RECIPIENT + CLAUSE_END, re.IGNORECASE),
    re.compile(WHEN_DONE + r"给\s*" + RECIPIENT + r"\s*发(?:送)?\s*(?:一封|个)?\s*邮件" + CLAUSE_END, re.IGNORECASE),
)
DEFAULT_NOTICE_PATTERN = re.compile(WHEN_DONE + MAIL_ACTION + r"\s*[。；;！？!?.]?$", re.IGNORECASE)
EDIT_DEFAULT_NOTICE = re.compile(r"(?:请\s*)?修改默认接收通知邮件地址\s*[。！!.]?")
# Leave time for hook bookkeeping before the installer-configured 120 second timeout.
RECIPIENT_DIALOG_TIMEOUT = 110
QUOTED_TEXT = re.compile(r'“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\r\n]*"')
EXAMPLE_LINE = re.compile(r"^(?:例如|比如|示例|样例|举例|引用|参考|格式示例)")


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class ReplyDeliveryUncertain(OSError):
    """The SMTP connection ended without establishing whether DATA was accepted."""


def windows_protect(data, *, encrypt):
    # DPAPI binds the saved authorization code to the current Windows user.
    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), source_buffer)
    target = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    function.argtypes = (
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
    )
    function.restype = wintypes.BOOL
    local_free = ctypes.WinDLL("kernel32").LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    ui_forbidden = 0x01
    if not function(ctypes.byref(source), None, None, None, None, ui_forbidden, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        local_free(target.data)


def email_address(value):
    value = value.strip()
    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError) as error:
        raise ValueError("请输入一个完整的邮箱地址。") from error
    if not (address.username and address.domain and value.isascii()):
        raise ValueError("请输入一个完整的邮箱地址。")
    return address.addr_spec


def write_json(path, value):
    # Readers see either complete version, including during a background hook run.
    pending = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    pending_path = Path(pending.name)
    try:
        with pending:
            json.dump(value, pending, indent=2)
        pending_path.replace(path)
    finally:
        pending_path.unlink(missing_ok=True)


def prompt_default_recipient(timeout, *, edit=False, current=""):
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = SETTINGS.with_name("qq-mail-recipient-window.ps1")
    command = [str(powershell), "-NoProfile", "-STA", "-ExecutionPolicy", "RemoteSigned", "-File", str(script),
               "-MailboxPattern", MAILBOX]
    if edit:
        command.append("-Edit")
    result = subprocess.run(
        command, input=current, capture_output=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout,
    )
    if result.returncode == 2:
        return None
    result.check_returncode()
    return email_address(result.stdout)


def get_default_recipient(*, edit=False):
    deadline = time.monotonic() + RECIPIENT_DIALOG_TIMEOUT
    with closing(sqlite3.connect(NOTICE_PREFERENCES, timeout=RECIPIENT_DIALOG_TIMEOUT)) as db, db:
        # Serialize changes without locking the separate turn-notice database.
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY CHECK (id=1), recipient TEXT NOT NULL)")
        saved = db.execute("SELECT recipient FROM preferences WHERE id=1").fetchone()
        current = saved[0] if saved else ""
        if saved and not edit:
            return email_address(current)
        recipient = prompt_default_recipient(max(0, deadline - time.monotonic()), edit=edit, current=current)
        if recipient:
            db.execute(
                "INSERT INTO preferences VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET recipient=excluded.recipient",
                (recipient,),
            )
        return recipient


def chat_id(reference):
    reference = reference.strip()
    try:
        if reference.startswith("codex://"):
            link = urlsplit(reference)
            if link.netloc != "threads" or parse_qs(link.query).get("hostId", ["local"]) != ["local"]:
                raise ValueError
            reference = link.path.strip("/")
        return str(UUID(reference))
    except ValueError as error:
        raise ValueError("请输入本地 Codex 会话链接或会话 ID。") from error


def hook_chat_id(event):
    # Forks can share a session_id. The rollout name carries the individual chat ID.
    transcript = event.get("transcript_path")
    if isinstance(transcript, str):
        match = ROLLOUT_NAME.fullmatch(PureWindowsPath(transcript).name)
        if match:
            return match.group(1).lower()
    return None


def read_route(path):
    try:
        return email_address(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None


def change_route(reference, recipient):
    thread_id = chat_id(reference)
    path = ROUTES / f"{thread_id}.json"
    if recipient is None:
        path.unlink(missing_ok=True)
        print("已关闭这条会话的邮件通知。")
        return
    recipient = email_address(recipient)
    ROUTES.mkdir(exist_ok=True)
    write_json(path, recipient)
    print(f"已设置这条会话的收件邮箱为 {recipient}。")


def list_routes():
    # Display active routes without reading the account credentials.
    count = 0
    for path in sorted(ROUTES.glob("*.json")):
        if not re.fullmatch(UUID_TEXT, path.stem):
            continue
        recipient = read_route(path)
        if recipient:
            print(f"codex://threads/{path.stem}\t{recipient}")
            count += 1
    if count == 0:
        print("当前没有开启邮件通知的会话。")


def setup():
    if not sys.stdin.isatty():
        raise ValueError("请在本机 PowerShell 或 Windows 终端中运行 setup。")
    sender = email_address(input("QQ 发件邮箱："))
    recipient = email_address(input("测试邮件收件邮箱（回车使用发件邮箱）：").strip() or sender)
    code = "".join(getpass.getpass("QQ 邮箱 SMTP 授权码（输入不可见）：").split())
    if not code or not code.isascii():
        raise ValueError("请输入有效的 QQ 邮箱 SMTP 授权码。")

    protected_code = windows_protect(code.encode("ascii"), encrypt=True)
    settings = {
        "sender": sender,
        "recipient": recipient,
        "protected_code": base64.b64encode(protected_code).decode("ascii"),
    }
    write_json(SETTINGS, settings)
    print("已保存本机邮件设置。运行 test 验证收件，运行 enable 为会话开启通知。")


def remember_reply_route(message_id, mailbox, recipient, subject, thread_id):
    # The envelope and stable chat ID keep reply routing independent of labels or renames.
    with closing(sqlite3.connect(REPLY_ROUTES, timeout=3)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS reply_routes (message_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, "
            "recipient TEXT NOT NULL, subject TEXT NOT NULL, thread_id TEXT NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS reply_topic ON reply_routes(mailbox, recipient, subject)")
        db.execute(
            "INSERT OR IGNORE INTO reply_routes VALUES (?, ?, ?, ?, ?)",
            (message_id, mailbox.casefold(), recipient.casefold(), subject, chat_id(thread_id)),
        )


def find_reply_chat(mailbox, recipient, subject, in_reply_to, references):
    if not REPLY_ROUTES.is_file():
        return None
    with closing(sqlite3.connect(REPLY_ROUTES.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        scope = (mailbox.casefold(), recipient.casefold())
        for ids in (in_reply_to, references):
            if not ids:
                continue
            placeholders = ",".join("?" for _ in ids)
            matches = db.execute(
                f"SELECT DISTINCT thread_id FROM reply_routes WHERE mailbox=? AND recipient=? AND message_id IN ({placeholders})",
                (*scope, *ids),
            ).fetchall()
            if matches:
                return _unique_reply_chat(matches)
        plain_subject, removed = REPLY_PREFIX.subn("", subject)
        if not removed:
            return None
        matches = db.execute(
            "SELECT DISTINCT thread_id FROM reply_routes WHERE mailbox=? AND recipient=? AND subject=?",
            (*scope, plain_subject.strip()),
        ).fetchall()
        return _unique_reply_chat(matches)


def _unique_reply_chat(rows):
    if len(rows) > 1:
        raise ValueError("回复关联到多条会话，请直接回复原始通知邮件。")
    return rows[0][0] if rows else None


def send_notice(*, test, recipient=None, subject=None, body=None, thread_id=None, reply=None):
    settings = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
    sender = settings["sender"]
    recipient = recipient or settings["recipient"]
    code = windows_protect(base64.b64decode(settings["protected_code"], validate=True), encrypt=False)
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)
    domain = sender.rsplit("@", 1)[1]
    base_subject = "Codex QQ 邮箱测试" if test else subject
    if reply:
        parent_id, references, client_id = reply
        message["Message-ID"] = f"<codex-qqmailchat.{client_id}@{domain}>"
        message["In-Reply-To"] = parent_id
        chain = [ref for ref in references if ref != parent_id]
        message["References"] = " ".join([*chain[-49:], parent_id])
        message["Auto-Submitted"] = "auto-replied"
        base_subject = REPLY_PREFIX.sub("", base_subject).strip() or "[完成通知]"
        message["Subject"] = f"Re: {base_subject}"
    else:
        token = secrets.token_hex(12) if thread_id and not test else None
        message["Message-ID"] = make_msgid(idstring=token, domain=domain)
        if token:
            # Clients that strip reply headers can still route using the subject.
            base_subject += f"[续聊码 {token}]"
        message["Subject"] = base_subject
    text = "QQ 邮箱通知配置测试。" if test else body
    message.set_content(text)
    message.add_alternative(mail_format.email_html(text), subtype="html")

    accepted = False
    try:
        with smtplib.SMTP_SSL(
            "smtp.qq.com", 465, timeout=10, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(sender, code.decode("ascii"))
            if reply and thread_id:
                remember_reply_route(str(message["Message-ID"]), sender, recipient, base_subject, thread_id)
            try:
                smtp.send_message(message)
            except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError):
                raise  # The server refused delivery, so retrying cannot duplicate it.
            except OSError as error:
                if reply:
                    raise ReplyDeliveryUncertain from error
                raise
            accepted = True
            if thread_id and not test and not reply:
                remember_reply_route(str(message["Message-ID"]), sender, recipient, base_subject, thread_id)
    except OSError:
        if not accepted:
            raise
        # Failure to send QUIT does not undo the server's acceptance of DATA.


def split_mail_request(prompt):
    # Extract an explicit notification clause while preserving the user's actual question.
    if prompt.lstrip().startswith("# Files mentioned by the user:") and "## My request:" in prompt:
        prompt = prompt.split("## My request:", 1)[1]
    recipients = set()
    use_default = False
    question = []
    fence = None
    after_example_label = False
    for raw_line in prompt.splitlines():
        question.append(raw_line)
        line = raw_line.strip()
        fence_line = re.match(r"^(`{3,}|~{3,})", line)
        if fence_line:
            marker = fence_line[1]
            if fence is None:
                fence = marker
            elif marker.startswith(fence):
                fence = None
            after_example_label = False
            continue
        if fence or not line or line.startswith(">") or raw_line.startswith(("    ", "\t")):
            continue
        if EXAMPLE_LINE.match(line):
            after_example_label = line.endswith((":", "："))
            continue
        if after_example_label:
            after_example_label = False
            continue
        masked = QUOTED_TEXT.sub(lambda match: " " * len(match[0]), line)
        masked = re.sub(r"`([^`]+)`", lambda m: m[0] if re.fullmatch(MAILBOX, m[1], re.IGNORECASE) else " " * len(m[0]), masked)
        spans = set()
        for pattern in NOTICE_PATTERNS:
            for match in pattern.finditer(masked):
                try:
                    recipients.add(email_address(match["recipient"]))
                except ValueError:
                    continue
                spans.add(match.span())
        default_match = DEFAULT_NOTICE_PATTERN.search(masked)
        if default_match and default_match[0] == line[default_match.start():]:
            # Masked text must not turn a quoted or malformed address into a bare request.
            use_default = True
            spans.add(default_match.span())
        if spans:
            for start, end in sorted(spans, reverse=True):
                line = line[:start] + line[end:]
            line = line.strip(" \t，,；;")
            line = re.sub(r"[，,；;](?=[。.!！?？]$)", "", line)
            question[-1] = line if line.strip("。.!！?？") else ""
    return recipients, use_default, "\n".join(question).strip()


def turn_key(event):
    session_id = event.get("session_id")
    turn_id = event.get("turn_id")
    if isinstance(session_id, str) and session_id and isinstance(turn_id, str) and turn_id:
        return hashlib.sha256(f"{session_id}\0{turn_id}".encode("utf-8")).hexdigest()
    return None


def ensure_turn_tables(db):
    db.execute("CREATE TABLE IF NOT EXISTS notices (turn_key TEXT PRIMARY KEY, recipient TEXT, recorded INTEGER NOT NULL)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS turn_questions (turn_key TEXT, digest TEXT, question BLOB, recorded INTEGER, "
        "PRIMARY KEY(turn_key, digest))"
    )


def record_turn_question(key, question, recipient, *, use_default=False):
    if not (question or recipient):
        return
    # Keep in-progress questions encrypted so a later steering message can request a notice.
    content = question.encode("utf-8") if question else None
    protected = windows_protect(content, encrypt=True) if content else None
    with closing(sqlite3.connect(TURN_NOTICES, timeout=2)) as db, db:
        ensure_turn_tables(db)
        db.execute("DELETE FROM notices WHERE recorded < unixepoch() - 2592000")
        db.execute("DELETE FROM turn_questions WHERE recorded < unixepoch() - 2592000")
        if protected:
            digest = hashlib.sha256(key.encode("ascii") + content).hexdigest()
            db.execute("INSERT OR IGNORE INTO turn_questions VALUES (?, ?, ?, unixepoch())", (key, digest, protected))
        if recipient:
            db.execute(
                "INSERT INTO notices VALUES (?, ?, unixepoch()) ON CONFLICT(turn_key) "
                "DO UPDATE SET recipient=excluded.recipient, recorded=excluded.recorded "
                "WHERE notices.recipient IS NOT NULL AND NOT ?",
                (key, recipient, use_default),
            )


def take_turn_notice(key, route=None):
    if not key:
        return (read_route(route) if route else None), ""
    if not route and not TURN_NOTICES.is_file():
        return None, ""
    with closing(sqlite3.connect(TURN_NOTICES, timeout=2)) as db, db:
        ensure_turn_tables(db)
        # Claim once and remove transient question text when this turn ends.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT recipient FROM notices WHERE turn_key=?", (key,)).fetchone()
        recipient = row[0] if row is not None else (read_route(route) if route else None)
        if recipient:
            db.execute(
                "INSERT INTO notices VALUES (?, NULL, unixepoch()) ON CONFLICT(turn_key) "
                "DO UPDATE SET recipient=NULL, recorded=excluded.recorded", (key,),
            )
        questions = db.execute("SELECT question FROM turn_questions WHERE turn_key=? ORDER BY rowid", (key,)).fetchall() if recipient else []
        db.execute("DELETE FROM turn_questions WHERE turn_key=?", (key,))
    question = "\n\n".join(windows_protect(q[0], encrypt=False).decode("utf-8") for q in questions)
    return recipient, question


def latest_state_db():
    databases = (path for path in CODEX_PROFILE.glob("state_*.sqlite") if path.stem.removeprefix("state_").isdigit())
    return max(databases, key=lambda path: int(path.stem.removeprefix("state_")), default=None)


def reply_turn_events(transcript, *, turn_id=None):
    # A queued message retains its App Server client ID in the persisted UserMessage item.
    turn_bytes = turn_id.encode("utf-8") if turn_id else None
    try:
        with Path(transcript).open("rb") as stream:
            for line in stream:
                if turn_bytes and turn_bytes not in line:
                    continue
                if b'"event_msg"' not in line or not (b'"client_id"' in line or b'"task_complete"' in line):
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue  # A live transcript can end in a partially written line.
                if record.get("type") == "event_msg":
                    event = record["payload"]
                    if not turn_id or event.get("turn_id") == turn_id:
                        yield event
    except FileNotFoundError:
        return


def user_item(event):
    item = event.get("item", {}) if event.get("type") == "item_completed" else {}
    return item if item.get("type") == "UserMessage" else {}


def reply_covers_recipient(event, thread_id, recipient):
    if not (thread_id and isinstance(event.get("turn_id"), str) and INBOX_STATE.is_file()):
        return False
    with closing(sqlite3.connect(INBOX_STATE.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='reply_notifications'").fetchone():
            return False  # The listener may still be using a database from the previous version.
        candidates = {row[0] for row in db.execute(
            "SELECT client_id FROM reply_notifications WHERE thread_id=? AND recipient=?", (thread_id, recipient.casefold()),
        )}
    if not candidates:
        return False
    return any(user_item(item).get("client_id") in candidates for item in reply_turn_events(
        event["transcript_path"], turn_id=event["turn_id"],
    ))


def workspace_key(path):
    # The local index can store a Windows extended-length path for the same workspace.
    if not isinstance(path, str) or not path:
        return None
    path = path.replace("/", "\\")
    if path[:8].casefold() == "\\\\?\\unc\\":
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path)) if ntpath.isabs(path) else None


def workspace_basename(path):
    # Use the original spelling in outgoing mail and the normalized path for matching.
    return ntpath.basename(ntpath.normpath(path)) if workspace_key(path) else ""


def subject_label(label):
    return " ".join((label or "").split()).translate(str.maketrans("[]", "［］"))


def project_and_chat(event):
    # Query only the owning chat and its project in Codex's current local index.
    defaults = ("未归属项目", "未命名会话")
    thread_id = hook_chat_id(event) or event.get("session_id")
    if not isinstance(thread_id, str) or not re.fullmatch(UUID_TEXT, thread_id, re.IGNORECASE):
        return defaults
    latest = latest_state_db()
    if latest is None:
        return defaults
    try:
        with closing(sqlite3.connect(latest.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute(
                "SELECT projects.name, threads.name, threads.cwd "
                "FROM threads LEFT JOIN projects ON projects.id=threads.project_id "
                "WHERE threads.id=?", (thread_id.lower(),),
            ).fetchone()
            if row is None:
                return defaults
            project, chat, cwd = row
            workspace = workspace_basename(cwd)
            cwd_key = workspace_key(cwd)
            if cwd_key:
                # The same source directory can be used by chats assigned to other projects.
                try:
                    candidates = {ident: name for ident, name, path in db.execute(
                        "SELECT projects.id, projects.name, project_roots.path FROM projects "
                        "JOIN project_roots ON projects.id=project_roots.project_id"
                    ) if workspace_key(path) == cwd_key}
                except sqlite3.Error:
                    candidates = {}  # The optional workspace lookup should not hide the chat name.
                if len(candidates) == 1:
                    workspace = next(iter(candidates.values())) or workspace
    except sqlite3.Error:
        return defaults
    return tuple(subject_label(label) or fallback for label, fallback in zip((workspace or project, chat), defaults))


def notice_content(event, question):
    workspace, chat = project_and_chat(event)
    subject = f"[完成通知][{workspace}][{chat}]"
    return subject, notice_body(question, event.get("last_assistant_message"))


def notice_body(question, conclusion):
    conclusion = conclusion.strip() if isinstance(conclusion, str) else ""
    question = question or "本轮未记录到任务问题。"
    conclusion = conclusion or "本轮未生成文本结论。"
    return f"问题\n{question}\n\n结论\n{conclusion}\n"


def run_prompt_hook():
    event = json.load(sys.stdin.buffer)
    key = turn_key(event)
    prompt = event.get("prompt")
    result = {}
    if event.get("hook_event_name") == "UserPromptSubmit" and key and isinstance(prompt, str) and not event.get("agent_id"):
        edit_default = EDIT_DEFAULT_NOTICE.fullmatch(prompt.strip()) is not None
        recipients, use_default, question = split_mail_request(prompt)
        configured = SETTINGS.is_file()
        recipient = next(iter(recipients)) if len(recipients) == 1 else None
        use_default = use_default and not recipients
        feedback = None
        if edit_default or use_default:
            try:
                selected = get_default_recipient(edit=edit_default)
            except subprocess.TimeoutExpired:
                feedback = "默认收件邮箱设置超时。请重新输入指令。"
            except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
                feedback = "默认收件邮箱读取或保存失败。请重新输入指令。"
            else:
                if edit_default:
                    feedback = "默认收件邮箱已保存。" if selected else "已取消修改默认收件邮箱。"
                elif selected:
                    recipient = selected
                else:
                    feedback = "已取消设置默认收件邮箱，本次请求未登记。"
        if configured and not edit_default:
            record_turn_question(key, question, recipient, use_default=use_default)
        if len(recipients) > 1:
            result["systemMessage"] = "本轮邮件通知检测到多个收件地址。请用一条指令指定一个地址。"
        elif recipient and not configured:
            result["systemMessage"] = "检测到完成邮件通知请求。请先在本机运行 codex_qq_mail.py setup 或 codex_qq_inbox.py setup。"
        elif recipient or feedback:
            if feedback:
                result["systemMessage"] = feedback
            result["hookSpecificOutput"] = {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    f"{feedback} 按原任务继续。" if feedback else
                    "本轮的一次性完成邮件通知已登记。按原任务继续，结束时由 Stop Hook 尝试投递，勿手动重复发信。"
                ),
            }
    print(json.dumps(result))


def run_hook():
    # The selected recipient gets this turn's question and final reply.
    event = json.load(sys.stdin.buffer)
    if event.get("hook_event_name") == "Stop" and SETTINGS.is_file():
        thread_id = hook_chat_id(event)
        route = ROUTES / f"{thread_id}.json" if thread_id else None
        recipient, question = take_turn_notice(turn_key(event), route)
        if recipient and not reply_covers_recipient(event, thread_id, recipient):
            subject, body = notice_content(event, question)
            send_notice(test=False, recipient=recipient, subject=subject, body=body, thread_id=thread_id)
    print("{}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("setup", "test", "hook", "prompt-hook", "list"):
        actions.add_parser(name)
    enable = actions.add_parser("enable", help="Enable notifications for one chat and set its recipient.")
    enable.add_argument("chat", help="Local Codex deep link or chat ID.")
    enable.add_argument("recipient", help="Email address for this chat.")
    disable = actions.add_parser("disable", help="Disable notifications for one chat.")
    disable.add_argument("chat", help="Local Codex deep link or chat ID.")
    args = parser.parse_args()
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    try:
        match args.action:
            case "setup":
                setup()
            case "test":
                send_notice(test=True)
                print("QQ SMTP 已接受测试邮件，请查看收件箱。")
            case "hook":
                run_hook()
            case "prompt-hook":
                run_prompt_hook()
            case "enable":
                change_route(args.chat, args.recipient)
            case "disable":
                change_route(args.chat, None)
            case "list":
                list_routes()
    except smtplib.SMTPAuthenticationError as error:
        print(f"QQ SMTP 认证失败（{error.smtp_code}）。请检查发件邮箱、SMTP 服务和授权码。", file=sys.stderr)
        return 1
    except smtplib.SMTPResponseException as error:
        print(f"QQ SMTP 返回错误（{error.smtp_code}）。", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("找不到本机邮件设置。请先运行 setup。", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, smtplib.SMTPException, sqlite3.Error) as error:
        interactive = args.action in ("setup", "enable", "disable")
        detail = str(error) if interactive and isinstance(error, ValueError) else type(error).__name__
        print(f"QQ 邮箱通知失败（{detail}）。", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

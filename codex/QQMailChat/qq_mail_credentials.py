"""Verify a QQ Mail authorization code received over a local private pipe."""

import base64
import imaplib
import importlib.util
import json
import smtplib
import ssl
import sys
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent
sys.dont_write_bytecode = True


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mail = load_module("codex_qq_mail")
inbox = load_module("codex_qq_inbox")


def previous_settings(path):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}


def save_smtp(mailbox, code, protected):
    settings = previous_settings(mail.SETTINGS)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=10, context=ssl.create_default_context()) as smtp:
        smtp.login(mailbox, code)
    # A new account's test messages should go to the new address by default.
    recipient = mailbox
    if settings.get("sender", "").casefold() == mailbox:
        recipient = settings.get("recipient", mailbox)
    mail.write_json(mail.SETTINGS, {
        **settings, "sender": mailbox, "recipient": recipient,
        "protected_code": protected,
    })


def save_imap(mailbox, protected):
    settings = previous_settings(inbox.CONFIG)
    # Preserve project and sender choices. A default allowlist follows a changed QQ account.
    senders = settings.get("senders")
    if not senders or senders == [settings.get("mailbox")]:
        senders = [mailbox]
    updated = {
        **settings, "mailbox": mailbox, "senders": senders,
        "project_root": settings.get("project_root", str(inbox.PROJECT_ROOT)),
        "protected_code": protected,
    }
    with inbox.inbox_connection(updated):
        pass
    mail.write_json(inbox.CONFIG, updated)


def verify_and_save(payload):
    mailbox = mail.email_address(payload["mailbox"]).casefold()
    code = "".join(payload["code"].split())
    if not code or not code.isascii():
        raise ValueError("请输入 QQ 邮箱客户端授权码。")
    protocol = payload["protocol"]
    if protocol not in {"smtp", "imap", "both"}:
        raise ValueError("邮件服务类型无效。")
    protected = base64.b64encode(mail.windows_protect(code.encode("ascii"), encrypt=True)).decode("ascii")
    results = {}
    checks = {"smtp": lambda: save_smtp(mailbox, code, protected), "imap": lambda: save_imap(mailbox, protected)}
    for name, check in checks.items():
        if protocol not in (name, "both"):
            continue
        try:
            check()
            results[name] = "verified"
        except (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error):
            results[name] = "rejected"
        except (OSError, smtplib.SMTPException):
            results[name] = "connection-or-save-error"
    return results


def main():
    # Output protocol statuses only. The credential and mailbox never go to tool output.
    try:
        results = verify_and_save(json.load(sys.stdin.buffer))
    except (ValueError, TypeError, KeyError, OSError):
        print(json.dumps({"setup": "invalid-input-or-local-error"}))
        return 1
    print(json.dumps(results))
    return int(any(status != "verified" for status in results.values()))


if __name__ == "__main__":
    sys.exit(main())

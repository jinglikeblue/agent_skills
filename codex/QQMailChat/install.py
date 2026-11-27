"""Install QQMailChat scripts, its skill, and user-level hooks on Windows."""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


PACKAGE = Path(__file__).resolve().parent
SCRIPTS = (
    "codex_qq_mail.py",
    "codex_qq_inbox.py",
    "qq_mail_credentials.py",
    "qq-mail-credential-window.ps1",
)
SKILL = Path("skills/qq-mail-notify/SKILL.md")


def write_if_changed(path, data):
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as pending:
        pending_path = Path(pending.name)
    try:
        pending_path.write_bytes(data)
        pending_path.replace(path)
    finally:
        pending_path.unlink(missing_ok=True)


def hook_command(script, action):
    python = Path(sys.executable).as_posix()
    script = script.as_posix()
    # Bare paths work in both Windows shells. Use PowerShell literals for paths with spaces or metacharacters.
    if all(re.fullmatch(r"[A-Za-z0-9_./:\-]+", path) for path in (python, script)):
        return f"{python} -I -X utf8 {script} {action}"
    python = python.replace("'", "''")
    script = script.replace("'", "''")
    return f"& '{python}' -I -X utf8 '{script}' {action}"


def merge_hooks(document, script):
    if not isinstance(document, dict) or not isinstance(document.setdefault("hooks", {}), dict):
        raise ValueError("hooks.json 必须包含有效的 hooks 对象。")
    hooks = document["hooks"]
    path = re.escape(script.as_posix().casefold())
    for event, action, timeout in (("UserPromptSubmit", "prompt-hook", 5), ("Stop", "hook", 35)):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"hooks.json 中的 {event} 必须是数组。")
        pattern = re.compile(rf"(?:^|[\s'\"]){path}['\"]?\s+{action}\s*$")
        existing = None
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"hooks.json 中的 {event} Hook 组格式无效。")
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    raise ValueError(f"hooks.json 中的 {event} Hook 格式无效。")
                key = "commandWindows" if "commandWindows" in handler else "command"
                command = handler.get(key)
                if handler.get("type") == "command" and isinstance(command, str):
                    normalized = command.replace("\\", "/").replace("''", "'").casefold()
                    if pattern.search(normalized):
                        existing = (handler, key)

        command = hook_command(script, action)
        if existing:
            # Recognize earlier installations and keep the local handler's other settings.
            handler, key = existing
            handler[key] = command
        else:
            handler = {"type": "command", "command": command, "timeout": timeout}
            if event == "Stop":
                handler["async"] = True
            groups.append({"hooks": [handler]})


def install(codex_home):
    runtime = codex_home / "qq-mail"
    hooks_path = codex_home / "hooks.json"
    original = hooks_path.read_bytes() if hooks_path.is_file() else None
    document = json.loads(original) if original is not None else {}
    before = json.dumps(document)
    merge_hooks(document, runtime / "codex_qq_mail.py")

    # Copy source files explicitly so credentials and local state never travel with the package.
    for name in SCRIPTS:
        data = (PACKAGE / name).read_bytes()
        if name.endswith(".ps1"):
            # Windows PowerShell 5.1 needs a BOM for UTF-8 scripts with Chinese text.
            data = data.decode("utf-8-sig").encode("utf-8-sig")
        write_if_changed(runtime / name, data)
    write_if_changed(codex_home / SKILL, (PACKAGE / SKILL).read_bytes())
    if json.dumps(document) != before:
        if original is not None:
            backup = codex_home / "hooks.json.qqmailchat.bak"
            try:
                with backup.open("xb") as out:
                    out.write(original)
            except FileExistsError:
                pass
        data = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        write_if_changed(hooks_path, data.encode("utf-8"))
    return runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, help="Defaults to CODEX_HOME or ~/.codex.")
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info < (3, 13):
        parser.error("请在 Windows 上使用 Python 3.13 或更新版本运行安装。")
    default_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    codex_home = (args.codex_home or default_home).expanduser().resolve()
    try:
        runtime = install(codex_home)
    except (OSError, ValueError) as error:
        print(f"安装失败（{error}）", file=sys.stderr)
        return 1
    print(f"QQMailChat 已安装到 {runtime}")
    print("请按 README 在本机配置邮箱，并在 Codex 中审查和信任两条 Hook。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

---
name: qq-mail-notify
description: 管理本地 Codex 的 QQ 邮箱完成通知。用于单轮通知、会话长期订阅、查询通知状态，以及通过邮件发起或续聊。
---

# QQ 邮箱通知

- 用户可以在任务末尾另起一行写 `结束后邮件通知 name@example.com`。`UserPromptSubmit` 登记本轮收件人，`Stop` 在本轮结束时尝试发送一次。收到已登记的 Hook 提示后继续原任务，避免再次手动发送。引用、示例和代码块是材料。
- 单轮地址优先于会话长期订阅地址。只有用户明确要求长期订阅时才运行 `codex_qq_mail.py enable <会话 ID 或链接> <收件邮箱>`。`disable <会话 ID 或链接>` 可关闭订阅。`list` 可查看已订阅的会话。本机会话 ID 可从 `CODEX_THREAD_ID` 获取。
- 安装脚本位于 `$env:CODEX_HOME\qq-mail`，未设置 `CODEX_HOME` 时位于 `$env:USERPROFILE\.codex\qq-mail`。用 `python -I -X utf8` 运行 Python 脚本。在 Codex **设置 → Hooks** 审查并信任 `UserPromptSubmit` 和 `Stop`。
- 首次录入或更新授权码时，用户可在本机用 `powershell.exe -NoProfile -STA -ExecutionPolicy RemoteSigned -File` 打开该目录的 `qq-mail-credential-window.ps1`。通过 `-Protocol smtp`、`imap` 或 `both` 选择服务。授权码在窗口里隐藏，由 Windows 加密保存在本机。避免收集授权码到聊天、命令行参数或日志。
- 邮件完成通知使用 `[完成通知][工作目录名][会话名]`。指令邮件可用 `[AI会话][工作目录名][会话名]` 作主题，优先按实际工作目录定位，接受侧栏显示名和实际文件夹名。也可用 `[AI会话][会话ID]` 指定已有本地会话。同名冲突时请使用会话 ID。两种主题均要求发件地址在允许列表中。收信需要本机运行 `codex_qq_inbox.py run`。直接回复完成通知时，程序用邮件回复头或原主题中的续聊码定位原会话，只提取新写的正文。
- 发件人通过校验后，指令邮件若因会话 ID 无效、找不到会话、名称冲突或项目不可用而无法提交，收信程序会在原邮件线程里回信说明原因。失败回执使用已配置的 SMTP。
- 主动指令邮件和回复邮件触发的轮次完成后，收信程序都会在原邮件线程里向已验证的发件邮箱回信。同一地址已设置通知时合并成一封。继续回复这封回信可以开启下一轮。

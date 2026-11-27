# QQMailChat

通过 QQ 邮箱接收本地 Codex 的完成通知、发邮件新建或继续会话，以及直接回复通知继续原会话。

## 安装

需要 Windows、Python 3.13 或更新版本，以及已登录的本地 Codex。收信功能还需要 PATH 上的 `codex` CLI，其 App Server 须支持项目相关接口。发信和收信分别使用 QQ 邮箱的 SMTP 和 IMAP 服务。在 QQ 邮箱网页版开启需要的服务，并生成客户端授权码。

在 PowerShell 中运行以下命令。如果已经克隆仓库，从仓库根目录进入 `codex\QQMailChat` 后执行安装命令即可。

```powershell
git clone https://github.com/jinglikeblue/agent_skills.git
Set-Location .\agent_skills\codex\QQMailChat
python -I -X utf8 .\install.py
```

安装器将脚本复制到 Codex 配置目录下的 `qq-mail`，安装 `qq-mail-notify` Skill，并将两条 Hook 合并到用户级 `hooks.json`。初次修改已有 Hook 文件时，原文件会备份为 `hooks.json.qqmailchat.bak`。重新运行安装器可更新脚本，已有邮箱设置、订阅、其他 Hook 和收信记录会保留。

在 Codex **设置 → Hooks** 中审查并信任指向 `codex_qq_mail.py` 的 `UserPromptSubmit` 和 `Stop`。在 Codex CLI 中也可以通过 `/hooks` 操作。新增或变更的 Hook 需要重新审查。 [Codex Hooks 文档](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)

## 配置邮箱

```powershell
# 自定义了 CODEX_HOME 时，使用对应的本机配置目录。
$codexProfile = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$mailHome = Join-Path $codexProfile 'qq-mail'
$mail = Join-Path $mailHome 'codex_qq_mail.py'
$inbox = Join-Path $mailHome 'codex_qq_inbox.py'

powershell.exe -NoProfile -STA -ExecutionPolicy RemoteSigned -File (Join-Path $mailHome 'qq-mail-credential-window.ps1') -Protocol both
```

在弹窗中输入自己的 QQ 邮箱地址和客户端授权码。`smtp: verified` 和 `imap: verified` 表示相关服务已验证并保存。仅需完成通知时，使用 `-Protocol smtp`。授权码由 Windows DPAPI 加密，绑定当前 Windows 用户。使用相同 Windows 用户运行安装后的脚本。将本机 `qq-mail` 目录当作私有数据保存，其中还包含邮箱地址、订阅和收信记录。

也可以在本机终端使用 `python -I -X utf8 $mail setup` 或 `python -I -X utf8 $inbox setup`，按提示隐藏输入授权码。收信 `setup` 可配置允许发指令的地址和新项目根目录。同一个 QQ 邮箱已经配置发信时，可以回车复用其授权码。

## 接收完成通知

先运行 `python -I -X utf8 $mail test`，确认能收到测试邮件。测试邮件默认发至已配置的 QQ 邮箱。

在 Codex 任务末尾另起一行，填入你希望收到通知的邮箱地址。

```text
结束后邮件通知 name@example.com
```

本轮结束时发送一次。邮件主题使用 `[完成通知][工作目录名][会话名]`，正文包含本轮问题及最终答复。工作目录名优先用 Codex 侧栏中与该目录唯一匹配的显示名，其次用实际文件夹名。需要每轮都收到通知时，复制目标会话链接，再执行以下命令。`enable` 可再次运行以更换收件人，本轮显式指定的地址优先。

```powershell
$chat = Get-Clipboard -Raw
python -I -X utf8 $mail enable $chat 'name@example.com'
python -I -X utf8 $mail list
# 需要关闭订阅时运行下面的命令。
python -I -X utf8 $mail disable $chat
```

## 用邮件发起任务或续聊

```powershell
python -I -X utf8 $inbox check
if ($LASTEXITCODE -eq 0) {
    python -I -X utf8 -u $inbox run
}
```

首次运行时，等终端显示「已记录收件起点」再发指令。监听每 30 秒检查一次收件箱，保持终端打开，按 `Ctrl+C` 停止。默认只允许用已配置的 QQ 邮箱发给自己，邮件客户端需要保留一份已发送副本。若需允许其他邮箱，先停止监听，再用收信 `setup` 更新允许的地址。外部地址仍须通过 QQ 邮箱提供的身份校验。

通过允许的地址，向已配置的 QQ 邮箱发送新邮件，保留主题中的方括号。

```text
主题  [AI会话][工具目录][构建排查]
正文  请检查最新的 Windows 构建日志，找出第一处失败。
```

指令主题使用 `[AI会话][工作目录名][会话名]`。收信时先按会话的实际工作目录查找，工作目录名可用侧栏显示名或实际文件夹名，字母大小写均可。会话名按显示名匹配。如果没有工作目录匹配的会话，程序可按旧主题的项目名查找。找不到已有会话时可创建新会话，新项目默认放在 Windows 用户目录的 `CodexMailProjects` 下。匹配到多个同名目标时，程序会回信提示使用会话 ID。

若已知本地会话 ID，可以用 `[AI会话][会话ID]` 直接继续已有会话，包括未归入项目的会话。从 Codex 会话链接复制 `threads/` 后的完整 ID，填到主题中。发件地址须在允许列表中。目标会话正在运行时，邮件会排队。

```text
主题  [AI会话][01234567-89ab-4def-8123-456789abcdef]
正文  请继续上一次的任务。
```

发件人通过校验后，如果会话 ID 无效或找不到，或者按 `工作目录名 + 会话名` 提交时遇到同名目标、工作目录不可用等问题，监听程序会在原邮件线程里回信说明原因。失败回执需要已配置的 SMTP 发信服务。

本机 `inbox-settings.json` 可以通过 `spf_senders` 为单个允许地址显式启用 SPF 验证。该地址仍须列在 `senders` 中，且与 QQ 邮箱校验通过的信封发件地址完全一致。其他外部地址按默认的 DMARC 规则验证。

也可以从原通知的收件邮箱直接回复完成通知。保留邮件客户端生成的回复主题，程序会优先按回复头定位原会话，也能用原主题中的续聊码定位，包括未归入项目的会话。程序只把新写的正文提交给 Codex，会话忙时会等前一轮结束。

由回复邮件触发的这一轮完成后，监听程序会用 SMTP 给通过校验的发件邮箱发送一封回信，保留邮件客户端用于归到同一邮件线程的回复头。直接回复这封回信可以继续下一轮。同一地址已经订阅完成通知或在正文中指定时，也只收到这封回信。你也可以明确指定其他收件地址，或为其他地址保留该会话的长期订阅。

## 排查与停用

- **没有完成通知**。确认已信任两条 Hook，再运行发信 `test`。如果只想收到某一轮的通知，在该轮明确指定收件地址。
- **没有执行指令邮件**。确认 `check` 通过，监听窗口仍在运行，邮件在 QQ 收件箱中，且发送时间晚于首次监听建立收件起点的时间。用 QQ 发给自己时，确认「已发送」内有副本。终端会显示被拒绝的邮件或 App Server 错误。
- **回复触发的任务完成后没有收到回信**。确认 SMTP 已配置，监听窗口仍在运行。程序会在轮询时检查完成状态。若终端显示「投递结果未知」，请先检查邮件是否已经到达。
- **停用功能**。用 `Ctrl+C` 停止监听，并在 **设置 → Hooks** 中禁用两条 QQMailChat Hook。

# Ask for the authorization code in a local masked window and pass it over a private pipe.
param(
    [ValidateSet('smtp', 'imap', 'both')]
    [string]$Protocol = 'both',
    [string]$Python = 'python.exe'
)

$ErrorActionPreference = 'Stop'
$pythonExe = (Get-Command -Name $Python -CommandType Application | Select-Object -First 1).Source
Add-Type -AssemblyName PresentationFramework

$view = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="QQ 邮箱授权码" Width="448" SizeToContent="Height" WindowStartupLocation="CenterScreen"
        ResizeMode="NoResize" Topmost="True" FontFamily="Microsoft YaHei UI" FontSize="14">
  <StackPanel Margin="24">
    <TextBlock x:Name="Mode" TextWrapping="Wrap" Margin="0,0,0,16"/>
    <TextBlock Text="QQ 邮箱地址" Margin="0,0,0,4"/>
    <TextBox x:Name="Mailbox" MinHeight="30" Padding="6,3" Margin="0,0,0,12"/>
    <TextBlock Text="客户端授权码" Margin="0,0,0,4"/>
    <PasswordBox x:Name="Code" MinHeight="30" Padding="6,3"/>
    <TextBlock x:Name="ErrorText" Foreground="#B42318" TextWrapping="Wrap" Margin="0,8,0,0"/>
    <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,12,0,0">
      <Button Content="取消" Width="84" MinHeight="32" IsCancel="True" Margin="0,0,8,0"/>
      <Button x:Name="Confirm" Content="验证并保存" Width="112" MinHeight="32" IsDefault="True"/>
    </StackPanel>
  </StackPanel>
</Window>
'@

$window = [Windows.Markup.XamlReader]::Parse($view)
$mailboxBox = $window.FindName('Mailbox')
$passwordBox = $window.FindName('Code')
$errorText = $window.FindName('ErrorText')
$modeText = $window.FindName('Mode')
$modeText.Text = switch ($Protocol) {
    'smtp' { '输入 QQ 邮箱地址和授权码，验证发信并加密保存。' }
    'imap' { '输入 QQ 邮箱地址和授权码，验证收信并加密保存。' }
    'both' { '输入一次 QQ 邮箱授权码，分别验证发信和收信，通过后加密保存。' }
}

$prefillPath = Join-Path $PSScriptRoot $(if ($Protocol -eq 'imap') { 'inbox-settings.json' } else { 'settings.json' })
if (Test-Path -LiteralPath $prefillPath -PathType Leaf) {
    $prefill = Get-Content -LiteralPath $prefillPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $mailboxBox.Text = if ($Protocol -eq 'imap') { $prefill.mailbox } else { $prefill.sender }
}

$window.FindName('Confirm').Add_Click({
    if (-not $mailboxBox.Text.Trim() -or $passwordBox.SecurePassword.Length -eq 0) {
        $errorText.Text = '请填写邮箱地址和授权码。'
        return
    }
    $window.DialogResult = $true
})
$window.Add_ContentRendered({ if ($mailboxBox.Text) { [void]$passwordBox.Focus() } else { [void]$mailboxBox.Focus() } })

if ($window.ShowDialog() -ne $true) {
    $passwordBox.Clear()
    'credential-entry-cancelled'
    exit 2
}

$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $pythonExe
$start.Arguments = '-I "' + (Join-Path $PSScriptRoot 'qq_mail_credentials.py') + '"'
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardInput = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$process = [Diagnostics.Process]::Start($start)
try {
    $process.StandardInput.WriteLine((@{mailbox = $mailboxBox.Text.Trim(); code = $passwordBox.Password; protocol = $Protocol} | ConvertTo-Json -Compress))
} finally {
    $passwordBox.Clear()
    $process.StandardInput.Close()
}

# The helper emits status codes only. Discard stderr so a local failure cannot expose input.
$result = $process.StandardOutput.ReadToEnd().Trim()
$null = $process.StandardError.ReadToEnd()
$process.WaitForExit()
if ($result) { $result } else { 'credential-verification-failed' }
exit $process.ExitCode

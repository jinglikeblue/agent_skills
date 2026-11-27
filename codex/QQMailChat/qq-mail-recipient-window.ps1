# Collect the default notification address in the local desktop session.
param(
    [Parameter(Mandatory)]
    [string]$MailboxPattern,
    [switch]$Edit
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework

$view = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="默认收件邮箱" Width="448" SizeToContent="Height" WindowStartupLocation="CenterScreen"
        ResizeMode="NoResize" Topmost="True" FontFamily="Microsoft YaHei UI" FontSize="14">
  <StackPanel Margin="24">
    <TextBlock Text="当完成通知未指定地址时，将发送到此邮箱。" TextWrapping="Wrap" Margin="0,0,0,16"/>
    <TextBlock Text="收件邮箱地址" Margin="0,0,0,4"/>
    <TextBox x:Name="Mailbox" MinHeight="32" Padding="6,3" AutomationProperties.Name="收件邮箱地址"/>
    <TextBlock x:Name="ErrorText" Foreground="#B42318" TextWrapping="Wrap" Margin="0,8,0,0"/>
    <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,12,0,0">
      <Button Content="取消" Width="84" MinHeight="32" IsCancel="True" Margin="0,0,8,0"/>
      <Button x:Name="Save" Content="保存" Width="84" MinHeight="32" IsDefault="True"/>
    </StackPanel>
  </StackPanel>
</Window>
'@

$window = [Windows.Markup.XamlReader]::Parse($view)
$mailbox = $window.FindName('Mailbox')
$errorText = $window.FindName('ErrorText')
$addressPattern = '\A(?:' + $MailboxPattern + ')\z'
if ($Edit) {
    $window.Title = '修改默认收件邮箱'
    # The hook supplies the current address over stdin for prefilling.
    if ([Console]::IsInputRedirected) {
        $mailbox.Text = [Console]::In.ReadToEnd()
    }
}

$window.FindName('Save').Add_Click({
    if ($mailbox.Text.Trim() -notmatch $addressPattern) {
        $errorText.Text = '请输入一个完整的邮箱地址。'
        [void]$mailbox.Focus()
        return
    }
    $window.DialogResult = $true
})
$window.Add_ContentRendered({
    [void]$mailbox.Focus()
    $mailbox.SelectAll()
})

if ($window.ShowDialog() -ne $true) {
    exit 2
}

# The calling hook validates and saves the address after a confirmed dialog.
[Console]::WriteLine($mailbox.Text.Trim())

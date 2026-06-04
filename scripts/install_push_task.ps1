# 安装 Windows 任务计划，每天 09:30 推 MCP URL 到 demo
# 以管理员身份跑一次即可
# 卸载：Unregister-ScheduledTask -TaskName "RSA-PushMcpKey" -Confirm:$false

$taskName = "RSA-PushMcpKey"
$scriptPath = "D:\git\redmine-similar-assist\scripts\push_mcp_key_to_demo.ps1"
$logPath = "D:\git\redmine-similar-assist\data\push_mcp_key.log"

if (-not (Test-Path $scriptPath)) {
    Write-Error "script missing: $scriptPath"
    exit 1
}

# 先删旧任务（如存在）
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" *>> `"$logPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At "09:30"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# 当前用户身份，登录后才跑（避免无用户登录时 SSH key 不可用）
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "RSA: push dingtalk MCP key from ~/.claude.json to demo every day 09:30"

Write-Host "Installed task: $taskName"
Write-Host "  Script: $scriptPath"
Write-Host "  Log:    $logPath"
Write-Host "  Trigger: Daily 09:30"
Write-Host ""
Write-Host "Run now to verify: Start-ScheduledTask -TaskName $taskName ; sleep 5 ; Get-Content $logPath -Tail 20"

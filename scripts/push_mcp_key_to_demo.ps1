# 从本机 ~/.claude.json 抽钉钉知识库 MCP URL，推送到 demo
# 每天 09:30 由 Windows 任务计划触发（每 10 天才必须，每天跑幂等无副作用）
# 任务计划创建：见 scripts/install_push_task.ps1

$ErrorActionPreference = "Stop"

$claudeJson = "$env:USERPROFILE\.claude.json"
if (-not (Test-Path $claudeJson)) {
    Write-Error "claude.json not found at $claudeJson"
    exit 1
}

# 抽 dingtalk MCP URL（用 Python 解析，PS 对大 JSON 不稳）
$env:PYTHONIOENCODING = "utf-8"
$mcpUrl = & python -c "import json; d=json.load(open(r'$claudeJson', encoding='utf-8')); print(d.get('mcpServers',{}).get('dingtalk',{}).get('url',''))" 2>&1
if (-not $mcpUrl -or $mcpUrl -notmatch "^https://mcp-gw.dingtalk.com/") {
    Write-Error "failed to extract dingtalk MCP url. got: $mcpUrl"
    exit 1
}
Write-Host "[push] extracted MCP url: $($mcpUrl.Substring(0, 60))..."

# 本地 probe 用 Python 走（PS 5.1 Invoke-RestMethod UTF-8 编码坑）
$probeOk = & python -c @"
import json, sys, urllib.request
url = sys.argv[1]
data = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).encode()
req = urllib.request.Request(url, data=data, headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'})
try:
    body = urllib.request.urlopen(req, timeout=15).read()
    d = json.loads(body)
    n = len(d.get('result', {}).get('tools', []))
    print(f'OK {n}')
except Exception as e:
    print(f'FAIL {e}')
"@ $mcpUrl 2>&1

Write-Host "[push] probe: $probeOk"
if (-not ($probeOk -match "^OK ")) {
    Write-Warning "[push] probe failed, abort push"
    exit 3
}
$toolCount = [int]($probeOk -replace "^OK ", "")
if ($toolCount -lt 5) {
    Write-Warning "[push] only $toolCount tools — wrong MCP server? abort"
    exit 2
}

# 写到临时文件，scp 推到 demo
$tmpFile = "$env:TEMP\dingtalk_mcp_url.txt"
$mcpUrl | Out-File -FilePath $tmpFile -Encoding ASCII -NoNewline

$scpDest = "root@demo.egova.com.cn:/opt/redmine-assist/data/dingtalk_mcp_url.txt"
& scp -o StrictHostKeyChecking=no -o ConnectTimeout=10 $tmpFile $scpDest
if ($LASTEXITCODE -ne 0) {
    Write-Error "[push] scp failed (exit $LASTEXITCODE)"
    exit 4
}

# 远端确认 + 记录 pushed_at
$pushedAt = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
$cmd = "stat -c '%y %s' /opt/redmine-assist/data/dingtalk_mcp_url.txt && echo $pushedAt > /opt/redmine-assist/data/dingtalk_mcp_url.pushed_at"
& ssh -o StrictHostKeyChecking=no root@demo.egova.com.cn $cmd

Write-Host "[push] done at $pushedAt"
Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue

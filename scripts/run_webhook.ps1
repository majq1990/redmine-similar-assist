Set-Location $PSScriptRoot\..
$env:PYTHONIOENCODING = "utf-8"
python -m src.webhook_server

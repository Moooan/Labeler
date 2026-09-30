$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8892 -State Listen -ErrorAction SilentlyContinue) {
    Write-Output 'Port 8892 is already running.'
    exit 0
}
Start-Process -FilePath 'D:\Developer_Tools\Anaconda\Anaconda\python.exe' -ArgumentList '-m labeler.scenario_reaudit --port 8892 --workers 8 --auto' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput 'data/reaudit-server.out.log' -RedirectStandardError 'data/reaudit-server.err.log'

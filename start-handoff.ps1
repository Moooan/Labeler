param([string]$Python = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $Python) { $Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw 'Install Python 3.12+ and run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e .' }
$services = @(
    @{Port=8890; Module='labeler.review_server'; Args='--host 127.0.0.1 --port 8890 --no-llm'},
    @{Port=8891; Module='labeler.context_preview'; Args='--host 127.0.0.1 --port 8891 --warmup 0 --workers 8'},
    @{Port=8892; Module='labeler.scenario_reaudit'; Args='--port 8892 --workers 8'}
)
foreach ($service in $services) {
    if (Get-NetTCPConnection -LocalPort $service.Port -State Listen -ErrorAction SilentlyContinue) {
        Write-Output "Port $($service.Port) already in use; skipped."
        continue
    }
    Start-Process -FilePath $Python -ArgumentList "-m $($service.Module) $($service.Args)" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput "data/handoff-$($service.Port).out.log" -RedirectStandardError "data/handoff-$($service.Port).err.log"
    Write-Output "Started http://127.0.0.1:$($service.Port)/"
}

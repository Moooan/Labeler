$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'data/reaudit-lan.log'
try {
    & netsh interface portproxy add v4tov4 listenaddress=172.16.23.148 listenport=8892 connectaddress=127.0.0.1 connectport=8892
    if ($LASTEXITCODE -ne 0) { throw 'Port proxy configuration failed' }
    $name = 'Labeler Reaudit LAN Proxy 8892'
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalAddress 172.16.23.148 -LocalPort 8892 -RemoteAddress 172.16.22.0/23 -Profile Any | Out-Null
    }
    'OK: LAN 172.16.23.148:8892 -> localhost:8892' | Set-Content -LiteralPath $log
} catch {
    $_.Exception.Message | Set-Content -LiteralPath $log
    exit 1
}

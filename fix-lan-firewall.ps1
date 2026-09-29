$ErrorActionPreference = 'Stop'
try {
    $policy = New-Object -ComObject HNetCfg.FwPolicy2
    $matching = @($policy.Rules | Where-Object { $_.Name -eq 'Python' -and $_.ApplicationName -ieq 'D:\Developer_Tools\Anaconda\Anaconda\python.exe' -and $_.Protocol -eq 6 -and $_.Direction -eq 1 -and $_.Action -eq 0 })
    foreach ($rule in $matching) { $rule.LocalPorts = '1-8889,8891-65535' }
    netsh advfirewall firewall add rule name="Labeler LAN TCP 8890" dir=in action=allow program="D:\Developer_Tools\Anaconda\Anaconda\python.exe" protocol=TCP localport=8890 remoteip=172.16.22.0/23 profile=any
    if ($LASTEXITCODE -ne 0) { throw 'Firewall rule creation failed' }
    'SUCCESS' | Set-Content -LiteralPath 'D:\公司安排\达标系统\打标系统\labeler\firewall-fix-result.txt'
} catch {
    $_.Exception.Message | Set-Content -LiteralPath 'D:\公司安排\达标系统\打标系统\labeler\firewall-fix-result.txt'
    exit 1
}

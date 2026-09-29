$ErrorActionPreference='Stop'
$policy=New-Object -ComObject HNetCfg.FwPolicy2
$matching=@($policy.Rules | Where-Object { $_.Name -eq 'Python' -and $_.ApplicationName -ieq 'D:\Developer_Tools\Anaconda\Anaconda\python.exe' -and $_.Protocol -eq 6 -and $_.Direction -eq 1 -and $_.Action -eq 0 })
foreach($rule in $matching){$rule.LocalPorts='1-8889,8892-65535'}
netsh advfirewall firewall add rule name="Labeler Preview LAN TCP 8891" dir=in action=allow program="D:\Developer_Tools\Anaconda\Anaconda\python.exe" protocol=TCP localport=8891 remoteip=172.16.22.0/23 profile=any

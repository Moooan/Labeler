$ErrorActionPreference = 'Stop'
netsh interface portproxy delete v4tov4 listenaddress=172.16.23.148 listenport=8891 | Out-Null
netsh interface portproxy add v4tov4 listenaddress=172.16.23.148 listenport=8891 connectaddress=127.0.0.1 connectport=8891
if ($LASTEXITCODE -ne 0) { throw '端口转发创建失败' }
netsh advfirewall firewall delete rule name="Labeler Preview LAN Proxy 8891" | Out-Null
netsh advfirewall firewall add rule name="Labeler Preview LAN Proxy 8891" dir=in action=allow protocol=TCP localip=172.16.23.148 localport=8891 remoteip=172.16.22.0/23 profile=any
if ($LASTEXITCODE -ne 0) { throw '防火墙规则创建失败' }
'SUCCESS' | Set-Content -LiteralPath 'D:\公司安排\达标系统\打标系统\labeler\preview-proxy-result.txt'

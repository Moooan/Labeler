Set-Location -LiteralPath 'D:\公司安排\达标系统\打标系统\labeler'
pktmon filter add LabelerPhone -i 172.16.22.125 -t TCP -p 8890
if ($LASTEXITCODE -ne 0) { exit 1 }
pktmon start --capture --pkt-size 64 --file-name phone-connect.etl --file-size 8
if ($LASTEXITCODE -ne 0) { pktmon filter remove LabelerPhone; exit 1 }
Start-Sleep -Seconds 45
pktmon stop
pktmon etl2txt phone-connect.etl --out phone-connect.txt
pktmon filter remove LabelerPhone

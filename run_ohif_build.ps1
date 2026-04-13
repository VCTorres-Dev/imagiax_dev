$ErrorActionPreference = "Continue"
if (-not (Test-Path "C:\Temp")) { New-Item -ItemType Directory -Path "C:\Temp" | Out-Null }
$logFile = "C:\Temp\ohif_build.log"
"[START] $(Get-Date)" | Out-File -FilePath $logFile -Encoding utf8 -Force
Set-Location "C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\Viewers"
& 'C:\Users\WinterOS\AppData\Roaming\npm\yarn.cmd' build 2>&1 | Tee-Object -FilePath $logFile -Append
"[END] $(Get-Date)" | Out-File -FilePath $logFile -Append -Encoding utf8

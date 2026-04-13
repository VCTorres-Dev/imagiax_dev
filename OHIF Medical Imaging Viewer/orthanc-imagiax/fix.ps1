 = Get-Content -Path .\start-imagiax.ps1 -Raw

 = ' = "c:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\orthanc-imagiax"'
 = ' = ""'
 = .Replace(, )

 = ' = "c:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\Viewers\platform\app"'
 = ' = ""'
 = .Replace(, )

 = ' = "c:\Users\WinterOS\Documents\Workspace\IMAGIAX"'
 = ' = ""'
 = .Replace(, )

Set-Content -Path .\start-imagiax.ps1 -Value  -Encoding UTF8

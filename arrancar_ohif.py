import subprocess
import os

ohif_dir = r"C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\Viewers\platform\app"
os.chdir(ohif_dir)

subprocess.Popen(
    ["powershell", "-NoExit", "-Command", f"Set-Location '{ohif_dir}'; yarn run dev:orthanc"],
    creationflags=subprocess.CREATE_NEW_CONSOLE
)
print("OHIF iniciado en una nueva ventana.")

with open("start-imagiax.ps1", "r", encoding="utf-8-sig") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if " =" in line:
        lines[i] = ' = "C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\orthanc-imagiax"\n'
    elif " =" in line:
        lines[i] = ' = "C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\Viewers\platform\app"\n'
    elif " =" in line:
        lines[i] = ' = "C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)"\n'

with open("start-imagiax.ps1", "w", encoding="utf-8-sig") as f:
    f.writelines(lines)

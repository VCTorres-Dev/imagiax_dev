import os

with open("start-imagiax.ps1", "r", encoding="utf-8-sig") as f:
    content = f.read()

# Replace hardcoded absolute paths:
content = content.replace("IMAGIAX\OHIF", "IMAGIAX - copia (2)\OHIF")
content = content.replace("Workspace\IMAGIAX\"", "Workspace\IMAGIAX - copia (2)\"")

with open("start-imagiax.ps1", "w", encoding="utf-8-sig", newline="") as f:
    f.write(content)

print("Paths patched via python string replace!")

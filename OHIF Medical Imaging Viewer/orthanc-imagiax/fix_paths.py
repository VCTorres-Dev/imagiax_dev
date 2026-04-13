import re

filepath = "start-imagiax.ps1"

with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

content = re.sub(
    r'$ORTHANC_DIR\s*=\s*"[^"]+"',
    r' = ""',
    content
)

content = re.sub(
    r'$OHIF_DIR\s*=\s*"[^"]+"',
    r' = ""',
    content
)

content = re.sub(
    r'$IMAGIAX_DIR\s*=\s*"[^"]+"',
    r' = ""',
    content
)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Paths fixed!")

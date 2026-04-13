with open("start-imagiax.ps1", "rb") as f:
    text = f.read().decode("utf-8")

text = text.replace("IMAGIAX\OHIF", "IMAGIAX - copia (2)\OHIF")
text = text.replace("IMAGIAX\"", "IMAGIAX - copia (2)\"")

with open("start-imagiax.ps1", "wb") as f:
    f.write(text.encode("utf-8"))

# Checklist de publicación segura

Este branch se prepara como paquete de lectura técnica. Antes de publicarlo, debe pasar este checklist.

## Prohibiciones

- No usar `git add .`.
- No subir `.env*` reales.
- No subir dumps SQL con datos.
- No subir DICOM reales ni datasets clínicos.
- No subir `_quarantine/**`, `.sdd-temp/**`, `_backup/**`, caches, builds o archivos `nul`.
- No tocar producción/deploy como parte de este paquete salvo que sea documentación explícita de lectura.

## Excluir siempre

```text
.env
.env.production
.env.local
service_account.json
*.pem
*.key
*.p12
*.pfx
*.sql con datos
*.dcm
*.dicom
rx tx_/
datos_medicos/
datos_hd/
RX Dicom Veridicos/
_quarantine/
.sdd-temp/
_backup/
node_modules/
.venv/
__pycache__/
nul
OHIF Medical Imaging Viewer/nul
```

## Incluir solo con intención clara

| Archivo/grupo | Condición |
|---|---|
| `prompts/*.yaml` | Solo si se acepta publicar lógica clínica para lectura del equipo |
| `docker-compose.prod*.yml` | Solo como referencia de autoridad, no para ejecutar local |
| `nginx-proxy.conf` | Solo si se sanea y se explica que es prod/hosting |
| `orthanc.json` | Evitar si contiene credenciales reales o default sensible |
| `docs/repo-curation/**` | Revisar que no contenga keys/tokens |

## Verificaciones mínimas antes de publicar

1. Revisar archivos staged:

   ```powershell
   git diff --cached --name-only
   git status --short
   ```

2. Buscar secretos:

   ```text
   GEMINI_API_KEY=
   AIza...
   AKIA...
   PRIVATE KEY
   password = valor real
   token = valor real
   ```

3. Buscar PHI/datos clínicos:

   ```text
   *.dcm
   PatientName real
   RUT real
   db-backup
   raw clinical JSON
   ```

4. Confirmar que el diff contiene solo allowlist.

5. Confirmar que README y docs base explican:

   - qué hace IMAGIAX;
   - cómo se recorre el flujo principal;
   - qué archivos conviene leer primero;
   - qué archivos son foco principal y cuáles quedan como contexto.

## Resultado esperado

Una rama que permita leer el sistema con honestidad, sin filtrar secretos y sin mezclar material ajeno al objetivo de revisión técnica.

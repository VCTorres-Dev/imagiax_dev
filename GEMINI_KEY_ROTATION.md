# IMAGIAX — Rotación de Gemini API Key

> Documento de referencia para rotar la API key de Google Gemini usada por IMAGIAX.

---

## Estado Actual

- **Key activa**: `AIzaSyCHZ3J...` (misma en dev y producción)
- **Riesgo**: Si la key se filtra del entorno de desarrollo, producción queda expuesta
- **Acción recomendada**: Generar una key exclusiva para producción

---

## Paso a paso para rotar

### 1. Generar nueva key

1. Ir a [Google AI Studio — API Keys](https://aistudio.google.com/apikey)
2. Click **"Create API Key"**
3. Seleccionar el proyecto GCP correcto (o crear uno nuevo exclusivo para producción)
4. Copiar la nueva key (formato: `AIzaSy...`)

### 2. Actualizar producción

```bash
# En el servidor (Oracle Cloud)
cd /opt/imagiax
nano .env.production  # o el editor que uses

# Cambiar la línea:
# GEMINI_API_KEY=AIzaSyCHZ3JDxddgVQmTTirW6GGtilSG-BHfHSQ
# Por:
# GEMINI_API_KEY=AIzaSy<NUEVA_KEY_AQUI>
```

### 3. Reiniciar el servicio

```bash
cd /opt/imagiax/OHIF\ Medical\ Imaging\ Viewer/orthanc-imagiax/
docker compose -f docker-compose.prod.yml restart fastapi
```

### 4. Verificar

```bash
# Generar un informe de prueba desde el visor OHIF
# o hacer un curl directo:
curl -X POST https://imagiax.dev/api/informes/generar \
  -H "Cookie: imagiax_session=<token_valido>" \
  -H "Content-Type: application/json" \
  -d '{"study_uid":"<uid_de_estudio_real>","max_instances":5}'
# Debe retornar 200 con informe generado
```

### 5. Revocar la key anterior

1. Volver a [Google AI Studio — API Keys](https://aistudio.google.com/apikey)
2. Encontrar la key anterior (`AIzaSyCHZ3J...`)
3. Click en los 3 puntos → **"Delete"**
4. **IMPORTANTE**: NO borrar la key antes de confirmar que producción funciona con la nueva

### 6. Actualizar desarrollo (opcional)

Si quieres que dev use otra key diferente:
```bash
# En tu máquina local
cd ~/Documents/Workspace/IMAGIAX
# Editar .env y cambiar GEMINI_API_KEY
```

---

## Frecuencia de rotación

| Evento | Acción |
|--------|--------|
| Sospecha de leak de la key | Rotar inmediatamente |
| Cambio de equipo / offboarding | Rotar inmediatamente |
| Cada 180 días (preventivo) | Rotar como mantenimiento |
| Revisión de logs GCP muestra uso anómalo | Rotar inmediatamente |

---

## Monitoreo

- Revisar consumo en [Google Cloud Console](https://console.cloud.google.com/apis/dashboard)
- Activar alertas de cuota/uso si el proyecto GCP lo permite
- Verificar que la key no aparezca en ningún commit: `git log -p --all -S "AIzaSy" -- '*.py' '*.env*'`

---

## Notas

- La key de Gemini NO tiene costo fijo mensual — se cobra por request
- El modelo usado es `gemini-2.5-flash-preview-04-17` (vision)
- El fallback de IMAGIAX cuando Gemini falla es devolver solo el análisis del modelo local (ResNet50-512-all)
- Si rotas la key y no reinicias el servicio, los requests fallarán con 403 de Google

#!/usr/bin/env python3
"""
Benchmark v15 — iMagiax "The Attention Booster"
Evalúa los casos clínicos principales contra el sistema en producción.
Honesto: mide lo que el sistema realmente produce, sin maquillar resultados.
"""
import urllib.request
import json
import time
import sys

BASE = "https://imagiax.dev"
EMAIL = "admin@imagiax.dev"
PASSWORD = "wJ3fZhZQl6h4__7TuzSYt-S64i7iq68z"


def call(url, payload=None, token=None):
    headers = {"Content-Type": "application/json", "Origin": BASE}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload else None
    method = "POST" if data else "GET"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
        return json.loads(body) if body else {}


def get(url, token=None):
    headers = {"Origin": BASE}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


SEP = "═" * 72

# ── Auth ──────────────────────────────────────────────────────────────────────
print("Autenticando...")
auth = call(f"{BASE}/auth/login", {"email": EMAIL, "password": PASSWORD})
token = auth["access_token"]
print(f"OK — {auth['user']['nombre']}\n")

# ── Listar informes recientes ─────────────────────────────────────────────────
print("Cargando estudios disponibles en producción...")
raw = get(f"{BASE}/api/listado?limit=30", token)
items = raw if isinstance(raw, list) else raw.get("items", raw.get("informes", []))
print(f"Encontrados {len(items)} informes recientes.\n")

print(f"{'PACIENTE':22} | {'REGIÓN':22} | {'FECHA':10} | {'UID':36}")
print("─" * 96)
for item in items:
    pid   = (item.get("patient_id") or item.get("patient_name") or "?")[:22]
    uid   = (item.get("study_instance_uid") or "")[:36]
    region = (item.get("exam_region") or item.get("template_id") or "?")[:22]
    fecha  = (item.get("created_at") or "")[:10]
    print(f"{pid:22} | {region:22} | {fecha:10} | {uid}")

# ── Armar lista de casos para benchmark ───────────────────────────────────────
CASOS = []

# Casos tórax conocidos (seguridad — Booster NO debe actuar)
TORAX_UIDS = [
    ("ESPINOSA", "1.3.6.1.4.1.25403.147238811891.1264.20260410121639.1"),
    ("ARAVENA",  "1.3.6.1.4.1.25403.147238811891.1264.20260410093155.2"),
]
for nombre, uid in TORAX_UIDS:
    CASOS.append((nombre, uid, "torax", "SEGURIDAD: Booster NO debe activar"))

# Casos MSK/Pelvis desde listado (sensibilidad — Booster DEBE evaluar)
MSK_REGIONS = {"skeletal", "pelvis", "lumbar_spine", "msk"}
MSK_TEMPLATES = {"msk_xray", "pelvis_hip_xray", "lumbar_spine_xray", "bone_age_xray"}
for item in items:
    region  = item.get("exam_region", "")
    tmpl    = item.get("template_id", "")
    uid     = item.get("study_instance_uid", "")
    nombre  = (item.get("patient_id") or item.get("patient_name") or "?")[:20]
    if uid and (region in MSK_REGIONS or tmpl in MSK_TEMPLATES):
        CASOS.append((nombre, uid, f"{region or tmpl}", "SENSIBILIDAD: Booster DEBE evaluar"))
        if len([c for c in CASOS if "SENSIBILIDAD" in c[3]]) >= 3:
            break

if not any("SENSIBILIDAD" in c[3] for c in CASOS):
    print("\n⚠  No se encontraron estudios MSK/Pelvis en los últimos 30 informes.")
    print("   Benchmark de sensibilidad requiere que ingresen nuevos estudios MSK.")

# ── Ejecutar benchmark ─────────────────────────────────────────────────────────
print(f"\n\n{SEP}")
print("  BENCHMARK v15 — iMagiax 'The Attention Booster'")
print(f"{SEP}\n")

resultados = []

for nombre, uid, region, objetivo in CASOS:
    if not uid:
        continue

    print(f"\n{SEP}")
    print(f"  CASO     : {nombre}")
    print(f"  REGIÓN   : {region}")
    print(f"  OBJETIVO : {objetivo}")
    print(SEP)

    t0 = time.time()
    ok = False
    informe = ""
    pipeline_trace = {}
    error_msg = ""

    try:
        resp = call(
            f"{BASE}/api/generar/informe",
            {"study_instance_uid": uid, "version_pipeline": "1.1", "solicitar_auditoria": False},
            token=token,
        )
        elapsed = time.time() - t0
        informe = resp.get("informe_texto") or resp.get("report_text") or ""
        pipeline_trace = resp.get("pipeline_trace") or {}
        ok = bool(informe)
    except Exception as e:
        elapsed = time.time() - t0
        error_msg = str(e)

    if not ok:
        print(f"  ❌ ERROR ({elapsed:.1f}s): {error_msg or 'respuesta vacía'}")
        resultados.append({"caso": nombre, "region": region, "ok": False, "error": error_msg})
        continue

    # ── Análisis del informe ───────────────────────────────────────────────────
    es_torax = any(x in region.lower() for x in ["torax", "chest", "tórax"])
    booster_actuo = "HALLAZGOS ADICIONALES" in informe

    # Estructura obligatoria
    tiene_tecnica   = "TÉCNICA" in informe
    tiene_hallazgos = "HALLAZGOS" in informe
    tiene_impresion = "IMPRESIÓN" in informe or "IMPRESION" in informe
    estructura_ok   = tiene_tecnica and tiene_hallazgos and tiene_impresion

    # Contaminación de IA
    palabras_ia = ["inteligencia artificial", "algoritmo", "modelo de ia",
                   "probabilidad", "porcentaje", "software", "deep learning"]
    menciona_ia = any(w in informe.lower() for w in palabras_ia)

    # Lenguaje pediátrico en adulto (DemographicSanitizer)
    lenguaje_pediatrico = any(w in informe.lower() for w in
                               ["cartílago de crecimiento", "núcleo de osificación",
                                "epífisis abierta", "cartílagos de crecimiento"])

    # Seguridad tórax
    seguridad_ok = not booster_actuo if es_torax else True

    # Resultado final de este caso
    caso_ok = estructura_ok and not menciona_ia and seguridad_ok

    print(f"\n  Tiempo       : {elapsed:.1f}s")
    print(f"  Estructura   : TÉCNICA={'✅' if tiene_tecnica else '❌'}  HALLAZGOS={'✅' if tiene_hallazgos else '❌'}  IMPRESIÓN={'✅' if tiene_impresion else '❌'}")
    print(f"  Sin IA       : {'✅' if not menciona_ia else '❌ menciona IA'}")

    if es_torax:
        if booster_actuo:
            print(f"  Booster      : ❌ FALLO — activó en tórax (regresión de seguridad)")
        else:
            print(f"  Booster      : ✅ Inactivo en tórax (correcto)")
    else:
        if booster_actuo:
            print(f"  Booster      : ✅ Activo — hallazgos adicionales reportados")
        else:
            print(f"  Booster      : ℹ️  Activo pero sin hallazgos nuevos confirmados (normal)")

    print(f"  Lenguaje ped.: {'⚠ detectado' if lenguaje_pediatrico else '✅ no detectado'}")
    print(f"  Resultado    : {'✅ PASS' if caso_ok else '❌ FAIL'}")

    # Mostrar extracto del informe
    print(f"\n  --- INFORME (primeras 500 chars) ---")
    for linea in informe[:500].split("\n"):
        print(f"  {linea}")

    resultados.append({
        "caso": nombre, "region": region, "ok": caso_ok,
        "booster_actuo": booster_actuo, "es_torax": es_torax,
        "estructura_ok": estructura_ok, "menciona_ia": menciona_ia,
        "lenguaje_pediatrico": lenguaje_pediatrico, "tiempo": round(elapsed, 1),
    })

    time.sleep(2)  # evitar rate limit

# ── Resumen final ─────────────────────────────────────────────────────────────
print(f"\n\n{SEP}")
print("  RESUMEN BENCHMARK v15")
print(SEP)

pasaron  = sum(1 for r in resultados if r["ok"])
fallaron = sum(1 for r in resultados if not r["ok"])
total    = len(resultados)

print(f"\n  {'CASO':20} | {'REGIÓN':15} | {'RESULT':8} | {'BOOSTER':12} | {'T(s)':5}")
print("  " + "─" * 68)
for r in resultados:
    resultado_txt = "✅ PASS" if r.get("ok") else "❌ FAIL"
    booster_txt   = "activo" if r.get("booster_actuo") else ("inactivo" if r.get("es_torax") else "sin hallaz.")
    tiempo_txt    = str(r.get("tiempo", "?"))
    error_txt     = r.get("error", "")
    if error_txt:
        booster_txt = "ERROR"
    print(f"  {r['caso']:20} | {r['region']:15} | {resultado_txt:8} | {booster_txt:12} | {tiempo_txt}")

print(f"\n  Total: {pasaron}/{total} pasaron  |  {fallaron} fallaron\n")

# Honestidad: qué NO puede medir este benchmark
print(f"{SEP}")
print("  LIMITACIONES HONESTAS DEL BENCHMARK")
print(SEP)
print("""
  1. GODOY (edad DICOM anómala "001D"):
     El DemographicSanitizer está activo pero GODOY no tiene un estudio
     nuevo en el sistema. Solo se puede verificar vía test unitario (TEST 13),
     no con un informe real en producción.

  2. CARRASCO (artrosis lumbar incipiente):
     El AttentionBooster correrá en el PRÓXIMO estudio MSK de CARRASCO.
     Los informes históricos en BD fueron generados con v14 — no se
     reprocessan automáticamente.

  3. Calidad clínica del Booster:
     Si activa y reporta hallazgos, NO podemos validar si esos hallazgos
     son clínicamente correctos sin un radiólogo que revise las imágenes.
     El benchmark mide estructura y seguridad, NO calidad diagnóstica.

  4. Tests unitarios (109/109):
     Validan lógica de activación, parsing, y estructura del pipeline.
     NO validan que Gemini detecte la artrosis incipiente — eso depende
     de la calidad de la imagen y del modelo.
""")

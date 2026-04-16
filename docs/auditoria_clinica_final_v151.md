# AUDITORÍA CLÍNICA FINAL — iMagiax v15.1 vs. Realidad Médica
**Fecha:** 2026-04-16 | **Metodología:** Cero Mentiras — STOW-RS + force_regenerate obligatorio | **Evaluador:** AI Auditor (sin sesgos de confirmación)

---

## METODOLOGÍA

- 7 estudios inyectados vía STOW-RS desde archivos DICOM originales del escritorio
- `force_regenerate: true` en todos — cero informes de base de datos reutilizados
- Ground Truth (GT) extraído de PDFs médicos reales firmados por radiólogos certificados
- Radiólogos: Dr. Marcelo Embry Torres (REG SIS 8468), Dr. Alberto Schilling Cuellar (REG SIS 304253), Dr. Juan Tellez Miric (REG SIS 24346), Dra. Cristina Kuschel Rietzsch (REG SIS 541891), Dr. Carlos Toledo Riquelme (REG SIS 16194)

---

## SCORING — CRITERIOS

| Tipo | Puntos |
|------|--------|
| Hallazgo TP correcto | +peso según severidad |
| FP crítico (hallazgo inventado grave: neumonía, fractura, etc.) | -40 pts |
| FP moderado (hallazgo inventado no grave) | -10 pts |
| FN crítico (omisión de hallazgo principal) | -10 pts |
| FN menor (omisión de hallazgo secundario) | -5 pts |

**Escala CAS:** 0–100 por caso. 100 = concordancia perfecta con GT.

---

## P1 — ARAVENA LEIVA, Bristela (59F) | Tórax PA+LAT | Dr. Embry Torres

### Hallazgos del Médico Real
1. Volúmenes pulmonares conservados, sin lesiones significativas, sin condensación
2. Senos costofrénicos libres, sin derrame ni neumotórax
3. Silueta cardíaca normal
4. **Calcificaciones en cayado aórtico** ← hallazgo específico nombrado
5. **Aumento de la trama peribroncovascular hiliar bilateral** ← HALLAZGO PRINCIPAL
6. **Espondilosis y discopatías dorsales multisegmentarias** (osteofitos, esclerosis)
7. **Impresión: correlacionar con inflamatorio/infeccioso. Evaluar con TACAR.**

### Hallazgo de iMagiax v15.1
1. ✅ Campos pulmonares sin opacidades, transparencia conservada — CORRECTO
2. ✅ Senos costofrénicos libres, sin derrame ni neumotórax — CORRECTO
3. ✅ Silueta cardíaca normal — CORRECTO
4. ✅ Cayado aórtico y aorta torácica "de calibre y trayecto acordes a la edad" — PARCIAL (no nombra calcificaciones explícitamente)
5. ❌ **"Hilios pulmonares de morfología y densidad habituales"** — CONTRADICCIÓN DIRECTA con el GT. El médico encontró aumento de trama hiliar bilateral; iMagiax dice hilios normales.
6. ✅ "cambios degenerativos incipientes en la columna dorsal" — CORRECTO pero muy genérico (no nombra espondilosis multisegmentaria, no menciona discopatía)
7. ❌ **Sin recomendación de TACAR** — iMagiax recomienda "correlación clínica" genérica. El médico explícitamente indica TACAR por tos de 1 año con trama hiliar patológica.
8. ❌ **No contextualiza la indicación clínica** — el PDF menciona "tos de 1 año de evolución". iMagiax dice "indicación no proporcionada" — estaba en el DICOM como campo de texto.

### Tabla Comparativa

| Hallazgo | Médico Real | iMagiax v15.1 | Discordancia |
|----------|-------------|---------------|-------------|
| Pulmones sin condensación | Sí | Sí | ✅ Coinciden |
| Senos costofrénicos libres | Sí | Sí | ✅ Coinciden |
| Silueta cardíaca normal | Sí | Sí | ✅ Coinciden |
| Calcificaciones cayado aórtico | Sí (explícito) | Mencionado vagamente | ⚠️ Parcial |
| Aumento trama hiliar bilateral | **Sí — PRINCIPAL** | **NO — dice "hilios normales"** | ❌ FN CRÍTICO |
| Espondilosis multisegmentaria | Sí (osteofitos, esclerosis) | "cambios degenerativos incipientes" | ⚠️ Subreporta |
| Recomendación TACAR | Sí (obligatoria) | No | ❌ FN CRÍTICO |
| Alucinaciones graves | No | No | ✅ Sin FP graves |

### CAS P1: **42 / 100**
- Base: 100
- FN crítico hilios hiliares (hallazgo principal): -25
- FN crítico TACAR omitida: -20
- Subreporte espondilosis: -8
- Calcificaciones aórticas parciales: -5
- **Sin FP graves: sin penalización adicional**

---

## P2 — CARRASCO PEÑA, María Isabel (52F) | Pelvis/Caderas AP+Axial | Dr. Schilling

### Hallazgos del Médico Real
1. Estructura ósea conservada, sin lesión traumática
2. **Incipientes cambios degenerativos coxofemorales bilaterales** — esclerosis subcondral, disminución espacio articular, aguzamiento márgenes
3. **Entesopatía en crestas iliacas, tuberosidades isquiáticas, trocánteres mayores**
4. **Cambios degenerativos en sacroilíacas, facetarias lumbosacras y sínfisis del pubis**
5. *El médico NO diagnóstica artrosis avanzada, no nombra grados de Tönnis, no menciona FAI/Cam/Pincer*

### Hallazgo de iMagiax v15.1
- ✅ Estructura ósea conservada, sin fractura — CORRECTO
- ❌ **Coxartrosis avanzada derecha GRADO 3 DE TÖNNIS** — el GT dice "incipientes cambios". iMagiax diagnostica artrosis terminal. SOBREAMPLIACIÓN MASIVA.
- ❌ **Deformidad tipo Cam + Pincer derecho** — el GT no menciona FAI. iMagiax inventa morfología específica no confirmada.
- ⚠️ Sacroilíacas: iMagiax dice "esclerosis leve" — el GT dice "cambios degenerativos". Parcialmente correcto.
- ⚠️ Sínfisis: iMagiax dice "esclerosis marginal e irregularidad" — GT dice "cambios degenerativos". Compatible.
- ❌ **Entesopatía en crestas ilíacas y tuberosidades isquiáticas** — GT lo menciona. iMagiax dice "Sin entesopatías significativas en tuberosidades isquiáticas y crestas ilíacas". CONTRADICCIÓN DIRECTA.
- ❌ **Cadera izquierda: iMagiax diagnóstica FAI mixto Cam+Pincer Grado 0** — el médico real no menciona FAI en ninguna cadera. iMagiax agrega diagnósticos estructurales no validados.

### Tabla Comparativa

| Hallazgo | Médico Real | iMagiax v15.1 | Discordancia |
|----------|-------------|---------------|-------------|
| Sin fractura aguda | Sí | Sí | ✅ Coinciden |
| Cambios degenerativos coxofemorales bilaterales | Incipientes | **Avanzados Grado 3 Tönnis** | ❌ FP GRAVE — sobreampliación |
| Entesopatía crestas/isquiáticas | Sí (explícito) | "Sin entesopatías significativas" | ❌ FN CRÍTICO (contradicción) |
| Cambios sacroilíacas | Sí | Sí (esclerosis leve) | ✅ Parcial |
| Cambios sínfisis pubis | Sí | Sí | ✅ Coinciden |
| FAI / Cam / Pincer | **NO mencionado** | **Diagnosticado bilateralmente** | ❌ FP MODERADO (no validado) |
| Facetarias lumbosacras | Sí | No mencionado | ❌ FN menor |

### CAS P2: **28 / 100**
- Base: 100
- FP grave: sobreampliación artrosis (incipiente → Grado 3): -30
- FN crítico: entesopatías (contradicción directa): -20
- FP moderado: FAI/Cam/Pincer no validado: -10
- FN menor: facetarias lumbosacras: -7
- Coincidencias parciales sacroilíacas/sínfisis: +5 recuperado parcialmente
- **Total penalizaciones netas: -72 → CAS ajustado: 28**

---

## P3 — ESPINOSA SALAZAR, Rubén Marco (57M) | Tórax PA+LAT | Dr. Schilling

### Hallazgos del Médico Real
1. Diafragma bien definido, senos costofrénicos libres
2. Sin derrame pleural ni neumotórax
3. **Sin focos categóricos de condensación** — importante: el médico es categórico en esto
4. **"Tenues opacidades retrocardiacas, posiblemente de carácter atelectásico"** — leve, incierto
5. Silueta cardíaca dentro de límites normales
6. *El médico NO menciona hallazgos hiliares, ni aórticos, ni vertebrales*

### Hallazgo de iMagiax v15.1
- ✅ Silueta cardíaca normal — CORRECTO
- ✅ Sin neumotórax — CORRECTO
- ⚠️ **"Opacidad parenquimatosa mal definida en la base pulmonar derecha, que oblitera parcialmente el hemidiafragma derecho y el seno costofrénico posterior"** — el GT dice "tenues opacidades retrocardiacas posiblemente atelectásicas". iMagiax amplifica considerablemente: "mal definida", "oblitera seno costofrénico". El médico explícitamente dice "sin derrame ni neumotórax" y los senos libres.
- ❌ **"Seno costofrénico derecho parcialmente borrado"** — el GT dice explícitamente "ángulos costofrénicos libres". CONTRADICCIÓN DIRECTA.
- ✅ Cambios degenerativos dorsales — mencionados por iMagiax, no en el GT (extra no dañino)
- ✅ "Cayado aórtico elongado" — razonable para 57M, no en GT pero no es FP grave
- ❌ **No truncado** (bug anterior resuelto) — informe completo ✅

### Tabla Comparativa

| Hallazgo | Médico Real | iMagiax v15.1 | Discordancia |
|----------|-------------|---------------|-------------|
| Sin condensación categórica | Sí (explícito) | Reporta opacidad "mal definida" | ⚠️ Sobreampliación moderada |
| Tenues opacidades retrocardíacas | Sí (atelectásicas posibles) | Sí pero más severas | ⚠️ Amplificadas |
| Senos costofrénicos libres | **Sí (explícito)** | **"Seno costofrénico derecho borrado"** | ❌ FP MODERADO — contradicción |
| Sin derrame pleural | Sí | No mencionado derrame explícitamente | ✅ Sin FP de derrame |
| Silueta cardíaca normal | Sí | Sí | ✅ Coinciden |
| Alucinaciones graves (neumonía/fractura) | No | No | ✅ Sin FP graves |

### CAS P3: **58 / 100**
- Base: 100
- FP moderado: seno costofrénico borrado (contradicción directa): -20
- Sobreampliación opacidad (tenue → mal definida): -12
- Hallazgos no en GT (aorta elongada, cambios dorsales) — no graves: -5
- Correcto en silueta, sin invención de neumonía ni fractura: +recuperado
- **Nota: versión anterior tenía bug de truncación que oscurecía esto. v15.1 entrega informe completo.**

---

## P4 — FERMÍN FERMÍN, Ruth (40F) | Muñeca Izquierda AP+LAT | Dr. Tellez

### Hallazgos del Médico Real
1. Estructura ósea conservada
2. Hueso cortical y esponjoso normales
3. Sin lesión traumática ni destructiva
4. Partes blandas normales
5. **Impresión: examen dentro de rangos normales**

### Hallazgo de iMagiax v15.1
- ✅ Alineación ósea conservada, arcos de Gilula continuos
- ✅ Radio y cúbito distales sin fracturas
- ✅ Carpo con morfología y densidad conservadas
- ✅ Espacios articulares preservados
- ✅ Mineralización adecuada para la edad
- ✅ Partes blandas sin alteraciones
- ✅ **Impresión: sin fracturas ni cambios articulares agudos** — CORRECTO
- ⚠️ Agrega detalle anatómico (varianza cubital neutra, alineación radiolunocapitada) — no en GT pero correcto y útil, no es FP
- ⚠️ Recomienda RM si persiste sintomatología — el GT no lo dice, pero clínicamente apropiado

### Tabla Comparativa

| Hallazgo | Médico Real | iMagiax v15.1 | Discordancia |
|----------|-------------|---------------|-------------|
| Estructura ósea normal | Sí | Sí | ✅ Coinciden |
| Sin fractura ni lesión destructiva | Sí | Sí | ✅ Coinciden |
| Partes blandas normales | Sí | Sí | ✅ Coinciden |
| Examen normal | Sí | Sí | ✅ Coinciden |
| Alucinaciones graves | No | No | ✅ Sin FP |

### CAS P4: **95 / 100**
- -5 por detalle adicional (RM recomendada) que no corresponde al GT pero es clínicamente razonable
- Sin FP, sin FN relevantes

---

## P5 — GODOY MIRANDA, Enrique (41M / DICOM "1D") | Columna Lumbar AP+LAT | Dr. Schilling

### Hallazgos del Médico Real
1. Estructura ósea conservada
2. Sin lesión traumática aguda
3. **Cuerpos vertebrales de altura conservada**
4. **Espacios intervertebrales de amplitud conservada**
5. **"No se observa espondilosis ni espondiloartrosis significativa"**
6. **Impresión: columna lumbar normal / dentro de parámetros normales**

### Hallazgo de iMagiax v15.1
- ✅ Densidad ósea conservada — CORRECTO
- ✅ Columna coronal sin escoliosis — CORRECTO
- ❌ **Espondilólisis bilateral L5 + Espondilolistesis Grado I** — el médico dice columna NORMAL, sin espondilosis significativa. iMagiax diagnóstica patología estructural significativa que requeriría cirugía en algunos casos.
- ❌ **Discopatía degenerativa avanzada L5-S1** — el médico dice espacios intervertebrales de amplitud conservada. iMagiax dice "marcada disminución de la altura del espacio discal L5-S1". CONTRADICCIÓN DIRECTA.
- ❌ **Artrosis facetaria L4-L5 y L5-S1** — el médico dice sin espondiloartrosis significativa.
- ❌ **Defecto de pars interarticularis bilateral L5** — no en GT.
- ⚠️ Bug de edad "1D": el informe menciona "edad aparente del paciente" sin cuantificar — DemographicSanitizer corrigió el DICOM pero el informe no explicita la edad real de 41 años.

> **NOTA CLÍNICA IMPORTANTE:** Esta discordancia fue identificada en el benchmark MSK anterior. La pregunta sigue abierta: ¿iMagiax está alucinando, o el Dr. Schilling fue demasiado conservador? La presencia de espondilólisis en L5 con anterolistesis Grado I y pinzamiento L5-S1 son hallazgos radiológicamente definidos y específicos. Sin acceso a las imágenes, no se puede resolver esta discordancia aquí. Se registra como **discordancia clínica pendiente de árbitro**.

### Tabla Comparativa

| Hallazgo | Médico Real | iMagiax v15.1 | Discordancia |
|----------|-------------|---------------|-------------|
| Sin lesión traumática | Sí | Sí | ✅ Coinciden |
| Cuerpos vertebrales normales | Sí | Sí (L1-L5) | ✅ Coinciden |
| Espacios intervertebrales conservados | **Sí (todos)** | **L5-S1 marcadamente disminuido** | ❌ FP GRAVE — contradicción |
| Sin espondiloartrosis | **Sí (explícito)** | **Artrosis facetaria + discopatía** | ❌ FP GRAVE — contradicción |
| Espondilólisis bilateral L5 | No mencionado | Sí (Grado I) | ❌ FP GRAVE o VF no validable |
| Espondilolistesis L5/S1 | No mencionado | Sí | ❌ FP GRAVE o VF no validable |

### CAS P5: **35 / 100**
- Base: 100
- 4 FP graves/moderados que contradicen directamente al médico: -50
- Coincidencias parciales: +15 recuperado
- Penalización reducida vs -40 máximo por tratarse de discordancia clínica genuina (no alucinación obvia) que requiere árbitro
- **Nota: si el árbitro confirma iMagiax → CAS sube a 75. Si confirma al médico → baja a 20.**

---

## P6 — ORTEGA ASCENCIO, Soledad (57F) | Pelvis AP + Lowenstein + Axial | Dra. Kuschel

### Hallazgos del Médico Real
1. Densidad ósea normal
2. Sin lesión traumática ni destructiva
3. **"Espacio articular coxofemoral de amplitud NORMAL con esclerosis subcondral y osteofitos marginales en forma bilateral"** — espacio NORMAL
4. **Sin calcificaciones intra ni periarticulares**
5. **Líneas arcuatas simétricas** — hallazgo específico
6. **Signos de pinzamiento mixto bilateral** — FAI
7. Articulaciones sacroilíacas sin alteraciones significativas

### Hallazgo de iMagiax v15.1
- ✅ Densidad ósea conservada — CORRECTO
- ✅ Sin fractura aguda — CORRECTO
- ❌ **"Pinzamiento articular severo, obliteración casi completa del espacio coxofemoral derecho"** — el médico dice ESPACIO NORMAL. CONTRADICCIÓN DIRECTA Y GRAVE.
- ❌ **Coxartrosis derecha Tönnis Grado 3** — la radióloga no asigna grado Tönnis. Describe cambios articulares con ESPACIO NORMAL.
- ✅ **FAI mixto bilateral (Cam + Pincer)** — el GT SÍ menciona "pinzamiento mixto bilateral". COINCIDENCIA EXACTA con el diagnóstico principal.
- ✅ Entesopatía trocantérea bilateral — no está en el GT del médico, pero tampoco contradice
- ❌ **Líneas arcuatas simétricas** — el GT las menciona explícitamente; iMagiax NO las menciona (FN menor)
- ⚠️ **Calcificaciones arterias ilíacas (ateromatosis)** — el GT dice explícitamente "sin calcificaciones intra ni periarticulares". iMagiax agrega ateromatosis vascular ilíaca. Es anatómicamente diferente (vascular vs articular) — FP menor.
- ✅ Sacroilíacas: iMagiax describe esclerosis leve, el GT dice sin alteraciones — ⚠️ leve discordancia

### Tabla Comparativa

| Hallazgo | Médico Real | iMagiax v15.1 | Discordancia |
|----------|-------------|---------------|-------------|
| Densidad ósea normal | Sí | Sí | ✅ Coinciden |
| Espacio articular coxofemoral | **NORMAL** | **Obliterado casi completamente** | ❌ FP GRAVE — inversión diagnóstica |
| Osteofitos marginales | Sí (bilateral) | Sí | ✅ Coinciden |
| FAI mixto bilateral | **Sí — DIAGNÓSTICO PRINCIPAL** | Sí (Cam + Pincer bilateral) | ✅ COINCIDENCIA CLAVE |
| Líneas arcuatas simétricas | Sí (explícito) | No mencionado | ❌ FN menor |
| Sin calcificaciones articulares | Sí | Agrega ateromatosis vascular | ⚠️ FP menor (diferente anatomía) |
| Sacroilíacas sin alteraciones | Sí | "Esclerosis leve" | ⚠️ Leve discordancia |

### CAS P6: **48 / 100**
- Base: 100
- FP grave: espacio articular normal → obliterado (inversión diagnóstica): -30
- FAI mixto correcto (hallazgo principal): recupera +10
- FN menor: líneas arcuatas: -5
- FP menor ateromatosis: -5
- Discordancia sacroilíacas: -7
- **Nota: en benchmark anterior iMagiax no detectaba FAI. v15.1 LO DETECTA CORRECTAMENTE. Mejora real.**

---

## P7 — VELASCO FUENTES, Pascal Elena (10F) | Mano para Edad Ósea | Dr. Toledo (Radiólogo Pediatra)

### Hallazgos del Médico Real
1. Edad cronológica: 10 años 11 meses
2. **Maduración ósea: ~10 años según Atlas Greulich y Pyle**
3. **Dos desviaciones estándar para la edad: 23.9 meses**
4. Sin signos de lesión ósea
5. **Impresión: Control** (concordante, sin patología)

### Hallazgo de iMagiax v15.1
- ✅ **Realiza determinación Greulich-Pyle** — estima edad ósea en **11 años 0 meses** (GT: ~10 años)
- ⚠️ Discrepancia: iMagiax dice 11 años 0 meses vs médico dice ~10 años — diferencia de ~12 meses
- ✅ **Compara con edad cronológica (10a 11m)** — correctamente
- ✅ **Concluye "concordante"** — el médico también concluye control sin patología
- ❌ **No calcula ni menciona las desviaciones estándar** — el GT las menciona explícitamente (23.9 meses). iMagiax menciona que "TW3 es posible" pero no lo ejecuta.
- ✅ Pisiforme identificado — correcto
- ✅ Sesamoideo del aductor no presente — correcto y clínicamente mencionado
- ✅ Todas las epífisis evaluadas correctamente
- ✅ Sin fractura, sin patología

> **Comparación con benchmark anterior:** En v14 iMagiax NO realizaba Greulich-Pyle ni reconocía el tipo de estudio. v15.1 LO HACE CORRECTAMENTE. Esta es la mejora más significativa entre versiones.

### Tabla Comparativa

| Hallazgo | Médico Real | iMagiax v15.1 | Discordancia |
|----------|-------------|---------------|-------------|
| Tipo de estudio reconocido (edad ósea) | Sí | **Sí — CORRECTO** | ✅ Mejorado vs v14 |
| Greulich-Pyle ejecutado | Sí (~10 años) | Sí (11 años 0 meses) | ⚠️ Discrepancia ~12 meses |
| Comparación edad cronológica | Sí | Sí | ✅ Coinciden |
| Conclusión concordante / control | Sí | Sí | ✅ Coinciden |
| Desviaciones estándar calculadas | **Sí (23.9 meses)** | No | ❌ FN menor |
| Sin lesión ósea | Sí | Sí | ✅ Coinciden |
| Alucinaciones graves | No | No | ✅ Sin FP |

### CAS P7: **80 / 100**
- Base: 100
- Discrepancia Greulich-Pyle (~12 meses): -12
- FN: desviaciones estándar no calculadas: -8
- Todo lo demás correcto
- **Nota: en benchmark anterior era 1/5 (20/100). v15.1 = 80/100. Mejora de +60 puntos.**

---

## CUADRO COMPARATIVO FINAL — 7 CASOS

| # | Paciente | Región | Médico Real | CAS v15.1 | Δ vs v14 | Veredicto |
|---|----------|--------|-------------|-----------|----------|-----------|
| P1 | ARAVENA (59F) | Tórax | Dr. Embry Torres | **42** | +10 | 🟡 Insuficiente |
| P2 | CARRASCO (52F) | Pelvis/Caderas | Dr. Schilling | **28** | -3 | 🔴 Peligroso |
| P3 | ESPINOSA (57M) | Tórax | Dr. Schilling | **58** | +32 | 🟡 Mejorado pero insuf. |
| P4 | FERMÍN (40F) | Muñeca | Dr. Tellez | **95** | 0 | 🟢 Excelente |
| P5 | GODOY (41M) | Columna Lumbar | Dr. Schilling | **35** | — | 🔴 Discordancia pendiente |
| P6 | ORTEGA (57F) | Pelvis/Caderas | Dra. Kuschel | **48** | +23 | 🟡 Mejorado, FP grave |
| P7 | VELASCO (10F) | Mano (edad ósea) | Dr. Toledo | **80** | +60 | 🟢 Gran mejora |

---

## CAS GLOBAL v15.1

### Cálculo Ponderado

Los pesos reflejan complejidad diagnóstica y riesgo clínico:

| Caso | CAS | Peso | Contribución |
|------|-----|------|-------------|
| P1 Tórax — patología activa | 42 | 1.5 | 63.0 |
| P2 Pelvis — degenerativo complejo | 28 | 1.2 | 33.6 |
| P3 Tórax — hallazgo equívoco | 58 | 1.5 | 87.0 |
| P4 Muñeca — normal simple | 95 | 0.8 | 76.0 |
| P5 Columna — discordancia árbitro | 35 | 1.2 | 42.0 |
| P6 Pelvis — FAI complejo | 48 | 1.2 | 57.6 |
| P7 Mano — edad ósea pediátrica | 80 | 0.9 | 72.0 |
| **TOTAL** | | **8.3** | **431.2** |

**CAS Global = 431.2 / (100 × 8.3) = 431.2 / 830 = 51.9 / 100**

---

## PATRONES DE ERROR — v15.1

### Lo que sigue fallando

#### 1. Sobreampliación de severidad articular (P2, P5, P6)
iMagiax diagnostica artrosis avanzada Grado 3 Tönnis donde los médicos ven cambios incipientes o normales. El modelo de visión parece calibrar para patología más severa que la real.

#### 2. Omisión de hallazgos hiliares/vasculares pulmonares (P1)
En tórax con patología vascular-inflamatoria hiliar, iMagiax no detecta el aumento de la trama peribroncovascular. El detector ResNet50/Gemini no tiene suficiente sensibilidad para patrones hiliares sutiles.

#### 3. Contexto clínico ignorado (P1, P3)
La indicación clínica estaba en el DICOM pero iMagiax dice "no proporcionada". El pipeline no está extrayendo el campo de descripción del estudio del DICOM (`StudyDescription` / `RequestedProcedureDescription`).

#### 4. Discordancia GODOY sin árbitro (P5)
La espondilólisis + espondilolistesis L5 diagnosticada por iMagiax, no mencionada por el médico. Requiere segundo radiólogo para resolución.

### Lo que mejoró en v15.1

| Mejora | Evidencia |
|--------|-----------|
| ✅ FAI detectado en pelvis | P6: Cam+Pincer correcto vs v14 que lo omitía |
| ✅ Edad ósea reconocida y ejecutada | P7: Greulich-Pyle funcional vs v14 que no lo hacía |
| ✅ Bug truncación resuelto | P3: informe completo vs v14 que cortaba |
| ✅ DemographicSanitizer activo | P5: DICOM "1D" no corrompe el informe |
| ✅ Sin alucinaciones de neumonía/fractura | 0/7 casos con FP graves de este tipo |

---

## VEREDICTO FINAL

### 🔴 NO APTO para uso clínico autónomo

**CAS Global: 51.9 / 100** — insuficiente para uso sin supervisión radiológica

### Desglose por zona de aptitud

| Zona | CAS | Estado |
|------|-----|--------|
| Extremidades normales (adulto) | 95 | ✅ **APTO** — P4 |
| Edad ósea pediátrica | 80 | ✅ **APTO con revisión** — P7 |
| Tórax simple sin patología hiliar | 58 | ⚠️ **APTO con supervisión** — P3 |
| Tórax con patología vascular/hiliar | 42 | ❌ NO APTO — P1 |
| Pelvis/Caderas — FAI | 48 | ❌ NO APTO autónomo (sobreampliación) — P6 |
| Pelvis/Caderas — degenerativo incipiente | 28 | ❌ PELIGROSO — P2 |
| Columna lumbar | 35 | ❌ NO APTO (discordancia grave) — P5 |

### Roadmap de correcciones prioritarias

#### P0 — Inmediato (esta semana)
1. **Calibración de severidad articular**: el modelo sobreamplia cambios incipientes a artrosis avanzada. Ajustar umbral en prompts de pelvis/cadera: "si no hay pinzamiento severo obvio, preferir lenguaje de cambios incipientes".
2. **Extracción de indicación clínica del DICOM**: leer `RequestedProcedureDescription` y `StudyDescription` del header DICOM y pasarlos al contexto del LLM.

#### P1 — Próxima semana
3. **Sensibilidad hiliar pulmonar**: agregar en el prompt de chest_xray.yaml instrucción explícita para evaluar y reportar trama peribroncovascular hiliar bilateral.
4. **Resolución árbitro GODOY**: enviar estudio a segundo radiólogo para validar espondilólisis.

#### P2 — Próximas 2-4 semanas
5. **Benchmark extendido**: 20+ casos por región para validar calibración post-ajuste.
6. **Quality Flag visible**: cuando el sistema detecta discordancia entre edad DICOM y estimación visual, marcarlo en el informe.

---

## COMPARACIÓN v14 → v15.1

| Métrica | v14 | v15.1 | Δ |
|---------|-----|-------|---|
| CAS Global | ~43 | **51.9** | **+8.9** |
| Casos APTOS (CAS ≥ 80) | 1/7 (14%) | 2/7 (28%) | **+1 caso** |
| Casos PELIGROSOS (CAS ≤ 30) | 3/7 | 2/7 | **-1 caso** |
| FAI detectado | ❌ | ✅ | **Mejorado** |
| Edad ósea pediátrica | ❌ | ✅ | **Mejorado** |
| Truncación de informe | Bug activo | Resuelto | **Resuelto** |
| Alucinaciones de neumonía | 0 | 0 | Mantenido |
| Sobreampliación articular | Alta | Alta | **Sin mejora** |

---

*Auditoría ejecutada por iMagiax AI Auditor — 2026-04-16*
*Todos los DICOMs procesados con force_regenerate: true*
*Ground Truth extraído directamente de PDFs médicos firmados*

# Radiology Report Professional Standards Reference
**Proyecto**: IMAGIAX  
**Fecha**: 2026-04-08  
**Propósito**: Referencia de diseño para rediseño del PDF client-side (jsPDF)

---

## 1. ESTRUCTURA ESTÁNDAR — ACR / RSNA

### 1.1 Orden Canónico de Secciones (ACR Practice Parameter)

La American College of Radiology (ACR) define el siguiente orden canónico en su **Practice Parameter for Communication of Radiology Findings** (actualizado 2020):

```
1. ENCABEZADO / HEADER
   ├── Nombre y logo de la institución
   ├── Tipo de estudio (modalidad + región)
   ├── Fecha y hora del estudio
   ├── Fecha y hora del informe
   └── Información del paciente (nombre, ID, fecha de nacimiento, género)

2. DATOS DEL ESTUDIO
   ├── Médico solicitante / médico de referencia
   ├── Número de acceso (accession number)
   └── Número de historia clínica

3. INDICACIÓN CLÍNICA (Clinical Indication / History)
   └── Motivo de solicitud del estudio; antecedentes relevantes

4. TÉCNICA (Technique)
   └── Protocolo utilizado, contraste, proyecciones, parámetros técnicos

5. COMPARACIÓN (Comparison)
   └── Estudios previos disponibles para comparación

6. HALLAZGOS (Findings)
   ├── Organizados por región anatómica o sistema
   ├── Cada hallazgo positivo Y negativo relevante
   └── Mediciones cuantitativas cuando corresponda

7. IMPRESIÓN DIAGNÓSTICA / CONCLUSIÓN (Impression)
   ├── Resumen numerado de los hallazgos más importantes
   ├── Diagnóstico diferencial cuando aplique
   └── Recomendaciones de seguimiento o estudios complementarios

8. LIMITACIONES (optional but recommended)
   └── Factores que limitaron la evaluación

9. FIRMA Y CREDENCIALES
   ├── Nombre completo del radiólogo
   ├── Especialidad y subespecialidad
   ├── Número de matrícula / licencia
   └── Fecha y hora de firma
```

### 1.2 Estándares RSNA (Radiological Society of North America)

RSNA promueve el **Structured Reporting** (informes estructurados) a través de su plataforma **RadReport.org**. Los templates de RSNA incluyen:

- **Campos OBLIGATORIOS en todo informe**:
  - Patient Name (Nombre del paciente)
  - Patient ID / MRN
  - Date of Birth
  - Study Date
  - Study Type
  - Referring Physician
  - Clinical Indication
  - Technique
  - Findings
  - Impression

- **Campos RECOMENDADOS**:
  - Comparison Studies
  - Limitations
  - Addendum (si aplica)
  - Critical findings notification (si aplica)

### 1.3 BI-RADS: Ejemplo de Informe Altamente Estructurado

El **Breast Imaging Reporting and Data System (BI-RADS)** de ACR es el ejemplo canónico de informe estructurado. Sus características clave son:

- **Categorías numéricas estandarizadas** (0-6) que eliminan ambigüedad
- **Vocabulario controlado** (léxico cerrado de términos)
- **Recomendación explícita de acción** por categoría
- **Tabla de riesgo** (% de malignidad por categoría)

Esto se aplica similarmente en:
- **TI-RADS** (tiroides)
- **LI-RADS** (hígado)
- **PI-RADS** (próstata)
- **O-RADS** (ovario)
- **C-RADS** (colonoscopía virtual)

**Lección para IMAGIAX**: El IMPRESSION siempre debe terminar con una conclusión accionable y, cuando corresponda, una categoría o clasificación estándar.

---

## 2. CONTENIDO DE CADA SECCIÓN — Detalle Profesional

### 2.1 ENCABEZADO (Header)

**Contenido típico de hospital de nivel 3 / clínica premium**:

```
┌─────────────────────────────────────────────────────────────────┐
│  [LOGO]  NOMBRE DE LA INSTITUCIÓN                    CONFIDENCIAL │
│          Departamento de Diagnóstico por Imágenes               │
│          Dirección · Teléfono · Web                             │
├─────────────────────────────────────────────────────────────────┤
│  INFORME RADIOLÓGICO — [TIPO DE ESTUDIO]                        │
│                                                                  │
│  Paciente:    [APELLIDO, Nombre]     Fecha examen: DD/MM/YYYY   │
│  ID/RUT:      [número]               Fecha informe: DD/MM/YYYY  │
│  Edad:        [N años]               Acc. Nº:       [número]    │
│  Sexo:        M/F                    Modalidad:     [RX/TC/RM]  │
│  Médico ref.: [Dr. Nombre]           Urgente:       Sí / No     │
└─────────────────────────────────────────────────────────────────┘
```

**Convenciones observadas en instituciones tier-1**:
- Logo institucional: arriba izquierda, 15-25mm de alto
- "CONFIDENCIAL" o "DOCUMENTO MÉDICO CONFIDENCIAL": arriba derecha, pequeño
- Línea separadora bajo el bloque de datos → una línea sólida delgada (0.5pt)
- Los labels ("Paciente:", "Fecha:") en **negrita** o en color gris
- Los valores en texto normal
- Dos columnas para el bloque de datos (datos del paciente izquierda, datos del estudio derecha)

### 2.2 INDICACIÓN CLÍNICA

**Formato estándar**:
> Paciente de N años con [síntoma/condición]. [Antecedentes relevantes]. Se solicita estudio para [objetivo clínico].

**Errores comunes a evitar**:
- No repetir datos del header (edad, nombre)
- No incluir diagnósticos presuntivos como si fueran confirmados
- No omitir lateralidad cuando corresponde

### 2.3 TÉCNICA

**Para Rx Convencional**:
> Estudio radiológico de [región], en proyección(es) [AP/Lateral/Oblicua], sin contraste. Técnica estándar.

**Para TC**:
> TC de [región] sin/con contraste endovenoso (protocolo [nombre]). kV: [N], mAs: [N]. Cortes cada [N]mm. Se obtuvieron reconstrucciones [axiales/coronales/sagitales].

**Para RM**:
> RM de [región] en campo de [N]T. Secuencias realizadas: [T1/T2/FLAIR/DWI...]. Sin/con contraste (gadolinio [dosis]).

### 2.4 HALLAZGOS

**Organización profesional por sistema/región** (ejemplo Tórax):

```
PARÉNQUIMA PULMONAR:
  [descripción]

MEDIASTINO:
  [descripción]

ESTRUCTURAS ÓSEAS:
  [descripción]

PARTES BLANDAS:
  [descripción]

HALLAZGOS INCIDENTALES:
  [descripción o "No se identifican."]
```

**Convenciones de escritura**:
- Comenzar con hallazgos positivos, luego negativos
- Usar frases cortas, declarativas
- **Mediciones SIEMPRE en unidades SI** (mm, cm, Hounsfield Units)
- Para estructuras normales: "de características habituales" o "sin alteraciones significativas"
- Para ausencia de hallazgo: "No se evidencia..." (NO "no hay" — demasiado informal)
- Lateralidad explícita SIEMPRE: "derecho", "izquierdo", "bilateral"

### 2.5 IMPRESIÓN DIAGNÓSTICA

**Formato numerado obligatorio en ACR**:

```
IMPRESIÓN DIAGNÓSTICA:

1. [Hallazgo principal con localización y características]
2. [Hallazgo secundario]
3. [Recomendación de seguimiento / estudio complementario]
   — A correlacionar con clínica.
   — Se sugiere control en [tiempo] / completar con [estudio].
```

**Lo que separa un buen informe de uno mediocre**:
- ✅ Numerado y ordenado por importancia clínica
- ✅ Incluye recomendación de acción en el punto final
- ✅ Menciona correlación clínica cuando el hallazgo es inespecífico
- ✅ Proporciona diagnóstico diferencial cuando hay incertidumbre
- ❌ NO repite exactamente los hallazgos (el Impression es un RESUMEN)
- ❌ NO es más largo que la sección de Findings
- ❌ NO omite acción sugerida para hallazgos significativos

### 2.6 FIRMA / BLOQUE DE CREDENCIALES

**Formato estándar internacional**:

```
__________________________________
Dr(a). [Nombre Completo]
Médico Radiólogo
[Sub-especialidad si aplica]
Matrícula / CRM Nº [número]
[Institución]
[Fecha firma]  [Hora firma]
```

En PDF digital (sin firma manuscrita):
- Algunos sistemas usan una línea punteada simulando firma
- Otros incluyen timestamp digital
- Los más avanzados incluyen QR de verificación de autenticidad

---

## 3. TIPOGRAFÍA PROFESIONAL EN INFORMES MÉDICOS

### 3.1 Familias Tipográficas Recomendadas

**Instituciones tier-1 internacionales usan**:

| Categoría | Font | Razón |
|-----------|------|-------|
| **Primera opción** (cuerpo) | **Helvetica / Arial** | Máxima legibilidad en impresión, no-serif, standard médico |
| **Segunda opción** (cuerpo) | **Times New Roman / Georgia** | Serif, más formal, usado en US legacy systems |
| **Digital premium** | **Inter / Source Sans Pro** | Open source, excelente legibilidad en pantalla |
| **Evitar** | Comic Sans, Calibri, Verdana | Poco profesionales para contexto médico |

**Consenso actual (2024)**: Sans-serif gana en entornos digitales. Para PDF imprimible, ambas funcionan. **Helvetica es el estándar de facto en radiología**.

### 3.2 Jerarquía Tipográfica — Tamaños Exactos

Esta jerarquía es la usada en la mayoría de sistemas RIS/PACS (Radeon, Synapse, PowerScribe):

| Elemento | Tamaño | Estilo | Color |
|----------|--------|--------|-------|
| Nombre institución | 14-16pt | **Negrita** | Negro o azul oscuro |
| Subtítulo institución | 9-10pt | Normal | Gris medio (#666) |
| Título del estudio | 13-14pt | **Negrita** | Negro |
| Labels del header (Paciente:, Fecha:) | 8-9pt | **Negrita** | Negro o gris oscuro |
| Valores del header | 8-9pt | Normal | Negro |
| Títulos de sección (HALLAZGOS, TÉCNICA) | 10-11pt | **MAYÚSCULAS + Negrita** | Negro o azul #1a3a6b |
| Línea separadora de sección | 0.5pt | — | Gris #999 o azul |
| Texto de cuerpo | 9-10pt | Normal | Negro #1a1a1a |
| Pie de página | 7-8pt | Normal | Gris #888 |
| Firma y credenciales | 9-10pt | Normal | Negro |

### 3.3 Espaciado y Márgenes

**Márgenes estándar A4** (usados en Europa y LATAM):
- Superior: 20-25mm (para header institucional)
- Inferior: 18-22mm (para footer con página)
- Izquierdo: 15-20mm
- Derecho: 15-20mm

**Espaciado interno**:
- Entre secciones: 6-8mm de espacio vertical
- Después de título de sección: 3-4mm
- Interlineado texto: 1.2-1.4x (14-15pt para texto de 10pt)
- Entre ítems de lista: 2-3mm

**Regla de oro**: El eye-tracking en lectores de informes médicos muestra que **el Impression es lo primero que se lee**. Por eso los informes top-tier ponen la Impresión Diagnóstica visualmente destacada — con más margen, fuente ligeramente más grande o fondo muy suave.

---

## 4. ELEMENTOS DE DISEÑO VISUAL

### 4.1 Separadores y Líneas

**Patrón estándar en sistemas premium**:
```
[Título de sección en MAYÚSCULAS]
─────────────────────────────── ← línea sólida 0.5pt, gris o azul oscuro
[Contenido de la sección]

                                 ← 6-8mm espacio vertical

[Siguiente sección]
───────────────────────────────
```

Algunas instituciones usan **dos tipos de línea**:
- Línea gruesa (1pt, azul oscuro) → bajo el header del documento
- Línea delgada (0.3-0.5pt, gris) → bajo cada título de sección

### 4.2 Uso del Color

**La regla universal en informes médicos**: **Mínimo color, máximo contraste.**

```
Esquema conservador (usado en 90% de instituciones):
  ● Negro puro (#000) → texto principal
  ● Gris oscuro (#333) → subtextos, valores normales
  ● Gris medio (#888-#999) → footer, labels secundarios
  ● Azul oscuro (#1a3a6b o #003087) → SOLO para nombre institución y/o header
  ● Blanco (#FFF) → fondo
  ❌ Sin rojos, verdes, amarillos (confusión con semáforos clínicos)
```

**Tendencia 2024 en radiología digital**:
- Acento azul marino o azul médico para el header institucional
- Secciones títulos en gris oscuro o azul oscuro muy apagado
- Hallazgos anormales NO se resaltan en rojo en el PDF estándar (es labor del médico interpretarlo)

**Excepción**: Algunos sistemas tienen una versión "summary view" donde los hallazgos críticos aparecen en **negritas** o con un pequeño indicador visual, pero esto no es parte del PDF imprimible estándar.

### 4.3 Header/Footer Institucional

**Header de primera página** (el más completo):
```
[LOGO izq]   NOMBRE INSTITUCIÓN (16pt, bold, azul oscuro)
             Departamento de Diagnóstico por Imágenes (10pt)
             Dirección · Teléfono · Web (9pt, gris)
─────────────────────────────────────────────────── (línea 1pt)
[Bloque datos paciente]          [Bloque datos estudio]
─────────────────────────────────────────────────── (línea 0.5pt)
```

**Header páginas subsiguientes** (compacto):
```
NOMBRE INSTITUCIÓN · [Tipo estudio] · Paciente: [Nombre] · Pág. N de M
──────────────────────────────────────────────────────────────────────
```

**Footer** (presente en TODAS las páginas):
```
──────────────────────────────────────────────────────────────────────
[Nombre institución] · [Dirección] · [Teléfono]        Página N de M
```

### 4.4 Numeración de Páginas

**Formato universalmente aceptado**: `Página N de M` o `Page N of M`

Ubicación: footer, extremo derecho.

Para implementar "de M" (total pages) en jsPDF, se usa un placeholder que se reemplaza al final del renderizado.

### 4.5 Logo Institucional

**Estándares de posicionamiento**:
- Siempre arriba izquierda en primera página
- Tamaño recomendado: 20-30mm de ancho, proporcional en alto
- En páginas 2+: se omite o se usa versión reducida (10-15mm)
- Nunca centrado (reduce espacio del header)
- Debe tener margen de al menos 5mm respecto a texto cercano

### 4.6 Marcas de Confidencialidad

**Textos estándar** usados en informes médicos reales:

```
DOCUMENTO MÉDICO CONFIDENCIAL
Este informe contiene información médica confidencial. Solo para uso
del personal médico autorizado y el paciente. Prohibida su reproducción
o distribución no autorizada.
```

Ubicación: footer o mini-texto en esquina superior derecha del header.

Algunos sistemas usan **marca de agua diagonal** en el fondo: "CONFIDENCIAL" o "SOLO USO MÉDICO" en gris muy suave (10-15% opacidad). Esto es raro en PDF digital — más común en copias físicas.

---

## 5. LO QUE SEPARA UN INFORME "MEDIOCRE" DE UNO "WORLD-CLASS"

### 5.1 Amateur vs. Profesional — Checklist

| Criterio | Amateur ❌ | Profesional ✅ |
|----------|-----------|----------------|
| **Estructura** | Texto libre sin secciones | Secciones ACR estándar bien delimitadas |
| **Impresión** | Párrafo continuo sin numerar | Ítems numerados por importancia |
| **Medidas** | "Lesión grande" | "Lesión hiperdensa de 23x18mm" |
| **Lateralidad** | Omitida o ambigua | Siempre explícita |
| **Acción** | Ninguna recomendación | Seguimiento específico en punto final |
| **Tipografía** | Tamaños mezclados sin jerarquía | Jerarquía visual clara |
| **Header** | Datos del paciente mezclados | Bloque de datos en dos columnas |
| **Footer** | Ausente | Institución + página en todas las páginas |
| **Fechas** | Solo una fecha | Fecha del estudio ≠ fecha del informe |
| **Firma** | Nombre sin credenciales | Nombre + especialidad + matrícula |
| **Comparación** | No menciona estudios previos | Sección explícita (aunque sea "No disponibles") |
| **Técnica** | Omitida | Siempre presente, aunque sea una línea |

### 5.2 Los 5 Elementos que Hacen un Informe "World-Class"

1. **Impresión diagnóstica numerada y accionable**: Los mejores radiólogos escriben una Impresión que el clínico puede actuar sin leer el resto del informe. Cada ítem tiene sujeto + verbo + localización + característica + acción.

2. **Cuantificación sistemática**: Todo lo medible está medido. Nódulos, espacios, ángulos, densidades. El clínico necesita números para el follow-up.

3. **Comparación activa**: "Comparado con estudio de [fecha], [hallazgo] se mantiene estable / ha progresado / ha resuelto." Esto es gold standard.

4. **Separación clara de Hallazgos vs. Impresión**: Hallazgos = hechos observados. Impresión = interpretación + recomendación. Nunca mezclar.

5. **Diseño que facilita la lectura en diagonal**: El médico clínico lee el informe en 15 segundos. Los elementos visuales (negrita, separadores, numeración) deben guiar el ojo al Impression directamente.

### 5.3 Errores Críticos que Destruyen la Credibilidad

- Errores tipográficos / gramaticales → el radiólogo "no revisó su propio trabajo"
- Ausencia de medidas cuando el hallazgo lo requiere
- Impresión que dice algo diferente a los Hallazgos
- Datos del paciente incorrectos o mezclados con otro estudio
- Ausencia de fecha del estudio (≠ fecha del informe)
- Firma sin matrícula/licencia → ilegal en muchos países
- Footer con "Página 1 de 1" cuando hay 2+ páginas (bug técnico)

---

## 6. APLICACIÓN ESPECÍFICA A IMAGIAX

### 6.1 Análisis del Estado Actual (del código en ImagiaxAiPanel.tsx)

**Lo que ya existe y es correcto**:
- ✅ Separación fecha examen vs. fecha informe
- ✅ Mapeo DICOM → Español (MODALITY_MAP, BODY_PART_MAP)
- ✅ Footer con número de página
- ✅ Logo institucional
- ✅ Bloque de datos del paciente en header
- ✅ Secciones estructuradas (HALLAZGOS, IMPRESIÓN, TÉCNICA)
- ✅ COMPARACIÓN y LIMITACIONES en prompts YAML

**Gaps identificados respecto a estándares profesionales**:
- ⚠️ No hay doble columna en bloque header (paciente izq. / estudio der.)
- ⚠️ No hay sección "TÉCNICA" explícita por defecto (depende de lo que genera la IA)
- ⚠️ No hay marca de confidencialidad
- ⚠️ El Impression no siempre termina con recomendación de acción
- ⚠️ Falta número de accesión (Accession Number) del estudio DICOM
- ⚠️ Falta sexo del paciente en header
- ⚠️ La firma no siempre incluye subespecialidad

### 6.2 Recomendaciones de Diseño para jsPDF

#### Layout del Header — Propuesta Concreta

```
Página 1:
┌───────────────────────────────────────────────────────────────────┐
│ [LOGO 25mm]  BRAND_NAME (14pt bold, #1a3a6b)                     │
│              BRAND_SUBTITLE (9pt, #666)                          │
│              [dirección · teléfono] (8pt, #888)                  │
├───────────────────── (línea 1pt, #1a3a6b) ────────────────────────┤
│ Col. Izquierda (50%):      │ Col. Derecha (50%):                  │
│ Paciente: [NOMBRE] bold    │ Fecha Examen: [DD.MM.YYYY]           │
│ RUT/ID:   [valor]          │ Fecha Informe: [DD.MM.YYYY]          │
│ Edad:     [N años]         │ Modalidad: [tipo]                    │
│ Sexo:     [M/F]            │ N° Acceso: [si disponible]           │
│ Médico ref.: [nombre]      │ ID Estudio: [shortId]                │
├───────────────────── (línea 0.5pt, #bbb) ─────────────────────────┤
│ INFORME DE [TIPO ESTUDIO EN MAYÚSCULAS] (12pt bold)               │
└───────────────────────────────────────────────────────────────────┘

Páginas 2+:
┌───────────────────────────────────────────────────────────────────┐
│ [BRAND_NAME] · [Paciente: NOMBRE] · [Fecha: DD.MM.YYYY]          │
├───────────────────── (línea 0.5pt) ───────────────────────────────┤
```

#### Secciones — Propuesta de Renderizado

```javascript
// Para cada sección:
// 1. Escribir título MAYÚSCULAS + negrita (10pt, #1a1a1a)
// 2. Dibujar línea separadora (0.3pt, #cccccc) debajo del título
// 3. Escribir contenido (9.5pt, normal, #1a1a1a)
// 4. Añadir 6mm de espacio vertical antes de la siguiente sección

// Para IMPRESIÓN DIAGNÓSTICA: tratamiento especial
// - Fondo suave: rectangle con fillColor(248, 248, 252) — azul muy suave
// - Título más grande: 11pt bold
// - Contenido con numeración explícita
// - Bottom padding extra: 4mm
```

#### Tipografía Recomendada para jsPDF

```javascript
// jsPDF solo tiene Helvetica, Times, Courier embebidas por defecto
// Usar Helvetica (equivalente a Arial/Swiss) para máximo profesionalismo

// Jerarquía implementable hoy:
const FONTS = {
  institutionName: { font: 'helvetica', style: 'bold', size: 14, color: [26, 58, 107] },
  institutionSub:  { font: 'helvetica', style: 'normal', size: 9, color: [102, 102, 102] },
  sectionTitle:    { font: 'helvetica', style: 'bold', size: 10.5, color: [26, 26, 26] },
  headerLabel:     { font: 'helvetica', style: 'bold', size: 8.5, color: [80, 80, 80] },
  headerValue:     { font: 'helvetica', style: 'normal', size: 8.5, color: [26, 26, 26] },
  bodyText:        { font: 'helvetica', style: 'normal', size: 9.5, color: [26, 26, 26] },
  footer:          { font: 'helvetica', style: 'normal', size: 7.5, color: [140, 140, 140] },
  signature:       { font: 'helvetica', style: 'normal', size: 9, color: [26, 26, 26] },
};
```

#### Colores Específicos para IMAGIAX

```javascript
// Paleta profesional médica
const COLORS = {
  institutionBlue: [26, 58, 107],    // #1a3a6b — azul médico oscuro
  textPrimary: [26, 26, 26],          // #1a1a1a — casi negro
  textSecondary: [80, 80, 80],        // #505050
  textMuted: [140, 140, 140],         // #8c8c8c
  separatorHeavy: [26, 58, 107],      // Línea principal bajo header
  separatorLight: [204, 204, 204],    // #cccccc — entre secciones
  impressionBg: [248, 249, 252],      // Fondo suave de Impresión
  footerLine: [180, 180, 180],        // #b4b4b4
};
```

#### Espaciado Exacto Recomendado (en mm para jsPDF)

```javascript
const SPACING = {
  marginLeft: 15,
  marginRight: 15,
  marginTopPage1: 10,   // espacio antes del header en página 1
  marginTopPage2: 8,    // espacio antes del mini-header en pág 2+
  marginBottom: 22,     // reservado para footer
  afterHeader: 6,       // después del bloque de datos del paciente
  beforeSectionTitle: 7, // antes de cada título de sección
  afterSectionTitle: 3,  // entre título y línea separadora
  afterSectionLine: 4,   // entre línea y primer párrafo
  betweenParagraphs: 2,  // entre líneas de texto dentro de sección
  beforeImpression: 10,  // espacio extra antes de la sección estrella
  afterImpression: 6,
  beforeSignature: 12,   // espacio generoso antes de firma
};
```

---

## 7. EJEMPLOS DE INFORMES PROFESIONALES REALES

### 7.1 Estructura de Informe Rx de Tórax — Formato ACR

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CENTRO MÉDICO SAN LUCAS — Diagnóstico por Imágenes
Av. Los Héroes 2450 · Santiago · Tel: (2) 2345-6789
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INFORME RADIOLÓGICO — RADIOGRAFÍA DE TÓRAX

Paciente:    PÉREZ SILVA, Juan Andrés    Fecha examen: 07.04.2026
RUT:         12.345.678-9               Fecha informe: 08.04.2026
Edad:        58 años                    Modalidad: RX Convencional
Sexo:        Masculino                  N° Acceso: RX-2026-00445
Médico ref.: Dr. Carlos Muñoz (Medicina Interna)

──────────────────────────────────────────────────────

INDICACIÓN CLÍNICA
──────────────────
Paciente de 58 años con tos de 3 semanas de evolución y disnea de esfuerzo.
Antecedente de tabaquismo suspendido hace 5 años (30 paquetes/año).
Se solicita estudio para descartar patología pulmonar.

TÉCNICA
───────
Radiografía de tórax en proyecciones posteroanterior (PA) y lateral, en
bipedestación, sin contraste. Técnica estándar.

COMPARACIÓN
───────────
No se dispone de estudios previos para comparación.

HALLAZGOS
─────────
PARÉNQUIMA PULMONAR:
Campos pulmonares de adecuado volumen. Se identifica imagen de mayor
densidad de bordes mal definidos en lóbulo superior derecho, de
aproximadamente 35x28mm. No se evidencian otras opacidades de consolidación
ni imágenes de nódulos en los segmentos evaluados.

SILUETA CARDIOVASCULAR:
Índice cardiotorácico dentro de límites normales (< 0.5). Botón aórtico
de características habituales.

MEDIASTINO:
Sin ensanchamiento. Tráquea en línea media.

COSTILLAS Y ESTRUCTURAS ÓSEAS:
Sin fracturas evidentes. Sin lesiones líticas ni blásticas significativas.
Cambios artrósicos costovertebrales de carácter degenerativo.

ESTRUCTURAS BLANDAS:
Sin alteraciones relevantes.

──────────────────────────────────────────────────────

IMPRESIÓN DIAGNÓSTICA
─────────────────────
1. Imagen de mayor densidad de bordes mal definidos en lóbulo superior
   derecho (35x28mm), a descartar proceso neoplásico versus infeccioso.

2. Sin evidencia de derrame pleural, neumotórax ni cardiomegalia.

3. Se recomienda completar evaluación con TC de tórax con contraste para
   caracterización de la lesión descrita. Correlacionar con clínica y
   laboratorio.

──────────────────────────────────────────────────────

___________________________________
Dra. María Fernández Vargas
Médica Radióloga — Subespecialidad Tórax
Matrícula CRM Nº 45.892
Centro Médico San Lucas
08.04.2026 — 14:32 hrs

                                               Página 1 de 1
──────────────────────────────────────────────────────────────
Centro Médico San Lucas · Av. Los Héroes 2450 · (2) 2345-6789
DOCUMENTO MÉDICO CONFIDENCIAL
```

### 7.2 Análisis del Ejemplo Anterior

**Lo que hace este informe "world-class"**:
1. **Header en dos columnas** → Información del paciente y del estudio separadas visualmente
2. **Fechas separadas** → fecha del examen vs. fecha del informe (pueden diferir)
3. **COMPARACIÓN explícita** → aunque no hay estudios previos, se dice explícitamente
4. **HALLAZGOS organizados** → por sistema anatómico, con subtítulos internos
5. **Medidas exactas** → "35x28mm", no "lesión grande"
6. **IMPRESIÓN numerada** → Ítem 1 = más importante, Ítem 3 = acción
7. **Recomendación específica** → TC de tórax con contraste, no solo "completar estudios"
8. **Firma completa** → nombre + especialidad + subespecialidad + matrícula + fecha + hora
9. **Footer en TODAS las páginas** → institución + paginación
10. **Marca de confidencialidad** → en el footer

---

## 8. FUENTES Y REFERENCIAS

### 8.1 Fuentes Oficiales Verificadas

1. **ACR (American College of Radiology)** — Practice Parameter for Communication of Radiology Findings (2020)
   - Estructura estándar de 7 secciones
   - Requisitos de firma y comunicación de hallazgos críticos

2. **RSNA (Radiological Society of North America)** — Structured Reporting Initiative
   - RadReport.org — Templates de informes por especialidad
   - Vocabulario controlado y campos obligatorios

3. **RadiologyInfo.org** (RSNA + ACR joint) — descripción verificada de secciones:
   - Type of exam, History/Reason, Comparison/Priors, Technique, Findings, Impression

4. **BI-RADS ACR** — Atlas de sistema de reporte estandarizado para mama
   - Ejemplo paradigmático de informe estructurado con categorías accionables

### 8.2 Prácticas de Diseño Basadas en Sistemas Reales

Observadas en sistemas RIS/PACS líderes:
- **PowerScribe (Nuance/Microsoft)** — sistema más usado en EEUU
- **Synapse (Fujifilm)** — sistema líder APAC
- **Sectra** — sistema premium Europa
- **Centricity (GE Healthcare)** — instituciones latinoamericanas tier-1

Todos comparten:
- Helvetica/Arial como fuente base
- Header con dos columnas de datos
- Footer con número de página en TODAS las páginas
- Impression visualmente destacada del resto

---

*Documento generado para uso interno de IMAGIAX — referencia de diseño para PDF profesional.*
*Actualizar cuando se adopten nuevas versiones de los estándares ACR/RSNA.*

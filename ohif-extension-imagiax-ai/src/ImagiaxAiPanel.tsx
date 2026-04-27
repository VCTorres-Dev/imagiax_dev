import React, { useState, useEffect, useCallback, useMemo, useRef, memo } from 'react';
import jsPDF from 'jspdf';
import QRCode from 'qrcode';
import { useActiveViewportDisplaySets, utils } from '@ohif/core';
import {
  generarInforme,
  guardarDecision,
  checkSalud,
  checkSaludV2,
  checkEstadoCreditos,
  compararV1V2,
  getMetodoPago,
  borrarMetodoPago,
  login,
  logout,
  getStoredUser,
  setOnAuthExpired,
  getCurrentUser,
  logoutApi,
  listadoInformes,
  type AIResultados,
  type InformeDB,
  type V2CompararResponse,
  type EstadoCreditosResponse,
  type MetodoPagoResponse,
  type AuthUser,
  ImagiaxApiError,
  ImagiaxTimeoutError,
} from './services/imagiaxApi';

// ── Configuración ──────────────────────────────────────────────────────────────
// Valores iniciales (se sobrescriben con /config/frontend al montar)
const _cfg = (typeof window !== 'undefined' && (window as any).__IMAGIAX_CONFIG__) || {};
let DOCTOR_NAME      = _cfg.doctor_name      || 'Dr. Alejandro Rodríguez';
let DOCTOR_SPECIALTY = _cfg.doctor_specialty  || 'Médico Radiólogo';
let DOCTOR_LICENSE   = _cfg.doctor_license    || 'CRM 12345';
let BRAND_NAME       = _cfg.brand_name        || 'Centro Médico Andes Salud';
let BRAND_SUBTITLE   = _cfg.brand_subtitle    || 'Diagnóstico por Imágenes · Talca';
const IMAGIAX_BACKEND  = _cfg.backend_url       || 'http://localhost:8010';
// Code-reading package note: the real demo PDF logo asset is intentionally omitted.
// The placeholder keeps the reviewable UI/PDF flow understandable without shipping binaries.
const LOGO_IMAGIAX_SVG =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="220" height="80"><rect width="100%" height="100%" rx="12" fill="#0f172a"/><text x="50%" y="48%" dominant-baseline="middle" text-anchor="middle" fill="#f8fafc" font-family="Arial" font-size="24" font-weight="700">IMAGIAX</text><text x="50%" y="72%" dominant-baseline="middle" text-anchor="middle" fill="#93c5fd" font-family="Arial" font-size="11">PDF demo logo placeholder</text></svg>');
const MAX_HISTORY_ENTRIES = 50;

// ── Institutional footer defaults ─────────────────────────────────────────────
// Used as fallbacks when backend config doesn't supply these fields.
const INSTITUTION_DEFAULTS = {
  name:    'Centro Médico Andes Salud',
  address: 'Mendoza, Argentina',
  phone:   '',
  url:     'www.imagiax.dev',
};
let INSTITUTION_PHONE = _cfg.institution_phone || INSTITUTION_DEFAULTS.phone;
let INSTITUTION_URL   = _cfg.institution_url   || INSTITUTION_DEFAULTS.url;

// ── PDF Theme — unified typography, color and spacing constants ───────────────
// Single source of truth for jsPDF rendering. All visual tokens live here.
const PDF_THEME = {
  title:   { size: 16, font: 'bold',   color: [26, 58, 94] as [number,number,number] },
  section: {
    size: 11, font: 'bold', color: [26, 58, 94] as [number,number,number],
    separatorColor: [204, 204, 204] as [number,number,number],
    separatorWidth: 0.3,
  },
  body:    { size: 9.5, font: 'normal', color: [51, 51, 51] as [number,number,number], lineHeight: 5.5 },
  subHeader: { spaceBefore: 3, fontStyle: 'bold' as const },
  patient: {
    labelSize: 9, labelFont: 'bold', valueSize: 9, valueFont: 'normal',
    labelColor: [80, 80, 80] as [number,number,number],
    valueColor: [26, 26, 26] as [number,number,number],
    gridBg: [248, 249, 250] as [number,number,number],
    rowBg:  [241, 243, 245] as [number,number,number],
  },
  footer:  { size: 7.5, font: 'normal', color: [102, 102, 102] as [number,number,number] },
  confidentiality: { size: 7, font: 'italic', color: [153, 153, 153] as [number,number,number] },
  conclusion: {
    bg: [245, 245, 245] as [number,number,number],
    border: [204, 204, 204] as [number,number,number],
    padding: 4,
    borderWidth: 1,  // WARNING 3 fix: sourced from theme, not hardcoded in renderConclusionBox
  },
  header: {
    institutionSize: 13, institutionFont: 'bold',
    subtitleSize: 8, subtitleColor: [120, 120, 120] as [number,number,number],
    reportLabelSize: 8, reportLabelColor: [140, 140, 140] as [number,number,number],
    examTitleSize: 12, examTitleFont: 'bold', examTitleColor: [0, 0, 0] as [number,number,number],
    ruleSeparatorColor: [0, 0, 0] as [number,number,number],
    ruleWidth: 0.8,
    footerSeparatorColor: [180, 180, 180] as [number,number,number],
  },
  signature: {
    lineColor: [30, 30, 30] as [number,number,number],
    lineWidth: 0.3,
    nameSize: 10,  // body.size + 0.5
    nameFont: 'bold',
    detailSize: 9,  // body.size - 0.5
  },
  clinicalNote: {
    titleSize: 11, titleColor: [26, 58, 94] as [number,number,number],
    bodySize: 9.5, bodyColor: [51, 51, 51] as [number,number,number],
  },
  spacing: { sectionGap: 8, margins: { l: 20, r: 20, t: 25, b: 30 } },
} as const;

// PERF-H5: Promise-based singleton prevents double-fetch on simultaneous mounts.
// Old _configLoaded boolean was not safe when two component instances mounted
// concurrently — both could pass the `if (_configLoaded)` guard before either
// resolved and set the flag.
let _configPromise: Promise<void> | null = null;
async function loadBackendConfig(): Promise<void> {
  return (_configPromise ??= (async () => {
    try {
      const resp = await fetch(`${IMAGIAX_BACKEND}/config/frontend`, { credentials: 'include' });
      if (resp.ok) {
        const cfg = await resp.json();
        if (cfg.doctor_name) DOCTOR_NAME = cfg.doctor_name;
        if (cfg.doctor_specialty) DOCTOR_SPECIALTY = cfg.doctor_specialty;
        if (cfg.doctor_license) DOCTOR_LICENSE = cfg.doctor_license;
        if (cfg.brand_name) BRAND_NAME = cfg.brand_name;
        if (cfg.brand_subtitle) BRAND_SUBTITLE = cfg.brand_subtitle;
        // Optional institutional contact fields — use backend value if present
        if (cfg.institution_phone) INSTITUTION_PHONE = cfg.institution_phone;
        if (cfg.institution_url)   INSTITUTION_URL   = cfg.institution_url;
      }
    } catch { /* backend no disponible aún, se usan defaults */ }
  })());
}

async function getLogoPng(_size = 180): Promise<string> {
  return LOGO_IMAGIAX_SVG;
}

// ── Tipos ──────────────────────────────────────────────────────────────────────
interface PatientInfo {
  rut: string;
  nombre: string;
  edad: string;
  fechaExamen: string;
  medicoReferencia: string;
  tipoExamen: string;
}

/** Estado de conexión con el backend IMAGIAX */
type ApiStatus = 'unknown' | 'checking' | 'online' | 'offline';
interface ReportSection {
  title: string;
  lines: string[];
}
interface HistoryEntry {
  informeId: string;
  fecha: string;
  patient: PatientInfo;
  sections: ReportSection[];
  pdfUrl: string;
  qrDataUrl: string;
  notaClinica?: string;
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function formatDate(d: Date): string {
  return d.toLocaleDateString('es-CL', { day: '2-digit', month: 'long', year: 'numeric' });
}
/** Formato DD.MM.YYYY para PDF/preview (estilo referencia clínica) — mantener para compatibilidad interna */
function formatDateDot(raw: string): string {
  // Acepta DD/MM/YYYY o YYYY-MM-DD o ya DD.MM.YYYY
  if (/^\d{2}\.\d{2}\.\d{4}$/.test(raw)) return raw;
  if (/^\d{2}\/\d{2}\.\d{4}$/.test(raw)) return raw.replace(/\//g, '.');
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(raw)) return raw.replace(/\//g, '.');
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    const [y, m, d] = raw.split('-');
    return `${d}.${m}.${y}`;
  }
  // Fallback: devolver hoy en DD.MM.YYYY
  const now = new Date();
  return `${String(now.getDate()).padStart(2,'0')}.${String(now.getMonth()+1).padStart(2,'0')}.${now.getFullYear()}`;
}

/** Nombres de meses en español para formato médico profesional */
const MESES_ES = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'] as const;

/**
 * Formatea una fecha al estilo médico profesional:
 * - Solo fecha:     "06 de Abril de 2026"
 * - Fecha+hora:     "08 de Abril de 2026 — 17:47 hs"
 * Acepta: DD.MM.YYYY | DD/MM/YYYY | YYYY-MM-DD | DD.MM.YYYY HH:mm | ISO strings | Date objects
 */
function formatMedicalDate(raw: string | Date): string {
  let day: number, month: number, year: number;
  let hours = -1, minutes = -1;

  if (raw instanceof Date) {
    if (isNaN(raw.getTime())) return '';
    day = raw.getDate(); month = raw.getMonth(); year = raw.getFullYear();
    hours = raw.getHours(); minutes = raw.getMinutes();
  } else {
    const s = (raw || '').trim();
    if (!s) return '';

    // DD.MM.YYYY HH:mm  or  DD.MM.YYYY HH:mm:ss
    const dotDateTime = s.match(/^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})/);
    if (dotDateTime) {
      day = parseInt(dotDateTime[1],10); month = parseInt(dotDateTime[2],10)-1;
      year = parseInt(dotDateTime[3],10); hours = parseInt(dotDateTime[4],10); minutes = parseInt(dotDateTime[5],10);
    }
    // DD/MM/YYYY HH:mm
    else if (/^\d{2}\/\d{2}\/\d{4}\s+\d{2}:\d{2}/.test(s)) {
      const [datePart, timePart] = s.split(/\s+/);
      const [d2,m2,y2] = datePart.split('/');
      const [h2,min2] = timePart.split(':');
      day = parseInt(d2,10); month = parseInt(m2,10)-1; year = parseInt(y2,10);
      hours = parseInt(h2,10); minutes = parseInt(min2,10);
    }
    // DD.MM.YYYY
    else if (/^\d{2}\.\d{2}\.\d{4}$/.test(s)) {
      const [d3,m3,y3] = s.split('.');
      day = parseInt(d3,10); month = parseInt(m3,10)-1; year = parseInt(y3,10);
    }
    // DD/MM/YYYY
    else if (/^\d{2}\/\d{2}\/\d{4}$/.test(s)) {
      const [d4,m4,y4] = s.split('/');
      day = parseInt(d4,10); month = parseInt(m4,10)-1; year = parseInt(y4,10);
    }
    // YYYY-MM-DD (ISO date or ISO datetime)
    else if (/^\d{4}-\d{2}-\d{2}/.test(s)) {
      const d = new Date(s);
      if (!isNaN(d.getTime())) {
        day = d.getDate(); month = d.getMonth(); year = d.getFullYear();
        // Only include time for ISO datetime strings (have T or space separator)
        if (/T\d{2}:\d{2}/.test(s) || /\s\d{2}:\d{2}/.test(s)) {
          hours = d.getHours(); minutes = d.getMinutes();
        }
      } else {
        return s; // unparseable, return as-is
      }
    }
    else {
      return s; // unrecognized format
    }
  }

  const monthName = MESES_ES[month] ?? '';
  const dayStr = String(day).padStart(2,'0');
  const base = `${dayStr} de ${monthName} de ${year}`;
  if (hours >= 0 && minutes >= 0) {
    return `${base} — ${String(hours).padStart(2,'0')}:${String(minutes).padStart(2,'0')} hs`;
  }
  return base;
}
/**
 * Parse DICOM Age String (AS) or birth year into human-readable age.
 * Handles: "065Y" → "65 años", "018M" → "18 meses", "003W" → "3 semanas",
 *          "010D" → "10 días", "1959" → "67 años", "035Y" → "35 años"
 */
function calcAge(v: string): string {
  if (!v || !v.trim()) return '';
  const trimmed = v.trim().toUpperCase();
  // DICOM Age String format: nnnD | nnnW | nnnM | nnnY
  const dicomAS = trimmed.match(/^(\d{1,4})(D|W|M|Y)$/);
  if (dicomAS) {
    const num = parseInt(dicomAS[1], 10);
    switch (dicomAS[2]) {
      case 'Y': return `${num} año${num !== 1 ? 's' : ''}`;
      case 'M': return `${num} mes${num !== 1 ? 'es' : ''}`;
      case 'W': return `${num} semana${num !== 1 ? 's' : ''}`;
      case 'D': return `${num} día${num !== 1 ? 's' : ''}`;
    }
  }
  // Birth year (e.g. "1959") — compute age from current year
  const n = parseInt(trimmed, 10);
  if (!isNaN(n) && n > 1900 && n < 2100) { const a = new Date().getFullYear() - n; return `${a} año${a !== 1 ? 's' : ''}`; }
  // Plain number (e.g. "65") — assume years
  if (!isNaN(n) && n >= 0 && n < 200) return `${n} año${n !== 1 ? 's' : ''}`;
  return v;
}
// ── T-3.3: Expanded known-header set ─────────────────────────────────────────
// Normalized (NFD stripped) versions so accented/unaccented variants all match.
const KNOWN_HEADERS_NORMALIZED = new Set([
  'HALLAZGOS', 'HALLAZGOS RADIOLOGICOS', 'HALLAZGOS RADIOLÓGICOS',
  'IMPRESION DIAGNOSTICA', 'IMPRESION DIAGNOSTICA:', 'IMPRESION',
  'TECNICA', 'TECNICA RADIOLOGICA', 'TECNICA DE ESTUDIO',
  'INDICACION CLINICA', 'INDICACION',
  'CONCLUSION', 'CONCLUSIONES', 'CONCLUSION DIAGNOSTICA',
  'DESCRIPCION', 'DESCRIPCION DEL ESTUDIO',
  'ANTECEDENTES', 'ANTECEDENTES CLINICOS',
  'OBSERVACIONES', 'OBSERVACIONES ADICIONALES',
  'RECOMENDACIONES', 'RECOMENDACION',
  'COMPARACION', 'COMPARACION CON ESTUDIOS PREVIOS',
  'PROYECCIONES',
  'METODOLOGIA', 'METODOLOGIA DE ANALISIS',
  'LIMITACIONES', 'LIMITACIONES TECNICAS',
  'NOTA METODOLOGICA',
  'DESCRIPCION DETALLADA',
]);

/**
 * Sections that should be PARSED but NOT rendered in PDF or HTML preview.
 * These are internal/methodological sections not relevant to the patient report.
 */
const SKIP_SECTIONS = new Set([
  'COMPARACION',
  'COMPARACION CON ESTUDIOS PREVIOS',
  'COMPARACIÓN',
  'COMPARACION CON ESTUDIOS PREVIOS',
  'LIMITACIONES',
  'LIMITACIONES TECNICAS',
  'LIMITACIONES TÉCNICAS',
  'NOTA METODOLOGICA',
  'NOTA METODOLÓGICA',
  'METODOLOGIA',
  'METODOLOGIA DE ANALISIS',
  'METODOLOGÍA',
  'METODOLOGÍA DE ANÁLISIS',
]);

/** Normalize a line to plain uppercase ASCII for header matching */
function normalizeForHeader(line: string): string {
  return line.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toUpperCase().replace(/:$/, '').trim();
}

/** Limpia artefactos markdown que Gemini puede incluir en su respuesta */
function stripMarkdown(text: string): string {
  return text
    .replace(/\*\*(.*?)\*\*/g, '$1')          // **bold** → bold
    .replace(/(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)/g, '$1') // *italic* → italic
    .replace(/^[-–—]{2,}\s*$/gm, '')           // -- separator lines
    .replace(/^\s*#{1,6}\s+/gm, '')            // ## markdown headers
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')   // [text](url) → text
    .replace(/\n{3,}/g, '\n\n')                // collapse multiple blank lines
    .trim();
}
/** Elimina líneas que contengan rastros de IA/metadatos administrativos que puedan filtrarse */
function stripAILanguage(text: string): string {
  // Términos que, si aparecen en una línea, indican que toda la línea debe eliminarse
  const AI_LINE_TERMS = [
    'inteligencia artificial',
    'análisis computacional',
    'analisis computacional',
    'densenet',
    'algoritmo',
    'machine learning',
    'deep learning',
    'red neuronal',
    'sistema automatizado',
    'sistema de ia',
    'umbral clínico',
    'umbral clinico',
    'nivel de alerta',
    'probabilidad del',
    'probabilidad de',
    'id del paciente',
    'modalidad:',
    'imágenes analizadas',
    'imagenes analizadas',
    'paciente id',
    'nota legal',
    'disclaimer',
  ];

  const normalize = (s: string) =>
    s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

  const lines = text.split('\n');
  const filtered = lines.filter(line => {
    const n = normalize(line);
    return !AI_LINE_TERMS.some(term => n.includes(normalize(term)));
  });

  return filtered
    .join('\n')
    .replace(/\b\d+([.,]\d+)?\s*%/g, '')   // eliminar cualquier porcentaje (entero o decimal)
    .replace(/\bIMAGIAX\s*AI\b/gi, 'IMAGIAX')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}
/** Convierte texto plano en secciones estructuradas (limpia markdown) */
function parseReportSections(raw: string): ReportSection[] {
  const clean = stripAILanguage(stripMarkdown(raw));
  const sections: ReportSection[] = [];
  let cur: ReportSection | null = null;
  clean.split('\n').forEach(l => {
    const line = l.trim();
    if (!line) return;
    // T-3.3: uses shared KNOWN_HEADERS_NORMALIZED set + normalizeForHeader() for consistent parsing
    const _normalLine = normalizeForHeader(line);
    const isHeader =
      (line.endsWith(':') && line.length < 60 && line === line.toUpperCase()) ||
      KNOWN_HEADERS_NORMALIZED.has(_normalLine) ||
      (/^[A-ZÁÉÍÓÚÑÜ\s\(\)]{6,}$/.test(line) && line.includes(' ') && line === line.toUpperCase());
    if (isHeader) {
      if (cur) sections.push(cur);
      cur = { title: line.replace(/:$/, ''), lines: [] };
    } else if (cur) {
      cur.lines.push(line.replace(/^[-•*]\s*/, ''));
    } else {
      cur = { title: 'HALLAZGOS', lines: [line.replace(/^[-•*]\s*/, '')] };
    }
  });
  if (cur) sections.push(cur);
  return sections;
}
/** Parse report sections from DB data — no AI language filtering (text is already clean/edited) */
function parseReportSectionsFromDB(raw: string): ReportSection[] {
  const clean = stripMarkdown(raw);  // only strip markdown, NOT stripAILanguage
  const sections: ReportSection[] = [];
  let cur: ReportSection | null = null;
  clean.split('\n').forEach(l => {
    const line = l.trim();
    if (!line) return;
    // T-3.3: uses shared KNOWN_HEADERS_NORMALIZED set + normalizeForHeader() for consistent parsing
    const _normalLine = normalizeForHeader(line);
    const isHeader =
      (line.endsWith(':') && line.length < 60 && line === line.toUpperCase()) ||
      KNOWN_HEADERS_NORMALIZED.has(_normalLine) ||
      (/^[A-ZÁÉÍÓÚÑÜ\s\(\)]{6,}$/.test(line) && line.includes(' ') && line === line.toUpperCase());
    if (isHeader) {
      if (cur) sections.push(cur);
      cur = { title: line.replace(/:$/, ''), lines: [] };
    } else if (cur) {
      cur.lines.push(line.replace(/^[-•*]\s*/, ''));
    } else {
      cur = { title: 'HALLAZGOS', lines: [line.replace(/^[-•*]\s*/, '')] };
    }
  });
  if (cur) sections.push(cur);
  return sections;
}
/** Serializa secciones de vuelta a texto plano */
function sectionsToText(secs: ReportSection[]): string {
  return secs.map(s => `${s.title}\n${s.lines.map(l => `- ${l}`).join('\n')}`).join('\n\n');
}

/**
 * Convierte un InformeDB del backend a HistoryEntry para mostrar en el panel.
 * Usa informe_final si está disponible (médico lo editó), si no informe_generado.
 */
function informeDbToHistoryEntry(inf: InformeDB): HistoryEntry {
  const rawText = inf.informe_final || inf.informe_generado || '';
  // If informe_final exists (doctor edited), use parseReportSectionsFromDB (no AI filtering)
  // If only informe_generado (raw AI output), also use parseReportSectionsFromDB since it's already stored in DB
  const sections = parseReportSectionsFromDB(rawText);

  // Formatear fecha de creación: ISO → DD/MM/YYYY
  // Usa timestamp_creacion o created_at (el endpoint /informes/listado devuelve created_at)
  let fecha = '';
  try {
    const rawDate = inf.timestamp_creacion || inf.created_at || '';
    const d = new Date(rawDate);
    if (!isNaN(d.getTime())) {
      fecha = `${String(d.getDate()).padStart(2,'0')}/${String(d.getMonth()+1).padStart(2,'0')}/${d.getFullYear()}`;
    } else {
      fecha = rawDate;
    }
  } catch { fecha = inf.timestamp_creacion || inf.created_at || ''; }

  // Fecha del examen (study_date puede ser YYYYMMDD o null)
  let fechaExamen = '';
  if (inf.study_date) {
    if (/^\d{8}$/.test(inf.study_date)) {
      fechaExamen = `${inf.study_date.substring(6,8)}/${inf.study_date.substring(4,6)}/${inf.study_date.substring(0,4)}`;
    } else {
      fechaExamen = inf.study_date;
    }
  } else {
    fechaExamen = fecha;
  }

  return {
    informeId: inf.id,
    fecha,
    patient: {
      rut:              inf.patient_id   || '—',
      nombre:           inf.patient_name || '—',
      edad:             inf.patient_age || '',   // from DB (BUG-010)
      fechaExamen,
      medicoReferencia: '',   // Not in DB
      tipoExamen:       inf.modalidad    || 'Estudio Radiológico',
    },
    sections,
    pdfUrl:    '',   // Never persisted — regenerate on demand
    qrDataUrl: '',   // Never persisted — regenerate on demand
    notaClinica: inf.nota_clinica || undefined,
  };
}

/**
 * Guard against placeholder CRM values — returns null if the license is empty,
 * "12345", "CRM 12345", or any variation of the default placeholder.
 * Returns the display string if it's a real value.
 */
function formatLicenseDisplay(license: string): string | null {
  if (!license || !license.trim()) return null;
  const stripped = license.trim().replace(/^CRM\s*/i, '').trim();
  if (stripped === '12345' || stripped === '' || stripped === '0') return null;
  return license.trim();
}

function normalizeTitle(value: string): string {
  return value
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toUpperCase();
}

function getPatientFindings(sections: ReportSection[]): string[] {
  const hallazgos = sections.find(s => normalizeTitle(s.title).includes('HALLAZGOS'));
  if (hallazgos?.lines?.length) return hallazgos.lines.map(l => stripAILanguage(l)).filter(Boolean);

  const excluded = ['TECNICA', 'IMPRESION', 'RECOMENDACION', 'OBSERVACIONES', 'NOTA LEGAL', 'DISCLAIMER', 'PROBABILIDAD', 'ANALISIS IA', 'INTELIGENCIA ARTIFICIAL'];
  const candidates = sections.filter(s => {
    const title = normalizeTitle(s.title);
    return !excluded.some(key => title.includes(key));
  });
  const merged = candidates.flatMap(s => s.lines).map(l => stripAILanguage(l)).filter(Boolean);
  return merged.length > 0 ? merged : ['Sin hallazgos reportados.'];
}

// ── Mapas de terminología DICOM → Español ─────────────────────────────────────
const MODALITY_MAP: Record<string, string> = {
  'DX': 'Radiografía',
  'CR': 'Radiografía',
  'CT': 'Tomografía Computada',
  'MR': 'Resonancia Magnética',
  'US': 'Ecografía',
  'MG': 'Mamografía',
  'NM': 'Medicina Nuclear',
  'PT': 'PET',
  'RF': 'Fluoroscopía',
  'XA': 'Angiografía',
};

const BODY_PART_MAP: Record<string, string> = {
  'CHEST': 'Tórax',
  'TORAX': 'Tórax',
  'PECHO': 'Tórax',
  'ABDOMEN': 'Abdomen',
  'SPINE': 'Columna',
  'LSPINE': 'Columna Lumbar',
  'CSPINE': 'Columna Cervical',
  'TSPINE': 'Columna Torácica',
  'PELVIS': 'Pelvis',
  'HIP': 'Cadera',
  'KNEE': 'Rodilla',
  'ANKLE': 'Tobillo',
  'FOOT': 'Pie',
  'HAND': 'Mano',
  'WRIST': 'Muñeca',
  'ELBOW': 'Codo',
  'SHOULDER': 'Hombro',
  'SKULL': 'Cráneo',
  'HEAD': 'Cabeza',
  'EXTREMITY': 'Extremidad',
};

// ── DICOM encoding repair — defense-in-depth for Latin-1 mojibake ─────────────
// DICOM tags stored as ISO-8859-1 and decoded as ASCII/UTF-8 produce '?' in
// place of accented characters (e.g. "CRÁNEO" → "CR?NEO").  The backend already
// runs repair_dicom_encoding(); this client-side table catches any corruption
// that slips through (e.g. tags read directly by OHIF without the backend).
const DICOM_ENCODING_FIXES: Record<string, string> = {
  'CR?NEO':   'CRÁNEO',   'CRANEO':    'CRÁNEO',
  'T?RAX':    'TÓRAX',    'TORAX':     'TÓRAX',
  'RI?ON':    'RIÑÓN',    'RINON':     'RIÑÓN',
  'F?MUR':    'FÉMUR',    'FEMUR':     'FÉMUR',
  'H?MERO':   'HÚMERO',   'HUMERO':    'HÚMERO',
  'MU?ECA':   'MUÑECA',   'MUNECA':    'MUÑECA',
  'ES?FAGO':  'ESÓFAGO',  'ESOFAGO':   'ESÓFAGO',
  'EST?MAGO': 'ESTÓMAGO', 'ESTOMAGO':  'ESTÓMAGO',
  'PULM?N':   'PULMÓN',   'PULMON':    'PULMÓN',
  'CORAZ?N':  'CORAZÓN',  'CORAZON':   'CORAZÓN',
  '?TERO':    'ÚTERO',    'UTERO':     'ÚTERO',
  'DI?FISIS': 'DIÁFISIS', 'DIAFISIS':  'DIÁFISIS',
  'EP?FISIS': 'EPÍFISIS', 'EPIFISIS':  'EPÍFISIS',
};

/**
 * Repair common Latin-1 → ASCII/UTF-8 mojibake in DICOM free-text fields.
 * Applied to StudyDescription and BodyPartExamined before PDF/preview rendering.
 * Two passes: full-token lookup, then inline regex substitution.
 */
function fixDicomEncoding(text: string): string {
  if (!text) return text;
  // Pass 1 — full token match (entire trimmed value is a known corruption)
  const upper = text.trim().toUpperCase();
  if (DICOM_ENCODING_FIXES[upper]) return DICOM_ENCODING_FIXES[upper];
  // Pass 2 — inline substitution for corruption embedded in a longer string
  let fixed = text;
  fixed = fixed.replace(/CR\?NEO/gi,    'CRÁNEO');
  fixed = fixed.replace(/T\?RAX/gi,     'TÓRAX');
  fixed = fixed.replace(/RI\?ON/gi,     'RIÑÓN');
  fixed = fixed.replace(/F\?MUR/gi,     'FÉMUR');
  fixed = fixed.replace(/H\?MERO/gi,    'HÚMERO');
  fixed = fixed.replace(/MU\?ECA/gi,    'MUÑECA');
  fixed = fixed.replace(/ES\?FAGO/gi,   'ESÓFAGO');
  fixed = fixed.replace(/EST\?MAGO/gi,  'ESTÓMAGO');
  fixed = fixed.replace(/PULM\?N/gi,    'PULMÓN');
  fixed = fixed.replace(/CORAZ\?N/gi,   'CORAZÓN');
  fixed = fixed.replace(/\?TERO/gi,     'ÚTERO');
  fixed = fixed.replace(/DI\?FISIS/gi,  'DIÁFISIS');
  fixed = fixed.replace(/EP\?FISIS/gi,  'EPÍFISIS');
  return fixed;
}

/**
 * Genera una descripción legible del estudio a partir de los datos DICOM disponibles.
 * Prioridad: StudyDescription → Modality + BodyPart → Modality → BodyPart → fallback
 * Applies fixDicomEncoding() to studyDesc and bodyPart before mapping.
 */
function getStudyDescription(
  studyDesc: string | null,
  bodyPart: string | null,
  modality: string | null
): string {
  const fixedDesc = studyDesc ? fixDicomEncoding(studyDesc) : studyDesc;
  const fixedPart = bodyPart ? fixDicomEncoding(bodyPart) : bodyPart;
  if (fixedDesc && fixedDesc !== 'null' && fixedDesc !== 'DX' && fixedDesc.length > 2) {
    return fixedDesc;
  }
  const mod = MODALITY_MAP[(modality || '').toUpperCase()] || modality || '';
  const part = BODY_PART_MAP[(fixedPart || '').toUpperCase()] || fixedPart || '';
  if (mod && part) return `${mod} de ${part}`;
  if (mod) return mod;
  if (part) return part;
  return 'Estudio de Imagen';
}

// ── PDF Layout Engine ─────────────────────────────────────────────────────────

interface PageLayoutConfig {
  doc: any;
  marginLeft: number;
  marginRight: number;
  marginTop: number;
  marginBottom: number;
  footerY: number;
  pageWidth: number;
  pageHeight: number;
  contentWidth: number;
  currentY: number;
  pageNumber: number;
  totalPagesPlaceholder: string;
  institutionName: string;
  institutionAddress: string;
  institutionPhone: string;
  institutionUrl: string;
}

// Patient data shape used by the PDF header renderer (defined here so it's
// accessible both in the createPageLayout return type and inside the function).
interface HeaderPatientData {
  patientName: string; rut: string; edad: string; tipoExamen: string;
  fechaExamen: string; fechaInforme: Date; shortId: string;
  medicoReferencia?: string;  // FR-02: for Médico Referente full-width row
  logoPng?: string; logoH?: number;
}

function createPageLayout(
  doc: any,
  config?: Partial<PageLayoutConfig>
): {
  ensureSpace: (neededPt: number) => void;
  addFooter: () => void;
  drawHeader: (isFirstPage: boolean, patientData: HeaderPatientData) => void;
  finalize: () => void;
  getY: () => number;
  setY: (y: number) => void;
  addY: (delta: number) => void;
  newPage: () => void;
  getContentWidth: () => number;
  getMarginLeft: () => number;
} {
  // jsPDF unit is 'pt' when unit:'pt', but here we initialise with 'mm' —
  // so all numbers below are in mm (jsPDF default for a4 portrait is mm)
  const layout: PageLayoutConfig = {
    doc,
    marginLeft:   PDF_THEME.spacing.margins.l,  // 20mm — sourced from PDF_THEME
    marginRight:  PDF_THEME.spacing.margins.r,  // 20mm — sourced from PDF_THEME
    marginTop:    PDF_THEME.spacing.margins.t,  // 25mm — sourced from PDF_THEME
    marginBottom: PDF_THEME.spacing.margins.b,  // 30mm — sourced from PDF_THEME
    footerY: 0,
    pageWidth: doc.internal.pageSize.getWidth(),
    pageHeight: doc.internal.pageSize.getHeight(),
    contentWidth: 0,
    currentY: PDF_THEME.spacing.margins.t,  // start at marginTop — sourced from PDF_THEME
    pageNumber: 1,
    totalPagesPlaceholder: '{total_pages}',
    institutionName: '',
    institutionAddress: '',
    institutionPhone: '',
    institutionUrl: '',
    ...config,
  };

  layout.footerY = layout.pageHeight - layout.marginBottom;
  layout.contentWidth = layout.pageWidth - layout.marginLeft - layout.marginRight;

  const getY = () => layout.currentY;
  const setY = (y: number) => { layout.currentY = y; };
  const addY = (delta: number) => { layout.currentY += delta; };
  const getContentWidth = () => layout.contentWidth;
  const getMarginLeft = () => layout.marginLeft;

  const addFooter = () => {
    const d = layout.doc;
    const fy = layout.footerY + 4;
    // Thin separator line — T-5.3: use PDF_THEME.header.footerSeparatorColor
    d.setDrawColor(...PDF_THEME.header.footerSeparatorColor);
    d.setLineWidth(PDF_THEME.section.separatorWidth);
    d.line(layout.marginLeft, fy, layout.pageWidth - layout.marginRight, fy);
    // Footer row 1: institution info (left) + page number (right) — T-4.3
    d.setFont('helvetica', PDF_THEME.footer.font);
    d.setFontSize(PDF_THEME.footer.size);
    d.setTextColor(...PDF_THEME.footer.color);
    const instName = layout.institutionName || 'Centro Médico';
    const instParts = [instName];
    if (layout.institutionAddress) instParts.push(layout.institutionAddress);
    if (layout.institutionPhone)   instParts.push(layout.institutionPhone);
    if (layout.institutionUrl)     instParts.push(layout.institutionUrl);
    d.text(instParts.join('  ·  '), layout.marginLeft, fy + 4.5);
    d.text(
      `Página ${layout.pageNumber} de ${layout.totalPagesPlaceholder}`,
      layout.pageWidth - layout.marginRight,
      fy + 4.5,
      { align: 'right' }
    );
    // Footer row 2: confidentiality notice — spec FR-07: 7pt italic gray #999999 — T-4.3
    // Added 3mm gap vs row 1 (fy+4.5 → fy+12) for better visual separation
    d.setFont('helvetica', PDF_THEME.confidentiality.font);
    d.setFontSize(PDF_THEME.confidentiality.size);
    d.setTextColor(...PDF_THEME.confidentiality.color);
    d.text(
      'Este informe es confidencial y de uso exclusivamente médico.',
      layout.pageWidth / 2,
      fy + 12,
      { align: 'center' }
    );
  };

  // Cache patient data so newPage() can draw the compact continuation header automatically
  let _cachedPatientData: HeaderPatientData | null = null;

  const newPage = () => {
    addFooter();
    layout.doc.addPage();
    layout.pageNumber += 1;
    layout.currentY = layout.marginTop;
    // Draw compact continuation header automatically
    if (_cachedPatientData) {
      drawHeader(false, _cachedPatientData);
    }
  };

  const ensureSpace = (neededPt: number) => {
    if (layout.currentY + neededPt > layout.footerY) {
      newPage();
    }
  };

  // ── T-2.1: drawPatientInfoGrid — extracted 2-col patient demographics block ─
  function drawPatientInfoGrid(d: any, patientData: HeaderPatientData, startY: number): number {
    const ml = layout.marginLeft;
    const mr = layout.pageWidth - layout.marginRight;
    const contentW = layout.contentWidth;
    const colMid = ml + contentW / 2 + 2;
    let py = startY;

    // Light background rect for the grid block — T-5.3
    const gridH = 22;
    d.setFillColor(...PDF_THEME.patient.gridBg);
    d.setDrawColor(...PDF_THEME.section.separatorColor);
    d.setLineWidth(0.3);
    d.roundedRect(ml, py - 4, contentW, gridH, 1.5, 1.5, 'FD');

    const fechaExFmt = formatMedicalDate(patientData.fechaExamen || '');
    const fi = patientData.fechaInforme;
    const fechaInfFmt = formatMedicalDate(fi);

    // Helper to render a label+value pair using PDF_THEME patient sizes
    const renderField = (label: string, value: string, x: number, y: number) => {
      d.setFont('helvetica', PDF_THEME.patient.labelFont);
      d.setFontSize(PDF_THEME.patient.labelSize);
      d.setTextColor(...PDF_THEME.patient.labelColor);
      d.text(label, x, y);
      const labelW = d.getTextWidth(label);
      d.setFont('helvetica', PDF_THEME.patient.valueFont);
      d.setFontSize(PDF_THEME.patient.valueSize);
      d.setTextColor(...PDF_THEME.patient.valueColor);
      d.text(value, x + labelW + 2, y);  // FIX 4: 2mm gap after label colon (was 1mm — too tight)
    };

    // Left column: Paciente, Edad, Rut Paciente
    renderField('Paciente:', patientData.patientName.toUpperCase(), ml + 2, py);
    renderField('Edad:', calcAge(patientData.edad), ml + 2, py + 5.5);
    renderField('Rut Paciente:', patientData.rut, ml + 2, py + 11);

    // Right column: Fecha Examen, Fecha Informe, Modalidad (FR-02)
    renderField('Fecha Examen:', fechaExFmt, colMid, py);
    renderField('Fecha Informe:', fechaInfFmt, colMid, py + 5.5);
    renderField('Modalidad:', patientData.tipoExamen || 'Estudio de Imagen', colMid, py + 11);

    py += 17;

    // Full-width row: Médico Referente — only render when a real value is present
    const medicoVal = (patientData.medicoReferencia || '').trim();
    const medicoIsEmpty = !medicoVal || medicoVal === '—' || medicoVal === '-' || medicoVal === 'N/A';
    if (!medicoIsEmpty) {
      d.setFillColor(...PDF_THEME.patient.rowBg);
      d.setDrawColor(...PDF_THEME.section.separatorColor);
      d.roundedRect(ml, py - 2, contentW, 7, 1, 1, 'FD');
      d.setFont('helvetica', PDF_THEME.patient.labelFont);
      d.setFontSize(PDF_THEME.patient.labelSize);
      d.setTextColor(...PDF_THEME.patient.labelColor);
      d.text('Médico Referente:', ml + 2, py + 3);
      const medicoLabelW = d.getTextWidth('Médico Referente:');
      d.setFont('helvetica', PDF_THEME.patient.valueFont);
      d.setFontSize(PDF_THEME.patient.valueSize);
      d.setTextColor(...PDF_THEME.patient.valueColor);
      d.text(medicoVal, ml + 2 + medicoLabelW + 2, py + 3);  // FIX 4: 2mm gap after colon
      py += 10;
    }

    // Separator after grid
    d.setDrawColor(...PDF_THEME.section.separatorColor);
    d.setLineWidth(PDF_THEME.section.separatorWidth);
    d.line(ml, py, mr, py);

    return py + 4;  // returns next Y after the grid
  }

  function drawHeader(isFirstPage: boolean, patientData: HeaderPatientData): void {
    // Cache on first call so newPage() can redraw the compact header on continuation pages
    if (isFirstPage) _cachedPatientData = patientData;
    const d = layout.doc;
    const ml = layout.marginLeft;
    const mr = layout.pageWidth - layout.marginRight;

    if (isFirstPage) {
      // ─── First-page header ───────────────────────────────────────────────
      const headerTop = 8;

      // Logo (if available)
      const logoW = 36;
      let actualLogoH = patientData.logoH ?? 18;
      if (patientData.logoPng) {
        try {
          d.addImage(patientData.logoPng, 'PNG', ml, headerTop, logoW, actualLogoH);
        } catch { actualLogoH = 0; }
      }

      // Institution name + subtitle — left side — T-5.2/T-5.3
      const brandX = ml + (patientData.logoPng ? logoW + 4 : 0);
      d.setFont('helvetica', PDF_THEME.header.institutionFont);
      d.setFontSize(PDF_THEME.header.institutionSize);
      d.setTextColor(...PDF_THEME.title.color);
      d.text(layout.institutionName || BRAND_NAME, brandX, headerTop + 7);
      d.setFont('helvetica', 'normal');
      d.setFontSize(PDF_THEME.header.subtitleSize);
      d.setTextColor(...PDF_THEME.header.subtitleColor);
      const subtitle = layout.institutionUrl || BRAND_SUBTITLE;
      d.text(subtitle, brandX, headerTop + 13);

      // Report type label — right side (small, muted) — T-5.3
      d.setFont('helvetica', 'normal');
      d.setFontSize(PDF_THEME.header.reportLabelSize);
      d.setTextColor(...PDF_THEME.header.reportLabelColor);
      d.text('INFORME RADIOLÓGICO', mr, headerTop + 7, { align: 'right' });

      // Horizontal rule (heavy — institutional blue) — T-5.3
      const lineY = headerTop + Math.max(actualLogoH, 18) + 2;
      d.setDrawColor(...PDF_THEME.title.color);
      d.setLineWidth(PDF_THEME.header.ruleWidth);
      d.line(ml, lineY, mr, lineY);

      // ── Patient data grid — T-2.1: extracted to drawPatientInfoGrid ──────
      const afterGridY = drawPatientInfoGrid(d, patientData, lineY + 6);

      // Study description — larger, centered, underlined — T-5.2/T-5.3
      let py = afterGridY + 4;
      d.setFont('helvetica', PDF_THEME.header.examTitleFont);
      d.setFontSize(PDF_THEME.header.examTitleSize);
      d.setTextColor(...PDF_THEME.header.examTitleColor);
      const examTitle = patientData.tipoExamen.toUpperCase();
      d.text(examTitle, layout.pageWidth / 2, py, { align: 'center' });
      const etW = d.getTextWidth(examTitle);
      d.setLineWidth(0.35);
      d.setDrawColor(...PDF_THEME.header.ruleSeparatorColor);
      d.line(layout.pageWidth / 2 - etW / 2, py + 1.5, layout.pageWidth / 2 + etW / 2, py + 1.5);

      // Final separator
      py += 5;
      d.setDrawColor(...PDF_THEME.section.separatorColor);
      d.setLineWidth(PDF_THEME.section.separatorWidth);
      d.line(ml, py, mr, py);

      layout.currentY = py + 9;  // 9mm gap before first section (was 6mm) for breathing room
    } else {
      // ─── T-4.2: Continuation-page compact header (enhanced) ──────────────
      // Spec: patient name left, institution below, "Página X de Y" right, max 15mm total
      const headerTop = 6;

      // Patient name — left, bold, dark blue — T-5.3
      d.setFont('helvetica', PDF_THEME.section.font);
      d.setFontSize(PDF_THEME.header.reportLabelSize + 1);  // 9pt — compact
      d.setTextColor(...PDF_THEME.title.color);
      d.text(patientData.patientName.toUpperCase(), ml, headerTop + 5);

      // Institution name — below patient name, small, muted — T-5.3
      d.setFont('helvetica', 'normal');
      d.setFontSize(PDF_THEME.confidentiality.size);  // 7pt
      d.setTextColor(...PDF_THEME.footer.color);
      d.text(layout.institutionName || BRAND_NAME, ml, headerTop + 10);

      // "Página X de Y" — right aligned, same baseline as patient name — T-5.3
      d.setFont('helvetica', 'normal');
      d.setFontSize(PDF_THEME.header.subtitleSize);  // 8pt
      d.setTextColor(...PDF_THEME.footer.color);
      d.text(
        `Página ${layout.pageNumber} de ${layout.totalPagesPlaceholder}`,
        mr,
        headerTop + 5,
        { align: 'right' }
      );

      // Thin rule below
      d.setDrawColor(...PDF_THEME.section.separatorColor);
      d.setLineWidth(PDF_THEME.section.separatorWidth);
      d.line(ml, headerTop + 12, mr, headerTop + 12);

      layout.currentY = headerTop + 26;  // 14mm gap after separator (separator at +12) for clear content separation
    }
  }

  const finalize = () => {
    addFooter();
    layout.doc.putTotalPages(layout.totalPagesPlaceholder);
  };

  return { ensureSpace, addFooter, drawHeader, finalize, getY, setY, addY, newPage, getContentWidth, getMarginLeft };
}

// ── Generic section renderer ──────────────────────────────────────────────────

function renderSection(
  layout: ReturnType<typeof createPageLayout>,
  doc: any,
  title: string,
  lines: string[],
  opts?: { titleColor?: [number, number, number]; showSeparator?: boolean }
): void {
  const lineHeight = PDF_THEME.body.lineHeight;
  const minAfterTitle = 18 + lineHeight * 2;  // spec: min 2 lines before page break

  layout.ensureSpace(minAfterTitle);

  // Section title — T-5.2: use PDF_THEME.section sizes
  doc.setFont('helvetica', PDF_THEME.section.font);
  doc.setFontSize(PDF_THEME.section.size);
  const color = opts?.titleColor || PDF_THEME.section.color;
  doc.setTextColor(...color);
  doc.text(title, layout.getMarginLeft(), layout.getY());
  layout.addY(5.5);

  // Separator line — T-5.2: use PDF_THEME separator spec
  if (opts?.showSeparator !== false) {
    doc.setDrawColor(...PDF_THEME.section.separatorColor);
    doc.setLineWidth(PDF_THEME.section.separatorWidth);
    doc.line(layout.getMarginLeft(), layout.getY(), layout.getMarginLeft() + layout.getContentWidth(), layout.getY());
    layout.addY(4.5);
  }

  // Body text — T-5.2: use PDF_THEME.body
  doc.setFont('helvetica', PDF_THEME.body.font);
  doc.setFontSize(PDF_THEME.body.size);
  doc.setTextColor(...PDF_THEME.body.color);

  // Sub-header pattern: 1-4 words (with accented chars) followed by ':' at line start
  // Matches: "Huesos del tarso:", "Partes blandas:", "Alineación:", "Metatarsianos:", etc.
  const SUB_HEADER_RE = /^([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜa-záéíóúñü\s]{0,50}:)\s*(.*)$/;

  for (const line of lines) {
    const wrapped: string[] = doc.splitTextToSize(line, layout.getContentWidth());
    for (let wi = 0; wi < wrapped.length; wi++) {
      const wl = wrapped[wi];
      // Only test sub-header on the first wrapped line of each original line
      const isFirstWrappedLine = wi === 0;
      const subMatch = isFirstWrappedLine ? SUB_HEADER_RE.exec(wl) : null;

      if (subMatch) {
        // Add extra space before the sub-header (but not before the very first line)
        layout.ensureSpace(PDF_THEME.subHeader.spaceBefore + lineHeight);
        layout.addY(PDF_THEME.subHeader.spaceBefore);

        const prefix = subMatch[1];   // e.g. "Huesos del tarso:"
        const rest   = subMatch[2];   // e.g. "El astrágalo, calcáneo..."

        // Render prefix in bold
        doc.setFontSize(PDF_THEME.body.size);
        doc.setFont('helvetica', PDF_THEME.subHeader.fontStyle);
        const prefixW = doc.getTextWidth(prefix);
        doc.text(prefix, layout.getMarginLeft(), layout.getY());

        // Render rest in normal weight (if any text follows the colon)
        if (rest) {
          doc.setFont('helvetica', PDF_THEME.body.font);
          doc.text(rest, layout.getMarginLeft() + prefixW + 1.5, layout.getY());
        }

        // Restore normal font for subsequent wrapped lines
        doc.setFont('helvetica', PDF_THEME.body.font);
        layout.addY(lineHeight);
      } else {
        layout.ensureSpace(lineHeight);
        doc.setFont('helvetica', PDF_THEME.body.font);
        doc.setFontSize(PDF_THEME.body.size);
        doc.setTextColor(...PDF_THEME.body.color);
        doc.text(wl, layout.getMarginLeft(), layout.getY());
        layout.addY(lineHeight);
      }
    }
  }

  layout.addY(PDF_THEME.spacing.sectionGap);  // T-5.1: 8mm section gap
}

// ── T-3.2: Conclusion helpers ─────────────────────────────────────────────────

/** Returns true if this section title is a CONCLUSIÓN / IMPRESIÓN DIAGNÓSTICA variant */
function isConclusionSection(title: string): boolean {
  const n = normalizeForHeader(title);
  return (
    n === 'CONCLUSION' ||
    n === 'CONCLUSIONES' ||
    n === 'CONCLUSION DIAGNOSTICA' ||
    n === 'IMPRESION DIAGNOSTICA' ||
    n === 'IMPRESION' ||
    n === 'IMPRESION CLINICA'
  );
}

/**
 * T-3.2: renderConclusionBox — renders the CONCLUSIÓN section inside a
 * highlighted box: light gray bg (#f5f5f5), 1pt border (#cccccc), 4mm padding.
 */
function renderConclusionBox(
  layout: ReturnType<typeof createPageLayout>,
  doc: any,
  section: ReportSection
): void {
  const lines = section.lines
    .map(l => stripMarkdown(stripAILanguage(l.trim())))
    .filter(Boolean);
  if (!lines.length) return;

  const ml = layout.getMarginLeft();
  const cw = layout.getContentWidth();
  const lh = PDF_THEME.body.lineHeight;
  const pad = PDF_THEME.conclusion.padding;
  // Estimate total box height
  let totalLines = 0;
  for (const line of lines) {
    const wrapped: string[] = doc.splitTextToSize(line, cw - pad * 2);
    totalLines += wrapped.length;
  }
  const boxH = 5.5 + 4 + totalLines * lh + pad * 2 + 4;  // title + gap + body + padding

  layout.ensureSpace(boxH + 6);

  const boxY = layout.getY();

  // Draw box background + border
  doc.setFillColor(...PDF_THEME.conclusion.bg);
  doc.setDrawColor(...PDF_THEME.conclusion.border);
  doc.setLineWidth(PDF_THEME.conclusion.borderWidth);  // FR: sourced from PDF_THEME.conclusion.borderWidth
  doc.roundedRect(ml, boxY, cw, boxH, 2, 2, 'FD');

  // Section title inside box
  layout.addY(pad + 2);
  doc.setFont('helvetica', PDF_THEME.section.font);
  doc.setFontSize(PDF_THEME.section.size);
  doc.setTextColor(...PDF_THEME.section.color);
  doc.text(section.title, ml + pad, layout.getY());
  layout.addY(5.5);

  // Thin separator under title
  doc.setDrawColor(...PDF_THEME.conclusion.border);
  doc.setLineWidth(PDF_THEME.section.separatorWidth);
  doc.line(ml + pad, layout.getY(), ml + cw - pad, layout.getY());
  layout.addY(3);

  // Body text inside box
  doc.setFont('helvetica', PDF_THEME.body.font);
  doc.setFontSize(PDF_THEME.body.size);
  doc.setTextColor(...PDF_THEME.body.color);
  for (const line of lines) {
    const wrapped: string[] = doc.splitTextToSize(line, cw - pad * 2);
    for (const wl of wrapped) {
      doc.text(wl, ml + pad, layout.getY());
      layout.addY(lh);
    }
  }

  layout.addY(pad + PDF_THEME.spacing.sectionGap);
}

// ── T-4.1: drawSignatureBlock — smart placement with 15mm gap ─────────────────
/**
 * Places the signature block smartly relative to content and page bottom.
 *
 * Strategy:
 *   - If very little space remains (<= sigBlockH + sigGap), move to a new page.
 *   - If the signature would leave more than SIG_FLOAT_THRESHOLD of blank space
 *     above the footer, anchor it to the bottom of the printable area instead
 *     (so it doesn't float awkwardly in the middle of an otherwise empty page).
 *   - Otherwise place it normally at layout.getY() + sigGap (close to content).
 *
 * Spec FR-05: 60mm line, bold doctor name, normal specialty, normal license.
 */
function drawSignatureBlock(
  doc: any,
  layout: ReturnType<typeof createPageLayout>,
  qrDataUrl: string
): void {
  const sigBlockH = 33; // actual signature content height: line + name + specialty + license
  const sigGap = 8;     // gap before the signature line (~8mm)
  // ensureSpace includes BOTH the gap AND the block so near-bottom placement never overflows (FR-05)
  layout.ensureSpace(sigBlockH + sigGap);
  // If ensureSpace triggered a new page, the continuation header already adds ~14mm of space;
  // use a smaller gap so the signature doesn't float too far from the header.
  const isNewPageStart = layout.getY() <= PDF_THEME.spacing.margins.t + 16;

  // Compute where the signature would land if placed normally
  const normalGap = isNewPageStart ? 5 : sigGap;
  const normalSigY = layout.getY() + normalGap;

  // Footer boundary (footerY is computed inside createPageLayout as pageHeight - marginBottom)
  // We access it via a getY() probe — use a conservative estimate instead
  const pageH   = doc.internal.pageSize.getHeight();
  const footerY = pageH - PDF_THEME.spacing.margins.b;

  // Space that would remain BELOW the signature block when placed normally
  const spaceBelow = footerY - (normalSigY + sigBlockH);

  // SIG_FLOAT_THRESHOLD: if more than 40mm would remain below the block, anchor to bottom.
  // This prevents the signature from floating mid-page when page 2 has a short last section.
  const SIG_FLOAT_THRESHOLD = 40;

  let finalSigY: number;
  if (spaceBelow > SIG_FLOAT_THRESHOLD) {
    // Anchor signature 15mm above the footer — professional anchored look
    finalSigY = footerY - sigBlockH - 15;
    // Safety: never go ABOVE the current content position (preserve min gap)
    finalSigY = Math.max(finalSigY, layout.getY() + normalGap);
  } else {
    finalSigY = normalSigY;
  }

  layout.setY(finalSigY);

  const ml = layout.getMarginLeft();
  const sigCenterX = ml + 30; // center of 60mm line

  // Signature line — 60mm horizontal — T-5.3
  doc.setDrawColor(...PDF_THEME.signature.lineColor);
  doc.setLineWidth(PDF_THEME.signature.lineWidth);
  doc.line(ml, layout.getY(), ml + 60, layout.getY());
  layout.addY(5.5);

  // Doctor name — bold — T-5.2/T-5.3
  doc.setFont('helvetica', PDF_THEME.signature.nameFont);
  doc.setFontSize(PDF_THEME.signature.nameSize);
  doc.setTextColor(...PDF_THEME.body.color);
  doc.text(DOCTOR_NAME, sigCenterX, layout.getY(), { align: 'center' });
  layout.addY(5);

  // Specialty — normal — T-5.2/T-5.3
  doc.setFont('helvetica', PDF_THEME.body.font);
  doc.setFontSize(PDF_THEME.signature.detailSize);
  doc.text(DOCTOR_SPECIALTY, sigCenterX, layout.getY(), { align: 'center' });
  layout.addY(5);

  // License — only if real (T-1.2: formatLicenseDisplay guard) — T-5.3
  const licenseDisplay = formatLicenseDisplay(DOCTOR_LICENSE);
  if (licenseDisplay) {
    doc.setFont('helvetica', PDF_THEME.body.font);
    doc.setFontSize(PDF_THEME.signature.detailSize);
    doc.text(licenseDisplay, sigCenterX, layout.getY(), { align: 'center' });
    layout.addY(5);
  }

  // QR code — right side of signature block — T-5.3
  if (qrDataUrl) {
    const qrSize = 24;
    const qrX = doc.internal.pageSize.getWidth() - layout.getMarginLeft() - qrSize;
    const qrY = layout.getY() - 28;
    try {
      doc.addImage(qrDataUrl, 'PNG', qrX, qrY, qrSize, qrSize);
      doc.setFont('helvetica', PDF_THEME.body.font);
      doc.setFontSize(PDF_THEME.confidentiality.size);  // 7pt
      doc.setTextColor(...PDF_THEME.footer.color);
      doc.text('Escanee para verificar', qrX + qrSize / 2, qrY + qrSize + 4, { align: 'center' });
    } catch { /* QR not available */ }
  }
}


async function generateReportPDF(
  patient: PatientInfo,
  sections: ReportSection[],
  informeId: string,
  qrDataUrl: string,
  notaClinica?: string
): Promise<string> {
  // Ensure backend config is loaded (doctor/brand data)
  await loadBackendConfig();

  const doc = new jsPDF({ format: 'a4', orientation: 'portrait', unit: 'mm' });
  const shortId = informeId.length > 12 ? informeId.substring(0, 12).toUpperCase() : informeId.toUpperCase();

  // ── Institutional config from loaded backend config ──────────────────────
  // BRAND_NAME / BRAND_SUBTITLE are module-level vars already loaded by loadBackendConfig().
  // Merge with INSTITUTION_DEFAULTS so footer always has meaningful content.
  const layout = createPageLayout(doc, {
    institutionName:    BRAND_NAME    || INSTITUTION_DEFAULTS.name,
    institutionAddress: BRAND_SUBTITLE || INSTITUTION_DEFAULTS.address,
    institutionPhone:   INSTITUTION_PHONE,   // empty string → gracefully omitted in footer
    institutionUrl:     INSTITUTION_URL,
  });

  // ── Load logo ────────────────────────────────────────────────────────────
  let logoPng: string | undefined;
  let logoH = 18;
  try {
    logoPng = await getLogoPng(220);
    const tmpImg = new Image();
    await new Promise<void>(res => {
      tmpImg.onload = () => res();
      tmpImg.onerror = () => res();
      tmpImg.src = logoPng!;
    });
    const logoW = 36;
    if (tmpImg.naturalWidth > 0 && tmpImg.naturalHeight > 0) {
      logoH = logoW * (tmpImg.naturalHeight / tmpImg.naturalWidth);
    }
  } catch { logoPng = undefined; }

  // ── Study description — already mapped at collection time (DICOM → Spanish) ────
  // tipoExamen is pre-mapped by getStudyDescription() in the useEffect; use directly.
  const tipoExamenHuman = patient.tipoExamen || 'Estudio de Imagen';

  // ── First-page header ────────────────────────────────────────────────────
  layout.drawHeader(true, {
    patientName: patient.nombre,
    rut: patient.rut,
    edad: patient.edad,
    tipoExamen: tipoExamenHuman,
    fechaExamen: patient.fechaExamen,
    fechaInforme: new Date(),
    shortId,
    medicoReferencia: patient.medicoReferencia,  // FR-02: Médico Referente row
    logoPng,
    logoH,
  });

  // ── Indicación Clínica ───────────────────────────────────────────────────
  if (notaClinica && notaClinica.trim()) {
    const safeNota = notaClinica.trim().replace(/<[^>]*>/g, '').substring(0, 500);
    layout.ensureSpace(24);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(PDF_THEME.clinicalNote.titleSize);
    doc.setTextColor(...PDF_THEME.clinicalNote.titleColor);
    doc.text('INDICACIÓN CLÍNICA', layout.getMarginLeft(), layout.getY());
    layout.addY(5.5);
    // Separator line to match other section headers
    doc.setDrawColor(...PDF_THEME.section.separatorColor);
    doc.setLineWidth(PDF_THEME.section.separatorWidth);
    doc.line(layout.getMarginLeft(), layout.getY(), layout.getMarginLeft() + layout.getContentWidth(), layout.getY());
    layout.addY(4.5);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(PDF_THEME.clinicalNote.bodySize);
    doc.setTextColor(...PDF_THEME.clinicalNote.bodyColor);
    const wrappedNote: string[] = doc.splitTextToSize(safeNota, layout.getContentWidth());
    for (const wl of wrappedNote) {
      layout.ensureSpace(5.5);
      doc.text(wl, layout.getMarginLeft(), layout.getY());
      layout.addY(5.5);
    }
    layout.addY(4);
  }

  // ── T-3.1: Dynamic section rendering — ordered loop over parsed sections ─────
  // Sections that are rendered inline before the dynamic loop (skip them in loop)
  const SKIP_IN_LOOP = new Set(['HALLAZGOS', 'INDICACION CLINICA', 'INDICACIÓN CLÍNICA']);

  const findings = getPatientFindings(sections);

  // HALLAZGOS — always first (pre-loop, uses getPatientFindings for best-effort extraction)
  const cleanFindings = findings
    .map(l => stripMarkdown(l.trim()))
    .filter(Boolean);
  if (cleanFindings.length) {
    renderSection(layout, doc, 'HALLAZGOS', cleanFindings);
  }

  // Dynamic loop — all other sections in original AI order
  // Routes CONCLUSIÓN → renderConclusionBox, others → renderSection
  for (const sec of sections) {
    const secNorm = normalizeForHeader(sec.title);
    // Skip HALLAZGOS (already rendered) and INDICACIÓN CLÍNICA (rendered above separately)
    if (SKIP_IN_LOOP.has(secNorm)) continue;
    // Skip internal/methodological sections that should not appear in the patient report
    if (SKIP_SECTIONS.has(secNorm)) continue;
    // Skip sections with no content
    const lines = sec.lines
      .map(l => stripMarkdown(stripAILanguage(l.trim())))
      .filter(l => l && !normalizeTitle(l).includes('SIN OBSERVACIONES'));
    if (!lines.length) continue;

    if (isConclusionSection(sec.title)) {
      // T-3.2: route through highlighted conclusion box — display title in UPPERCASE
      const displayTitle = sec.title.normalize('NFC').toUpperCase().replace(/:$/, '').trim();
      renderConclusionBox(layout, doc, { title: displayTitle, lines });
    } else {
      // Normalize title to UPPERCASE for consistent professional medical report style
      // Preserve accented characters (NFC normalization, not NFD accent-stripping)
      const displayTitle = sec.title.normalize('NFC').toUpperCase().replace(/:$/, '').trim();
      renderSection(layout, doc, displayTitle, lines);
    }
  }

  // ── T-4.1: Smart signature block — 15mm gap, CRM guard ───────────────────
  drawSignatureBlock(doc, layout, qrDataUrl);

  // ── Finalize: footer on last page + replace total pages placeholder ───────
  layout.finalize();

  return URL.createObjectURL(doc.output('blob'));
}

// ── MedicalReportDocument ─────────────────────────────────────────────────────
interface DocProps {
  patient: PatientInfo; sections: ReportSection[];
  informeId: string; qrDataUrl: string;
  notaClinica?: string;
}
const CLEAN_FILTER = ['NOTA LEGAL','DISCLAIMER','PROBABILIDAD','SOLO USO INTERNO','SÓLO USO INTERNO','NIVEL DE ALERTA','ANÁLISIS COMPUTACIONAL','ANÁLISIS IA','MODELO','INTELIGENCIA ARTIFICIAL'];

// ── T-6.1/T-6.2/T-6.3: CSS helpers derived from PDF_THEME ────────────────────
// These mirror PDF_THEME values for pixel-accurate HTML↔PDF visual sync.
// rgb() from PDF_THEME tuples
const pdfRgb = (c: readonly [number,number,number]) => `rgb(${c[0]},${c[1]},${c[2]})`;
// Convert PDF pt sizes to px (1pt ≈ 1.33px at 96dpi) — scaled for 794px canvas
const pdfPx = (pt: number) => `${(pt * 1.25).toFixed(0)}px`;

// Shared section body line style
const PREVIEW_BODY_STYLE: React.CSSProperties = {
  fontSize: pdfPx(PDF_THEME.body.size),
  lineHeight: '1.65',
  color: pdfRgb(PDF_THEME.body.color),
  marginBottom: '4px',
};

// Shared section title style
const PREVIEW_TITLE_STYLE: React.CSSProperties = {
  fontWeight: '700',
  fontSize: pdfPx(PDF_THEME.section.size),
  color: pdfRgb(PDF_THEME.section.color),
  marginBottom: '6px',
  borderBottom: `${PDF_THEME.section.separatorWidth}px solid ${pdfRgb(PDF_THEME.section.separatorColor)}`,
  paddingBottom: '3px',
};

// ── T-6.3: Patient info grid helper ───────────────────────────────────────────
const PREVIEW_LABEL_STYLE: React.CSSProperties = {
  fontWeight: '700',
  color: pdfRgb(PDF_THEME.patient.labelColor),
  fontSize: pdfPx(PDF_THEME.patient.labelSize),
};
const PREVIEW_VALUE_STYLE: React.CSSProperties = {
  fontWeight: 'normal',
  color: pdfRgb(PDF_THEME.patient.valueColor),
  fontSize: pdfPx(PDF_THEME.patient.valueSize),
};

// ── FIX 1: Sub-header pattern renderer for HTML preview ──────────────────────
// Mirrors PDF renderSection() sub-header logic: detects "Word(s):" prefix, renders bold.
const PREVIEW_SUBHEADER_RE = /^([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜa-záéíóúñü\s]{0,50}:)\s*(.*)$/;

function renderPreviewLines(lines: string[]): React.ReactNode[] {
  return lines.map((line, j) => {
    const m = PREVIEW_SUBHEADER_RE.exec(line);
    if (m) {
      return (
        <div key={j} style={{ ...PREVIEW_BODY_STYLE, marginTop: j === 0 ? 0 : `${PDF_THEME.subHeader.spaceBefore * 4}px` }}>
          <strong style={{ fontWeight: '700', color: pdfRgb(PDF_THEME.body.color) }}>{m[1]}</strong>
          {m[2] ? <span>{' '}{m[2]}</span> : null}
        </div>
      );
    }
    return <div key={j} style={PREVIEW_BODY_STYLE}>{line}</div>;
  });
}

// memo evita re-renders innecesarios cuando el parent actualiza estado no relacionado
const MedicalReportDocument = memo(({ patient, sections, informeId, qrDataUrl, notaClinica }: DocProps) => {
  // PERF-H4: memoize expensive derivations — sections is stable (parent memoizes before passing)
  const hallazgos = useMemo(() => getPatientFindings(sections), [sections]);
  const shortId = informeId.length > 12 ? informeId.substring(0, 12).toUpperCase() : informeId.toUpperCase();
  const fechaExFmtPreview = formatMedicalDate(patient.fechaExamen || '');
  const now = new Date();
  const fechaInfFmtPreview = formatMedicalDate(now);

  // ── T-6.1: dynamic section list — skip HALLAZGOS + INDICACION (rendered separately) ─
  const SKIP_PREVIEW = new Set(['HALLAZGOS', 'INDICACION CLINICA', 'INDICACIÓN CLÍNICA']);
  // PERF-H4: memoize filtered section list — avoids re-filtering on every keystroke in parent
  const dynamicSections = useMemo(() => sections.filter(sec => {
    const n = normalizeForHeader(sec.title);
    if (SKIP_PREVIEW.has(n)) return false;
    // Skip internal/methodological sections (SKIP_SECTIONS)
    if (SKIP_SECTIONS.has(n)) return false;
    if (CLEAN_FILTER.some(f => normalizeTitle(sec.title).includes(normalizeTitle(f)))) return false;
    const lines = sec.lines.map(l => stripMarkdown(stripAILanguage(l.trim()))).filter(Boolean);
    return lines.length > 0;
    // SKIP_PREVIEW, SKIP_SECTIONS and CLEAN_FILTER are module-level constants — they never
    // change, so including them in the deps array would only add noise without any benefit.
  }), [sections]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div
      style={{ width:'794px', minHeight:'1123px', backgroundColor:'#fff',
        fontFamily:'"Arial","Helvetica Neue",Helvetica,sans-serif',
        fontSize: pdfPx(PDF_THEME.body.size), color: pdfRgb(PDF_THEME.body.color),
        padding:'40px 60px 90px', boxSizing:'border-box', position:'relative', lineHeight:'1.5' }}>

      {/* Cabecera: Logo+Marca  |  Informe ID + fechas */}
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
        borderBottom: `3px solid ${pdfRgb(PDF_THEME.title.color)}`,
        paddingBottom:'16px', marginBottom:'28px' }}>
        <div style={{ display:'flex', alignItems:'center', gap:'14px' }}>
          <img src={LOGO_IMAGIAX_SVG} alt="Centro Médico Andes Salud"
            style={{ width:'72px', height:'auto', display:'block' }} />
          <div>
            <div style={{ fontSize: pdfPx(PDF_THEME.header.institutionSize), fontWeight:'800',
              color: pdfRgb(PDF_THEME.title.color), letterSpacing:'0.4px', lineHeight:'1.2' }}>
              {BRAND_NAME}
            </div>
            <div style={{ fontSize: pdfPx(PDF_THEME.header.subtitleSize),
              color: pdfRgb(PDF_THEME.header.subtitleColor), marginTop:'2px' }}>{BRAND_SUBTITLE}</div>
          </div>
        </div>
        <div style={{ textAlign:'right' }}>
          <div style={{ fontSize: pdfPx(PDF_THEME.header.reportLabelSize),
            color: pdfRgb(PDF_THEME.header.reportLabelColor), marginBottom:'2px' }}>Informe {shortId}</div>
          <div style={{ fontSize: pdfPx(PDF_THEME.footer.size), color: pdfRgb(PDF_THEME.footer.color) }}>
            Fecha Examen: {fechaExFmtPreview}</div>
          <div style={{ fontSize: pdfPx(PDF_THEME.footer.size), color: pdfRgb(PDF_THEME.footer.color), marginTop:'2px' }}>
            Fecha Informe: {fechaInfFmtPreview}</div>
        </div>
      </div>

      {/* T-6.3: Datos del paciente — 2-column CSS grid matching PDF drawPatientInfoGrid */}
      <div style={{
        marginBottom: `${PDF_THEME.spacing.sectionGap}mm`,
        background: pdfRgb(PDF_THEME.patient.gridBg),
        border: `0.3px solid ${pdfRgb(PDF_THEME.section.separatorColor)}`,
        borderRadius: '4px',
        padding: '10px 12px',
      }}>
        {/* Two-column row */}
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:'4px 16px', marginBottom:'8px' }}>
          {/* Left column */}
          <div>
            <div><span style={PREVIEW_LABEL_STYLE}>Paciente: </span>
              <span style={{ ...PREVIEW_VALUE_STYLE, textTransform:'uppercase' }}>{patient.nombre}</span></div>
            <div style={{ marginTop:'4px' }}><span style={PREVIEW_LABEL_STYLE}>Edad: </span>
              <span style={PREVIEW_VALUE_STYLE}>{calcAge(patient.edad)}</span></div>
            <div style={{ marginTop:'4px' }}><span style={PREVIEW_LABEL_STYLE}>Rut Paciente: </span>
              <span style={PREVIEW_VALUE_STYLE}>{patient.rut}</span></div>
          </div>
          {/* Right column — FR-02: Fecha Examen / Fecha Informe / Modalidad */}
          <div>
            <div><span style={PREVIEW_LABEL_STYLE}>Fecha Examen: </span>
              <span style={PREVIEW_VALUE_STYLE}>{fechaExFmtPreview}</span></div>
            <div style={{ marginTop:'4px' }}><span style={PREVIEW_LABEL_STYLE}>Fecha Informe: </span>
              <span style={PREVIEW_VALUE_STYLE}>{fechaInfFmtPreview}</span></div>
            <div style={{ marginTop:'4px' }}><span style={PREVIEW_LABEL_STYLE}>Modalidad: </span>
              <span style={PREVIEW_VALUE_STYLE}>{patient.tipoExamen || 'Estudio de Imagen'}</span></div>
          </div>
        </div>
        {/* Full-width row: Médico Referente — only shown when a real value is present */}
        {(() => {
          const mv = (patient.medicoReferencia || '').trim();
          const empty = !mv || mv === '—' || mv === '-' || mv === 'N/A';
          return !empty ? (
            <div style={{
              background: pdfRgb(PDF_THEME.patient.rowBg),
              borderRadius: '3px', padding: '4px 8px', marginTop:'4px',
            }}>
              <span style={PREVIEW_LABEL_STYLE}>Médico Referente: </span>
              <span style={{ ...PREVIEW_VALUE_STYLE, textTransform:'uppercase' }}>{mv}</span>
            </div>
          ) : null;
        })()}
      </div>

      {/* Indicación Clínica — solo si fue proporcionada */}
      {notaClinica && notaClinica.trim() && (
        <div style={{ marginBottom:'18px', padding:'10px 12px', background:'#f8fafc',
          border:'1px solid #e2e8f0', borderRadius:'5px',
          fontSize: pdfPx(PDF_THEME.clinicalNote.titleSize), lineHeight:'1.6' }}>
          <div style={{ fontWeight:'700', marginBottom:'4px',
            color: pdfRgb(PDF_THEME.clinicalNote.titleColor) }}>
            INDICACIÓN CLÍNICA
          </div>
          <div style={{ color: pdfRgb(PDF_THEME.clinicalNote.bodyColor) }}>
            {notaClinica.trim().replace(/<[^>]*>/g, '').substring(0, 500)}
          </div>
        </div>
      )}

      {/* Título del examen — centrado, subrayado real */}
      <div style={{ textAlign:'center', marginBottom:'28px' }}>
        <div style={{ fontSize: pdfPx(PDF_THEME.header.examTitleSize),
          fontWeight:'700', textTransform:'uppercase', display:'inline-block',
          borderBottom:`2px solid ${pdfRgb(PDF_THEME.header.examTitleColor)}`,
          paddingBottom:'3px', letterSpacing:'0.6px' }}>
          {patient.tipoExamen}
        </div>
      </div>

      {/* Hallazgos — always first */}
      <div style={{ marginBottom: `${PDF_THEME.spacing.sectionGap}mm` }}>
        <div style={PREVIEW_TITLE_STYLE}>Hallazgos:</div>
        {renderPreviewLines(hallazgos.map(l => stripMarkdown(l)).filter(Boolean))}
      </div>

      {/* T-6.1: Dynamic sections — render ALL remaining sections in original order */}
      {dynamicSections.map((sec, i) => {
        const lines = sec.lines
          .map(l => stripMarkdown(stripAILanguage(l.trim())))
          .filter(l => l && !normalizeTitle(l).includes('SIN OBSERVACIONES'));
        if (!lines.length) return null;

        // T-6.2: Conclusion box — styled to match PDF renderConclusionBox
        if (isConclusionSection(sec.title)) {
          return (
            <div key={i} style={{
              marginBottom: `${PDF_THEME.spacing.sectionGap}mm`,
              background: pdfRgb(PDF_THEME.conclusion.bg),
              border: `1px solid ${pdfRgb(PDF_THEME.conclusion.border)}`,
              borderRadius: '4px',
              padding: `${PDF_THEME.conclusion.padding * 1.5}px ${PDF_THEME.conclusion.padding * 3}px`,
            }}>
              <div style={{ ...PREVIEW_TITLE_STYLE, color: pdfRgb(PDF_THEME.section.color) }}>
                {sec.title}
              </div>
              {renderPreviewLines(lines)}
            </div>
          );
        }

        // Default section renderer
        return (
          <div key={i} style={{ marginBottom: `${PDF_THEME.spacing.sectionGap}mm` }}>
            <div style={PREVIEW_TITLE_STYLE}>{sec.title}</div>
            {renderPreviewLines(lines)}
          </div>
        );
      })}

      {/* Cierre */}
      <div style={{ marginTop:'16px', fontSize: pdfPx(PDF_THEME.body.size),
        color: pdfRgb(PDF_THEME.body.color) }}>Saluda Atentamente</div>

      {/* Firma + QR */}
      <div style={{ marginTop:'24px', display:'flex', justifyContent:'space-between', alignItems:'flex-end' }}>
        <div style={{ textAlign:'center' }}>
          <div style={{ borderTop:`1.5px solid ${pdfRgb(PDF_THEME.signature.lineColor)}`,
            width:'220px', paddingTop:'6px',
            fontSize: pdfPx(PDF_THEME.signature.detailSize) }}>
            <div style={{ fontWeight:'700', fontSize: pdfPx(PDF_THEME.signature.nameSize) }}>{DOCTOR_NAME}</div>
            <div style={{ color: pdfRgb(PDF_THEME.footer.color), marginTop:'2px' }}>{DOCTOR_SPECIALTY}</div>
            {formatLicenseDisplay(DOCTOR_LICENSE) && (
              <div style={{ color: pdfRgb(PDF_THEME.footer.color) }}>
                Lic. N° {formatLicenseDisplay(DOCTOR_LICENSE)}
              </div>
            )}
          </div>
        </div>
        {qrDataUrl && (
          <div style={{ textAlign:'center' }}>
            <img src={qrDataUrl} alt="QR" style={{ width:'72px', height:'72px', display:'block' }} />
            <div style={{ fontSize: pdfPx(PDF_THEME.confidentiality.size),
              color: pdfRgb(PDF_THEME.confidentiality.color), marginTop:'4px' }}>
              Escanee para verificar
            </div>
          </div>
        )}
      </div>

    </div>
  );
});

// ── ScaledPreview — definida FUERA del componente para evitar re-montaje ──────
// Bug anterior: estaba dentro del render → nueva referencia en cada re-render → React desmontaba/remontaba el componente → parpadeo
const PDF_WIDTH = 794; // A4 width in px
const ScaledPreview = memo(({ sec, pid, qr, pat, nota, containerWidth }: { sec: ReportSection[]; pid: string; qr: string; pat: PatientInfo; nota?: string; containerWidth?: number }) => {
  const availableWidth = (containerWidth || 400) - 32; // 16px padding each side
  const scale = Math.min(0.56, availableWidth / PDF_WIDTH);
  const scaledHeight = Math.round(1123 * scale); // A4 height
  const inverseScale = scale > 0 ? 1 / scale : 1;
  const inversePercent = Math.round(inverseScale * 100) + '%';
  return (
    <div style={{ overflow:'hidden', border:'1px solid #1e3a5f', borderRadius:'6px', backgroundColor:'#fff', height: scaledHeight + 'px', position:'relative' }}>
      <div style={{ transform:'scale(' + scale + ')', transformOrigin:'top left', width: inversePercent, pointerEvents:'none' }}>
        <MedicalReportDocument patient={pat} sections={sec} informeId={pid} qrDataUrl={qr} notaClinica={nota} />
      </div>
    </div>
  );
});

// ── SectionEditor — editor estructurado por secciones ─────────────────────────
// UI profesional: cada sección del informe tiene su propio campo de edición
// con label visible, textarea redimensionable, y controles claros.
interface SectionEditorProps {
  sections: ReportSection[];
  onChange: (updated: ReportSection[]) => void;
  onSave: () => void;
  onCancel: () => void;
}
const SectionEditor: React.FC<SectionEditorProps> = ({ sections, onChange, onSave, onCancel }) => {
  // Cada sección se edita como bloque libre de texto; las líneas se separan por \n
  const updateText = (si: number, text: string) => {
    const lines = text.split('\n').map(l => l.trimEnd()).filter(l => l.length > 0);
    const next = sections.map((s, i) => i !== si ? s : { ...s, lines });
    onChange(next);
  };

  const patientSecs  = sections.filter(s => !CLEAN_FILTER.some(f => s.title.includes(f)));
  const internalSecs = sections.filter(s =>  CLEAN_FILTER.some(f => s.title.includes(f)));

  const renderBlock = (s: ReportSection, si: number, readOnly = false) => (
    <div key={si} style={{ display:'flex', flexDirection:'column', gap:'6px' }}>
      <div style={{ display:'flex', alignItems:'center', gap:'6px' }}>
        <div style={{ width:'4px', height:'4px', borderRadius:'50%',
          background: readOnly ? '#f59e0b' : '#3b82f6', flexShrink:0 }} />
        <span style={{ color: readOnly ? '#f59e0b' : '#93c5fd', fontSize:'10px',
          fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.6px' }}>
          {s.title}
        </span>
        {readOnly && (
          <span style={{ marginLeft:'auto', color:'#78350f', fontSize:'9px',
            background:'#451a03', padding:'1px 7px', borderRadius:'3px' }}>Solo médico · no editable</span>
        )}
      </div>
      <textarea
        readOnly={readOnly}
        value={s.lines.join('\n')}
        onChange={readOnly ? undefined : e => updateText(sections.indexOf(s), e.target.value)}
        rows={Math.max(3, s.lines.length + 1)}
        style={{
          width:'100%', boxSizing:'border-box',
          background: readOnly ? '#0a0f1a' : '#111827',
          color: readOnly ? '#475569' : '#e2e8f0',
          border:`1px solid ${readOnly ? '#1a150a' : '#1e3a5f'}`,
          borderRadius:'6px', padding:'10px 12px', fontSize:'12px', fontFamily:'inherit',
          lineHeight:'1.7', outline:'none', resize:'vertical',
          transition:'border-color 0.15s', cursor: readOnly ? 'default' : 'text',
        }}
        onFocus={readOnly ? undefined : e => { e.target.style.borderColor = '#3b82f6'; }}
        onBlur={readOnly ? undefined : e => { e.target.style.borderColor = '#1e3a5f'; }}
        placeholder={readOnly ? '' : 'Escribe una observación por línea…'}
      />
    </div>
  );

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:'14px' }}>
      {/* Barra de acciones */}
      <div style={{ background:'#0d1420', borderRadius:'8px', border:'1px solid #1e3a5f',
        padding:'12px 16px', display:'flex', justifyContent:'space-between', alignItems:'center' }}>
        <div>
          <div style={{ color:'#f1f5f9', fontWeight:'700', fontSize:'13px' }}>Editor de informe</div>
          <div style={{ color:'#475569', fontSize:'10px', marginTop:'1px' }}>Cada sección es un bloque de texto libre — una observación por línea</div>
        </div>
        <div style={{ display:'flex', gap:'8px' }}>
          <button onClick={onCancel}
            style={{ background:'transparent', color:'#64748b', border:'1px solid #334155',
              borderRadius:'6px', padding:'6px 14px', fontSize:'12px', cursor:'pointer' }}>
            Cancelar
          </button>
          <button onClick={onSave}
            style={{ background:'linear-gradient(135deg,#1e40af,#3b82f6)', color:'white',
              border:'none', borderRadius:'6px', padding:'6px 16px', fontSize:'12px',
              cursor:'pointer', fontWeight:'700' }}>
            ✓ Guardar
          </button>
        </div>
      </div>

      {/* Secciones del informe al paciente */}
      {patientSecs.length > 0 && (
        <div style={{ display:'flex', flexDirection:'column', gap:'14px',
          background:'#0a0f1a', border:'1px solid #1e3a5f', borderRadius:'8px', padding:'14px' }}>
          <div style={{ color:'#64748b', fontSize:'9.5px', fontWeight:'700',
            textTransform:'uppercase', letterSpacing:'0.6px', marginBottom:'-4px' }}>
            Informe al paciente
          </div>
          {patientSecs.map((s) => renderBlock(s, sections.indexOf(s), false))}
        </div>
      )}

      {/* Secciones internas — solo lectura en editor */}
      {internalSecs.length > 0 && (
        <details style={{ background:'#0a0f1a', border:'1px solid #1a150a', borderRadius:'8px', padding:'14px' }}>
          <summary style={{ color:'#f59e0b', fontSize:'10px', fontWeight:'700',
            textTransform:'uppercase', letterSpacing:'0.6px', cursor:'pointer', userSelect:'none' }}>
            Datos técnicos IA (solo lectura)
          </summary>
          <div style={{ display:'flex', flexDirection:'column', gap:'14px', marginTop:'12px' }}>
            {internalSecs.map((s) => renderBlock(s, sections.indexOf(s), true))}
          </div>
        </details>
      )}
    </div>
  );
};

// ── HistoryCard ────────────────────────────────────────────────────────────────
const HistoryCard = memo(({ entry, onView, onDownloadPdf }: { entry: HistoryEntry; onView: () => void; onDownloadPdf: () => void }) => (
  <div style={{ background:'#0d1420', border:'1px solid #1e3a5f', borderRadius:'8px',
    padding:'12px 14px', display:'flex', justifyContent:'space-between', alignItems:'center', gap:'10px' }}>
    <div style={{ flex:1, minWidth:0 }}>
      <div style={{ color:'#f1f5f9', fontSize:'12px', fontWeight:'600', marginBottom:'2px',
        overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{entry.patient.nombre}</div>
      <div style={{ color:'#64748b', fontSize:'10px', marginBottom:'2px' }}>{entry.patient.tipoExamen}</div>
      <div style={{ color:'#475569', fontSize:'10px' }}>
        {entry.fecha} · <span style={{ color:'#3b82f6' }}>{entry.informeId}</span>
      </div>
    </div>
    <div style={{ display:'flex', gap:'6px', flexShrink:0 }}>
      {entry.sections?.length > 0 && (
        <button onClick={onDownloadPdf}
          style={{ background:'#1e3a5f', color:'#93c5fd', border:'none', borderRadius:'5px',
            padding:'6px 10px', fontSize:'11px', cursor:'pointer',
            display:'flex', alignItems:'center', gap:'4px' }}>
          PDF
        </button>
      )}
      <button onClick={onView}
        style={{ background:'#1d4ed8', color:'white', border:'none', borderRadius:'5px',
          padding:'6px 10px', fontSize:'11px', cursor:'pointer', fontWeight:'600' }}>
        Ver
      </button>
    </div>
  </div>
));

// ── ReportViewerModal — floating PDF preview ──────────────────────────────────
interface ReportViewerModalProps {
  entry: HistoryEntry;
  onClose: () => void;
  onDownloadPdf: () => void;
}

const ZOOM_MIN = 0.35;
const ZOOM_MAX = 1.5;
const ZOOM_DEFAULT = 0.72;
const ZOOM_STEP = 0.12;
const ZOOM_WHEEL_STEP = 0.06;

const ReportViewerModal: React.FC<ReportViewerModalProps> = ({ entry, onClose, onDownloadPdf }) => {
  const [zoom, setZoom] = useState(ZOOM_DEFAULT);

  const zoomIn = useCallback(() => setZoom(z => Math.min(ZOOM_MAX, +(z + ZOOM_STEP).toFixed(2))), []);
  const zoomOut = useCallback(() => setZoom(z => Math.max(ZOOM_MIN, +(z - ZOOM_STEP).toFixed(2))), []);
  const zoomReset = useCallback(() => setZoom(ZOOM_DEFAULT), []);

  // Close on Escape key only — zoom via mouse wheel only (no keyboard shortcuts that bleed into OHIF viewer)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onClose]);

  // Prevent body scroll while modal is open
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, []);

  // Mouse wheel zoom — no Ctrl needed; always preventDefault + stopPropagation so OHIF viewer is never affected
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const delta = e.deltaY > 0 ? -ZOOM_WHEEL_STEP : ZOOM_WHEEL_STEP;
    setZoom(z => Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, +(z + delta).toFixed(2))));
  }, []);

  const zoomPct = Math.round(zoom * 100);

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 9999,
        background: 'rgba(0,0,0,0.65)',
        backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: '20px',
        animation: 'modalFadeIn 0.22s ease-out',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: '#0d1420',
          border: '1px solid #1e3a5f',
          borderRadius: '12px',
          width: '100%', maxWidth: '840px',
          maxHeight: '92vh',
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
          boxShadow: '0 25px 60px -12px rgba(0,0,0,0.7), 0 0 0 1px rgba(59,130,246,0.08)',
          animation: 'modalSlideIn 0.28s cubic-bezier(0.16,1,0.3,1)',
        }}
      >
        {/* Header */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '14px 20px',
          borderBottom: '1px solid #1e3a5f',
          background: 'linear-gradient(135deg,#0d1b2a,#1a2744)',
          flexShrink: 0,
        }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ color: '#f1f5f9', fontSize: '14px', fontWeight: '700',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {entry.patient.nombre}
            </div>
            <div style={{ color: '#64748b', fontSize: '11px', marginTop: '3px',
              display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span>{entry.fecha}</span>
            </div>
          </div>

          {/* Zoom controls */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: '2px',
            background: 'rgba(255,255,255,0.04)', border: '1px solid #1e3a5f',
            borderRadius: '8px', padding: '2px', marginLeft: '10px', flexShrink: 0,
          }}>
            <button onClick={zoomOut} disabled={zoom <= ZOOM_MIN} title="Alejar (−)"
              style={{ background: 'transparent', border: 'none', borderRadius: '6px',
                width: '28px', height: '28px', display: 'flex', alignItems: 'center', justifyContent: 'center',
                cursor: zoom <= ZOOM_MIN ? 'default' : 'pointer',
                color: zoom <= ZOOM_MIN ? '#1e293b' : '#94a3b8', fontSize: '15px', fontWeight: '700',
                transition: 'all 0.12s',
              }}
              onMouseEnter={e => { if (zoom > ZOOM_MIN) e.currentTarget.style.background = 'rgba(255,255,255,0.08)'; }}
              onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; }}
            >−</button>
            <button onClick={zoomReset} title="Restablecer zoom (0)"
              style={{ background: 'transparent', border: 'none', borderRadius: '4px',
                padding: '2px 6px', cursor: 'pointer',
                color: zoom === ZOOM_DEFAULT ? '#475569' : '#93c5fd',
                fontSize: '10px', fontWeight: '700', fontVariantNumeric: 'tabular-nums',
                minWidth: '38px', textAlign: 'center', transition: 'color 0.12s',
              }}
              onMouseEnter={e => { e.currentTarget.style.background = 'rgba(255,255,255,0.06)'; }}
              onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; }}
            >{zoomPct}%</button>
            <button onClick={zoomIn} disabled={zoom >= ZOOM_MAX} title="Acercar (+)"
              style={{ background: 'transparent', border: 'none', borderRadius: '6px',
                width: '28px', height: '28px', display: 'flex', alignItems: 'center', justifyContent: 'center',
                cursor: zoom >= ZOOM_MAX ? 'default' : 'pointer',
                color: zoom >= ZOOM_MAX ? '#1e293b' : '#94a3b8', fontSize: '15px', fontWeight: '700',
                transition: 'all 0.12s',
              }}
              onMouseEnter={e => { if (zoom < ZOOM_MAX) e.currentTarget.style.background = 'rgba(255,255,255,0.08)'; }}
              onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; }}
            >+</button>
          </div>

          <button
            onClick={onClose}
            style={{
              background: 'rgba(255,255,255,0.04)', border: '1px solid #1e3a5f',
              borderRadius: '8px', width: '34px', height: '34px',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              cursor: 'pointer', color: '#64748b', fontSize: '15px',
              transition: 'all 0.15s', flexShrink: 0, marginLeft: '6px',
            }}
            onMouseEnter={e => {
              e.currentTarget.style.background = 'rgba(239,68,68,0.12)';
              e.currentTarget.style.color = '#f87171';
              e.currentTarget.style.borderColor = '#7f1d1d';
            }}
            onMouseLeave={e => {
              e.currentTarget.style.background = 'rgba(255,255,255,0.04)';
              e.currentTarget.style.color = '#64748b';
              e.currentTarget.style.borderColor = '#1e3a5f';
            }}
          >
            ✕
          </button>
        </div>

        {/* Body — scrollable document preview with zoom */}
        <div
          onWheel={handleWheel}
          style={{
            flex: 1, overflow: 'auto', padding: '20px',
            display: 'flex', justifyContent: 'center',
            background: '#080c14',
          }}
        >
          <div style={{
            transform: `scale(${zoom})`, transformOrigin: 'top center',
            width: '794px', flexShrink: 0,
            transition: 'transform 0.15s ease-out',
          }}>
            <MedicalReportDocument
              patient={entry.patient}
              sections={entry.sections}
              informeId={entry.informeId}
              qrDataUrl={entry.qrDataUrl}
              notaClinica={entry.notaClinica}
            />
          </div>
        </div>

        {/* Footer */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '12px 20px', gap: '8px',
          borderTop: '1px solid #1e3a5f',
          background: '#0d1420',
          flexShrink: 0,
        }}>
          <div style={{ color: '#334155', fontSize: '9px' }}>
            Rueda del mouse para zoom
          </div>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={onClose}
              style={{
                background: 'transparent', color: '#64748b', border: '1px solid #334155',
                borderRadius: '6px', padding: '8px 20px', fontSize: '12px', cursor: 'pointer',
                transition: 'all 0.15s',
              }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = '#475569'; e.currentTarget.style.color = '#94a3b8'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = '#334155'; e.currentTarget.style.color = '#64748b'; }}
            >
              Cerrar
            </button>
            {entry.sections?.length > 0 && (
              <button
                onClick={onDownloadPdf}
                style={{
                  background: 'linear-gradient(135deg,#1e40af,#3b82f6)', color: 'white',
                  border: 'none', borderRadius: '6px', padding: '8px 20px',
                  fontSize: '12px', fontWeight: '700', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', gap: '6px',
                  transition: 'filter 0.15s',
                  boxShadow: '0 2px 8px rgba(59,130,246,0.3)',
                }}
                onMouseEnter={e => { e.currentTarget.style.filter = 'brightness(1.1)'; }}
                onMouseLeave={e => { e.currentTarget.style.filter = 'brightness(1)'; }}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" />
                </svg>
                Descargar PDF
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

// ── Panel Principal ────────────────────────────────────────────────────────────
type PanelState = 'idle' | 'analizando' | 'revisar' | 'guardado' | 'rechazado';
type ActiveTab  = 'analisis' | 'historial';

interface ImagiaxAiPanelProps {
  servicesManager?: unknown;
  commandsManager?: unknown;
}

const ImagiaxAiPanel: React.FC<ImagiaxAiPanelProps> = () => {
  // ── Auth state ────────────────────────────────────────────────────────────
  const [authUser, setAuthUser] = useState<AuthUser | null>(() => getStoredUser());
  const [authLoading, setAuthLoading] = useState<boolean>(true);
  const [loginEmail, setLoginEmail] = useState('');
  const [loginPassword, setLoginPassword] = useState('');
  const [loginError, setLoginError] = useState('');
  const [loginLoading, setLoginLoading] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  useEffect(() => {
    setOnAuthExpired(() => setAuthUser(null));
  }, []);

  useEffect(() => {
    const storedUser = getStoredUser();

    getCurrentUser()
      .then(user => setAuthUser(user))
      .catch(() => {
        // Fallback limpio: conservar usuario local si existe; si no, login inline.
        setAuthUser(storedUser ?? null);
      })
      .finally(() => setAuthLoading(false));
  }, []);

  const handleLogin = useCallback(async () => {
    if (!loginEmail || !loginPassword) return;

    setLoginLoading(true);
    setLoginError('');
    try {
      const resp = await login(loginEmail.trim(), loginPassword);
      setAuthUser(resp.user);
      setLoginEmail('');
      setLoginPassword('');
      setShowPassword(false);
    } catch (err) {
      if (err instanceof ImagiaxApiError) {
        setLoginError(
          err.status === 429
            ? 'Demasiados intentos. Espera unos minutos y volvé a intentar.'
            : err.status === 401
              ? 'Credenciales incorrectas. Verificá tu email y contraseña.'
              : err.detail
        );
      } else {
        setLoginError('Error de conexión. Verificá que el servidor esté activo.');
      }
    } finally {
      setLoginLoading(false);
    }
  }, [loginEmail, loginPassword]);

  const handleLogout = useCallback(async () => {
    try {
      logout();
      await logoutApi();
    } finally {
      setAuthUser(null);
    }
  }, []);

  // ── PERF-H6: Abort controller ref (cancels in-flight fetch on unmount) ───
  const abortRef = useRef<AbortController | null>(null);
  // Stores milestone setTimeout IDs so they can be cleared on unmount
  const milestoneTimersRef = useRef<ReturnType<typeof setTimeout>[]>([]);

  // ── RESP-C5: Panel width tracking for responsive ScaledPreview ───────────
  const panelRef = useRef<HTMLDivElement>(null);
  const [panelWidth, setPanelWidth] = useState(400);

  // Cleanup: abort any in-flight analysis fetch and clear milestone timers
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      milestoneTimersRef.current.forEach(id => clearTimeout(id));
      milestoneTimersRef.current = [];
    };
  }, []);

  // ── RESP-C5: Track panel container width for responsive PDF preview ───────
  useEffect(() => {
    const el = panelRef.current;
    if (!el) return;
    setPanelWidth(el.clientWidth || 400);
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect?.width;
      if (w && w > 0) setPanelWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // ── OHIF context ───────────────────────────────────────────────────────────
  const activeDisplaySets = useActiveViewportDisplaySets();

  const [state,          setState]          = useState<PanelState>('idle');
  const [activeTab,      setActiveTab]      = useState<ActiveTab>('analisis');
  const [progress,       setProgress]       = useState(0);
  const [progressMsg,    setProgressMsg]    = useState('');
  const [sections,       setSections]       = useState<ReportSection[]>([]);
  const [editSections,   setEditSections]   = useState<ReportSection[]>([]);
  const [informeId,      setInformeId]      = useState('');
  const [qrDataUrl,      setQrDataUrl]      = useState('');
  const [pdfUrl,         setPdfUrl]         = useState('');
  const [isGeneratingPdf,setIsGeneratingPdf]= useState(false);
  const [pdfConfirmed,   setPdfConfirmed]   = useState(false);
  const [legalAccepted,  setLegalAccepted]  = useState(false);
  const [editingReport,  setEditingReport]  = useState(false);
  const [pdfError,       setPdfError]       = useState('');
  const [apiError,       setApiError]       = useState('');
  const [apiStatus,      setApiStatus]      = useState<ApiStatus>('unknown');
  const [aiResultados,   setAiResultados]   = useState<AIResultados | null>(null);
  const [studyUID,       setStudyUID]       = useState('');
  const [creditos,       setCreditos]       = useState<EstadoCreditosResponse | null>(null);
  const [metodoPago,     setMetodoPago]     = useState<MetodoPagoResponse | null>(null);

  // ── v1.1 Pipeline inputs ─────────────────────────────────────────────
  const [notaClinica,       setNotaClinica]       = useState('');
  const [solicitarAuditoria,setSolicitarAuditoria] = useState(false);
  const [selectedPipelineVer, setSelectedPipelineVer] = useState<'1.0' | '1.1'>('1.1');
  // ── v1.1 Pipeline result metadata ─────────────────────────────────
  const [pipelineVersion,   setPipelineVersion]   = useState<string | undefined>(undefined);
  const [calidadImagen,     setCalidadImagen]     = useState<string | undefined>(undefined);
  const [auditoriaEstado,   setAuditoriaEstado]   = useState<string | undefined>(undefined);


  // ── V2 Experimental — NV-Reason-CXR-3B ──────────────────────────────────
  const [v2Status,       setV2Status]       = useState<'unknown' | 'online' | 'offline'>('unknown');
  const [v2Loading,      setV2Loading]      = useState(false);
  const [v2Result,       setV2Result]       = useState<V2CompararResponse | null>(null);
  const [v2Error,        setV2Error]        = useState('');

  // ── Historial — fuente primaria: backend DB; caché secundaria: localStorage ──
  const [history, setHistory] = useState<HistoryEntry[]>(() => {
    // Inicializar desde localStorage como caché mientras el backend responde
    try {
      const raw = sessionStorage.getItem('imagiax_history_v1');
      if (raw) {
        const parsed = JSON.parse(raw) as HistoryEntry[];
        return parsed.map(e => ({ ...e, pdfUrl: '' }));
      }
    } catch { /* ignorar error de parse */ }
    return [];
  });
  const [viewingEntry,    setViewingEntry]    = useState<HistoryEntry | null>(null);
  const [historialLoading, setHistorialLoading] = useState(false);
  const [historialError,   setHistorialError]   = useState('');

  // Sincronizar caché localStorage cada vez que el historial en memoria cambia
  useEffect(() => {
    try {
      const trimmed = history.slice(0, MAX_HISTORY_ENTRIES);
      const toStore = trimmed.map(e => ({ ...e, pdfUrl: '' }));
      sessionStorage.setItem('imagiax_history_v1', JSON.stringify(toStore));
    } catch { /* cuota excedida u otro error */ }
  }, [history]);

  /**
   * Carga el historial desde el backend.
   * Reemplaza el estado local con los datos reales de la DB del médico autenticado.
   * Solo se llama cuando hay usuario autenticado.
   */
  const fetchHistorialFromBackend = useCallback(async () => {
    setHistorialLoading(true);
    setHistorialError('');
    try {
      const data = await listadoInformes();
      const entries = (data.items || []).map(informeDbToHistoryEntry);
      setHistory(entries);
    } catch (err) {
      if (err instanceof ImagiaxApiError && err.status === 401) {
        setAuthUser(null);
        return;
      }
      setHistorialError('No se pudo cargar el historial. Verifica tu conexión.');
    } finally {
      setHistorialLoading(false);
    }
  }, []);

  // Cargar historial desde backend al montar (una vez autenticado)
  useEffect(() => {
    if (authUser) {
      fetchHistorialFromBackend();
    }
  }, [authUser, fetchHistorialFromBackend]);

  // Recargar al cambiar a la pestaña Historial (para ver informes recientes).
  // fetchHistorialFromBackend is stable (useCallback with [] deps), so including it
  // here satisfies exhaustive-deps without causing extra re-renders.
  useEffect(() => {
    if (activeTab === 'historial' && authUser) {
      fetchHistorialFromBackend();
    }
  }, [activeTab, authUser, fetchHistorialFromBackend]);

  // ── Extract patient info from OHIF active display set ───────────────────
  const [patient, setPatient] = useState<PatientInfo>({
    rut: '',
    nombre: '',
    edad: '',
    fechaExamen: formatDate(new Date()),
    medicoReferencia: '',
    tipoExamen: '',
  });

  useEffect(() => {
    if (!activeDisplaySets?.length) return;
    const ds = activeDisplaySets[0];
    const instance = ds?.instances?.[0] || (ds as any)?.instance;
    if (!instance) return;

    const patName = instance.PatientName
      ? (typeof instance.PatientName === 'string'
          ? instance.PatientName
          : utils.formatPN(instance.PatientName))
      : '';

    const studyDate = instance.StudyDate
      ? `${instance.StudyDate.substring(6, 8)}/${instance.StudyDate.substring(4, 6)}/${instance.StudyDate.substring(0, 4)}`
      : formatDate(new Date());

    const modality = ds.Modality || instance.Modality || '';
    const bodyPart = (instance as any).BodyPartExamined || '';
    const studyDesc = (instance as any).StudyDescription || ds.SeriesDescription || '';

    // Build exam type: map raw DICOM tags (Modality, BodyPartExamined, StudyDescription)
    // to human-readable Spanish BEFORE storing, so tipoExamen is always clean.
    const tipoExamen = getStudyDescription(
      studyDesc || null,
      bodyPart || null,
      modality || null
    );

    // Resolve patient age from DICOM: prefer PatientAge (e.g. "065Y") over
    // PatientBirthDate (YYYYMMDD → extract birth year so calcAge computes delta)
    const rawAge = (instance as any).PatientAge || instance.PatientAge || '';
    const birthDate = (instance as any).PatientBirthDate || instance.PatientBirthDate || '';
    const edadValue = rawAge || (birthDate.length >= 4 ? birthDate.substring(0, 4) : '');

    setPatient(prev => ({
      ...prev,
      rut: instance.PatientID || (instance as any).PatientId || '',
      nombre: patName,
      edad: edadValue,
      fechaExamen: studyDate,
      tipoExamen,
    }));

    // Keep the StudyInstanceUID for API calls
    if (ds.StudyInstanceUID) {
      setStudyUID(ds.StudyInstanceUID);
    }
  }, [activeDisplaySets]);

  // ── Check API health on mount ────────────────────────────────────────────
  useEffect(() => {
    // Cargar configuración dinámica del backend (doctor/clínica)
    loadBackendConfig();
    setApiStatus('checking');
    checkSalud()
      .then(() => setApiStatus('online'))
      .catch(() => setApiStatus('offline'));
    // V2 health check in parallel (non-blocking)
    checkSaludV2()
      .then(() => setV2Status('online'))
      .catch(() => setV2Status('offline'));
    // Credit status check (non-blocking — doesn't affect core functionality)
    checkEstadoCreditos()
      .then(setCreditos)
      .catch(() => {/* silencioso si la DB de pagos no está lista */});
    // Card payment method check (non-blocking)
    getMetodoPago()
      .then(setMetodoPago)
      .catch(() => {/* silencioso */});
  }, []);

  const iniciarAnalisis = useCallback(async () => {
    if (!studyUID) {
      setApiError('No hay estudio activo. Abra un estudio DICOM primero.');
      return;
    }

    setState('analizando');
    setActiveTab('analisis');
    setProgress(0);
    setProgressMsg('Conectando con servidor...');
    setPdfUrl('');
    setPdfError('');
    setPdfConfirmed(false);
    setLegalAccepted(false);
    setEditingReport(false);
    setApiError('');
    setAiResultados(null);
    // v1.1 result reset
    setPipelineVersion(undefined);
    setCalidadImagen(undefined);
    setAuditoriaEstado(undefined);

    // S3.6: Milestone-based progress — real state transitions, no fake creep.
    // Milestones: 10% request sent, 30% quality gate (est), 50% analysis,
    // 70% report draft, 85% audit, 100% done (set on response).
    setProgress(10);
    setProgressMsg('Solicitud enviada...');
    // PERF-H6: store timer IDs in ref so cleanup useEffect can clear them on unmount
    const _t30 = setTimeout(() => { setProgress(30); setProgressMsg('Control de calidad...'); }, 3000);
    const _t50 = setTimeout(() => { setProgress(50); setProgressMsg('Analizando imagenes...'); }, 7000);
    const _t70 = setTimeout(() => { setProgress(70); setProgressMsg('Generando informe...'); }, 18000);
    const _t85 = setTimeout(() => { setProgress(85); setProgressMsg('Auditoria clinica...'); }, 30000);
    milestoneTimersRef.current = [_t30, _t50, _t70, _t85];
    const _clearMilestones = () => {
      clearTimeout(_t30); clearTimeout(_t50); clearTimeout(_t70); clearTimeout(_t85);
      milestoneTimersRef.current = [];
    };

    // PERF-H6: create fresh AbortController for this analysis request
    abortRef.current = new AbortController();

    try {
      const response = await generarInforme(
        studyUID,
        5,
        notaClinica || undefined,
        solicitarAuditoria || undefined,
        selectedPipelineVer,
      );

      _clearMilestones();
      setProgress(100);
      setProgressMsg('¡Análisis completado!');

      const reportText = response.informe_texto;
      const sec = parseReportSections(reportText);
      const id = response.informe_id;

      setSections(sec);
      setInformeId(id);
      setAiResultados({
        ...response.ai_resultados,
        hallazgos_estudio: response.ai_resultados?.hallazgos_estudio ?? [],
      });

      // Capture v1.1 pipeline metadata (present only in v1.1 responses)
      if (response.pipeline_version) setPipelineVersion(response.pipeline_version);
      if (response.calidad_imagen)   setCalidadImagen(response.calidad_imagen);
      if (response.auditoria_estado) setAuditoriaEstado(response.auditoria_estado);

      // Refrescar contador de créditos y método de pago tras análisis exitoso
      checkEstadoCreditos().then(setCreditos).catch(() => {});
      getMetodoPago().then(setMetodoPago).catch(() => {});

      // Mostrar advertencia si el cobro automático fue rechazado (pero análisis completado)
      if (response.pago_automatico?.estado === 'rejected') {
        setApiError(
          'Cobro rechazado. El informe se genero correctamente pero no pudimos procesar el pago. ' +
          'Por favor actualiza tu tarjeta para evitar interrupciones.'
        );
      }

      // Generate QR code
      try {
        const qr = await QRCode.toDataURL(
          `${IMAGIAX_BACKEND}/informes/${id}/ver`,
          { width: 128, margin: 1, color: { dark: '#000000', light: '#ffffff' } }
        );
        setQrDataUrl(qr);
      } catch {
        setQrDataUrl('');
      }

      setState('revisar');
    } catch (err) {
      _clearMilestones();
      setProgress(0);
      setProgressMsg('');

      if (err instanceof ImagiaxTimeoutError) {
        // Timeout específico — mensaje amigable
        setApiError(
          'El análisis está tomando más tiempo de lo esperado. ' +
          'El servidor puede seguir procesando. Espera un momento e intenta nuevamente. ' +
          'Si el problema persiste, contacta soporte.'
        );
      } else if (err instanceof ImagiaxApiError) {
        if (err.status === 401) {
          setAuthUser(null);
          setState('idle');
          return;
        } else if (err.status === 402) {
          const detail = typeof err.detail === 'object' ? (err.detail as any) : {};

          if (detail.error === 'Cobro rechazado') {
            // Pago con tarjeta rechazado — informe retenido
            setApiError(
              'No se pudo procesar el cobro con tu tarjeta. ' +
              'Verifica que tenga fondos disponibles o registra una nueva.'
            );
          } else {
            // Creditos agotados
            setApiError(
              'Creditos agotados. Adquiere un plan o registra una tarjeta para cobro automatico.'
            );
          }
          // No abrir ventana automáticamente — el usuario usará el botón del mensaje de error
          checkEstadoCreditos().then(setCreditos).catch(() => {});
          getMetodoPago().then(setMetodoPago).catch(() => {});
        } else {
          setApiError(`Error del servidor: ${typeof err.detail === 'string' ? err.detail : 'error desconocido'}`);
        }
      } else if (err instanceof TypeError && err.message.includes('fetch')) {
        setApiError('No se puede conectar con el servidor. Verifique que el servicio este activo.');
      } else {
        setApiError(`Error inesperado: ${(err as Error).message}`);
      }
      setState('idle');
    }
  }, [studyUID, notaClinica, solicitarAuditoria, selectedPipelineVer]);

  const handleGeneratePdf = useCallback(async () => {
    setIsGeneratingPdf(true);
    setPdfError('');
    try {
      const url = await generateReportPDF(patient, sections, informeId, qrDataUrl, notaClinica || undefined);
      setPdfUrl(url);
    } catch (err) {
      setPdfError('Error al generar el PDF. Intente nuevamente.');
      console.error(err);
    } finally {
      setIsGeneratingPdf(false);
    }
  }, [patient, sections, informeId, qrDataUrl]);

  // FIX: handleConfirmar saves to backend + generates PDF
  const handleConfirmar = useCallback(async () => {
    setIsGeneratingPdf(true);
    setPdfError('');
    setApiError(''); // Clear previous errors
    try {
      // Determine if the report was edited
      const wasEdited = editingReport || editSections.length > 0;
      const finalText = sectionsToText(sections);
      const estado = wasEdited ? 'editado_aceptado' : 'aceptado';

      // Save decision to backend
      let saveSucceeded = true;
      try {
        await guardarDecision(informeId, estado as any, finalText);
      } catch (err) {
        if (err instanceof ImagiaxApiError && err.status === 401) {
          setAuthUser(null);
          return;
        }
        console.warn('No se pudo guardar decisión en backend:', err);
        saveSucceeded = false;
        // Show warning but continue with PDF generation
        setApiError(
          'Advertencia: El informe se guardará localmente pero no se pudo registrar en el servidor. ' +
          'Contacta soporte si el problema persiste.'
        );
      }

      const url = await generateReportPDF(patient, sections, informeId, qrDataUrl, notaClinica || undefined);
      setPdfUrl(url);
      setHistory(prev => [{
        informeId, fecha: formatDate(new Date()), patient: { ...patient },
        sections: [...sections], pdfUrl: url, qrDataUrl, notaClinica,
      }, ...prev]);
      setState('guardado');
    } catch (err) {
      setPdfError('Error al generar el PDF. Intente nuevamente.');
      console.error(err);
    } finally {
      setIsGeneratingPdf(false);
    }
  }, [informeId, patient, sections, qrDataUrl, editingReport, editSections]);

  const handleEditar = useCallback(() => {
    setEditSections(sections.map(s => ({ ...s, lines: [...s.lines] })));
    setEditingReport(true);
  }, [sections]);

  const handleGuardarEdicion = useCallback(() => {
    setSections(editSections);
    setEditingReport(false);
    setPdfUrl('');
  }, [editSections]);

  const handleDownloadHistoryPdf = useCallback(async (entry: HistoryEntry) => {
    try {
      const url = await generateReportPDF(entry.patient, entry.sections, entry.informeId, entry.qrDataUrl, entry.notaClinica);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${entry.informeId}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Error regenerando PDF del historial:', err);
    }
  }, []);

  // ── V2 Experimental comparison trigger ─────────────────────────────────
  const handleCompararV2 = useCallback(async () => {
    if (!studyUID || v2Loading) return;
    setV2Loading(true);
    setV2Error('');
    setV2Result(null);
    try {
      const result = await compararV1V2(studyUID, 1);
      setV2Result(result);
    } catch (err) {
      if (err instanceof ImagiaxApiError && err.status === 401) {
        setAuthUser(null);
        return;
      }
      if (err instanceof ImagiaxApiError) {
        setV2Error('Error al comparar con el servidor V2.');
      } else if (err instanceof TypeError && err.message.includes('fetch')) {
        setV2Error('Servidor V2 no disponible. Verifique que esté corriendo en puerto 8011.');
      } else {
        setV2Error('Error inesperado al comparar.');
      }
    } finally {
      setV2Loading(false);
    }
  }, [studyUID, v2Loading]);

  const handleRechazar = useCallback(async () => {
    setApiError(''); // Clear previous errors
    if (informeId) {
      try {
        await guardarDecision(informeId, 'rechazado', undefined, 'Rechazado por el médico desde panel OHIF');
      } catch (err) {
        if (err instanceof ImagiaxApiError && err.status === 401) {
          setAuthUser(null);
          return;
        }
        console.warn('No se pudo guardar rechazo en backend:', err);
        setApiError(
          'Advertencia: El rechazo no se pudo registrar en el servidor. ' +
          'El informe permanece en estado pendiente en la base de datos.'
        );
      }
    }
    setState('rechazado');
    setPdfUrl('');
    setPdfError('');
  }, [informeId]);

  // FIX: reset completo de todos los estados incluyendo editSections y viewingEntry
  const handleReset = useCallback(() => {
    setState('idle');
    setSections([]);
    setEditSections([]);
    setPdfUrl('');
    setPdfError('');
    setPdfConfirmed(false);
    setLegalAccepted(false);
    setEditingReport(false);
    setProgress(0);
    setProgressMsg('');
    setInformeId('');
    setQrDataUrl('');
    setApiError('');
    setAiResultados(null);
    setV2Result(null);
    setV2Error('');
    setV2Loading(false);
    setViewingEntry(null);
    setPipelineVersion(undefined);
    setCalidadImagen(undefined);
    setAuditoriaEstado(undefined);
  }, []);

  // ── Renders ────────────────────────────────────────────────────────────────
  // PERF-H4: memoize payment/credit derived values — these are used in multiple
  // render helpers and re-computed on every render without memoization.
  const { tieneTarjeta, creditosDisponibles, usaraCreditos, usaraTarjeta, sinMetodoPago, canAnalyze } = useMemo(() => {
    const _tieneTarjeta = metodoPago?.tiene_tarjeta || creditos?.tiene_tarjeta || false;
    const _creditosDisponibles = creditos?.creditos_disponibles ?? 0;
    const _usaraCreditos = _creditosDisponibles > 0;
    const _usaraTarjeta = !_usaraCreditos && _tieneTarjeta;
    const _sinMetodoPago = !_usaraCreditos && !_tieneTarjeta;
    const _canAnalyze = apiStatus === 'online' && !!studyUID && !_sinMetodoPago;
    return {
      tieneTarjeta: _tieneTarjeta,
      creditosDisponibles: _creditosDisponibles,
      usaraCreditos: _usaraCreditos,
      usaraTarjeta: _usaraTarjeta,
      sinMetodoPago: _sinMetodoPago,
      canAnalyze: _canAnalyze,
    };
  }, [metodoPago, creditos, apiStatus, studyUID]);

  const renderIdle = () => (
    <div style={{ display:'flex', flexDirection:'column', gap:'0', padding:'0' }}>
      {/* Patient / Study card — most important info for doctors */}
      <div style={{ padding:'10px 12px', display:'flex', flexDirection:'column', gap:'10px' }}>
        {studyUID ? (
          <div style={{ background:'#0d1420', borderRadius:'8px', border:'1px solid #1e3a5f',
            padding:'10px 12px', display:'flex', flexDirection:'column', gap:'8px' }}>
            {/* Card header: label + status badge (merged from status bar) */}
            <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
              <span style={{ color:'#475569', fontSize:'9px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.5px' }}>Estudio activo</span>
              {/* Status badge — replaces standalone status bar */}
              <div style={{ display:'flex', alignItems:'center', gap:'4px' }}>
                <div style={{ width:'5px', height:'5px', borderRadius:'50%',
                  background: apiStatus === 'online' ? '#22c55e' : apiStatus === 'checking' ? '#f59e0b' : '#ef4444',
                  boxShadow: apiStatus === 'online' ? '0 0 3px #22c55e' : 'none' }} />
                <span style={{ color: apiStatus === 'online' ? '#64748b' : '#f87171', fontSize:'8px' }}>
                  {apiStatus === 'online' ? 'Conectado' : apiStatus === 'checking' ? 'Conectando...' : 'Sin conexion'}
                </span>
                {/* Créditos o método de pago como pill */}
                {creditosDisponibles > 0 && (
                  <span style={{ color:'#86efac', fontSize:'8px', fontWeight:'600',
                    background:'rgba(34,197,94,0.1)', padding:'1px 5px', borderRadius:'4px', marginLeft:'2px' }}>
                    {creditosDisponibles}✓
                  </span>
                )}
                {usaraTarjeta && (
                  <span style={{ color:'#94a3b8', fontSize:'8px',
                    background:'rgba(148,163,184,0.1)', padding:'1px 5px', borderRadius:'4px', marginLeft:'2px' }}>
                    💳
                  </span>
                )}
                {sinMetodoPago && (
                  <span style={{ color:'#f87171', fontSize:'8px',
                    background:'rgba(239,68,68,0.1)', padding:'1px 5px', borderRadius:'4px', marginLeft:'2px' }}>
                    Sin creditos
                  </span>
                )}
              </div>
            </div>
            {patient.nombre && (
              <div style={{ color:'#f1f5f9', fontSize:'14px', fontWeight:'700', lineHeight:'1.3' }}>
                {patient.nombre.toUpperCase()}
              </div>
            )}
            {/* FIX #3: Mejor line-height y legibilidad en datos clínicos */}
            <div style={{ display:'flex', gap:'12px', flexWrap:'wrap', lineHeight:'1.5' }}>
              {patient.rut && <span style={{ color:'#94a3b8', fontSize:'11px' }}>ID: {patient.rut}</span>}
              {patient.tipoExamen && <span style={{ color:'#64748b', fontSize:'11px' }}>{patient.tipoExamen}</span>}
            </div>
            {/* StudyUID — hidden (technical info not needed by doctors) */}
            {false && (
              <div
                title={studyUID}
                style={{ color:'#475569', fontSize:'9px', fontFamily:'monospace', marginTop:'2px',
                  overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', lineHeight:'1.5',
                  cursor:'help' }}>
                {studyUID}
              </div>
            )}
          </div>
        ) : (
          <div style={{ background:'#0d1420', borderRadius:'8px', border:'1px dashed #1e3a5f',
            padding:'20px 14px', textAlign:'center' }}>
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="1.5"
              style={{ margin:'0 auto 10px' }}>
              <path d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2v-4M9 21H5a2 2 0 01-2-2v-4m0 0h18" />
            </svg>
            <div style={{ color:'#94a3b8', fontSize:'12px', fontWeight:'600' }}>Abra un estudio DICOM</div>
            <div style={{ color:'#64748b', fontSize:'10px', marginTop:'4px', lineHeight:'1.5' }}>
              Seleccione un estudio en el visor para iniciar el analisis
            </div>
          </div>
        )}

        {/* FIX #6: Error message mejorado con botón de acción */}
        {apiError && (
          <div style={{ background:'#450a0a', borderRadius:'6px', padding:'12px 14px',
            border:'1px solid #7f1d1d' }}>
            <div style={{ color:'#fca5a5', fontSize:'11px', lineHeight:'1.5', marginBottom: apiError.includes('tarjeta') || apiError.includes('cobro') ? '10px' : '0' }}>
              {apiError}
            </div>
            {(apiError.includes('tarjeta') || apiError.includes('cobro') || apiError.includes('Cobro')) && (
              <button
                onClick={() => window.open(`${IMAGIAX_BACKEND}/pagar`, '_blank', 'noopener,noreferrer')}
                style={{ background:'#dc2626', color:'white', border:'none', borderRadius:'4px',
                  padding:'6px 12px', fontSize:'10px', fontWeight:'600', cursor:'pointer', width:'100%' }}>
                Actualizar metodo de pago
              </button>
            )}
          </div>
        )}

        {/* Info de método de cobro justo antes del botón CTA */}
        {/* Mostrar tarjeta SOLO si se usará (sin créditos disponibles) */}
        {usaraTarjeta && canAnalyze && (
          <div style={{ background:'#0f172a', border:'1px solid #1e3a5f', borderRadius:'6px',
            padding:'10px 14px', display:'flex', justifyContent:'space-between', alignItems:'center' }}>
            <div>
              <div style={{ color:'#94a3b8', fontSize:'10px', marginBottom:'2px' }}>Cobro automatico</div>
              <div style={{ color:'#f1f5f9', fontSize:'12px', fontWeight:'600' }}>
                Tarjeta ****{metodoPago?.card_last4 || creditos?.card_last4}
              </div>
            </div>
            <div style={{ textAlign:'right' }}>
              <div style={{ color:'#22c55e', fontSize:'14px', fontWeight:'700' }}>$990</div>
              <div style={{ color:'#64748b', fontSize:'9px' }}>por informe</div>
            </div>
          </div>
        )}
        {/* Mostrar créditos si se usarán */}
        {usaraCreditos && canAnalyze && (
          <div style={{ background:'#0f172a', border:'1px solid #1e3a5f', borderRadius:'6px',
            padding:'10px 14px', display:'flex', justifyContent:'space-between', alignItems:'center' }}>
            <div>
              <div style={{ color:'#94a3b8', fontSize:'10px', marginBottom:'2px' }}>Se descontara 1 credito</div>
              <div style={{ color:'#f1f5f9', fontSize:'12px', fontWeight:'600' }}>
                {creditosDisponibles} disponible{creditosDisponibles !== 1 ? 's' : ''}
                {creditos?.es_trial ? ' (trial)' : ''}
              </div>
            </div>
            {tieneTarjeta && (
              <div style={{ textAlign:'right' }}>
                <div style={{ color:'#64748b', fontSize:'9px' }}>Tarjeta como backup</div>
                <div style={{ color:'#475569', fontSize:'10px' }}>****{metodoPago?.card_last4 || creditos?.card_last4}</div>
              </div>
            )}
          </div>
        )}

        {/* v1.1 Clinical Note + Audit request — only shown when backend is online */}
        {canAnalyze && (
          <div style={{ display:'flex', flexDirection:'column', gap:'6px' }}>
            {/* Clinical note textarea */}
            <div style={{ background:'#0d1420', borderRadius:'6px', border:'1px solid #1e3a5f', padding:'8px 10px' }}>
              <label style={{ color:'#64748b', fontSize:'9px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.5px', display:'block', marginBottom:'4px' }}>
                Nota clínica (opcional)
              </label>
              <textarea
                value={notaClinica}
                onChange={e => setNotaClinica(e.target.value.slice(0, 500))}
                placeholder="Contexto clínico relevante: síntomas, hallazgos previos, antecedentes..."
                maxLength={500}
                rows={3}
                style={{
                  width:'100%', boxSizing:'border-box', background:'transparent',
                  border:'none', outline:'none', resize:'none',
                  color:'#e2e8f0', fontSize:'11px', lineHeight:'1.5',
                  fontFamily:'inherit',
                }}
              />
              <div style={{ color:'#475569', fontSize:'9px', textAlign:'right', marginTop:'2px' }}>
                {notaClinica.length}/500
              </div>
            </div>
            {/* Audit request checkbox */}
            <label
              style={{ display:'flex', alignItems:'center', gap:'8px', cursor:'pointer',
                background:'#0d1420', borderRadius:'6px', border:'1px solid #1e3a5f',
                padding:'8px 10px' }}
            >
              <input
                type="checkbox"
                checked={solicitarAuditoria}
                onChange={e => setSolicitarAuditoria(e.target.checked)}
                style={{ accentColor:'#3b82f6', width:'14px', height:'14px', cursor:'pointer', flexShrink:0 }}
              />
              <div>
                <div style={{ color:'#e2e8f0', fontSize:'11px', fontWeight:'600' }}>Solicitar auditoría IA</div>
                <div style={{ color:'#475569', fontSize:'9px', marginTop:'2px', lineHeight:'1.4' }}>
                  Revisión secundaria del informe por modelo avanzado
                </div>
              </div>
            </label>
            {/* Pipeline version toggle — real switch between v1.0 and v1.1 */}
            <div style={{
              background:'#0d1420', borderRadius:'6px', border:'1px solid #1e3a5f',
              padding:'8px 10px', display:'flex', alignItems:'center', justifyContent:'space-between',
            }}>
              <div>
                <div style={{ color:'#e2e8f0', fontSize:'11px', fontWeight:'600' }}>
                  Pipeline {selectedPipelineVer === '1.1' ? 'v1.1' : 'v1.0'}
                  {selectedPipelineVer === '1.1' && (
                    <span style={{
                      marginLeft:'6px', background:'#164e63', color:'#22d3ee', fontSize:'8px',
                      fontWeight:'700', padding:'1px 5px', borderRadius:'3px', verticalAlign:'middle',
                    }}>AVANZADO</span>
                  )}
                </div>
                <div style={{ color:'#475569', fontSize:'9px', marginTop:'2px', lineHeight:'1.4' }}>
                  {selectedPipelineVer === '1.1'
                    ? 'Quality Gate + Nota clínica + Auditor selectivo'
                    : 'Análisis estándar (ResNet50 + Gemini)'}
                </div>
              </div>
              <button
                onClick={() => setSelectedPipelineVer(v => v === '1.0' ? '1.1' : '1.0')}
                title={selectedPipelineVer === '1.1' ? 'Cambiar a Pipeline v1.0' : 'Cambiar a Pipeline v1.1'}
                style={{
                  width:'40px', height:'22px', borderRadius:'11px', border:'none', cursor:'pointer',
                  background: selectedPipelineVer === '1.1'
                    ? 'linear-gradient(135deg,#0891b2,#22d3ee)'
                    : '#334155',
                  position:'relative', transition:'background 0.2s', flexShrink:0,
                }}
              >
                <div style={{
                  width:'16px', height:'16px', borderRadius:'50%', background:'#fff',
                  position:'absolute', top:'3px',
                  left: selectedPipelineVer === '1.1' ? '21px' : '3px',
                  transition:'left 0.2s', boxShadow:'0 1px 3px rgba(0,0,0,0.3)',
                }} />
              </button>
            </div>
          </div>
        )}

        {/* Main CTA con hover effect */}
        <button
          onClick={sinMetodoPago ? () => window.open(`${IMAGIAX_BACKEND}/pagar`, '_blank', 'noopener,noreferrer') : iniciarAnalisis}
          disabled={!canAnalyze && !sinMetodoPago}
          onMouseEnter={e => { if (canAnalyze || sinMetodoPago) (e.target as HTMLElement).style.filter = 'brightness(1.1)'; }}
          onMouseLeave={e => { (e.target as HTMLElement).style.filter = 'brightness(1)'; }}
          style={{
            background: sinMetodoPago ? 'linear-gradient(135deg,#7f1d1d,#dc2626)' : canAnalyze ? 'linear-gradient(135deg,#1e40af,#3b82f6)' : '#1e293b',
            color: (canAnalyze || sinMetodoPago) ? 'white' : '#475569',
            border:'none', borderRadius:'8px', padding:'14px', fontSize:'13px', fontWeight:'700', width:'100%',
            cursor: (canAnalyze || sinMetodoPago) ? 'pointer' : 'not-allowed',
            transition:'filter 0.15s, transform 0.1s',
            boxShadow: canAnalyze ? '0 4px 12px rgba(59,130,246,0.3)' : 'none'
          }}>
          {sinMetodoPago ? 'Adquirir creditos' : 'Iniciar Analisis IA'}
        </button>

        {apiStatus === 'offline' && (
          <button onClick={() => { setApiStatus('checking'); checkSalud().then(() => setApiStatus('online')).catch(() => setApiStatus('offline')); }}
            style={{ background:'transparent', color:'#3b82f6', border:'1px solid #1e3a5f',
              borderRadius:'6px', padding:'8px 16px', fontSize:'11px', cursor:'pointer', width:'100%' }}>
            Reintentar conexion
          </button>
        )}

        {/* FIX #1: Quick links con mejor contraste (WCAG) */}
        <div style={{ display:'flex', gap:'8px', justifyContent:'center', marginTop:'4px' }}>
          <a href={`${IMAGIAX_BACKEND}/pagar`} target="_blank" rel="noopener noreferrer"
            style={{ color:'#64748b', fontSize:'10px', textDecoration:'none', padding:'4px 0',
              transition:'color 0.15s', cursor:'pointer' }}
            onMouseEnter={e => { (e.target as HTMLElement).style.color = '#94a3b8'; }}
            onMouseLeave={e => { (e.target as HTMLElement).style.color = '#64748b'; }}>
            Planes
          </a>
          <span style={{ color:'#334155', fontSize:'10px' }}>|</span>
          <a href={`${IMAGIAX_BACKEND}/historial`} target="_blank" rel="noopener noreferrer"
            style={{ color:'#64748b', fontSize:'10px', textDecoration:'none', padding:'4px 0',
              transition:'color 0.15s', cursor:'pointer' }}
            onMouseEnter={e => { (e.target as HTMLElement).style.color = '#94a3b8'; }}
            onMouseLeave={e => { (e.target as HTMLElement).style.color = '#64748b'; }}>
            Historial
          </a>
          {tieneTarjeta && (
            <>
              <span style={{ color:'#334155', fontSize:'10px' }}>|</span>
              <a href={`${IMAGIAX_BACKEND}/pagar`} target="_blank" rel="noopener noreferrer"
                style={{ color:'#64748b', fontSize:'10px', textDecoration:'none', padding:'4px 0',
                  transition:'color 0.15s', cursor:'pointer' }}
                onMouseEnter={e => { (e.target as HTMLElement).style.color = '#94a3b8'; }}
                onMouseLeave={e => { (e.target as HTMLElement).style.color = '#64748b'; }}>
                Editar tarjeta
              </a>
            </>
          )}
        </div>
      </div>
    </div>
  );

  const renderAnalizando = () => (
    <div style={{ padding:'20px 14px', display:'flex', flexDirection:'column', gap:'16px' }}>
      {patient.nombre && (
        <div style={{ textAlign:'center', color:'#94a3b8', fontSize:'11px', fontWeight:'600' }}>
          {patient.nombre.toUpperCase()}
        </div>
      )}
      <div style={{ position:'relative', height:'6px', background:'#1e293b', borderRadius:'3px', overflow:'hidden' }}>
        <div style={{ position:'absolute', inset:0, background:'linear-gradient(90deg,#1e40af,#3b82f6,#60a5fa)',
          width:`${progress}%`, transition:'width 0.3s ease', borderRadius:'3px' }} />
      </div>
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
        <span style={{ color:'#475569', fontSize:'11px' }}>{progressMsg || 'Procesando...'}</span>
        <span style={{ color:'#3b82f6', fontSize:'12px', fontWeight:'700', fontVariantNumeric:'tabular-nums' }}>{Math.round(progress)}%</span>
      </div>
    </div>
  );

  const renderRevisar = () => (
    <div style={{ display:'flex', flexDirection:'column', gap:'0' }}>
      {/* Alert Level Banner — full width, high visibility */}
      {aiResultados && (
        (() => {
          const hallazgosEstudio = aiResultados.hallazgos_estudio ?? [];
          return (
        <div style={{
          background: aiResultados.nivel_alerta === 'ALTA' ? '#450a0a' : aiResultados.nivel_alerta === 'MEDIA' ? '#451a03' : aiResultados.nivel_alerta === 'BAJA' ? '#0c1e3a' : '#052e16',
          borderBottom: `2px solid ${aiResultados.nivel_alerta === 'ALTA' ? '#dc2626' : aiResultados.nivel_alerta === 'MEDIA' ? '#f59e0b' : aiResultados.nivel_alerta === 'BAJA' ? '#3b82f6' : '#22c55e'}`,
          padding:'10px 14px' }}>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
            <div style={{ display:'flex', alignItems:'center', gap:'8px' }}>
              <span style={{ background: aiResultados.nivel_alerta === 'ALTA' ? '#dc2626' : aiResultados.nivel_alerta === 'MEDIA' ? '#f59e0b' : aiResultados.nivel_alerta === 'BAJA' ? '#3b82f6' : '#22c55e',
                color: aiResultados.nivel_alerta === 'MEDIA' ? '#000' : '#fff',
                padding:'3px 10px', borderRadius:'4px', fontSize:'11px', fontWeight:'800', letterSpacing:'0.5px' }}>
                {aiResultados.nivel_alerta === 'NORMAL' ? 'NORMAL' : aiResultados.nivel_alerta}
              </span>
              <span style={{ color:'#94a3b8', fontSize:'10px' }}>
                {aiResultados.total_analizadas} imagen{aiResultados.total_analizadas !== 1 ? 'es' : ''}
              </span>
            </div>
          </div>
          {hallazgosEstudio.length > 0 ? (
            <div style={{ display:'flex', flexDirection:'column', gap:'3px', marginTop:'8px' }}>
              {hallazgosEstudio.map((h, i) => (
                <div key={i} style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
                  padding:'3px 8px', background:'rgba(0,0,0,0.2)', borderRadius:'3px' }}>
                  <span style={{ color:'#e2e8f0', fontSize:'11px' }}>{h.patologia}</span>
                  <span style={{ color: h.probabilidad_pct_max >= 80 ? '#f87171' : h.probabilidad_pct_max >= 60 ? '#fbbf24' : '#60a5fa',
                    fontSize:'11px', fontWeight:'700', fontVariantNumeric:'tabular-nums' }}>{h.probabilidad_pct_max.toFixed(1)}%</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color:'#4ade80', fontSize:'11px', marginTop:'6px' }}>Sin hallazgos patologicos sobre umbrales clinicos</div>
          )}
          {aiResultados.errores && aiResultados.errores.length > 0 && (
            <div style={{ color:'#fbbf24', fontSize:'10px', marginTop:'4px', opacity:0.8 }}>
              {aiResultados.errores.length} instancia{aiResultados.errores.length !== 1 ? 's' : ''} con error
            </div>
          )}
        </div>
          );
        })()
      )}

      {/* v1.1 Pipeline metadata badges */}
      {(calidadImagen || auditoriaEstado || pipelineVersion) && (
        <div style={{ padding:'6px 14px', display:'flex', gap:'6px', flexWrap:'wrap',
          borderBottom:'1px solid #1e293b', background:'#080e1a' }}>
          {pipelineVersion && (
            <span style={{ color:'#475569', fontSize:'9px', fontWeight:'600',
              background:'rgba(71,85,105,0.15)', padding:'2px 7px', borderRadius:'4px',
              border:'1px solid #1e293b' }}>
              Pipeline {pipelineVersion}
            </span>
          )}
          {calidadImagen && (
            <span style={{
              fontSize:'9px', fontWeight:'700', padding:'2px 7px', borderRadius:'4px',
              background: calidadImagen === 'adequate' ? 'rgba(34,197,94,0.1)' : calidadImagen === 'suboptimal' ? 'rgba(245,158,11,0.1)' : 'rgba(239,68,68,0.1)',
              color:       calidadImagen === 'adequate' ? '#4ade80'             : calidadImagen === 'suboptimal' ? '#fbbf24'              : '#f87171',
              border:      calidadImagen === 'adequate' ? '1px solid #4ade80'  : calidadImagen === 'suboptimal' ? '1px solid #fbbf24'   : '1px solid #f87171',
            }}>
              Imagen: {calidadImagen === 'adequate' ? 'óptima' : calidadImagen === 'suboptimal' ? 'suboptimal' : calidadImagen === 'not_analyzable' ? 'no analizable' : calidadImagen}
            </span>
          )}
          {auditoriaEstado && auditoriaEstado !== 'sin_auditoria' && (
            <span style={{
              fontSize:'9px', fontWeight:'700', padding:'2px 7px', borderRadius:'4px',
              background: auditoriaEstado === 'sin_hallazgos' ? 'rgba(34,197,94,0.1)' : auditoriaEstado === 'observaciones' ? 'rgba(245,158,11,0.1)' : auditoriaEstado === 'alerta' ? 'rgba(239,68,68,0.1)' : 'rgba(71,85,105,0.1)',
              color:       auditoriaEstado === 'sin_hallazgos' ? '#4ade80'              : auditoriaEstado === 'observaciones' ? '#fbbf24'              : auditoriaEstado === 'alerta' ? '#f87171'              : '#94a3b8',
              border:      auditoriaEstado === 'sin_hallazgos' ? '1px solid #4ade80'   : auditoriaEstado === 'observaciones' ? '1px solid #fbbf24'   : auditoriaEstado === 'alerta' ? '1px solid #f87171'   : '1px solid #475569',
            }}>
              Auditoría: {auditoriaEstado === 'sin_hallazgos' ? '✔ sin hallazgos' : auditoriaEstado === 'observaciones' ? '⚠ observaciones' : auditoriaEstado === 'alerta' ? '🚨 alerta' : auditoriaEstado}
            </span>
          )}
        </div>
      )}

      <div style={{ padding:'12px 14px', display:'flex', flexDirection:'column', gap:'10px' }}>
        {/* Technical AI Report — collapsible to save space */}
        <details open style={{ background:'#0d1420', border:'1px solid #1e3a5f', borderRadius:'8px', overflow:'hidden' }}>
          <summary style={{ padding:'10px 14px', cursor:'pointer', userSelect:'none',
            display:'flex', alignItems:'center', gap:'8px', listStyle:'none',
            background:'#0d1420', borderBottom:'1px solid #1e293b' }}>
            <span style={{ backgroundColor:'#1e3a5f', color:'#93c5fd', padding:'2px 8px', borderRadius:'4px', fontSize:'10px', fontWeight:'700' }}>
              ANALISIS TECNICO IA
            </span>
            <span style={{ color:'#475569', fontSize:'10px', flex:1 }}>solo para medico</span>
            <span style={{ color:'#334155', fontSize:'10px' }}>▾</span>
          </summary>
          <div style={{ padding:'12px 14px', maxHeight:'220px', overflowY:'auto' }}>
            {sections.map((s, i) => (
              <div key={i} style={{ marginBottom:'10px' }}>
                <div style={{ color:'#60a5fa', fontSize:'10px', fontWeight:'700', marginBottom:'3px', textTransform:'uppercase', letterSpacing:'0.3px' }}>
                  {s.title}
                </div>
                {s.lines.map((l, j) => (
                  <div key={j} style={{ color:'#94a3b8', fontSize:'11px', lineHeight:'1.6', paddingLeft:'10px' }}>{l}</div>
                ))}
              </div>
            ))}
            <div style={{ marginTop:'8px', padding:'8px 10px', backgroundColor:'#111827', borderRadius:'4px', border:'1px solid #1e293b' }}>
              <div style={{ color:'#64748b', fontSize:'9px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.3px' }}>Nota legal</div>
              <div style={{ color:'#475569', fontSize:'9px', marginTop:'3px', lineHeight:'1.5' }}>
                Analisis generado con asistencia de IA (IMAGIAX). Debe ser validado por un medico calificado. No constituye diagnostico definitivo.
              </div>
            </div>
          </div>
        </details>

        {/* V2 Experimental — NV-Reason-CXR-3B */}
        <details style={{ background:'#0a0f1a', border:`1px solid ${v2Status === 'online' ? '#1e293b' : '#1a150a'}`, borderRadius:'8px', overflow:'hidden' }}>
          <summary style={{ padding:'8px 12px', cursor:'pointer', userSelect:'none',
            display:'flex', alignItems:'center', gap:'8px', listStyle:'none' }}>
            <div style={{ width:'5px', height:'5px', borderRadius:'50%',
              background: v2Status === 'online' ? '#22c55e' : '#ef4444',
              boxShadow: v2Status === 'online' ? '0 0 4px #22c55e' : 'none' }} />
            <span style={{ color:'#475569', fontSize:'9px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.5px', flex:1 }}>
              NV-Reason-CXR-3B
            </span>
            <span style={{ color:'#334155', fontSize:'8px', textTransform:'uppercase' }}>Experimental</span>
          </summary>
          <div style={{ borderTop:'1px solid #1e293b' }}>
            {v2Status === 'offline' && (
              <div style={{ padding:'8px 12px', color:'#64748b', fontSize:'10px' }}>
                Servidor V2 no disponible (puerto 8011).
              </div>
            )}
            {v2Status === 'online' && !v2Loading && !v2Result && (
              <div style={{ padding:'8px 12px' }}>
                <button onClick={handleCompararV2}
                  style={{ background:'#1e293b', color:'#93c5fd', border:'1px solid #1e3a5f', borderRadius:'5px',
                    padding:'5px 12px', fontSize:'10px', fontWeight:'600', cursor:'pointer', width:'100%' }}>
                  Ejecutar comparacion
                </button>
              </div>
            )}
            {v2Loading && (
              <div style={{ padding:'10px 12px', display:'flex', alignItems:'center', gap:'6px' }}>
                <Spinner />
                <span style={{ color:'#3b82f6', fontSize:'10px' }}>Procesando (~60s)...</span>
              </div>
            )}
            {v2Error && (
              <div style={{ padding:'8px 12px', color:'#f87171', fontSize:'10px', background:'#450a0a' }}>
                {v2Error}
              </div>
            )}
            {v2Result && (
              <div style={{ padding:'10px 12px', display:'flex', flexDirection:'column', gap:'8px' }}>
                <div>
                  <div style={{ color:'#475569', fontSize:'9px', fontWeight:'700', textTransform:'uppercase', marginBottom:'3px' }}>
                    Hallazgos CXR-3B (EN)
                  </div>
                  <div style={{ color:'#93c5fd', fontSize:'11px', lineHeight:'1.5' }}>
                    {v2Result.v2_cxr3b.answer_en || 'No finding'}
                  </div>
                </div>
                <div>
                  <div style={{ display:'flex', justifyContent:'space-between', marginBottom:'3px' }}>
                    <span style={{ color:'#475569', fontSize:'9px', fontWeight:'700', textTransform:'uppercase' }}>
                      Informe V2
                    </span>
                    <span style={{ color:'#334155', fontSize:'9px' }}>
                      {v2Result.v2_cxr3b.latencia_inferencia_s + v2Result.v2_cxr3b.latencia_traduccion_s}s
                    </span>
                  </div>
                  <div style={{ background:'#111827', borderRadius:'4px', padding:'8px', maxHeight:'120px', overflowY:'auto' }}>
                    <pre style={{ color:'#cbd5e1', fontSize:'10px', lineHeight:'1.6', whiteSpace:'pre-wrap', margin:0, fontFamily:'inherit' }}>
                      {v2Result.v2_cxr3b.informe_es}
                    </pre>
                  </div>
                </div>
                <div style={{ display:'flex', gap:'8px', alignItems:'center' }}>
                  <span style={{ color:'#475569', fontSize:'9px', textTransform:'uppercase' }}>Triage:</span>
                  <span style={{
                    background: v2Result.v2_cxr3b.triage?.nivel_alerta === 'ALTA' ? '#dc2626' :
                                v2Result.v2_cxr3b.triage?.nivel_alerta === 'MEDIA' ? '#f59e0b' :
                                v2Result.v2_cxr3b.triage?.nivel_alerta === 'BAJA' ? '#3b82f6' : '#22c55e',
                    color: v2Result.v2_cxr3b.triage?.nivel_alerta === 'MEDIA' ? '#000' : '#fff',
                    padding:'1px 7px', borderRadius:'3px', fontSize:'10px', fontWeight:'700' }}>
                    {v2Result.v2_cxr3b.triage?.nivel_alerta ?? '—'}
                  </span>
                  <span style={{ color:'#334155', fontSize:'9px' }}>
                    {v2Result.latencia_total_comparacion_s}s
                  </span>
                </div>
                <button onClick={() => { setV2Result(null); setV2Error(''); }}
                  style={{ background:'transparent', color:'#334155', border:'1px solid #1e293b', borderRadius:'4px',
                    padding:'4px', fontSize:'9px', cursor:'pointer', alignSelf:'flex-end' }}>
                  Cerrar
                </button>
              </div>
            )}
          </div>
        </details>

        {/* Divider — Step 2 */}
        <div style={{ display:'flex', alignItems:'center', gap:'8px', margin:'2px 0' }}>
          <div style={{ flex:1, height:'1px', background:'#1e293b' }} />
          <span style={{ color:'#334155', fontSize:'9px', whiteSpace:'nowrap', textTransform:'uppercase', letterSpacing:'0.5px' }}>Informe para el paciente</span>
          <div style={{ flex:1, height:'1px', background:'#1e293b' }} />
        </div>

        {editingReport ? (
          <SectionEditor
            sections={editSections}
            onChange={setEditSections}
            onSave={handleGuardarEdicion}
            onCancel={() => setEditingReport(false)}
          />
        ) : (
          <ScaledPreview sec={sections} pid={informeId} qr={qrDataUrl} pat={patient} nota={notaClinica} containerWidth={panelWidth} />
        )}

        {/* Action buttons — prominent and clear */}
        {!pdfConfirmed ? (
          <div style={{ display:'flex', gap:'6px' }}>
            <button onClick={() => setPdfConfirmed(true)}
              style={{ flex:1, background:'linear-gradient(135deg,#1e40af,#2563eb)', color:'white', border:'none',
                borderRadius:'6px', padding:'11px', fontSize:'12px', fontWeight:'700', cursor:'pointer',
                display:'flex', alignItems:'center', justifyContent:'center', gap:'6px' }}>
              Aceptar informe
            </button>
            <button onClick={handleEditar} title="Editar contenido"
              style={{ background:'#111827', color:'#93c5fd', border:'1px solid #1e3a5f', borderRadius:'6px',
                padding:'11px 14px', fontSize:'12px', cursor:'pointer', fontWeight:'600' }}>
              Editar
            </button>
            <button onClick={handleRechazar} title="Rechazar informe"
              style={{ background:'#111827', color:'#64748b', border:'1px solid #1e293b', borderRadius:'6px',
                padding:'11px 14px', fontSize:'11px', cursor:'pointer' }}>
              x
            </button>
          </div>
        ) : (
          <div style={{ background:'#0d1420', border:'1px solid #1e3a5f', borderRadius:'8px', padding:'14px', display:'flex', flexDirection:'column', gap:'12px' }}>
            <div style={{ color:'#f59e0b', fontSize:'11px', fontWeight:'700' }}>Confirmacion definitiva</div>
            <label style={{ display:'flex', gap:'10px', alignItems:'flex-start', cursor:'pointer' }}>
              <input type="checkbox" checked={legalAccepted} onChange={e => setLegalAccepted(e.target.checked)}
                style={{ marginTop:'3px', flexShrink:0, width:'14px', height:'14px', cursor:'pointer' }} />
              <span style={{ color:'#94a3b8', fontSize:'11px', lineHeight:'1.6' }}>
                Declaro haber revisado y verificado el contenido de este informe y asumo la responsabilidad medica correspondiente antes de su entrega al paciente.
              </span>
            </label>
            {pdfError && <div style={{ color:'#f87171', fontSize:'11px', background:'#450a0a', borderRadius:'4px', padding:'8px 10px' }}>{pdfError}</div>}
            <div style={{ display:'flex', gap:'8px' }}>
              <button
                onClick={legalAccepted && !isGeneratingPdf ? handleConfirmar : undefined}
                disabled={!legalAccepted || isGeneratingPdf}
                style={{ flex:1, background: legalAccepted && !isGeneratingPdf ? '#166534' : '#1e293b',
                  color: legalAccepted && !isGeneratingPdf ? '#86efac' : '#475569',
                  border:'none', borderRadius:'6px', padding:'11px', fontSize:'12px', fontWeight:'700',
                  cursor: legalAccepted && !isGeneratingPdf ? 'pointer' : 'not-allowed',
                  display:'flex', alignItems:'center', justifyContent:'center', gap:'8px', transition:'all 0.2s' }}>
                {isGeneratingPdf
                  ? (<><Spinner />Generando PDF...</>)
                  : 'Confirmar definitivamente'}
              </button>
              <button onClick={() => { setPdfConfirmed(false); setLegalAccepted(false); setPdfError(''); }}
                style={{ background:'#111827', color:'#64748b', border:'1px solid #1e293b', borderRadius:'6px', padding:'11px 14px', fontSize:'12px', cursor:'pointer' }}>
                Volver
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );

  const renderGuardado = () => (
    <div style={{ display:'flex', flexDirection:'column', gap:'0' }}>
      {/* Success banner */}
      <div style={{ background:'#052e16', borderBottom:'2px solid #22c55e', padding:'12px 14px',
        display:'flex', alignItems:'center', gap:'10px' }}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ade80" strokeWidth="2.5">
          <path d="M20 6L9 17l-5-5" />
        </svg>
        <div>
          <div style={{ color:'#86efac', fontWeight:'700', fontSize:'12px' }}>Informe validado y guardado</div>
          <div style={{ color:'#4ade80', fontSize:'9px', marginTop:'1px', opacity:0.8 }}>ID: {informeId} · {formatDate(new Date())}</div>
        </div>
      </div>

      <div style={{ padding:'12px 14px', display:'flex', flexDirection:'column', gap:'10px' }}>
        <ScaledPreview sec={sections} pid={informeId} qr={qrDataUrl} pat={patient} nota={notaClinica} containerWidth={panelWidth} />
        {pdfError && <div style={{ color:'#f87171', fontSize:'11px', background:'#450a0a', borderRadius:'4px', padding:'8px 10px' }}>{pdfError}</div>}
        {!pdfUrl ? (
          <button onClick={handleGeneratePdf} disabled={isGeneratingPdf}
            style={{ background: isGeneratingPdf ? '#1e293b' : 'linear-gradient(135deg,#1e40af,#3b82f6)',
              color:'white', border:'none', borderRadius:'6px', padding:'12px', fontSize:'13px', fontWeight:'700',
              cursor: isGeneratingPdf ? 'not-allowed' : 'pointer',
              display:'flex', alignItems:'center', justifyContent:'center', gap:'8px' }}>
            {isGeneratingPdf ? (<><Spinner />Generando PDF...</>) : 'Generar y descargar PDF'}
          </button>
        ) : (
          <a href={pdfUrl} download={`${informeId}.pdf`}
            style={{ background:'linear-gradient(135deg,#166534,#16a34a)', color:'#86efac', borderRadius:'6px',
              padding:'12px', fontSize:'13px', fontWeight:'700', textDecoration:'none',
              display:'flex', alignItems:'center', justifyContent:'center', gap:'8px' }}>
            Descargar PDF
          </a>
        )}
        <button onClick={handleReset}
          style={{ background:'transparent', color:'#475569', border:'1px solid #1e293b', borderRadius:'6px',
            padding:'9px', fontSize:'11px', cursor:'pointer' }}>
          Nuevo analisis
        </button>
      </div>
    </div>
  );

  const renderRechazado = () => (
    <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:'16px', padding:'48px 24px', textAlign:'center' }}>
      <div style={{ width:'44px', height:'44px', borderRadius:'50%', background:'#1e293b', border:'2px solid #334155',
        display:'flex', alignItems:'center', justifyContent:'center' }}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="2">
          <path d="M18 6L6 18M6 6l12 12" />
        </svg>
      </div>
      <div>
        <div style={{ color:'#94a3b8', fontWeight:'700', fontSize:'13px' }}>Informe descartado</div>
        <div style={{ color:'#334155', fontSize:'11px', marginTop:'4px' }}>El analisis fue rechazado por el medico</div>
      </div>
      <button onClick={handleReset}
        style={{ background:'#111827', color:'#94a3b8', border:'1px solid #1e293b', borderRadius:'6px', padding:'9px 28px', fontSize:'12px', cursor:'pointer' }}>
        Nuevo analisis
      </button>
    </div>
  );

  const renderHistorial = () => (
    <div style={{ padding:'12px', display:'flex', flexDirection:'column', gap:'10px' }}>
      {/* Floating modal — renders over the viewport when viewing a report */}
      {viewingEntry && (
        <ReportViewerModal
          entry={viewingEntry}
          onClose={() => setViewingEntry(null)}
          onDownloadPdf={() => handleDownloadHistoryPdf(viewingEntry)}
        />
      )}

      {/* Header con contador y botón de actualizar */}
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
        <div style={{ color:'#64748b', fontSize:'10px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.5px' }}>
          {historialLoading
            ? 'Cargando…'
            : `${history.length} informe${history.length !== 1 ? 's' : ''} registrado${history.length !== 1 ? 's' : ''}`}
        </div>
        <button
          onClick={fetchHistorialFromBackend}
          disabled={historialLoading}
          title="Actualizar historial"
          style={{ background:'transparent', border:'1px solid #1e3a5f', borderRadius:'5px',
            color: historialLoading ? '#334155' : '#3b82f6', padding:'3px 8px', fontSize:'10px',
            cursor: historialLoading ? 'default' : 'pointer', display:'flex', alignItems:'center', gap:'4px' }}>
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            style={{ animation: historialLoading ? 'spin 1s linear infinite' : 'none' }}>
            <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/>
          </svg>
          {historialLoading ? 'Cargando' : 'Actualizar'}
        </button>
      </div>

      {/* Error state */}
      {historialError && (
        <div style={{ background:'#1a0a0a', border:'1px solid #7f1d1d', borderRadius:'6px',
          padding:'10px 12px', color:'#fca5a5', fontSize:'11px' }}>
          {historialError}
        </div>
      )}

      {/* Loading skeleton */}
      {historialLoading && history.length === 0 && (
        <div style={{ display:'flex', flexDirection:'column', gap:'8px' }}>
          {[1,2,3].map(i => (
            <div key={i} style={{ background:'#0d1420', border:'1px solid #1e3a5f', borderRadius:'8px',
              padding:'12px 14px', height:'64px', opacity: 0.5 + i * 0.15 }} />
          ))}
        </div>
      )}

      {/* Empty state — solo si no está cargando y no hay error */}
      {!historialLoading && !historialError && history.length === 0 && (
        <div style={{ textAlign:'center', padding:'48px 16px', color:'#475569', fontSize:'12px' }}>
          <div style={{ color:'#64748b', fontWeight:'600', marginBottom:'4px' }}>Sin informes registrados</div>
          <div style={{ color:'#334155', fontSize:'11px' }}>Los informes confirmados aparecerán aquí.</div>
        </div>
      )}

      {/* Lista de informes — siempre visible */}
      {!historialLoading && history.length > 0 && (
        history.map(e => (
          <HistoryCard key={e.informeId} entry={e} onView={() => setViewingEntry(e)} onDownloadPdf={() => handleDownloadHistoryPdf(e)} />
        ))
      )}
    </div>
  );

  // ── Render ────────────────────────────────────────────────────────────────

  // Loading gate — wait for session check
  if (authLoading) {
    return (
      <div style={{ padding: '20px', textAlign: 'center', color: '#aaa' }}>
        Verificando sesión...
      </div>
    );
  }

  // Auth gate — redirect to /login when session is expired or absent
  if (!authUser) {
    return (
      <div style={{ height:'100%', background:'#0a0f1a', color:'#e2e8f0', display:'flex', alignItems:'center', justifyContent:'center', padding:'20px' }}>
        <div style={{ width:'100%', maxWidth:'360px', background:'#0d1420', border:'1px solid #1e3a5f', borderRadius:'10px', padding:'18px', display:'flex', flexDirection:'column', gap:'10px' }}>
          <div style={{ color:'#f1f5f9', fontSize:'14px', fontWeight:'700' }}>Ingreso médico IMAGIAX</div>
          <div style={{ color:'#64748b', fontSize:'11px' }}>Iniciá sesión para continuar con el análisis.</div>

          <input
            type="email"
            value={loginEmail}
            onChange={e => setLoginEmail(e.target.value)}
            placeholder="Email"
            autoComplete="username"
            style={{ background:'#111827', color:'#e2e8f0', border:'1px solid #1e293b', borderRadius:'6px', padding:'9px 10px', fontSize:'12px', outline:'none' }}
          />

          <div style={{ position:'relative' }}>
            <input
              type={showPassword ? 'text' : 'password'}
              value={loginPassword}
              onChange={e => setLoginPassword(e.target.value)}
              placeholder="Contraseña"
              autoComplete="current-password"
              onKeyDown={e => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  handleLogin();
                }
              }}
              style={{ width:'100%', boxSizing:'border-box', background:'#111827', color:'#e2e8f0', border:'1px solid #1e293b', borderRadius:'6px', padding:'9px 36px 9px 10px', fontSize:'12px', outline:'none' }}
            />
            <button
              type="button"
              onClick={() => setShowPassword(v => !v)}
              title={showPassword ? 'Ocultar contraseña' : 'Mostrar contraseña'}
              style={{ position:'absolute', right:'6px', top:'50%', transform:'translateY(-50%)', background:'transparent', border:'none', color:'#64748b', cursor:'pointer', fontSize:'12px' }}
            >
              {showPassword ? 'Ocultar' : 'Ver'}
            </button>
          </div>

          {loginError && (
            <div style={{ background:'#450a0a', border:'1px solid #7f1d1d', borderRadius:'6px', padding:'8px 10px', color:'#fca5a5', fontSize:'11px', lineHeight:'1.45' }}>
              {loginError}
            </div>
          )}

          <button
            onClick={handleLogin}
            disabled={loginLoading || !loginEmail || !loginPassword}
            style={{ background: loginLoading ? '#1e293b' : 'linear-gradient(135deg,#1e40af,#3b82f6)', color: loginLoading ? '#64748b' : 'white', border:'none', borderRadius:'6px', padding:'10px', fontSize:'12px', fontWeight:'700', cursor: loginLoading ? 'not-allowed' : 'pointer' }}
          >
            {loginLoading ? 'Ingresando...' : 'Iniciar sesión'}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div ref={panelRef} style={{ height:'100%', overflowY:'auto', background:'#0a0f1a', color:'#e2e8f0' }}>
      {/* Header */}
      <div style={{ background:'linear-gradient(135deg,#0d1b2a,#1a2744)', padding:'10px 16px',
        borderBottom:'1px solid #1e3a5f', display:'flex', alignItems:'center', gap:'10px',
        position:'sticky', top:0, zIndex:10 }}>
        <div style={{ width:'28px', height:'28px', borderRadius:'6px', background:'linear-gradient(135deg,#1e3a8a,#3b82f6)', display:'flex', alignItems:'center', justifyContent:'center', flexShrink:0 }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
            <path d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2v-4M9 21H5a2 2 0 01-2-2v-4m0 0h18" />
          </svg>
        </div>
        <div style={{ flex:1 }}>
          <div style={{ color:'#f1f5f9', fontWeight:'700', fontSize:'13px', letterSpacing:'0.3px' }}>IMAGIAX AI</div>
          <div style={{ color:'#475569', fontSize:'9.5px' }}>
            {state === 'idle' && (apiStatus === 'online' ? (studyUID ? 'Listo para analizar' : 'Esperando estudio DICOM') : 'Sin conexion')}
            {state === 'analizando' && 'Procesando...'}
            {state === 'revisar' && 'Revision pendiente'}
            {state === 'guardado' && 'Informe guardado'}
            {state === 'rechazado' && 'Informe rechazado'}
          </div>
        </div>
        {/* User badge + admin link + logout */}
        <div style={{ display:'flex', alignItems:'center', gap:'6px' }}>
          {history.length > 0 && (
            <div style={{ background:'#1e40af', color:'#93c5fd', borderRadius:'10px', padding:'1px 7px', fontSize:'10px', fontWeight:'700' }}>
              {history.length}
            </div>
          )}
          {/* Botón panel admin — visible solo para admin/superadmin */}
          {(authUser.rol === 'admin' || authUser.rol === 'superadmin') && (
            <a href={`${IMAGIAX_BACKEND}/admin`} target="_blank" rel="noopener noreferrer"
              title="Abrir panel de administración"
              style={{ background:'transparent', color:'#475569', border:'none', cursor:'pointer',
                padding:'2px', display:'flex', alignItems:'center', transition:'color 0.15s',
                textDecoration:'none' }}
              onMouseEnter={e => { (e.currentTarget as HTMLAnchorElement).style.color = '#a78bfa'; }}
              onMouseLeave={e => { (e.currentTarget as HTMLAnchorElement).style.color = '#475569'; }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>
                <rect x="3" y="14" width="7" height="7" rx="1"/><line x1="14" y1="17.5" x2="21" y2="17.5"/>
                <line x1="17.5" y1="14" x2="17.5" y2="21"/>
              </svg>
            </a>
          )}
          <button onClick={handleLogout} title={`${authUser.nombre} — Cerrar sesion`}
            style={{ background:'transparent', color:'#475569', border:'none', cursor:'pointer', padding:'2px',
              display:'flex', alignItems:'center', transition:'color 0.15s' }}
            onMouseEnter={e => { (e.currentTarget as HTMLElement).style.color = '#f87171'; }}
            onMouseLeave={e => { (e.currentTarget as HTMLElement).style.color = '#475569'; }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
            </svg>
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display:'flex', borderBottom:'1px solid #1e3a5f', background:'#0d1420' }}>
        {(['analisis', 'historial'] as ActiveTab[]).map(tab => (
          <button key={tab} onClick={() => setActiveTab(tab)}
            style={{ flex:1, padding:'9px 8px', background:'transparent', border:'none',
              borderBottom: activeTab === tab ? '2px solid #3b82f6' : '2px solid transparent',
              color: activeTab === tab ? '#93c5fd' : '#475569',
              fontSize:'11.5px', fontWeight: activeTab === tab ? '700' : '400',
              cursor:'pointer', transition:'all 0.15s' }}>
            {tab === 'analisis' ? 'Análisis' : `Historial${history.length > 0 ? ` (${history.length})` : ''}`}
          </button>
        ))}
      </div>

      {/* Contenido */}
      {activeTab === 'analisis' && (
        <>
          {state === 'idle'      && renderIdle()}
          {state === 'analizando'&& renderAnalizando()}
          {state === 'revisar'   && renderRevisar()}
          {state === 'guardado'  && renderGuardado()}
          {state === 'rechazado' && renderRechazado()}
        </>
      )}
      {activeTab === 'historial' && renderHistorial()}

      <style>{`
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes modalFadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes modalSlideIn { from { opacity: 0; transform: scale(0.94) translateY(12px); } to { opacity: 1; transform: scale(1) translateY(0); } }
      `}</style>
    </div>
  );
};

// ── Spinner inline ─────────────────────────────────────────────────────────────
const Spinner: React.FC = () => (
  <span style={{ display:'inline-block', width:'13px', height:'13px',
    border:'2px solid rgba(255,255,255,0.25)', borderTopColor:'white',
    borderRadius:'50%', animation:'spin 0.7s linear infinite' }} />
);

export default ImagiaxAiPanel;

import React, { useState, useEffect, useCallback, useMemo, useRef, memo } from 'react';
import QRCode from 'qrcode';
import { useActiveViewportDisplaySets, utils } from '@ohif/core';
import { LOGO_IMAGIAX_SVG } from './logoBase64';
import {
  generarInforme,
  getEstadoGeneracionInforme,
  guardarDecision,
  downloadInformePdf,
  checkSalud,
  checkSaludV2,
  checkEstadoCreditos,
  compararV1V2,
  getMetodoPago,
  borrarMetodoPago,
  getCurrentUser,
  logoutApi,
  listadoInformes,
  type AIResultados,
  type GenerarInformeResponse,
  type GenerarInformeResult,
  type InformeDB,
  type V2CompararResponse,
  type EstadoCreditosResponse,
  type MetodoPagoResponse,
  type AuthUser,
  ImagiaxApiError,
  ImagiaxTimeoutError,
  isGenerarInformeAcceptedResponse,
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
const MAX_HISTORY_ENTRIES = 100;
const ACTIVE_ANALYSIS_JOB_KEY = 'imagiax_active_analysis_job_v1';
const ANALYSIS_MAX_POLL_MS = 10 * 60 * 1000;
const ANALYSIS_POLL_DELAYS_MS = [2000, 2500, 3000, 5000, 5000, 8000, 10000] as const;
const HISTORY_PAGE_SIZE = 25;

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
    bg: [235, 244, 255] as [number,number,number],   // v52.2: azul celeste suave — matches pdf_service.py _CONCLUSION_BG
    border: [147, 197, 253] as [number,number,number], // v52.2: azul celeste borde — matches pdf_service.py _CONCLUSION_BDR
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

// Caché en memoria para evitar regenerar/descargar el mismo PDF varias veces
// durante la sesión activa del visor. Se mantiene pequeño para no crecer sin control.
const PDF_BLOB_CACHE = new Map<string, Blob>();
const PDF_BLOB_CACHE_LIMIT = 12;

function getCachedPdfBlob(informeId: string): Blob | null {
  return PDF_BLOB_CACHE.get(informeId) || null;
}

function cachePdfBlob(informeId: string, blob: Blob): void {
  if (PDF_BLOB_CACHE.has(informeId)) {
    PDF_BLOB_CACHE.delete(informeId);
  }
  PDF_BLOB_CACHE.set(informeId, blob);
  if (PDF_BLOB_CACHE.size > PDF_BLOB_CACHE_LIMIT) {
    const oldestKey = PDF_BLOB_CACHE.keys().next().value;
    if (oldestKey) PDF_BLOB_CACHE.delete(oldestKey);
  }
}

interface ActiveAnalysisJob {
  jobId: string;
  studyUID: string;
  userId: string | null;
  startedAt: string;
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function formatDate(d: Date): string {
  return d.toLocaleDateString('es-CL', { day: '2-digit', month: 'long', year: 'numeric' });
}

function sanitizePatientDisplayName(name: string | null | undefined): string {
  if (!name) return 'No consignado';
  const raw = String(name).trim();
  if (!raw) return 'No consignado';

  const normalized = raw.replace(/\^+/g, ' ').replace(/\s+/g, ' ').trim();
  if (!normalized) return 'No consignado';

  const parts = normalized.split(' ');
  return parts.map(part => {
    if (!part) return part;
    return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
  }).join(' ');
}

function formatClinicalDateShort(raw: string | null | undefined): string {
  if (!raw) return '-';
  const value = String(raw).trim();
  if (/^\d{2}-\d{2}-\d{4}$/.test(value)) return value;
  if (/^\d{8}$/.test(value)) return `${value.slice(6, 8)}-${value.slice(4, 6)}-${value.slice(0, 4)}`;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${value.slice(8, 10)}-${value.slice(5, 7)}-${value.slice(0, 4)}`;
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(value)) return value.replace(/\//g, '-');
  const parsed = new Date(value);
  if (!isNaN(parsed.getTime())) {
    return `${String(parsed.getDate()).padStart(2, '0')}-${String(parsed.getMonth() + 1).padStart(2, '0')}-${parsed.getFullYear()}`;
  }
  return value;
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function readActiveAnalysisJob(): ActiveAnalysisJob | null {
  try {
    const raw = localStorage.getItem(ACTIVE_ANALYSIS_JOB_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ActiveAnalysisJob;
    if (!parsed?.jobId || !parsed?.studyUID) return null;
    return parsed;
  } catch {
    return null;
  }
}

function persistActiveAnalysisJob(job: ActiveAnalysisJob | null): void {
  try {
    if (!job) {
      localStorage.removeItem(ACTIVE_ANALYSIS_JOB_KEY);
      return;
    }
    localStorage.setItem(ACTIVE_ANALYSIS_JOB_KEY, JSON.stringify(job));
  } catch {
    // ignore storage errors
  }
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
  if (!v || !v.trim()) return 'No consignada';
  const trimmed = v.trim().toUpperCase();
  // DICOM Age String format: nnnD | nnnW | nnnM | nnnY
  const dicomAS = trimmed.match(/^(\d{1,4})(D|W|M|Y)$/);
  if (dicomAS) {
    const num = parseInt(dicomAS[1], 10);
    switch (dicomAS[2]) {
      case 'Y': return num === 0 ? 'No consignada' : `${num} año${num !== 1 ? 's' : ''}`;
      case 'M': return num === 0 ? 'No consignada' : `${num} mes${num !== 1 ? 'es' : ''}`;
      case 'W': return `${num} semana${num !== 1 ? 's' : ''}`;
      case 'D': return `${num} día${num !== 1 ? 's' : ''}`;
    }
  }
  // Birth year (e.g. "1959") — compute age from current year
  const n = parseInt(trimmed, 10);
  if (!isNaN(n) && n > 1900 && n < 2100) { const a = new Date().getFullYear() - n; return a === 0 ? 'No consignada' : `${a} año${a !== 1 ? 's' : ''}`; }
  // Plain number (e.g. "65") — assume years
  if (!isNaN(n) && n >= 0 && n < 200) return n === 0 ? 'No consignada' : `${n} año${n !== 1 ? 's' : ''}`;
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
  if (!line) return '';
  return line.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toUpperCase().replace(/:$/, '').trim();
}

/** Limpia artefactos markdown que Gemini puede incluir en su respuesta */
function stripMarkdown(text: string): string {
  if (!text) return '';
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
  if (!text) return '';
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
    (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

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
  if (!raw) return [];
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
      nombre:           sanitizePatientDisplayName(inf.patient_name),
      edad:             inf.patient_age || '',   // from DB (BUG-010)
      fechaExamen,
      medicoReferencia: '',   // Not in DB
      tipoExamen:       isDicomEmpty(inf.modalidad)
                          ? 'Estudio Radiológico'
                          : (MODALITY_MAP[(inf.modalidad || '').toUpperCase()] || inf.modalidad),
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
 * Returns true when a DICOM string value is semantically empty:
 * null, undefined, empty, whitespace-only, or a literal "null"/"none"/"unknown"
 * that PACS/Orthanc may inject when the real tag is absent.
 */
function isDicomEmpty(v: string | null | undefined): boolean {
  if (v == null) return true;
  const t = v.trim().toUpperCase();
  return t === '' || t === 'NULL' || t === 'NONE' || t === 'UNKNOWN' || t === 'UNDEFINED';
}

/**
 * Genera una descripción legible del estudio a partir de los datos DICOM disponibles.
 * Prioridad: StudyDescription → Modality + BodyPart → Modality → BodyPart → fallback
 * Applies fixDicomEncoding() to studyDesc and bodyPart before mapping.
 * Sanitizes null-like strings ("null", "none", "unknown") before processing.
 */
function getStudyDescription(
  studyDesc: string | null,
  bodyPart: string | null,
  modality: string | null
): string {
  const safeDesc = isDicomEmpty(studyDesc) ? null : studyDesc;
  const safePart = isDicomEmpty(bodyPart) ? null : bodyPart;
  const fixedDesc = safeDesc ? fixDicomEncoding(safeDesc) : null;
  const fixedPart = safePart ? fixDicomEncoding(safePart) : null;
  if (fixedDesc && fixedDesc !== 'DX' && fixedDesc.length > 2) {
    return fixedDesc;
  }
  const mod = MODALITY_MAP[(modality || '').toUpperCase()] || modality || '';
  const part = fixedPart ? (BODY_PART_MAP[fixedPart.toUpperCase()] || fixedPart) : '';
  if (mod && part) return `${mod} de ${part}`;
  if (mod) return mod;
  if (part) return part;
  return 'Estudio de Imagen';
}

// ── MedicalReportDocument ─────────────────────────────────────────────────────
interface DocProps {
  patient: PatientInfo; sections: ReportSection[];
  informeId: string; qrDataUrl: string;
  notaClinica?: string;
}
const CLEAN_FILTER = ['NOTA LEGAL','DISCLAIMER','PROBABILIDAD','SOLO USO INTERNO','SÓLO USO INTERNO','NIVEL DE ALERTA','ANÁLISIS COMPUTACIONAL','ANÁLISIS IA','MODELO','INTELIGENCIA ARTIFICIAL'];

/** Detect conclusion-like sections for highlighted rendering. Pure UI utility — no PDF dependency. */
function isConclusionSection(title: string): boolean {
  return /CONCLUSI[OÓ]N/i.test(title);
}

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
  const fechaExFmtPreview = formatClinicalDateShort(patient.fechaExamen || '');
  const now = new Date();
  const fechaInfFmtPreview = `${formatClinicalDateShort(now.toISOString())} — ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')} hs`;
  const displayName = sanitizePatientDisplayName(patient.nombre);
  const examTitle = patient.tipoExamen || 'Informe Radiológico';
  const reportLabel = 'Informe validado - documento clínico';

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
        fontFamily:'"Inter","Helvetica Neue",Helvetica,sans-serif',
        fontSize: pdfPx(PDF_THEME.body.size), color: pdfRgb(PDF_THEME.body.color),
        padding:'28px 42px 82px', boxSizing:'border-box', position:'relative', lineHeight:'1.55' }}>

      {/* Cabecera: Logo+Marca  |  Informe ID + fechas */}
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
        borderBottom: '1px solid #d8e1ec',
        paddingBottom:'14px', marginBottom:'18px' }}>
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
        border: `1px solid ${pdfRgb(PDF_THEME.section.separatorColor)}`,
        borderRadius: '10px',
        boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.85)',
        padding: '14px 18px',
      }}>
        {/* Two-column row */}
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:'4px 16px', marginBottom:'8px' }}>
          {/* Left column */}
          <div>
            <div><span style={PREVIEW_LABEL_STYLE}>Paciente: </span>
              <span style={{ ...PREVIEW_VALUE_STYLE, textTransform:'uppercase' }}>{displayName}</span></div>
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

      <div style={{ fontWeight:'700', color: pdfRgb(PDF_THEME.section.color), fontSize: pdfPx(PDF_THEME.section.size), marginBottom:'10px' }}>
        {reportLabel}
      </div>

      {/* Título del examen — centrado, subrayado real */}
      <div style={{ textAlign:'center', marginBottom:'28px' }}>
        <div style={{ fontSize: pdfPx(PDF_THEME.header.examTitleSize),
          fontWeight:'700', textTransform:'uppercase', display:'inline-block',
          borderBottom:`2px solid ${pdfRgb(PDF_THEME.header.examTitleColor)}`,
          paddingBottom:'3px', letterSpacing:'0.6px' }}>
          {examTitle}
        </div>
      </div>

      {/* Hallazgos — always first */}
      <div style={{ marginBottom: `${PDF_THEME.spacing.sectionGap}mm` }}>
        <div style={PREVIEW_TITLE_STYLE}>Hallazgos:</div>
        {renderPreviewLines((hallazgos || []).map(l => stripMarkdown(l || '')).filter(Boolean))}
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
      <div style={{ marginTop:'18px', fontSize: pdfPx(PDF_THEME.body.size),
        color: pdfRgb(PDF_THEME.body.color) }}>Saluda Atentamente,</div>

      {/* Firma + QR */}
      <div style={{ marginTop:'24px', display:'flex', justifyContent:'space-between', alignItems:'flex-end' }}>
        <div style={{ textAlign:'center' }}>
          <div style={{ borderTop:`1.5px solid ${pdfRgb(PDF_THEME.signature.lineColor)}`,
            width:'220px', paddingTop:'6px',
            fontSize: pdfPx(PDF_THEME.signature.detailSize) }}>
            <div style={{ fontSize: pdfPx(PDF_THEME.confidentiality.size), color: pdfRgb(PDF_THEME.confidentiality.color), marginBottom:'2px' }}>
              Firmado electrónicamente por:
            </div>
            <div style={{ fontWeight:'700', fontSize: pdfPx(PDF_THEME.signature.nameSize) }}>{DOCTOR_NAME}</div>
            <div style={{ color: pdfRgb(PDF_THEME.footer.color), marginTop:'2px' }}>{DOCTOR_SPECIALTY}</div>
            {formatLicenseDisplay(DOCTOR_LICENSE) && (
              <div style={{ color: pdfRgb(PDF_THEME.footer.color) }}>
                Lic. N° {formatLicenseDisplay(DOCTOR_LICENSE)}
              </div>
            )}
            <div style={{ fontSize: pdfPx(PDF_THEME.confidentiality.size), color: pdfRgb(PDF_THEME.confidentiality.color), marginTop:'4px' }}>
              {now.toISOString().replace('.000', '')}
            </div>
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

const ReportViewerModal: React.FC<ReportViewerModalProps> = ({ entry, onClose, onDownloadPdf }) => {
  const displayName = sanitizePatientDisplayName(entry.patient.nombre);
  const [pdfObjectUrl, setPdfObjectUrl] = useState('');
  const [pdfLoading, setPdfLoading] = useState(true);
  const [pdfError, setPdfError] = useState('');

  // Close on Escape key only.
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

  useEffect(() => {
    let active = true;
    let objectUrl = '';

    const loadPdf = async () => {
      setPdfLoading(true);
      setPdfError('');
      try {
        const blob = getCachedPdfBlob(entry.informeId) || await downloadInformePdf(entry.informeId);
        if (!active) return;
        cachePdfBlob(entry.informeId, blob);
        objectUrl = URL.createObjectURL(blob);
        setPdfObjectUrl(objectUrl);
      } catch (err) {
        if (!active) return;
        if (err instanceof ImagiaxApiError) {
          setPdfError(`No se pudo abrir el PDF (${err.status}).`);
        } else {
          setPdfError('No se pudo cargar la vista previa del PDF.');
        }
      } finally {
        if (active) setPdfLoading(false);
      }
    };

    loadPdf();

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [entry.informeId]);

  const handleOpenPdf = useCallback(() => {
    if (!pdfObjectUrl) return;
    window.open(pdfObjectUrl, '_blank', 'noopener,noreferrer');
  }, [pdfObjectUrl]);

  const handleDownloadCurrentPdf = useCallback(() => {
    if (!pdfObjectUrl) {
      onDownloadPdf();
      return;
    }
    const a = document.createElement('a');
    a.href = pdfObjectUrl;
    a.download = `${entry.informeId}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }, [entry.informeId, onDownloadPdf, pdfObjectUrl]);

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
              {displayName}
            </div>
            <div style={{ color: '#64748b', fontSize: '11px', marginTop: '3px',
              display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span>{entry.fecha}</span>
            </div>
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

        {/* Body — authoritative PDF preview */}
        <div
          style={{
            flex: 1, overflow: 'auto', padding: '20px',
            display: 'flex', justifyContent: 'center', alignItems: 'stretch',
            background: '#080c14',
          }}
        >
          {pdfLoading && (
            <div style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#93c5fd', fontSize: '12px', gap: '8px' }}>
              <Spinner />
              Generando vista previa del PDF final...
            </div>
          )}

          {!pdfLoading && pdfError && (
            <div style={{ width: '100%', maxWidth: '720px', background:'#111827', border:'1px solid #7f1d1d', borderRadius:'10px', padding:'18px', color:'#fca5a5', fontSize:'12px', lineHeight:'1.6' }}>
              {pdfError}
              <div style={{ marginTop:'10px', color:'#94a3b8' }}>
                Podés descargar el PDF directamente para revisarlo fuera del visor.
              </div>
            </div>
          )}

          {!pdfLoading && !pdfError && pdfObjectUrl && (
            <iframe
              title={`PDF ${entry.informeId}`}
              src={pdfObjectUrl}
              style={{ width:'100%', height:'100%', minHeight:'70vh', border:'1px solid #1e293b', borderRadius:'8px', background:'#fff' }}
            />
          )}
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
            Estás viendo el mismo PDF final que se descarga desde IMAGIAX.
          </div>
          <div style={{ display: 'flex', gap: '8px' }}>
            {pdfObjectUrl && (
              <button
                onClick={handleOpenPdf}
                style={{
                  background: 'transparent', color: '#93c5fd', border: '1px solid #1e3a5f',
                  borderRadius: '6px', padding: '8px 16px', fontSize: '12px', cursor: 'pointer',
                }}>
                Abrir PDF
              </button>
            )}
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
                onClick={handleDownloadCurrentPdf}
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
  const [authUser, setAuthUser] = useState<AuthUser | null>(null);
  const [authLoading, setAuthLoading] = useState<boolean>(true);

  useEffect(() => {
    getCurrentUser()
      .then(user => setAuthUser(user))
      .catch(() => setAuthUser(null))
      .finally(() => setAuthLoading(false));
  }, []);

  const handleLogout = useCallback(async () => {
    try {
      await logoutApi();
    } finally {
      setAuthUser(null);
    }
  }, []);

  // ── PERF-H6: Abort controller ref (cancels in-flight fetch on unmount) ───
  const abortRef = useRef<AbortController | null>(null);
  // Stores milestone setTimeout IDs so they can be cleared on unmount
  const milestoneTimersRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  const isMountedRef = useRef(true);
  const pollingJobRef = useRef<string | null>(null);

  // ── RESP-C5: Panel width tracking for responsive ScaledPreview ───────────
  const panelRef = useRef<HTMLDivElement>(null);
  const [panelWidth, setPanelWidth] = useState(400);

  // Cleanup: abort any in-flight analysis fetch and clear milestone timers
  useEffect(() => {
    return () => {
      isMountedRef.current = false;
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
  const [activeAnalysisJob, setActiveAnalysisJob] = useState<ActiveAnalysisJob | null>(() => readActiveAnalysisJob());

  // ── v1.1 Pipeline inputs ─────────────────────────────────────────────
  const [notaClinica,       setNotaClinica]       = useState('');
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
        return parsed.map(e => ({ ...e, pdfUrl: '', patient: { ...e.patient, nombre: sanitizePatientDisplayName(e.patient?.nombre) } }));
      }
    } catch { /* ignorar error de parse */ }
    return [];
  });
  const [viewingEntry,    setViewingEntry]    = useState<HistoryEntry | null>(null);
  const [historialLoading, setHistorialLoading] = useState(false);
  const [historialError,   setHistorialError]   = useState('');
  const [historyHasMore,   setHistoryHasMore]   = useState(false);
  const [historyNextCursor,setHistoryNextCursor]= useState<string | null>(null);
  const [historyTotal,     setHistoryTotal]     = useState(0);
  const historyCursorRef = useRef<string | null>(null);

  // Sincronizar caché localStorage cada vez que el historial en memoria cambia
  useEffect(() => {
    try {
      const trimmed = history.slice(0, MAX_HISTORY_ENTRIES);
      const toStore = trimmed.map(e => ({ ...e, pdfUrl: '' }));
      sessionStorage.setItem('imagiax_history_v1', JSON.stringify(toStore));
    } catch { /* cuota excedida u otro error */ }
  }, [history]);

  useEffect(() => {
    historyCursorRef.current = historyNextCursor;
  }, [historyNextCursor]);

  /**
   * Carga el historial desde el backend.
   * Reemplaza el estado local con los datos reales de la DB del médico autenticado.
   * Solo se llama cuando hay usuario autenticado.
   */
  const fetchHistorialFromBackend = useCallback(async (append = false) => {
    setHistorialLoading(true);
    if (!append) setHistorialError('');
    try {
      const data = await listadoInformes(undefined, HISTORY_PAGE_SIZE, append ? (historyCursorRef.current || undefined) : undefined);
      const entries = (data.items || []).map(informeDbToHistoryEntry);
      setHistory(prev => append ? [...prev, ...entries] : entries);
      setHistoryHasMore(Boolean(data.has_more));
      setHistoryNextCursor(data.next_cursor || null);
      if (!append) setHistoryTotal(data.total || entries.length);
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
      nombre: sanitizePatientDisplayName(patName),
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

  const clearActiveAnalysis = useCallback(() => {
    persistActiveAnalysisJob(null);
    setActiveAnalysisJob(null);
    pollingJobRef.current = null;
  }, []);

  const completeAnalysis = useCallback(async (response: GenerarInformeResponse) => {
    if (!isMountedRef.current) return;
    clearActiveAnalysis();
    setProgress(100);
    setProgressMsg('¡Análisis completado!');

    const reportText = response.informe_texto;
    const sec = parseReportSections(reportText);
    const id = response.informe_id;

    setSections(sec);
    setInformeId(id);
    setAiResultados(response.ai_resultados);

    if (response.pipeline_version) setPipelineVersion(response.pipeline_version);
    if (response.calidad_imagen) setCalidadImagen(response.calidad_imagen);
    if (response.auditoria_estado) setAuditoriaEstado(response.auditoria_estado);

    checkEstadoCreditos().then(setCreditos).catch(() => {});
    getMetodoPago().then(setMetodoPago).catch(() => {});

    if (response.pago_automatico?.estado === 'rejected') {
      setApiError(
        'Cobro rechazado. El informe se genero correctamente pero no pudimos procesar el pago. ' +
        'Por favor actualiza tu tarjeta para evitar interrupciones.'
      );
    }

    try {
      const qr = await QRCode.toDataURL(
        `${IMAGIAX_BACKEND}/informes/${id}/ver`,
        { width: 128, margin: 1, color: { dark: '#000000', light: '#ffffff' } }
      );
      if (isMountedRef.current) setQrDataUrl(qr);
    } catch {
      if (isMountedRef.current) setQrDataUrl('');
    }

    if (isMountedRef.current) setState('revisar');
  }, [clearActiveAnalysis]);

  const handleAnalysisError = useCallback((err: unknown) => {
    const shouldPreserveActiveJob =
      !!activeAnalysisJob && (
        err instanceof ImagiaxTimeoutError ||
        (err instanceof ImagiaxApiError && (err.status === 401 || err.status >= 500)) ||
        (err instanceof TypeError && err.message.includes('fetch'))
      );

    if (!shouldPreserveActiveJob) {
      clearActiveAnalysis();
    }

    setProgress(0);
    setProgressMsg('');

    if (shouldPreserveActiveJob) {
      if (err instanceof ImagiaxApiError && err.status === 401) {
        setAuthUser(null);
        setApiError(
          'Tu sesión expiró mientras el análisis seguía procesándose. ' +
          'Volvé a iniciar sesión: conservamos el seguimiento para reanudarlo sin perder el trabajo.'
        );
      } else {
        setApiError(
          'Perdimos temporalmente la conexión con el seguimiento del análisis, pero el procesamiento puede seguir activo. ' +
          'No inicies otro análisis para este estudio: al volver a abrirlo retomaremos la consulta automáticamente.'
        );
      }
    } else if (err instanceof ImagiaxTimeoutError) {
      setApiError(
        'El análisis sigue en curso o el servidor tardó demasiado en confirmar el estado. ' +
        'Esperá unos instantes, volvé a abrir este estudio y reintentá solo si el informe aún no aparece.'
      );
    } else if (err instanceof ImagiaxApiError) {
      if (err.status === 401) {
        setAuthUser(null);
        setState('idle');
        return;
      }
      if (err.status === 402) {
        const detail = typeof err.detail === 'object' ? (err.detail as any) : {};

        if (detail.error === 'Cobro rechazado') {
          setApiError(
            'No se pudo procesar el cobro con tu tarjeta. ' +
            'Verifica que tenga fondos disponibles o registra una nueva.'
          );
        } else {
          setApiError(
            'Creditos agotados. Adquiere un plan o registra una tarjeta para cobro automatico.'
          );
        }
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
  }, [activeAnalysisJob, clearActiveAnalysis]);

  const pollAnalysisJob = useCallback(async (job: ActiveAnalysisJob): Promise<GenerarInformeResponse> => {
    pollingJobRef.current = job.jobId;
    const deadline = new Date(job.startedAt).getTime() + ANALYSIS_MAX_POLL_MS;
    let attempt = 0;
    let transientErrors = 0;

    while (isMountedRef.current && Date.now() < deadline) {
      if (attempt > 0) {
        const delayMs = ANALYSIS_POLL_DELAYS_MS[Math.min(attempt - 1, ANALYSIS_POLL_DELAYS_MS.length - 1)];
        await delay(delayMs);
      }

      const elapsedSec = Math.max(1, Math.round((Date.now() - new Date(job.startedAt).getTime()) / 1000));
      setProgress(current => Math.max(current, Math.min(97, 16 + attempt * 4)));
      setProgressMsg(
        elapsedSec < 30
          ? 'El servidor está procesando el análisis...'
          : elapsedSec < 120
            ? 'Generando informe clínico en segundo plano...'
            : 'El análisis sigue en curso. Esperando confirmación del servidor...'
      );

      try {
        const statusResponse = await getEstadoGeneracionInforme(job.jobId);
        transientErrors = 0;

        if (isGenerarInformeAcceptedResponse(statusResponse)) {
          attempt += 1;
          continue;
        }

        return statusResponse;
      } catch (err) {
        const isRecoverable =
          err instanceof ImagiaxTimeoutError ||
          (err instanceof ImagiaxApiError && (err.status === 401 || err.status >= 500)) ||
          (err instanceof TypeError && err.message.includes('fetch'));

        if (isRecoverable && transientErrors < 3) {
          transientErrors += 1;
          attempt += 1;
          setProgressMsg('Seguimos esperando respuesta del servidor...');
          continue;
        }

        throw err;
      }
    }

    throw new ImagiaxTimeoutError(ANALYSIS_MAX_POLL_MS, '/generar/informe/estado');
  }, []);

  useEffect(() => {
    if (!studyUID || !authUser) return;

    const persistedJob = readActiveAnalysisJob();
    if (!persistedJob || persistedJob.studyUID !== studyUID) return;
    if (persistedJob.userId && persistedJob.userId !== authUser.user_id) {
      persistActiveAnalysisJob(null);
      setActiveAnalysisJob(null);
      return;
    }
    if (pollingJobRef.current === persistedJob.jobId) return;
    if (state === 'revisar') return;

    setActiveAnalysisJob(persistedJob);
    setState('analizando');
    setActiveTab('analisis');
    setApiError('');
    setProgress(current => Math.max(current, 12));
    setProgressMsg('Reanudando análisis en curso...');

    pollAnalysisJob(persistedJob)
      .then(completeAnalysis)
      .catch(handleAnalysisError);
  }, [authUser, completeAnalysis, handleAnalysisError, pollAnalysisJob, state, studyUID]);

  const iniciarAnalisis = useCallback(async () => {
    if (!studyUID) {
      setApiError('No hay estudio activo. Abra un estudio DICOM primero.');
      return;
    }

    if (state === 'analizando' || (activeAnalysisJob && activeAnalysisJob.studyUID === studyUID)) {
      setApiError('Ya hay un análisis en curso para este estudio. Espera la respuesta del servidor.');
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
    setPipelineVersion(undefined);
    setCalidadImagen(undefined);
    setAuditoriaEstado(undefined);
    clearActiveAnalysis();

    setProgress(10);
    setProgressMsg('Solicitud enviada...');
    const _t30 = setTimeout(() => { setProgress(30); setProgressMsg('Control de calidad...'); }, 3000);
    const _t50 = setTimeout(() => { setProgress(50); setProgressMsg('Analizando imagenes...'); }, 7000);
    const _t70 = setTimeout(() => { setProgress(70); setProgressMsg('Generando informe...'); }, 18000);
    const _t85 = setTimeout(() => { setProgress(85); setProgressMsg('Auditoria clinica...'); }, 30000);
    milestoneTimersRef.current = [_t30, _t50, _t70, _t85];
    const _clearMilestones = () => {
      clearTimeout(_t30); clearTimeout(_t50); clearTimeout(_t70); clearTimeout(_t85);
      milestoneTimersRef.current = [];
    };

    abortRef.current = new AbortController();

    try {
      const response: GenerarInformeResult = await generarInforme(
        studyUID,
        5,
        notaClinica || undefined,
        false,
        '1.1',
      );

      _clearMilestones();

      if (isGenerarInformeAcceptedResponse(response)) {
        const job: ActiveAnalysisJob = {
          jobId: response.job_id,
          studyUID,
          userId: authUser?.user_id || null,
          startedAt: new Date().toISOString(),
        };

        persistActiveAnalysisJob(job);
        setActiveAnalysisJob(job);
        setProgress(current => Math.max(current, 18));
        setProgressMsg('El estudio sigue procesándose en segundo plano...');

        const finalResponse = await pollAnalysisJob(job);
        await completeAnalysis(finalResponse);
        return;
      }

      await completeAnalysis(response);
    } catch (err) {
      _clearMilestones();
      handleAnalysisError(err);
    }
  }, [activeAnalysisJob, authUser, clearActiveAnalysis, completeAnalysis, handleAnalysisError, notaClinica, pollAnalysisJob, state, studyUID]);

  const handleGeneratePdf = useCallback(async () => {
    setIsGeneratingPdf(true);
    setPdfError('');
    try {
      const blob = await downloadInformePdf(informeId);
      const url = URL.createObjectURL(blob);
      setPdfUrl(url);
    } catch (err) {
      if (err instanceof ImagiaxApiError && err.status === 401) {
        setAuthUser(null);
        return;
      }
      setPdfError('Error al generar el PDF. Intente nuevamente.');
      console.error(err);
    } finally {
      setIsGeneratingPdf(false);
    }
  }, [informeId]);

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

      const blob = await downloadInformePdf(informeId);
      const url = URL.createObjectURL(blob);
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
      const blob = getCachedPdfBlob(entry.informeId) || await downloadInformePdf(entry.informeId);
      cachePdfBlob(entry.informeId, blob);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${entry.informeId}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Error descargando PDF del historial:', err);
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
    clearActiveAnalysis();
  }, [clearActiveAnalysis]);

  // ── Renders ────────────────────────────────────────────────────────────────
  // PERF-H4: memoize payment/credit derived values — these are used in multiple
  // render helpers and re-computed on every render without memoization.
  const { tieneTarjeta, creditosDisponibles, usaraCreditos, usaraTarjeta, sinMetodoPago, canAnalyze, hasActiveAnalysis } = useMemo(() => {
    const _tieneTarjeta = metodoPago?.tiene_tarjeta || creditos?.tiene_tarjeta || false;
    const _creditosDisponibles = creditos?.creditos_disponibles ?? 0;
    const _usaraCreditos = _creditosDisponibles > 0;
    const _usaraTarjeta = !_usaraCreditos && _tieneTarjeta;
    const _sinMetodoPago = !_usaraCreditos && !_tieneTarjeta;
    const _hasActiveAnalysis = state === 'analizando' || !!(activeAnalysisJob && activeAnalysisJob.studyUID === studyUID);
    const _canAnalyze = apiStatus === 'online' && !!studyUID && !_sinMetodoPago && !_hasActiveAnalysis;
    return {
      tieneTarjeta: _tieneTarjeta,
      creditosDisponibles: _creditosDisponibles,
      usaraCreditos: _usaraCreditos,
      usaraTarjeta: _usaraTarjeta,
      sinMetodoPago: _sinMetodoPago,
      hasActiveAnalysis: _hasActiveAnalysis,
      canAnalyze: _canAnalyze,
    };
  }, [activeAnalysisJob, metodoPago, creditos, apiStatus, state, studyUID]);

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
          {sinMetodoPago ? 'Adquirir creditos' : hasActiveAnalysis ? 'Analisis en curso...' : 'Iniciar Analisis IA'}
        </button>

        {activeAnalysisJob && activeAnalysisJob.studyUID === studyUID && (
          <div style={{ background:'#0f172a', border:'1px solid #1e3a5f', borderRadius:'6px', padding:'10px 12px' }}>
            <div style={{ color:'#93c5fd', fontSize:'10px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.4px' }}>
              {state === 'analizando' ? 'Analisis en segundo plano' : 'Seguimiento listo para reanudar'}
            </div>
            <div style={{ color:'#94a3b8', fontSize:'10px', lineHeight:'1.5', marginTop:'4px' }}>
              Este estudio ya tiene un procesamiento activo. Si recargás o volvés a abrir el visor, retomaremos el seguimiento automáticamente.
            </div>
          </div>
        )}

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
      {activeAnalysisJob && activeAnalysisJob.studyUID === studyUID && (
        <div style={{ background:'#0f172a', border:'1px solid #1e293b', borderRadius:'8px', padding:'10px 12px' }}>
          <div style={{ color:'#93c5fd', fontSize:'10px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.4px' }}>
            Seguimiento automático activo
          </div>
          <div style={{ color:'#64748b', fontSize:'10px', lineHeight:'1.5', marginTop:'4px' }}>
            El visor seguirá consultando el estado del análisis hasta recibir el informe final o agotar el límite máximo de 10 minutos.
          </div>
        </div>
      )}
    </div>
  );

  const renderRevisar = () => (
    <div style={{ display:'flex', flexDirection:'column', gap:'0' }}>
      {/* v1.1 Clean Metadata Badges - Sin errores de sintaxis */}
      {(calidadImagen || auditoriaEstado) && (
        <div style={{
          padding: '6px 14px',
          display: 'flex',
          gap: '6px',
          flexWrap: 'wrap',
          borderBottom: '1px solid #1e293b',
          background: '#080e1a'
        }}>
          {calidadImagen && (
            <span style={{
              color: '#475569', fontSize: '9px', fontWeight: '600',
              background: 'rgba(71,85,105,0.15)', padding: '2px 7px',
              borderRadius: '4px', border: '1px solid #1e293b'
            }}>
              Calidad {calidadImagen}
            </span>
          )}
          {auditoriaEstado && (
            <span style={{
              color: '#475569', fontSize: '9px', fontWeight: '600',
              background: 'rgba(71,85,105,0.15)', padding: '2px 7px',
              borderRadius: '4px', border: '1px solid #1e293b'
            }}>
              Auditoría {auditoriaEstado}
            </span>
          )}
        </div>
      )}

      <div style={{ padding:'12px 14px', display:'flex', flexDirection:'column', gap:'10px' }}>
        {/* Análisis Técnico IA */}
        <details open style={{ background:'#0d1420', border:'1px solid #1e3a5f', borderRadius:'8px', overflow:'hidden' }}>
          <summary style={{ padding:'10px 14px', cursor:'pointer', userSelect:'none', display:'flex', alignItems:'center', gap:'8px', listStyle:'none', background:'#0d1420', borderBottom:'1px solid #1e293b' }}>
            <span style={{ backgroundColor:'#1e3a5f', color:'#93c5fd', padding:'2px 8px', borderRadius:'4px', fontSize:'10px', fontWeight:'700' }}>ANALISIS TECNICO IA</span>
            <span style={{ color:'#475569', fontSize:'10px', flex:1 }}>solo para medico</span>
            <span style={{ color:'#334155', fontSize:'10px' }}>▾</span>
          </summary>
          <div style={{ padding:'12px 14px', maxHeight:'220px', overflowY:'auto' }}>
            {sections.map((s, i) => (
              <div key={i} style={{ marginBottom:'10px' }}>
                <div style={{ color:'#60a5fa', fontSize:'10px', fontWeight:'700', marginBottom:'3px', textTransform:'uppercase', letterSpacing:'0.3px' }}>{s.title}</div>
                {s.lines.map((l, j) => (
                  <div key={j} style={{ color:'#94a3b8', fontSize:'11px', lineHeight:'1.6', paddingLeft:'10px' }}>{l}</div>
                ))}
              </div>
            ))}
          </div>
        </details>

        {/* Vista Previa del Informe Final */}
        <ScaledPreview sec={sections} pid={informeId} qr={qrDataUrl} pat={patient} nota={notaClinica} containerWidth={panelWidth} />

        {/* Botones de Acción */}
        {!pdfConfirmed ? (
          <div style={{ display:'flex', gap:'6px' }}>
            <button
              onClick={() => setPdfConfirmed(true)}
              style={{ flex:1, background:'linear-gradient(135deg,#1e40af,#2563eb)', color:'white', border:'none', borderRadius:'6px', padding:'11px', fontSize:'12px', fontWeight:'700', cursor:'pointer' }}
            >
              Aceptar informe
            </button>
            <button onClick={handleEditar} style={{ background:'#111827', color:'#93c5fd', border:'1px solid #1e3a5f', borderRadius:'6px', padding:'11px 14px', fontSize:'12px', cursor:'pointer' }}>Editar</button>
          </div>
        ) : (
          <div style={{ background:'#0d1420', border:'1px solid #1e3a5f', borderRadius:'8px', padding:'14px', display:'flex', flexDirection:'column', gap:'12px' }}>
            <label style={{ display:'flex', gap:'10px', alignItems:'flex-start', cursor:'pointer' }}>
              <input type="checkbox" checked={legalAccepted} onChange={e => setLegalAccepted(e.target.checked)} style={{ marginTop:'3px' }} />
              <span style={{ color:'#94a3b8', fontSize:'11px', lineHeight:'1.6' }}>He verificado el contenido y asumo responsabilidad medica.</span>
            </label>
            <button onClick={handleConfirmar} disabled={!legalAccepted || isGeneratingPdf} style={{ background: legalAccepted ? '#166534' : '#1e293b', color:'white', border:'none', borderRadius:'6px', padding:'11px', fontSize:'12px', fontWeight:'700' }}>
              {isGeneratingPdf ? 'Generando...' : 'Confirmar definitivamente'}
            </button>
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
            : historyHasMore && historyTotal > history.length
              ? `${history.length} de ${historyTotal} informes cargados`
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
      {history.length > 0 && (
        <>
          {history.map(e => (
            <HistoryCard key={e.informeId} entry={e} onView={() => setViewingEntry(e)} onDownloadPdf={() => handleDownloadHistoryPdf(e)} />
          ))}
          {historyHasMore && (
            <button
              onClick={() => fetchHistorialFromBackend(true)}
              disabled={historialLoading}
              style={{ background:'transparent', color:'#93c5fd', border:'1px solid #1e3a5f', borderRadius:'8px', padding:'10px 12px', fontSize:'11px', fontWeight:'700', cursor: historialLoading ? 'default' : 'pointer' }}>
              {historialLoading ? 'Cargando más...' : 'Cargar más informes'}
            </button>
          )}
        </>
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
    window.location.href = '/login?returnTo=' + encodeURIComponent(window.location.pathname + window.location.search);
    return (
      <div style={{ padding: '20px', textAlign: 'center', color: '#94a3b8', fontSize: '13px' }}>
        Redirigiendo al inicio de sesión...
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

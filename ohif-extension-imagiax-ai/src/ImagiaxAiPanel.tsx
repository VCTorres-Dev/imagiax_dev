import React, { useState, useEffect, useCallback, memo } from 'react';
import jsPDF from 'jspdf';
import QRCode from 'qrcode';
import { useActiveViewportDisplaySets, utils } from '@ohif/core';
import logoAndesSaludPng from './logo-andes-salud.png';
import {
  generarInforme,
  guardarDecision,
  checkSalud,
  checkSaludV2,
  checkEstadoCreditos,
  compararV1V2,
  getMetodoPago,
  borrarMetodoPago,
  type AIResultados,
  type V2CompararResponse,
  type EstadoCreditosResponse,
  type MetodoPagoResponse,
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
const LOGO_IMAGIAX_SVG = logoAndesSaludPng;
const MAX_HISTORY_ENTRIES = 50;

// Cargar config dinámica del backend (doctor/clínica configurables)
let _configLoaded = false;
async function loadBackendConfig(): Promise<void> {
  if (_configLoaded) return;
  try {
    const resp = await fetch(`${IMAGIAX_BACKEND}/config/frontend`);
    if (resp.ok) {
      const cfg = await resp.json();
      if (cfg.doctor_name) DOCTOR_NAME = cfg.doctor_name;
      if (cfg.doctor_specialty) DOCTOR_SPECIALTY = cfg.doctor_specialty;
      if (cfg.doctor_license) DOCTOR_LICENSE = cfg.doctor_license;
      if (cfg.brand_name) BRAND_NAME = cfg.brand_name;
      if (cfg.brand_subtitle) BRAND_SUBTITLE = cfg.brand_subtitle;
      _configLoaded = true;
    }
  } catch { /* backend no disponible aún, se usan defaults */ }
}

async function getLogoPng(_size = 180): Promise<string> {
  return logoAndesSaludPng;
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
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function formatDate(d: Date): string {
  return d.toLocaleDateString('es-CL', { day: '2-digit', month: 'long', year: 'numeric' });
}
/** Formato DD.MM.YYYY para PDF/preview (estilo referencia clínica) */
function formatDateDot(raw: string): string {
  // Acepta DD/MM/YYYY o YYYY-MM-DD o ya DD.MM.YYYY
  if (/^\d{2}\.\d{2}\.\d{4}$/.test(raw)) return raw;
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(raw)) return raw.replace(/\//g, '.');
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    const [y, m, d] = raw.split('-');
    return `${d}.${m}.${y}`;
  }
  // Fallback: devolver hoy en DD.MM.YYYY
  const now = new Date();
  return `${String(now.getDate()).padStart(2,'0')}.${String(now.getMonth()+1).padStart(2,'0')}.${now.getFullYear()}`;
}
function calcAge(v: string): string {
  const n = parseInt(v, 10);
  if (!isNaN(n) && n > 1900 && n < 2100) return `${new Date().getFullYear() - n} año(s)`;
  if (!isNaN(n)) return `${n} año(s)`;
  return v;
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
    const isHeader =
      /^[A-ZÁÉÍÓÚÑÜ\s\(\)]{4,}$/.test(line) ||
      (line.endsWith(':') && line.length < 60 && line === line.toUpperCase());
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

// ── Generación de PDF ─────────────────────────────────────────────────────────
async function generateReportPDF(
  patient: PatientInfo,
  sections: ReportSection[],
  informeId: string,
  qrDataUrl: string
): Promise<string> {
  const doc = new jsPDF({ format: 'a4', orientation: 'portrait', unit: 'mm' });
  const pageW = doc.internal.pageSize.getWidth();
  const pageH = doc.internal.pageSize.getHeight();
  const marginX = 14;
  const contentW = pageW - marginX * 2;
  const findings = getPatientFindings(sections);
  const shortId = informeId.length > 12 ? informeId.substring(0, 12).toUpperCase() : informeId.toUpperCase();

  const ensureSpace = (requiredHeight: number, cursorY: number): number => {
    if (cursorY + requiredHeight > pageH - 18) {
      doc.addPage();
      return 16;
    }
    return cursorY;
  };

  // ── Cabecera: Logo + Branding  |  Fecha (right) ─────────────────────────
  const headerTop = 12;

  // Intentar renderizar el logo como imagen
  // logoH calculado para mantener la proporción real de la imagen
  const logoW = 44;
  let logoH = 34;
  try {
    const logoPng = await getLogoPng(220);
    // Detectar dimensiones reales para mantener ratio
    const tmpImg = new Image();
    await new Promise<void>(res => {
      tmpImg.onload = () => res();
      tmpImg.onerror = () => res();
      tmpImg.src = logoPng;
    });
    if (tmpImg.naturalWidth > 0 && tmpImg.naturalHeight > 0) {
      logoH = logoW * (tmpImg.naturalHeight / tmpImg.naturalWidth);
    }
    doc.addImage(logoPng, 'PNG', marginX, headerTop - 4, logoW, logoH);
  } catch {
    // Fallback: cruz médica simple
    doc.setDrawColor(30, 58, 143);
    doc.setLineWidth(1.5);
    doc.line(marginX + 5, headerTop + 3, marginX + 5, headerTop + 13);
    doc.line(marginX, headerTop + 8, marginX + 10, headerTop + 8);
  }

  // Nombre de marca — desplazado a la derecha del logo
  const brandX = marginX + logoW + 6;
  doc.setFont('helvetica', 'bold');
  doc.setTextColor(26, 42, 108);
  doc.setFontSize(13);
  doc.text(BRAND_NAME, brandX, headerTop + 7);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(8);
  doc.setTextColor(120, 120, 120);
  doc.text(BRAND_SUBTITLE, brandX, headerTop + 13);

  // Informe ID + Fecha — alineados a la derecha
  const rightX = pageW - marginX;
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(9);
  doc.setTextColor(100, 100, 100);
  doc.text(`Informe ${shortId}`, rightX, headerTop + 7, { align: 'right' });
  const fechaDot = formatDateDot(patient.fechaExamen || '');
  doc.text(`Fecha Examen: ${fechaDot}`, rightX, headerTop + 13, { align: 'right' });

  // Línea separadora — siempre debajo del logo
  const lineY = headerTop + Math.max(logoH, 18) + 2;
  doc.setDrawColor(26, 58, 143);
  doc.setLineWidth(0.7);
  doc.line(marginX, lineY, pageW - marginX, lineY);

  // ── Datos del paciente ───────────────────────────────────────────────────
  let y = lineY + 10;
  doc.setFont('helvetica', 'normal');
  doc.setTextColor(25, 25, 25);
  doc.setFontSize(11);
  doc.text(`RUT: ${patient.rut}`, marginX, y);
  y += 7;
  doc.text(`Paciente: ${patient.nombre.toUpperCase()}`, marginX, y);
  y += 7;
  doc.text(`Edad: ${calcAge(patient.edad)}`, marginX, y);
  y += 7;
  if (patient.medicoReferencia) {
    doc.text(`Médico de Referencia: ${patient.medicoReferencia.toUpperCase()}`, marginX, y);
    y += 7;
  }

  // ── Título del examen — centrado, subrayado ──────────────────────────────
  y += 8;
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(14);
  doc.setTextColor(0, 0, 0);
  const examTitle = (patient.tipoExamen || 'INFORME RADIOLÓGICO').toUpperCase();
  const examTitleW = doc.getTextWidth(examTitle);
  doc.text(examTitle, pageW / 2, y, { align: 'center' });
  doc.setLineWidth(0.4);
  doc.setDrawColor(0, 0, 0);
  doc.line(pageW / 2 - examTitleW / 2, y + 1.5, pageW / 2 + examTitleW / 2, y + 1.5);

  // ── Hallazgos ────────────────────────────────────────────────────────────
  y += 14;
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(12.5);
  doc.setTextColor(0, 0, 0);
  doc.text('Hallazgos:', marginX, y);
  y += 8;

  doc.setFont('times', 'normal');
  doc.setFontSize(12.5);
  doc.setTextColor(30, 30, 30);
  findings.forEach(line => {
    const cleaned = stripMarkdown(line.trim());
    if (!cleaned) return;
    const wrapped = doc.splitTextToSize(cleaned, contentW);
    y = ensureSpace((wrapped.length * 6) + 3, y);
    doc.text(wrapped, marginX, y);
    y += wrapped.length * 6 + 2;
  });

  // ── Impresión Diagnóstica (si existe) ────────────────────────────────────
  const impresionSec = sections.find(s => normalizeTitle(s.title).includes('IMPRESION'));
  if (impresionSec?.lines?.length) {
    y = ensureSpace(22, y + 8);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(12.5);
    doc.setTextColor(0, 0, 0);
    doc.text('Impresión Diagnóstica:', marginX, y);
    y += 8;
    doc.setFont('times', 'normal');
    doc.setFontSize(12.5);
    doc.setTextColor(30, 30, 30);
    impresionSec.lines.forEach(line => {
      const cleaned = stripMarkdown(stripAILanguage(line.trim()));
      if (!cleaned) return;
      const wrapped = doc.splitTextToSize(cleaned, contentW);
      y = ensureSpace((wrapped.length * 6) + 3, y);
      doc.text(wrapped, marginX, y);
      y += wrapped.length * 6 + 2;
    });
  }

  // ── Observaciones Visuales Adicionales (si existen) ──────────────────────
  const observacionesSec = sections.find(s => normalizeTitle(s.title).includes('OBSERVACIONES'));
  if (observacionesSec?.lines?.length) {
    const hasContent = observacionesSec.lines.some(l => !normalizeTitle(l).includes('SIN OBSERVACIONES'));
    if (hasContent) {
      y = ensureSpace(22, y + 8);
      doc.setFont('helvetica', 'bold');
      doc.setFontSize(12.5);
      doc.setTextColor(146, 64, 14); // amber-800
      doc.text('Observaciones Visuales Adicionales:', marginX, y);
      y += 8;
      doc.setFont('times', 'normal');
      doc.setFontSize(12.5);
      doc.setTextColor(120, 53, 15); // amber-900
      observacionesSec.lines.forEach(line => {
        const cleaned = stripMarkdown(stripAILanguage(line.trim()));
        if (!cleaned || normalizeTitle(cleaned).includes('SIN OBSERVACIONES')) return;
        const wrapped = doc.splitTextToSize(cleaned, contentW);
        y = ensureSpace((wrapped.length * 6) + 3, y);
        doc.text(wrapped, marginX, y);
        y += wrapped.length * 6 + 2;
      });
      doc.setTextColor(0, 0, 0);
    }
  }

  // ── Recomendaciones (si existen) ─────────────────────────────────────────
  const recomendacionesSec = sections.find(s => normalizeTitle(s.title).includes('RECOMENDACION'));
  if (recomendacionesSec?.lines?.length) {
    y = ensureSpace(22, y + 8);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(12.5);
    doc.setTextColor(0, 0, 0);
    doc.text('Recomendaciones:', marginX, y);
    y += 8;
    doc.setFont('times', 'normal');
    doc.setFontSize(12.5);
    doc.setTextColor(30, 30, 30);
    recomendacionesSec.lines.forEach(line => {
      const cleaned = stripMarkdown(stripAILanguage(line.trim()));
      if (!cleaned) return;
      const wrapped = doc.splitTextToSize(cleaned, contentW);
      y = ensureSpace((wrapped.length * 6) + 3, y);
      doc.text(wrapped, marginX, y);
      y += wrapped.length * 6 + 2;
    });
  }

  // ── Cierre / Firma ──────────────────────────────────────────────────────
  y += 10;
  y = ensureSpace(48, y);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(12);
  doc.setTextColor(0, 0, 0);
  doc.text('Saluda Atentamente', marginX, y);
  y += 30;

  const sigCenterX = marginX + 66;
  doc.setDrawColor(30, 30, 30);
  doc.setLineWidth(0.3);
  doc.line(sigCenterX - 32, y, sigCenterX + 32, y);
  doc.setFontSize(11);
  doc.setFont('helvetica', 'bold');
  doc.text(DOCTOR_NAME, sigCenterX, y + 6, { align: 'center' });
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(10);
  doc.text(`${DOCTOR_SPECIALTY} · ${DOCTOR_LICENSE}`, sigCenterX, y + 12, { align: 'center' });

  // QR — esquina inferior derecha
  if (qrDataUrl) {
    doc.addImage(qrDataUrl, 'PNG', pageW - marginX - 32, y - 16, 28, 28);
  }

  return URL.createObjectURL(doc.output('blob'));
}

// ── MedicalReportDocument ─────────────────────────────────────────────────────
interface DocProps {
  patient: PatientInfo; sections: ReportSection[];
  informeId: string; qrDataUrl: string;
}
const CLEAN_FILTER = ['NOTA LEGAL','DISCLAIMER','PROBABILIDAD','SOLO USO INTERNO','SÓLO USO INTERNO','NIVEL DE ALERTA','ANÁLISIS COMPUTACIONAL','ANÁLISIS IA','MODELO','INTELIGENCIA ARTIFICIAL'];

// memo evita re-renders innecesarios cuando el parent actualiza estado no relacionado
const MedicalReportDocument = memo(({ patient, sections, informeId, qrDataUrl }: DocProps) => {
  const hallazgos = getPatientFindings(sections);
  const impresion = sections.find(s => normalizeTitle(s.title).includes('IMPRESION'));
  const observaciones = sections.find(s => normalizeTitle(s.title).includes('OBSERVACIONES'));
  const recomendaciones = sections.find(s => normalizeTitle(s.title).includes('RECOMENDACION'));
  const shortId = informeId.length > 12 ? informeId.substring(0, 12).toUpperCase() : informeId.toUpperCase();
  const fechaDot = formatDateDot(patient.fechaExamen || '');
  return (
    <div
      style={{ width:'794px', minHeight:'1123px', backgroundColor:'#fff',
        fontFamily:'"Arial","Helvetica Neue",Helvetica,sans-serif',
        fontSize:'11px', color:'#1a1a1a', padding:'40px 60px 90px',
        boxSizing:'border-box', position:'relative', lineHeight:'1.5' }}>

      {/* Cabecera: Logo+Marca  |  Fecha (rightAligned) */}
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
        borderBottom:'3px solid #1a3a8f', paddingBottom:'16px', marginBottom:'28px' }}>
        <div style={{ display:'flex', alignItems:'center', gap:'14px' }}>
          <img src={LOGO_IMAGIAX_SVG} alt="Centro Médico Andes Salud"
            style={{ width:'72px', height:'auto', display:'block' }} />
          <div>
            <div style={{ fontSize:'15px', fontWeight:'800', color:'#1a2a6c', letterSpacing:'0.4px', lineHeight:'1.2' }}>
              {BRAND_NAME}
            </div>
            <div style={{ fontSize:'9px', color:'#888', marginTop:'2px' }}>{BRAND_SUBTITLE}</div>
          </div>
        </div>
        <div style={{ textAlign:'right' }}>
          <div style={{ fontSize:'9px', color:'#888', marginBottom:'2px' }}>Informe {shortId}</div>
          <div style={{ fontSize:'10px', color:'#444' }}>Fecha Examen: {fechaDot}</div>
        </div>
      </div>

      {/* Datos del paciente — bloque propio con espacio respirable */}
      <div style={{ marginBottom:'26px', fontSize:'12px', lineHeight:'2', color:'#111827' }}>
        <div>RUT: {patient.rut}</div>
        <div>Paciente: <span style={{ textTransform:'uppercase', fontWeight:'500' }}>{patient.nombre}</span></div>
        <div>Edad: {calcAge(patient.edad)}</div>
        {patient.medicoReferencia && (
          <div>Médico de Referencia: <span style={{ textTransform:'uppercase', fontWeight:'500' }}>{patient.medicoReferencia}</span></div>
        )}
      </div>

      {/* Título del examen — centrado, subrayado real */}
      <div style={{ textAlign:'center', marginBottom:'28px' }}>
        <div style={{ fontSize:'15px', fontWeight:'700', textTransform:'uppercase', display:'inline-block',
          borderBottom:'2px solid #111', paddingBottom:'3px', letterSpacing:'0.6px' }}>
          {patient.tipoExamen}
        </div>
      </div>

      {/* Hallazgos */}
      <div style={{ marginBottom:'22px' }}>
        <div style={{ fontWeight:'700', fontSize:'13px', marginBottom:'12px' }}>Hallazgos:</div>
        {hallazgos.map((line, j) => {
          const cleaned = stripMarkdown(line);
          if (!cleaned) return null;
          return (
            <div key={j} style={{ fontSize:'12px', lineHeight:'1.75', color:'#1f2937', marginBottom:'4px',
              fontFamily:'Georgia,"Times New Roman",Times,serif' }}>{cleaned}</div>
          );
        })}
      </div>

      {/* Impresión Diagnóstica */}
      {impresion?.lines?.length ? (
        <div style={{ marginBottom:'22px' }}>
          <div style={{ fontWeight:'700', fontSize:'13px', marginBottom:'12px' }}>Impresión Diagnóstica:</div>
          {impresion.lines.map((line, j) => {
            const cleaned = stripMarkdown(stripAILanguage(line));
            if (!cleaned) return null;
            return (
              <div key={j} style={{ fontSize:'12px', lineHeight:'1.75', color:'#1f2937', marginBottom:'4px',
                fontFamily:'Georgia,"Times New Roman",Times,serif' }}>{cleaned}</div>
            );
          })}
        </div>
      ) : null}

      {/* Observaciones Visuales Adicionales */}
      {observaciones?.lines?.length ? (
        <div style={{ marginBottom:'22px', padding:'12px 14px', backgroundColor:'#fffbeb', border:'1px solid #f59e0b', borderRadius:'6px' }}>
          <div style={{ fontWeight:'700', fontSize:'13px', marginBottom:'12px', color:'#92400e' }}>Observaciones Visuales Adicionales:</div>
          {observaciones.lines.map((line, j) => {
            const cleaned = stripMarkdown(stripAILanguage(line));
            if (!cleaned || normalizeTitle(cleaned).includes('SIN OBSERVACIONES')) return null;
            return (
              <div key={j} style={{ fontSize:'12px', lineHeight:'1.75', color:'#78350f', marginBottom:'4px',
                fontFamily:'Georgia,"Times New Roman",Times,serif' }}>{cleaned}</div>
            );
          })}
        </div>
      ) : null}

      {/* Recomendaciones */}
      {recomendaciones?.lines?.length ? (
        <div style={{ marginBottom:'22px' }}>
          <div style={{ fontWeight:'700', fontSize:'13px', marginBottom:'12px' }}>Recomendaciones:</div>
          {recomendaciones.lines.map((line, j) => {
            const cleaned = stripMarkdown(stripAILanguage(line));
            if (!cleaned) return null;
            return (
              <div key={j} style={{ fontSize:'12px', lineHeight:'1.75', color:'#1f2937', marginBottom:'4px',
                fontFamily:'Georgia,"Times New Roman",Times,serif' }}>{cleaned}</div>
            );
          })}
        </div>
      ) : null}

      {/* Cierre */}
      <div style={{ marginTop:'40px', fontSize:'12px' }}>Saluda Atentamente</div>

      {/* Firma + QR */}
      <div style={{ marginTop:'56px', display:'flex', justifyContent:'space-between', alignItems:'flex-end' }}>
        <div style={{ textAlign:'center' }}>
          <div style={{ borderTop:'1.5px solid #1a1a1a', width:'220px', paddingTop:'6px', fontSize:'10.5px' }}>
            <div style={{ fontWeight:'700', fontSize:'11px' }}>{DOCTOR_NAME}</div>
            <div style={{ color:'#555', marginTop:'2px' }}>{DOCTOR_SPECIALTY}</div>
            <div style={{ color:'#555' }}>Lic. N° {DOCTOR_LICENSE}</div>
          </div>
        </div>
        {qrDataUrl && (
          <div style={{ textAlign:'center' }}>
            <img src={qrDataUrl} alt="QR" style={{ width:'72px', height:'72px', display:'block' }} />
            <div style={{ fontSize:'8.5px', color:'#94a3b8', marginTop:'4px' }}>Escanee para verificar</div>
          </div>
        )}
      </div>

    </div>
  );
});

// ── ScaledPreview — definida FUERA del componente para evitar re-montaje ──────
// Bug anterior: estaba dentro del render → nueva referencia en cada re-render → React desmontaba/remontaba el componente → parpadeo
const ScaledPreview = memo(({ sec, pid, qr, pat }: { sec: ReportSection[]; pid: string; qr: string; pat: PatientInfo }) => (
  <div style={{ overflow:'hidden', border:'1px solid #1e3a5f', borderRadius:'6px', backgroundColor:'#fff', height:'440px', position:'relative' }}>
    <div style={{ transform:'scale(0.43)', transformOrigin:'top left', width:'233%', pointerEvents:'none' }}>
      <MedicalReportDocument patient={pat} sections={sec} informeId={pid} qrDataUrl={qr} />
    </div>
  </div>
));

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

// ── Panel Principal ────────────────────────────────────────────────────────────
type PanelState = 'idle' | 'analizando' | 'revisar' | 'guardado' | 'rechazado';
type ActiveTab  = 'analisis' | 'historial';

interface ImagiaxAiPanelProps {
  servicesManager?: unknown;
  commandsManager?: unknown;
}

const ImagiaxAiPanel: React.FC<ImagiaxAiPanelProps> = () => {
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

  // ── V2 Experimental — NV-Reason-CXR-3B ──────────────────────────────────
  const [v2Status,       setV2Status]       = useState<'unknown' | 'online' | 'offline'>('unknown');
  const [v2Loading,      setV2Loading]      = useState(false);
  const [v2Result,       setV2Result]       = useState<V2CompararResponse | null>(null);
  const [v2Error,        setV2Error]        = useState('');

  // ── Historial persistido en localStorage ────────────────────────────────
  const [history, setHistory] = useState<HistoryEntry[]>(() => {
    try {
      const raw = localStorage.getItem('imagiax_history_v1');
      if (raw) {
        const parsed = JSON.parse(raw) as HistoryEntry[];
        return parsed.map(e => ({ ...e, pdfUrl: '' }));
      }
    } catch { /* ignorar error de parse */ }
    return [];
  });
  const [viewingEntry, setViewingEntry] = useState<HistoryEntry | null>(null);

  useEffect(() => {
    try {
      // Limitar historial a MAX_HISTORY_ENTRIES entradas para no saturar localStorage
      const trimmed = history.slice(0, MAX_HISTORY_ENTRIES);
      const toStore = trimmed.map(e => ({ ...e, pdfUrl: '' }));
      localStorage.setItem('imagiax_history_v1', JSON.stringify(toStore));
    } catch { /* cuota excedida u otro error */ }
  }, [history]);

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

    // Build exam type from available metadata
    const examParts = [modality, bodyPart, studyDesc].filter(Boolean);
    const tipoExamen = examParts.length > 0
      ? examParts.join(' — ')
      : 'Estudio Radiológico';

    setPatient(prev => ({
      ...prev,
      rut: instance.PatientID || (instance as any).PatientId || '',
      nombre: patName,
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

    // FIX #5: Progress simulation con fases claras (incluye verificación de pago)
    let p = 0;
    const progressInterval = setInterval(() => {
      p += Math.random() * 3 + 1;
      if (p > 92) p = 92; // Cap at 92% until real response arrives
      setProgress(Math.min(p, 92));
      if (p < 10) setProgressMsg('Verificando metodo de pago...');
      else if (p < 20) setProgressMsg('Descargando estudio DICOM...');
      else if (p < 35) setProgressMsg('Preprocesando imagen medica...');
      else if (p < 55) setProgressMsg('Ejecutando modelo IA...');
      else if (p < 75) setProgressMsg('Generando informe clinico...');
      else if (p < 88) setProgressMsg('Validando resultados...');
      else setProgressMsg('Guardando en base de datos...');
    }, 280);

    try {
      const response = await generarInforme(studyUID);

      clearInterval(progressInterval);
      setProgress(100);
      setProgressMsg('¡Análisis completado!');

      const reportText = response.informe_texto;
      const sec = parseReportSections(reportText);
      const id = response.informe_id;

      setSections(sec);
      setInformeId(id);
      setAiResultados(response.ai_resultados);

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
      clearInterval(progressInterval);
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
        if (err.status === 402) {
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
  }, [studyUID]);

  const handleGeneratePdf = useCallback(async () => {
    setIsGeneratingPdf(true);
    setPdfError('');
    try {
      const url = await generateReportPDF(patient, sections, informeId, qrDataUrl);
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
        console.warn('No se pudo guardar decisión en backend:', err);
        saveSucceeded = false;
        // Show warning but continue with PDF generation
        setApiError(
          'Advertencia: El informe se guardará localmente pero no se pudo registrar en el servidor. ' +
          'Contacta soporte si el problema persiste.'
        );
      }

      const url = await generateReportPDF(patient, sections, informeId, qrDataUrl);
      setPdfUrl(url);
      setHistory(prev => [{
        informeId, fecha: formatDate(new Date()), patient: { ...patient },
        sections: [...sections], pdfUrl: url, qrDataUrl,
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
      const url = await generateReportPDF(entry.patient, entry.sections, entry.informeId, entry.qrDataUrl);
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
      if (err instanceof ImagiaxApiError) {
        setV2Error(`Error servidor V2 (${err.status}): ${err.detail}`);
      } else if (err instanceof TypeError && err.message.includes('fetch')) {
        setV2Error('Servidor V2 no disponible. Verifique que esté corriendo en puerto 8011.');
      } else {
        setV2Error(`Error V2: ${(err as Error).message}`);
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
  }, []);

  // ── Renders ────────────────────────────────────────────────────────────────
  const tieneTarjeta = metodoPago?.tiene_tarjeta || creditos?.tiene_tarjeta || false;
  const creditosDisponibles = creditos?.creditos_disponibles ?? 0;
  // NUEVA LÓGICA: Créditos tienen prioridad sobre tarjeta
  // Usuario solo necesita tarjeta si NO tiene créditos
  const usaraCreditos = creditosDisponibles > 0;
  const usaraTarjeta = !usaraCreditos && tieneTarjeta;
  const sinMetodoPago = !usaraCreditos && !tieneTarjeta;
  const canAnalyze = apiStatus === 'online' && !!studyUID && !sinMetodoPago;

  const renderIdle = () => (
    <div style={{ display:'flex', flexDirection:'column', gap:'0', padding:'0' }}>
      {/* Compact status bar — conexión + método de pago */}
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between',
        padding:'10px 14px', background:'#0d1420', borderBottom:'1px solid #1e293b' }}>
        <div style={{ display:'flex', alignItems:'center', gap:'6px' }}>
          <div style={{ width:'6px', height:'6px', borderRadius:'50%',
            background: apiStatus === 'online' ? '#22c55e' : apiStatus === 'checking' ? '#f59e0b' : '#ef4444',
            boxShadow: apiStatus === 'online' ? '0 0 4px #22c55e' : 'none' }} />
          <span style={{ color: apiStatus === 'online' ? '#94a3b8' : '#f87171', fontSize:'10px' }}>
            {apiStatus === 'online' ? 'Conectado' : apiStatus === 'checking' ? 'Conectando...' : 'Sin conexion'}
          </span>
        </div>
        {/* Mostrar créditos disponibles (siempre que haya) */}
        {creditosDisponibles > 0 && (
          <span style={{ color:'#86efac', fontSize:'10px', fontWeight:'600' }}>
            {creditosDisponibles} informe{creditosDisponibles !== 1 ? 's' : ''}
            {creditos?.es_trial ? ' (trial)' : ''}
          </span>
        )}
        {/* Si sin créditos pero con tarjeta, mostrar indicador */}
        {usaraTarjeta && (
          <span style={{ color:'#94a3b8', fontSize:'10px', fontWeight:'600' }}>
            Cobro automatico
          </span>
        )}
        {/* Sin nada */}
        {sinMetodoPago && (
          <span style={{ color:'#f87171', fontSize:'10px', fontWeight:'600' }}>
            Sin creditos
          </span>
        )}
      </div>

      {/* Patient / Study card — most important info for doctors */}
      <div style={{ padding:'16px 14px', display:'flex', flexDirection:'column', gap:'14px' }}>
        {studyUID ? (
          <div style={{ background:'#0d1420', borderRadius:'8px', border:'1px solid #1e3a5f',
            padding:'14px 16px', display:'flex', flexDirection:'column', gap:'8px' }}>
            <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
              <span style={{ color:'#475569', fontSize:'9px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.5px' }}>Estudio activo</span>
              <span style={{ color:'#1e3a5f', fontSize:'8px', fontFamily:'monospace' }}>DICOM</span>
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
            {/* StudyUID con truncado y tooltip */}
            <div
              title={studyUID}
              style={{ color:'#475569', fontSize:'9px', fontFamily:'monospace', marginTop:'2px',
                overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', lineHeight:'1.5',
                cursor:'help' }}>
              {studyUID}
            </div>
          </div>
        ) : (
          <div style={{ background:'#0d1420', borderRadius:'8px', border:'1px dashed #1e3a5f',
            padding:'28px 16px', textAlign:'center' }}>
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
          {aiResultados.hallazgos_estudio.length > 0 ? (
            <div style={{ display:'flex', flexDirection:'column', gap:'3px', marginTop:'8px' }}>
              {aiResultados.hallazgos_estudio.map((h, i) => (
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
          <ScaledPreview sec={sections} pid={informeId} qr={qrDataUrl} pat={patient} />
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
        <ScaledPreview sec={sections} pid={informeId} qr={qrDataUrl} pat={patient} />
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
      {viewingEntry ? (
        <>
          <button onClick={() => setViewingEntry(null)}
            style={{ background:'transparent', color:'#3b82f6', border:'none', padding:'4px 0', fontSize:'11px', cursor:'pointer', textAlign:'left', display:'flex', alignItems:'center', gap:'4px' }}>
            Volver al historial
          </button>
          <div style={{ background:'#0d1420', border:'1px solid #1e3a5f', borderRadius:'6px', padding:'10px 12px', marginBottom:'-4px' }}>
            <div style={{ color:'#f1f5f9', fontSize:'12px', fontWeight:'600' }}>{viewingEntry.patient.nombre}</div>
            <div style={{ color:'#64748b', fontSize:'10px', marginTop:'2px' }}>{viewingEntry.informeId} · {viewingEntry.fecha}</div>
          </div>
          <ScaledPreview sec={viewingEntry.sections} pid={viewingEntry.informeId} qr={viewingEntry.qrDataUrl} pat={viewingEntry.patient} />
          {viewingEntry.sections?.length > 0 && (
            <button onClick={() => handleDownloadHistoryPdf(viewingEntry)}
              style={{ background:'linear-gradient(135deg,#1e40af,#3b82f6)', color:'white', borderRadius:'6px',
                padding:'11px', fontSize:'12px', fontWeight:'700', border:'none',
                display:'flex', alignItems:'center', justifyContent:'center', gap:'6px', cursor:'pointer' }}>
              Descargar PDF
            </button>
          )}
        </>
      ) : history.length === 0 ? (
        <div style={{ textAlign:'center', padding:'48px 16px', color:'#475569', fontSize:'12px' }}>
          <div style={{ color:'#64748b', fontWeight:'600', marginBottom:'4px' }}>Sin informes registrados</div>
          <div style={{ color:'#334155', fontSize:'11px' }}>Los informes confirmados aparecerán aquí automaticamente.</div>
        </div>
      ) : (
        <>
          <div style={{ color:'#64748b', fontSize:'10px', fontWeight:'700', textTransform:'uppercase', letterSpacing:'0.5px' }}>
            {history.length} informe{history.length !== 1 ? 's' : ''} registrado{history.length !== 1 ? 's' : ''}
          </div>
          {history.map(e => (
            <HistoryCard key={e.informeId} entry={e} onView={() => setViewingEntry(e)} onDownloadPdf={() => handleDownloadHistoryPdf(e)} />
          ))}
        </>
      )}
    </div>
  );

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div style={{ height:'100%', overflowY:'auto', background:'#0a0f1a', color:'#e2e8f0' }}>
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
            {state === 'idle' && (apiStatus === 'online' ? (studyUID ? 'Listo para analizar' : 'Esperando estudio DICOM') : 'Sin conexión')}
            {state === 'analizando' && 'Procesando...'}
            {state === 'revisar' && 'Revisión pendiente'}
            {state === 'guardado' && 'Informe guardado'}
            {state === 'rechazado' && 'Informe rechazado'}
          </div>
        </div>
        {history.length > 0 && (
          <div style={{ background:'#1e40af', color:'#93c5fd', borderRadius:'10px', padding:'1px 7px', fontSize:'10px', fontWeight:'700' }}>
            {history.length}
          </div>
        )}
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

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
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

# infrastructure.pdf_service - PDF generation for medical reports.
# Primary engine: Gotenberg (Chromium headless)
# Fallback engine: fpdf2 (manual PDF primitives)
#
# Public API unchanged: build_pdf_streaming_response() and generate_informe_pdf()

from __future__ import annotations

import base64
import io
import logging
import os
import re as _re_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.responses import StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

# Absolute template root (Docker: /app/src/infrastructure/templates).
BASE_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = os.path.join(str(BASE_DIR), "templates")
if not os.path.isdir(_TEMPLATE_DIR):
    _crit = f"CRITICAL ERROR: Template directory not found at {_TEMPLATE_DIR}"
    print(_crit, flush=True)
    logger.critical(_crit)
_JINJA_ENV = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
)

# Log template resolution once per process (ops verify Docker path e.g. /app/src/infrastructure/templates)
_TEMPLATE_DIR_LOGGED = False


def _log_pdf_template_dir_once() -> None:
    global _TEMPLATE_DIR_LOGGED
    if _TEMPLATE_DIR_LOGGED:
        return
    _TEMPLATE_DIR_LOGGED = True
    logger.info(
        "IMAGIAX PDF: Jinja2 template directory is %s (informe_clinico.html)",
        _TEMPLATE_DIR,
    )


def _as_pdf_bytes(data: bytes | bytearray | memoryview | str) -> bytes:
    """Normalize fpdf2 / httpx payloads to true ``bytes`` for Starlette streaming.

    fpdf2's ``FPDF.output()`` often returns ``bytearray``; StreamingResponse then
    calls ``chunk.encode()`` for non-bytes types and crashes on ``bytearray``.
    """
    if isinstance(data, str):
        return data.encode("latin-1", errors="replace")
    return bytes(data)


# ── Logo loading (PNG, base64) ────────────────────────────────────────────────
_LOGO_PNG_BYTES: bytes | None = None
_LOGO_PNG_B64: str | None = None


def _get_logo_b64() -> str:
    """Return base64-encoded PNG logo for HTML data URI. Cached."""
    global _LOGO_PNG_B64, _LOGO_PNG_BYTES
    if _LOGO_PNG_B64 is not None:
        return _LOGO_PNG_B64
    try:
        from infrastructure.logo_b64 import ANDES_SALUD_PNG_B64
        _LOGO_PNG_B64 = ANDES_SALUD_PNG_B64
        _LOGO_PNG_BYTES = base64.b64decode(ANDES_SALUD_PNG_B64)
        return _LOGO_PNG_B64
    except Exception:
        return ""


def _get_logo_bytes() -> bytes | None:
    """Return PNG bytes for fpdf2 fallback. Cached."""
    global _LOGO_PNG_BYTES
    if _LOGO_PNG_BYTES is not None:
        return _LOGO_PNG_BYTES
    _get_logo_b64()  # populates both caches
    return _LOGO_PNG_BYTES
# ── Color tokens (v53 \u2014 Mayo-style clinical palette) ─────────────────────────
_TITLE_BLUE     = (26,  58,  94)     # section title text  PDF_THEME.title.color
_SECTION_SEP    = (204, 204, 204)    # section separator   PDF_THEME.section.separatorColor
_BODY_TEXT      = (17,  24,  39)     # body text \u2014 Ink-900 (v53: darker for readability)
_FOOTER_GRAY    = (102, 102, 102)    # footer text
_LABEL_GRAY     = (80,  80,  80)     # patient label
_VALUE_DARK     = (26,  26,  26)     # patient value
_PATIENT_BG     = (248, 249, 250)    # patient grid bg
_PATIENT_ROW_BG = (241, 243, 245)    # patient row alt bg
_CONCLUSION_BG  = (235, 244, 255)    # conclusion panel background (pale blue)
_CONCLUSION_BDR = (147, 197, 253)    # conclusion panel border
_CONCLUSION_BAR = (26,  58,  94)     # conclusion left-accent bar \u2014 dark navy
_DIVIDER        = (203, 213, 225)    # generic divider
_HEADER_LINE    = (26,  58,  94)     # header separator line \u2014 navy accent

# ── Regex patterns ─────────────────────────────────────────────────────────────
_SECTION_RE = _re_mod.compile(
    r'^([A-Z\xc1\xc9\xcd\xd3\xda\xd1\xc0-\xdc][A-Z\xc1\xc9\xcd\xd3\xda\xd1\xc0-\xdc '
    r'\u00c0-\u00dc\/\(\)\-]{3,})\s*$',
    _re_mod.M,
)

# Sub-header: "Word(s):" at start of line \u2014 bold prefix, normal continuation
_SUBHEADER_RE = _re_mod.compile(
    r'^([A-Z\xc1\xc9\xcd\xd3\xda\xd1][A-Za-z\xc0-\xff\s]{0,50}:)\s*(.*)$'
)

# Conclusion section detection
_CONCLUSION_RE = _re_mod.compile(r'CONCLUSI[O\xd3]N(ES)?', _re_mod.IGNORECASE)

# XML tag stripper
_XML_RE = _re_mod.compile(r'<[^>]+>')


# ── Text helpers ──────────────────────────────────────────────────────────────

def _pdf_safe(text: str) -> str:
    """Normalize text for fpdf2 core fonts (Latin-1).

    Replaces non-Latin-1 typographic characters with ASCII equivalents.
    Spanish accented characters (valid in Latin-1) are preserved.
    """
    _replacements = {
        '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2014': '-', '\u2013': '-',
        '\u2026': '...',
        '\u00b7': '*',
        '\u2022': '-',
        '\u00ae': '(R)',
        '\u00a0': ' ',
        '\u2192': '->',
        '\u2190': '<-',
        '\u2264': '<=',
        '\u2265': '>=',
        '\u2248': '~',
        '\u00d7': 'x',
    }
    result = []
    for ch in text:
        if ch in _replacements:
            result.append(_replacements[ch])
        else:
            try:
                ch.encode('latin-1')
                result.append(ch)
            except (UnicodeEncodeError, ValueError):
                result.append('?')
    return ''.join(result)


def _strip_markdown(text: str) -> str:
    """Remove LLM markdown markers so they don't appear as literal characters in the PDF."""
    text = _re_mod.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=_re_mod.DOTALL)
    text = _re_mod.sub(r'\*(.+?)\*',     r'\1', text, flags=_re_mod.DOTALL)
    text = _re_mod.sub(r'\_(.+?)\_',     r'\1', text, flags=_re_mod.DOTALL)
    text = _re_mod.sub(r'^#{1,4}\s*',    '',    text, flags=_re_mod.MULTILINE)
    return text


def _format_age_display_pdf(patient_age: str | None) -> str:
    """Age string for PDF patient grid — '-' instead of 'No consignada' (v58)."""
    s = _format_age(patient_age)
    return "-" if s == "No consignada" else s


def _format_age(patient_age: str | None) -> str:
    """Convert patient_age from DB to human-readable Spanish.

    Returns 'No consignada' when age is null, zero, or unparseable.
    Only shows months when the DICOM tag explicitly carries them.
    """
    if not patient_age:
        return 'No consignada'
    s = str(patient_age).strip().upper()
    m = _re_mod.match(r'^(\d+)Y(\d+)M$', s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if y == 0 and mo == 0:
            return 'No consignada'
        if y == 0:
            return f'{mo} meses'
        if y >= 18:
            return f'{y} años'
        if mo > 0:
            return f'{y} a\xf1os {mo} meses'
        return f'{y} a\xf1os'
    m = _re_mod.match(r'^(\d+)Y$', s)
    if m:
        y = int(m.group(1))
        return 'No consignada' if y == 0 else f'{y} a\xf1os'
    return 'No consignada'


def _format_license(license_str: str) -> str:
    """Return formatted license or empty string if not real/meaningful."""
    if not license_str:
        return ''
    s = license_str.strip()
    # Skip placeholder-like values
    if s.lower() in ('', '-', 'n/a', 'na', 'none', 'sin registro', 'sin matricula'):
        return ''
    return s


def _sanitize_patient_name(name: str | None) -> str:
    """DICOM PN (Family^Given^…) → 'Enrique Godoy'; otherwise collapse spaces + .title() (v59)."""
    if not name:
        return "No consignado"
    raw = str(name).strip()
    if not raw or raw.lower() in ("no consignado", "unknown", "anonymous"):
        return "No consignado"
    if "^" in raw:
        caret_parts = [p.strip() for p in raw.split("^") if p.strip()]
        if len(caret_parts) >= 2:
            family = _re_mod.sub(r"\s+", " ", caret_parts[0]).strip()
            given = _re_mod.sub(r"\s+", " ", caret_parts[1]).strip()
            out = f"{given.title()} {family.title()}".strip()
            return out or "No consignado"
        one = _re_mod.sub(r"\s+", " ", caret_parts[0]).strip()
        return one.title() if one else "No consignado"
    cleaned = _re_mod.sub(r"\s+", " ", raw.replace("^", " ")).strip()
    return cleaned.title() if cleaned else "No consignado"


def _format_chilean_rut(raw_rut: str | None) -> str:
    """Chilean RUT mask XX.XXX.XXX-X using thousands grouping (int body + DV)."""
    if not raw_rut:
        return "-"
    s = str(raw_rut).strip().upper()
    if not s or s in ("-", "NONE", "N/A"):
        return "-"
    if _re_mod.match(r"^\d{1,2}\.\d{3}\.\d{3}-[\dK]$", s):
        return s
    clean = _re_mod.sub(r"[^0-9K]", "", s)
    if len(clean) < 2:
        return str(raw_rut).strip() or "-"
    dv = clean[-1].upper()
    body = clean[:-1]
    if not body.isdigit() or not (dv.isdigit() or dv == "K"):
        return str(raw_rut).strip() or "-"
    try:
        formatted = f"{int(body):,}".replace(",", ".")
    except ValueError:
        return str(raw_rut).strip() or "-"
    return f"{formatted}-{dv}"


def _format_chilean_date(raw_date: str | None) -> str:
    """Convert study date to DD-MM-YYYY (YYYYMMDD or YYYY-MM-DD); pass through if already DD-MM-YYYY."""
    if not raw_date:
        return "-"
    value = str(raw_date).strip()
    if _re_mod.match(r"^\d{2}-\d{2}-\d{4}$", value):
        return value
    m = _re_mod.match(r"^(\d{4})(\d{2})(\d{2})$", value)
    if m:
        yyyy, mm, dd = m.groups()
        return f"{dd}-{mm}-{yyyy}"
    m2 = _re_mod.match(r"^(\d{4})-(\d{2})-(\d{2})$", value)
    if m2:
        yyyy, mm, dd = m2.groups()
        return f"{dd}-{mm}-{yyyy}"
    return value or "-"


def _sanitize_brand_subtitle(subtitle: str) -> str:
    """Fix known typo 'SALLID' -> 'SALUD' in institutional subtitle."""
    return subtitle.replace('SALLID', 'SALUD')


# Phrases removed from PDF body / labels before Jinja (v59 — clinical polish)
_PDF_STRIP_LITERAL_PHRASES: tuple[str, ...] = (
    "INFORME FINAL (VALIDADO POR MÉDICO)",
    "INFORME FINAL (VALIDADO POR MEDICO)",
    "Alerta: NORMAL",
    "ALERTA: NORMAL",
    "NOTA DE LIMITACIÓN:",
    "NOTA DE LIMITACION:",
    "NOTA ACLARATORIA:",
)

# Emoji / pictograph / replacement chars that render as ?? in PDF engines
_EMOJI_AND_JUNK_RE = _re_mod.compile(
    "["
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200B-\U0000200F"  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    "\U00002060-\U00002064"  # word joiner / invisible
    "\U0001F300-\U0001FAFF"  # symbols & pictographs / supplemental
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U00002600-\U000026FF"  # misc symbols
    "\U00002700-\U000027BF"  # dingbats
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001F1E6-\U0001F1FF"  # regional indicators
    "]+",
    _re_mod.UNICODE,
)


def _strip_pdf_noise_phrases(text: str) -> str:
    """Remove fixed boilerplate strings from report text (v59)."""
    if not text:
        return text
    t = text
    for ph in _PDF_STRIP_LITERAL_PHRASES:
        t = t.replace(ph, "")
    return _re_mod.sub(r"\n{3,}", "\n\n", t).strip()


def _strip_ai_mojibake_and_emoji(text: str) -> str:
    """Remove emoji/special Unicode that shows as ??; fix broken ?? before LIMITACIÓN (emergency PDF)."""
    if not text:
        return text
    s = text.replace("\ufffd", "")
    s = _EMOJI_AND_JUNK_RE.sub("", s)
    # Broken placeholder(s) before NOTA DE LIMITACIÓN → clean heading
    s = _re_mod.sub(
        r"\?{1,3}\s*NOTA\s+DE\s+LIMITACI[OÓ]N\s*:?",
        "NOTA DE LIMITACIÓN:",
        s,
        flags=_re_mod.IGNORECASE,
    )
    s = _re_mod.sub(r"\?{2,}\s*", "", s)  # stray ?? clusters
    s = _re_mod.sub(r"[ \t]+\n", "\n", s)
    s = _re_mod.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _remove_limitaciones_section_raw(text: str) -> str:
    """Drop LIMITACIONES block from raw report (heading + body) before section parsing."""
    if not text or not text.strip():
        return text
    lines = text.split("\n")
    out: list[str] = []
    skipping = False
    for line in lines:
        st = line.strip()
        if not skipping:
            if _is_limitaciones_heading(st):
                skipping = True
                continue
            out.append(line)
            continue
        if _SECTION_RE.match(st) and len(st) < 65 and not _is_limitaciones_heading(st):
            skipping = False
            out.append(line)
    return "\n".join(out).rstrip()


def _preprocess_ai_report_for_jinja(report_body: str) -> str:
    """Single pipeline before Jinja2 / section parse: LIMITACIONES cut, math, noise, emoji strip."""
    t = _strip_markdown((report_body or "").strip())
    t = _remove_limitaciones_section_raw(t)
    t = _sanitize_gemini_math_artifacts(t)
    t = _strip_pdf_noise_phrases(t)
    t = _strip_ai_mojibake_and_emoji(t)
    return t


def _is_limitaciones_heading(title: str | None) -> bool:
    """True if section title is the LIMITACIONES / NOTA DE LIMITACIÓN block (omitted from PDF v60)."""
    if not title:
        return False
    norm = title.strip().upper()
    norm = norm.replace("Ó", "O").replace("Á", "A").replace("Í", "I").replace("Ú", "U")
    return (
        norm.startswith("LIMITACION")
        or norm.startswith("NOTA DE LIMITACION")
        or norm.startswith("NOTA ACLARATORIA")
    )


def sanitize_pdf_demographics_bundle(informe: dict[str, Any]) -> dict[str, str]:
    """v59 — single scrub for patient row fields before Jinja2 / fpdf."""
    return {
        "patient_name": _sanitize_patient_name(informe.get("patient_name")),
        "patient_rut": _format_chilean_rut(informe.get("patient_id")),
        "study_date_fmt": _format_chilean_date(informe.get("study_date")),
        "medico_ref": _format_physician_display_line(
            informe.get("referring_physician") or informe.get("medico_referente"),
        ),
    }


def _format_physician_display_line(name: str | None) -> str:
    """Referring physician line for PDF — collapse spaces, title case (v58)."""
    if not name:
        return "-"
    s = _re_mod.sub(r"\s+", " ", str(name).replace("^", " ").strip())
    if not s or s == "-":
        return "-"
    return s.title()


def _sanitize_gemini_math_artifacts(text: str) -> str:
    """Turn accidental LaTeX-style fragments into plain Unicode (v58 — Impresión Diagnóstica)."""
    if not text:
        return text
    s = text
    # $>=2$ style (with optional whitespace)
    s = _re_mod.sub(r"\$\s*>=\s*(\d+(?:\.\d+)?)\s*\$", r"≥ \1", s)
    s = _re_mod.sub(r"\$\s*<=\s*(\d+(?:\.\d+)?)\s*\$", r"≤ \1", s)
    s = _re_mod.sub(r"\$\s*>\s*(\d+(?:\.\d+)?)\s*\$", r"> \1", s)
    s = _re_mod.sub(r"\$\s*<\s*(\d+(?:\.\d+)?)\s*\$", r"< \1", s)
    # \geq / \leq escapes sometimes leak from model output
    s = _re_mod.sub(r"\\geq\s*(\d+(?:\.\d+)?)", r"≥ \1", s)
    s = _re_mod.sub(r"\\leq\s*(\d+(?:\.\d+)?)", r"≤ \1", s)
    s = _re_mod.sub(r"\\ge\s*(\d+(?:\.\d+)?)", r"≥ \1", s)
    s = _re_mod.sub(r"\\le\s*(\d+(?:\.\d+)?)", r"≤ \1", s)
    return s


# ── PDF drawing helpers ───────────────────────────────────────────────────────

def _draw_section_header(pdf: Any, label: str) -> None:
    """Render a section heading: blue bold title + thin blue separator line."""
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_text_color(*_TITLE_BLUE)
    pdf.cell(0, 6, _pdf_safe(label), new_x='LMARGIN', new_y='NEXT')
    pdf.set_draw_color(*_TITLE_BLUE)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 170, pdf.get_y())
    pdf.ln(3)
    pdf.set_text_color(*_BODY_TEXT)
    pdf.set_font('Helvetica', '', 10)


def _multi_cell_safe(pdf: Any, text: str) -> None:
    """Render text with automatic word-wrap. Never clips content."""
    pdf.multi_cell(0, 5.5, _pdf_safe(text), new_x='LMARGIN', new_y='NEXT')


def _render_subheader_line(pdf: Any, line: str) -> None:
    """Render a line that matches 'Label: value' pattern with bold label."""
    m = _SUBHEADER_RE.match(line)
    if not m:
        pdf.multi_cell(0, 6, _pdf_safe(line), new_x='LMARGIN', new_y='NEXT')
        return

    prefix = _pdf_safe(m.group(1))  # e.g. "T\xe9cnica:"
    rest   = _pdf_safe(m.group(2).strip())

    # Calculate x after bold prefix to continue on the same line
    y_before = pdf.get_y()
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_text_color(*_VALUE_DARK)

    if rest:
        # Write bold prefix inline then normal continuation
        prefix_w = pdf.get_string_width(prefix + ' ')
        avail_w  = pdf.w - pdf.l_margin - pdf.r_margin

        if prefix_w < avail_w * 0.6:
            # Single line: bold prefix + normal rest
            pdf.cell(prefix_w, 6, prefix + ' ', new_x='RIGHT', new_y='TOP')
            pdf.set_font('Helvetica', '', 10)
            pdf.set_text_color(*_BODY_TEXT)
            remaining_w = avail_w - prefix_w
            pdf.multi_cell(remaining_w, 6, rest, new_x='LMARGIN', new_y='NEXT')
        else:
            # Prefix too wide \u2014 render on separate lines
            pdf.multi_cell(0, 6, prefix, new_x='LMARGIN', new_y='NEXT')
            pdf.set_font('Helvetica', '', 10)
            pdf.set_text_color(*_BODY_TEXT)
            pdf.multi_cell(0, 6, rest, new_x='LMARGIN', new_y='NEXT')
    else:
        # Prefix only (no value on same line)
        pdf.multi_cell(0, 6, prefix, new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(*_BODY_TEXT)


def _draw_conclusion_box(pdf: Any, title: str, body_lines: list[str]) -> None:
    """Render a conclusion section with Mayo-style left-accent-bar pattern.

    Layout:
    \u250c\u2500\u2500\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510
    \u2502\u258c \u2502  CONCLUSI\xd3N                              \u2502
    \u2502\u258c \u2502  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500            \u2502
    \u2502\u258c \u2502  Body text content here...               \u2502
    \u2502\u258c \u2502  More content...                          \u2502
    \u2514\u2500\u2500\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518

    Left bar: 3mm wide navy accent (_CONCLUSION_BAR)
    Panel: pale blue background (_CONCLUSION_BG) with thin border (_CONCLUSION_BDR)
    """
    BAR_W = 3    # mm \u2014 left accent bar width
    PAD   = 4    # mm \u2014 padding inside content area

    # Estimate content height
    line_h    = 6  # matching body line height for 10pt
    body_text = '\n'.join(body_lines)
    chars_per_line = 80  # fewer chars/line at 10pt
    estimated_lines = sum(
        max(1, (len(l) // chars_per_line) + 1) if l.strip() else 0
        for l in body_lines
    )
    box_h = PAD + 7 + 2 + (estimated_lines * line_h) + PAD  # title + sep + body + padding
    box_h = max(box_h, 22)

    # Page-break guard
    if pdf.get_y() + box_h + 10 > pdf.h - pdf.b_margin:
        pdf.add_page()

    x0  = pdf.l_margin
    y0  = pdf.get_y()
    w   = pdf.w - pdf.l_margin - pdf.r_margin

    # Draw pale blue content panel (full width)
    pdf.set_fill_color(*_CONCLUSION_BG)
    pdf.set_draw_color(*_CONCLUSION_BDR)
    pdf.set_line_width(0.4)
    pdf.rect(x0, y0, w, box_h, style='FD')

    # Draw left accent bar (navy, 3mm wide)
    pdf.set_fill_color(*_CONCLUSION_BAR)
    pdf.rect(x0, y0, BAR_W, box_h, style='F')

    # Title inside content area (offset by bar width + padding)
    content_x = x0 + BAR_W + PAD
    content_w = w - BAR_W - PAD * 2

    pdf.set_xy(content_x, y0 + PAD)
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_text_color(*_TITLE_BLUE)
    pdf.cell(content_w, 6, _pdf_safe(title), new_x='LMARGIN', new_y='NEXT')

    # Separator line inside content area
    sep_y = pdf.get_y()
    pdf.set_draw_color(*_CONCLUSION_BDR)
    pdf.set_line_width(0.3)
    pdf.line(content_x, sep_y, x0 + w - PAD, sep_y)
    pdf.ln(2)

    # Body text inside content area
    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(*_BODY_TEXT)

    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            pdf.ln(3)
            continue
        pdf.set_x(content_x)
        m = _SUBHEADER_RE.match(stripped)
        if m:
            prefix = _pdf_safe(m.group(1))
            rest   = _pdf_safe(m.group(2).strip())
            pdf.set_font('Helvetica', 'B', 10)
            pdf.set_text_color(*_VALUE_DARK)
            prefix_w = min(pdf.get_string_width(prefix + ' '), content_w * 0.6)
            pdf.set_x(content_x)
            pdf.cell(prefix_w, 6, prefix + ' ', new_x='RIGHT', new_y='TOP')
            pdf.set_font('Helvetica', '', 10)
            pdf.set_text_color(*_BODY_TEXT)
            if rest:
                pdf.multi_cell(content_w - prefix_w, 6, rest, new_x='LMARGIN', new_y='NEXT')
            else:
                pdf.ln(6)
        else:
            pdf.multi_cell(content_w, 6, _pdf_safe(stripped), new_x='LMARGIN', new_y='NEXT')

    # Move cursor below the box
    pdf.set_y(y0 + box_h + 3)


def _build_qr_image(url: str) -> Any | None:
    """Build QR code PNG in a BytesIO buffer (high module count for ~300dpi when printed small)."""
    try:
        import qrcode
        import qrcode.constants
        from io import BytesIO as _BytesIO

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = _BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        return None


# ── Main public API ───────────────────────────────────────────────────────────

# ── QR code generation ─────────────────────────────────────────────────────────
def _build_qr_svg(data: str) -> str:
    """Build QR code as SVG string (vector, not pixelated)."""
    try:
        import qrcode
        from io import BytesIO

        qr = qrcode.QRCode(version=1, box_size=4, border=1)
        qr.add_data(data)
        qr.make(fit=True)

        from qrcode.image.svg import SvgImage
        img = qr.make_image(fill_color='black', back_color='white', image_factory=SvgImage)
        buf = BytesIO()
        img.save(buf)
        buf.seek(0)
        return buf.getvalue().decode('utf-8')
    except Exception as exc:
        logger.warning("QR SVG generation failed: %s", exc)
        return ""


def _build_qr_svg_markup(data: str) -> str:
    """Vector QR for HTML/Gotenberg — path-based SVG, larger modules (v58, print-sharp)."""
    if not data:
        return ""
    try:
        import qrcode
        import qrcode.constants
        from io import BytesIO

        from qrcode.image.svg import SvgPathImage

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=2,
            image_factory=SvgPathImage,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode("utf-8")
        svg = _re_mod.sub(r"<\?xml[^>]*\?>", "", svg).strip()
        if not svg.lstrip().lower().startswith("<svg"):
            return ""
        return svg
    except Exception:
        try:
            import qrcode
            from io import BytesIO

            from qrcode.image.svg import SvgImage

            qr = qrcode.QRCode(version=None, box_size=6, border=2, image_factory=SvgImage)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf)
            svg = buf.getvalue().decode("utf-8")
            svg = _re_mod.sub(r"<\?xml[^>]*\?>", "", svg).strip()
            return svg if svg.lstrip().lower().startswith("<svg") else ""
        except Exception as exc:
            logger.warning("QR SVG markup generation failed: %s", exc)
            return ""


# ── Section parsing (shared between engines) ──────────────────────────────────
def _parse_sections(report_body: str) -> list[tuple[str | None, list[str]]]:
    """Parse report text into sections: list of (heading, [body_lines])."""
    raw_lines = _pdf_safe(report_body).split('\n')
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in raw_lines:
        stripped = line.strip()
        if _SECTION_RE.match(stripped) and len(stripped) < 65:
            if current_lines or current_heading is not None:
                sections.append((current_heading, current_lines))
            current_heading = stripped
            current_lines = []
        else:
            current_lines.append(line)

    sections.append((current_heading, current_lines))
    return sections


def _is_conclusion(title: str) -> bool:
    """Check if a section title matches CONCLUSION pattern."""
    return bool(_re_mod.search(r'CONCLUSI[O\u00d3]N(ES)?', title, _re_mod.IGNORECASE))


# ── Settings resolver ─────────────────────────────────────────────────────────
def _resolve_settings_defaults(
    institution_name: str,
    institution_subtitle: str,
    institution_url: str,
    doctor_name: str,
    doctor_specialty: str,
    doctor_license: str,
    base_url: str,
) -> tuple[str, str, str, str, str, str, str]:
    """Resolve branding kwargs, falling back to Settings when empty."""
    try:
        from app.core.config import settings as _s
        return (
            institution_name or _s.IMAGIAX_BRAND_NAME or 'IMAGIAX',
            institution_subtitle or _s.IMAGIAX_BRAND_SUBTITLE or 'imagiax.dev',
            institution_url or getattr(_s, 'IMAGIAX_VIEWER_URL', '') or 'imagiax.dev',
            doctor_name or _s.IMAGIAX_DOCTOR_NAME or '',
            doctor_specialty or _s.IMAGIAX_DOCTOR_SPECIALTY or '',
            doctor_license or _s.IMAGIAX_DOCTOR_LICENSE or '',
            base_url or _s.IMAGIAX_BASE_URL or '',
        )
    except Exception:
        # Fallback if Settings unavailable \u2014 use provided defaults
        return (
            institution_name or 'IMAGIAX',
            institution_subtitle or 'imagiax.dev',
            institution_url or 'imagiax.dev',
            doctor_name or '',
            doctor_specialty or '',
            doctor_license or '',
            base_url or '',
        )

# ══════════════════════════════════════════════════════════════════════════════
# PRIMARY ENGINE: Gotenberg (Chromium → PDF)
# ══════════════════════════════════════════════════════════════════════════════

async def _render_via_gotenberg(html: str) -> bytes:
    """Send HTML to Gotenberg and return PDF bytes.

    Raises httpx.HTTPStatusError or httpx.TimeoutException on failure.
    """
    import httpx

    gotenberg_url = "http://gotenberg:3000/forms/chromium/convert/html"
    try:
        from app.core.config import settings as _s
        gotenberg_url = f"{_s.IMAGIAX_GOTENBERG_URL.rstrip('/')}/forms/chromium/convert/html"
        timeout = _s.IMAGIAX_GOTENBERG_TIMEOUT
    except Exception:
        timeout = 30

    async with httpx.AsyncClient(timeout=timeout) as client:
        files = {
            "files": ("index.html", html, "text/html"),
        }
        data = {
            "scale": "1.0",                   # True size -- no Chromium scaling
            "paperWidth": "8.27",              # A4 width in inches
            "paperHeight": "11.69",            # A4 height in inches
            "marginTop": "0",                  # Zero margins -- controlled by HTML padding
            "marginBottom": "0",
            "marginLeft": "0",
            "marginRight": "0",
            "printBackground": "true",         # Keep institution colors alive
            "preferCssPageSize": "true",       # Respect @page { size: A4; } from template
        }
        resp = await client.post(gotenberg_url, files=files, data=data)
        resp.raise_for_status()
        return _as_pdf_bytes(resp.content)


async def _generate_gotenberg_pdf(
    informe: dict[str, Any],
    *,
    institution_name: str = 'IMAGIAX',
    institution_subtitle: str = 'imagiax.dev',
    institution_url: str = 'imagiax.dev',
    doctor_name: str = '',
    doctor_specialty: str = '',
    doctor_license: str = '',
    base_url: str = '',
) -> bytes:
    """Generate PDF via Gotenberg (Chromium headless).

    Renders the Jinja2 template to HTML, then sends to Gotenberg for PDF conversion.
    """
    _log_pdf_template_dir_once()
    # Resolve branding defaults
    institution_name, institution_subtitle, institution_url, \
        doctor_name, doctor_specialty, doctor_license, base_url = \
        _resolve_settings_defaults(
            institution_name, institution_subtitle, institution_url,
            doctor_name, doctor_specialty, doctor_license, base_url,
        )

    # Prepare report content
    informe_generado = (informe.get('informe_generado') or '').strip()
    informe_final = (informe.get('informe_final') or '').strip()

    if '<' in informe_generado:
        informe_generado = _re_mod.sub(r'\n{3,}', '\n\n', _XML_RE.sub('', informe_generado)).strip()
    if '<' in informe_final:
        informe_final = _re_mod.sub(r'\n{3,}', '\n\n', _XML_RE.sub('', informe_final)).strip()

    report_body = informe_final or informe_generado
    report_body = _preprocess_ai_report_for_jinja(report_body)
    report_label = (
        "Informe validado - documento clínico"
        if informe_final else
        "INFORME GENERADO POR INTELIGENCIA ARTIFICIAL"
    )

    informe_id = str(informe.get('id') or '')
    short_id = informe_id[:8].upper() if informe_id else 'UNKNOWN'

    # Format license
    lic = _format_license(doctor_license)

    # Clean base_url for QR (strip /api suffix)
    clean_url = base_url.rstrip('/')
    if clean_url.endswith('/api'):
        clean_url = clean_url[:-4]

    # Vector QR for HTML (v58 — no raster blur in Gotenberg/Chromium)
    qr_url = f'{clean_url}/informes/{informe_id[:8]}/ver' if base_url and informe_id else ''
    qr_svg_inline = _build_qr_svg_markup(qr_url) if qr_url else ''

    # Logo
    logo_b64 = _get_logo_b64()

    # Format dates
    now = datetime.now(timezone.utc)
    now_iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    now_formatted = now.strftime('%Y-%m-%d %H:%M UTC')

    _demo_pdf = sanitize_pdf_demographics_bundle(informe)
    study_date = _demo_pdf["study_date_fmt"]

    # Parse sections from report body
    sections_raw = _parse_sections(report_body)
    exam_title = _pdf_safe(str(informe.get("exam_description") or informe.get("modalidad") or "Informe Radiológico"))
    modality_display = exam_title if exam_title and exam_title != "Informe Radiológico" else _pdf_safe(str(informe.get("modalidad") or "-"))

    # Build section dicts for template
    template_sections = []
    for heading, body_lines in sections_raw:
        if _is_limitaciones_heading(heading):
            continue
        section_dict = {
            'title': _strip_ai_mojibake_and_emoji(heading or ''),
            'lines': [
                _strip_ai_mojibake_and_emoji(
                    _strip_pdf_noise_phrases(_sanitize_gemini_math_artifacts(l.strip())),
                )
                for l in body_lines
                if l.strip()
            ],
            'is_conclusion': _is_conclusion(heading) if heading else False,
        }
        template_sections.append(section_dict)

    # Render template — never send empty / broken HTML to Gotenberg
    try:
        template = _JINJA_ENV.get_template("informe_clinico.html")
        html = template.render(
            institution_name=institution_name,
            institution_subtitle=_sanitize_brand_subtitle(institution_subtitle),
            institution_url=institution_url,
            exam_title=exam_title,
            informe_id=short_id,
            fecha_examen=study_date,
            fecha_informe=now_formatted,
            patient_name=_pdf_safe(_demo_pdf["patient_name"]),
            patient_rut=_pdf_safe(_demo_pdf["patient_rut"]),
            patient_age=_format_age_display_pdf(informe.get("patient_age")),
            modalidad=modality_display,
            medico_ref=_pdf_safe(_demo_pdf["medico_ref"]),
            nota_clinica=_strip_ai_mojibake_and_emoji(
                _sanitize_gemini_math_artifacts((informe.get("nota_clinica") or "").strip()),
            ),
            report_label=report_label,
            sections=template_sections,
            is_conclusion=_is_conclusion,
            doctor_name=doctor_name,
            doctor_specialty=doctor_specialty,
            doctor_license=lic,
            logo_b64=logo_b64,
            qr_svg_inline=qr_svg_inline,
            qr_url=qr_url,
            now_iso=now_iso,
            now_formatted=now_formatted,
        )
    except Exception as exc:
        logger.exception(
            "Jinja2 PDF render failed (template_dir=%s, template=informe_clinico.html)",
            _TEMPLATE_DIR,
        )
        raise RuntimeError(f"PDF template render failed: {exc}") from exc

    if not isinstance(html, str) or not html.strip():
        msg = "Jinja2 produced empty HTML for PDF — aborting Gotenberg call"
        logger.error(msg)
        raise RuntimeError(msg)

    return await _render_via_gotenberg(html)


# ══════════════════════════════════════════════════════════════════════════════
# FALLBACK ENGINE: fpdf2 (manual PDF primitives) — KEPT INTACT
# ══════════════════════════════════════════════════════════════════════════════

def _generate_fallback_pdf(
    informe: dict[str, Any],
    *,
    institution_name: str = 'IMAGIAX',
    institution_subtitle: str = 'imagiax.dev',
    institution_url: str = 'imagiax.dev',
    doctor_name: str = '',
    doctor_specialty: str = '',
    doctor_license: str = '',
    base_url: str = '',
) -> bytes:
    """Generate a professional PDF in memory for a medical radiology report.

    Returns raw PDF bytes ready for streaming or saving.
    """
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise RuntimeError('fpdf2 no instalado. Agregue fpdf2 a requirements.txt.') from exc

    # ── Prepare content ───────────────────────────────────────────────────────
    informe_generado = (informe.get('informe_generado') or '').strip()
    informe_final    = (informe.get('informe_final')    or '').strip()

    if '<' in informe_generado:
        informe_generado = _re_mod.sub(r'\n{3,}', '\n\n', _XML_RE.sub('', informe_generado)).strip()
    if '<' in informe_final:
        informe_final = _re_mod.sub(r'\n{3,}', '\n\n', _XML_RE.sub('', informe_final)).strip()

    _log_pdf_template_dir_once()
    _report_body = _preprocess_ai_report_for_jinja(informe_final or informe_generado)
    _report_label = (
        "Informe validado - documento clínico"
        if informe_final else
        "INFORME GENERADO POR INTELIGENCIA ARTIFICIAL"
    )

    informe_id = str(informe.get('id') or '')
    exam_title = _pdf_safe(str(informe.get('exam_description') or informe.get('modalidad') or 'Informe Radiol\xf3gico'))

    # ── FPDF setup ────────────────────────────────────────────────────────────
    pdf = FPDF()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=30)
    pdf.set_margins(left=20, top=25, right=20)
    pdf.add_page()

    # ── Header (v53 \u2014 white background, navy accent line) ──────────────────────
    HEADER_H  = 28
    LOGO_W    = 26    # mm \u2014 logo width
    LOGO_H    = 16    # mm \u2014 logo height
    LOGO_X    = 20
    LOGO_Y    = 5

    # White background (no dark band \u2014 clean clinical style)
    # Logo \u2014 Andes Salud PNG embedded from logo_b64.py
    logo_bytes = _get_logo_bytes()
    if logo_bytes:
        _logo_buf = io.BytesIO(logo_bytes)
        try:
            pdf.image(_logo_buf, x=LOGO_X, y=LOGO_Y, w=LOGO_W, h=LOGO_H)
        except Exception:
            logo_bytes = None   # fallback to text-only header

    # Institution name \u2014 right of logo, DARK text on white
    TEXT_X = LOGO_X + LOGO_W + 5 if logo_bytes else 20
    pdf.set_xy(TEXT_X, 6)
    pdf.set_font('Helvetica', 'B', 14)
    pdf.set_text_color(*_VALUE_DARK)   # Dark text on white \u2014 clinical standard
    pdf.cell(210 - TEXT_X - 20, 7, _pdf_safe(institution_name), new_x='LMARGIN', new_y='NEXT')

    # Subtitle line
    pdf.set_xy(TEXT_X, 14)
    pdf.set_font('Helvetica', '', 7.5)
    pdf.set_text_color(*_LABEL_GRAY)
    pdf.cell(210 - TEXT_X - 20, 4,
             _pdf_safe(institution_subtitle) + '  |  Informe M\xe9dico Radiol\xf3gico  |  Confidencial',
             new_x='LMARGIN', new_y='NEXT')

    # Exam title \u2014 right-aligned
    pdf.set_xy(20, 21)
    pdf.set_font('Helvetica', 'B', 8)
    pdf.set_text_color(*_LABEL_GRAY)
    pdf.cell(170, 4, _pdf_safe(exam_title), align='R', new_x='LMARGIN', new_y='NEXT')

    # Navy accent line under header \u2014 the signature visual element
    pdf.set_y(HEADER_H)
    pdf.set_draw_color(*_HEADER_LINE)
    pdf.set_line_width(0.8)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(5)

    # ── Patient data grid ─────────────────────────────────────────────────────
    _demo_pdf = sanitize_pdf_demographics_bundle(informe)
    patient_name = _pdf_safe(_demo_pdf["patient_name"])
    patient_id   = _pdf_safe(_demo_pdf["patient_rut"])
    study_date   = _pdf_safe(_demo_pdf["study_date_fmt"])
    modalidad    = _pdf_safe(str(informe.get('modalidad') or '-'))
    patient_age  = _format_age_display_pdf(informe.get("patient_age"))
    medico_ref   = _pdf_safe(_demo_pdf["medico_ref"])

    y0   = pdf.get_y()
    bx   = pdf.l_margin
    bw   = pdf.w - pdf.l_margin - pdf.r_margin
    bh   = 36  # v58: sin fila de alerta

    # Grid background
    pdf.set_fill_color(*_PATIENT_BG)
    pdf.set_draw_color(*_SECTION_SEP)
    pdf.set_line_width(0.3)
    pdf.rect(bx, y0, bw, bh, 'FD')

    # Grid title row
    pdf.set_xy(bx + 4, y0 + 3)
    pdf.set_font('Helvetica', 'B', 6.5)
    pdf.set_text_color(*_LABEL_GRAY)
    pdf.cell(bw - 8, 5, 'DATOS DEL PACIENTE', new_x='LMARGIN', new_y='NEXT')
    pdf.set_draw_color(*_SECTION_SEP)
    pdf.set_line_width(0.15)
    sep_y = pdf.get_y()
    pdf.line(bx + 4, sep_y, bx + bw - 4, sep_y)
    pdf.ln(2)

    # Helper: render label + value pair in a cell
    def _grid_cell(label: str, value: str, cell_w: float, is_last: bool = False) -> None:
        pdf.set_font('Helvetica', 'B', 7)
        pdf.set_text_color(*_LABEL_GRAY)
        lw = pdf.get_string_width(label + ' ')
        pdf.cell(lw, 5, label, new_x='RIGHT', new_y='TOP')
        pdf.set_font('Helvetica', '', 7)
        pdf.set_text_color(*_VALUE_DARK)
        rest_w = cell_w - lw
        pdf.cell(rest_w, 5, value[:int(rest_w / 1.8)], new_x='RIGHT' if not is_last else 'LMARGIN', new_y='TOP' if not is_last else 'NEXT')

    COL_W = (bw - 8) / 2

    # Row 1: Paciente | RUT
    pdf.set_xy(bx + 4, pdf.get_y())
    _grid_cell('Paciente:', patient_name[:38], COL_W)
    _grid_cell('RUT:', patient_id[:22], COL_W, is_last=True)

    # Row 2: Modalidad | Fecha
    pdf.set_xy(bx + 4, pdf.get_y())
    _grid_cell('Modalidad:', modalidad[:20], COL_W)
    _grid_cell('Fecha:', study_date[:20], COL_W, is_last=True)

    # Row 3: Edad | M\xe9dico Referente
    pdf.set_xy(bx + 4, pdf.get_y())
    _grid_cell('Edad:', patient_age[:20], COL_W)
    _grid_cell('M\xe9dico Ref:', medico_ref[:22], COL_W, is_last=True)

    pdf.set_y(y0 + bh + 5)

    # ── Indicaci\xf3n cl\xednica ────────────────────────────────────────────────────
    nota_clinica = _strip_ai_mojibake_and_emoji(
        _sanitize_gemini_math_artifacts((informe.get("nota_clinica") or "").strip()),
    )
    if nota_clinica:
        _draw_section_header(pdf, 'INDICACI\xd3N CL\xcdNICA')
        pdf.set_font('Helvetica', 'I', 10)
        pdf.set_text_color(*_BODY_TEXT)
        _multi_cell_safe(pdf, nota_clinica)
        pdf.ln(8)

    # ── Report body ───────────────────────────────────────────────────────────
    if _report_body:
        _draw_section_header(pdf, _report_label)
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(*_BODY_TEXT)

        # Split into sections by all-caps heading detection
        raw_lines = _pdf_safe(_report_body).split('\n')

        # Group lines into sections: list of (heading | None, [body lines])
        # This lets us detect CONCLUSI\xd3N sections and box them
        sections: list[tuple[str | None, list[str]]] = []
        current_heading: str | None = None
        current_lines: list[str] = []

        for line in raw_lines:
            stripped = line.strip()
            if _SECTION_RE.match(stripped) and len(stripped) < 65:
                if current_lines or current_heading is not None:
                    sections.append((current_heading, current_lines))
                current_heading = stripped
                current_lines = []
            else:
                current_lines.append(line)

        sections.append((current_heading, current_lines))

        for heading, body_lines in sections:
            if _is_limitaciones_heading(heading):
                continue
            is_conclusion = heading and bool(_CONCLUSION_RE.search(heading))

            if is_conclusion:
                # Render as a left-accent-bar conclusion section
                pdf.ln(3)
                _draw_conclusion_box(pdf, heading, body_lines)
                pdf.ln(5)
                pdf.set_font('Helvetica', '', 10)
                pdf.set_text_color(*_BODY_TEXT)
                continue

            if heading:
                # Standard section header
                pdf.ln(5)
                _draw_section_header(pdf, heading)

            for line in body_lines:
                stripped = _strip_ai_mojibake_and_emoji(
                    _sanitize_gemini_math_artifacts(line.strip()),
                )
                if not stripped:
                    pdf.ln(3)
                    continue
                # Check sub-header pattern
                sm = _SUBHEADER_RE.match(stripped)
                if sm:
                    _render_subheader_line(pdf, stripped)
                else:
                    pdf.multi_cell(0, 6, stripped, new_x='LMARGIN', new_y='NEXT')

        pdf.ln(8)

    # ── Signature block (v53 \u2014 electronic signature with ISO 8601) ─────────────
    _FIRMA_HEIGHT_MM = 50
    if pdf.get_y() + _FIRMA_HEIGHT_MM > pdf.h - pdf.b_margin:
        pdf.add_page()

    pdf.ln(6)
    pdf.set_draw_color(*_DIVIDER)
    pdf.set_line_width(0.4)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)

    sig_block_y = pdf.get_y()
    SIG_LINE_W  = 60  # mm

    # "Firmado electr\xf3nicamente por:" label
    pdf.set_font('Helvetica', 'I', 8)
    pdf.set_text_color(*_LABEL_GRAY)
    pdf.cell(0, 4, 'Firmado electr\xf3nicamente por:', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)

    if doctor_name:
        # Professional signature: name, specialty, license
        pdf.set_draw_color(*_TITLE_BLUE)
        pdf.set_line_width(0.5)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + SIG_LINE_W, pdf.get_y())
        pdf.ln(2)

        pdf.set_x(pdf.l_margin)
        pdf.set_font('Helvetica', 'B', 10)
        pdf.set_text_color(*_VALUE_DARK)
        pdf.cell(SIG_LINE_W, 5, _pdf_safe(doctor_name), align='C', new_x='LMARGIN', new_y='NEXT')

        if doctor_specialty:
            pdf.set_x(pdf.l_margin)
            pdf.set_font('Helvetica', '', 8)
            pdf.set_text_color(*_LABEL_GRAY)
            pdf.cell(SIG_LINE_W, 4, _pdf_safe(doctor_specialty), align='C', new_x='LMARGIN', new_y='NEXT')

        lic = _format_license(doctor_license)
        if lic:
            pdf.set_x(pdf.l_margin)
            pdf.set_font('Helvetica', '', 7.5)
            pdf.set_text_color(*_FOOTER_GRAY)
            pdf.cell(SIG_LINE_W, 4, 'Mat. ' + _pdf_safe(lic), align='C', new_x='LMARGIN', new_y='NEXT')

        # ISO 8601 timestamp
        iso_ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        pdf.ln(1)
        pdf.set_x(pdf.l_margin)
        pdf.set_font('Helvetica', '', 7)
        pdf.set_text_color(*_FOOTER_GRAY)
        pdf.cell(SIG_LINE_W, 4, 'Fecha: ' + iso_ts, align='C', new_x='LMARGIN', new_y='NEXT')
    else:
        # Generic validation block
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_text_color(*_TITLE_BLUE)
        pdf.cell(0, 5, 'VALIDACI\xd3N M\xc9DICA', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(1)
        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(*_FOOTER_GRAY)
        pdf.multi_cell(
            0, 4.5,
            'Informe generado por sistema de inteligencia artificial IMAGIAX. '
            'Requiere revisi\xf3n, validaci\xf3n y firma del m\xe9dico radi\xf3logo tratante '
            'antes de ser considerado un documento m\xe9dico oficial.',
            new_x='LMARGIN', new_y='NEXT',
        )
        pdf.ln(3)
        # Two-column signature lines
        _sig_y  = pdf.get_y()
        _COL_W  = 80
        pdf.set_draw_color(*_TITLE_BLUE)
        pdf.set_line_width(0.4)
        pdf.line(pdf.l_margin, _sig_y, pdf.l_margin + _COL_W, _sig_y)
        pdf.line(pdf.l_margin + _COL_W + 8, _sig_y, pdf.l_margin + _COL_W * 2 + 8, _sig_y)
        pdf.set_y(_sig_y + 2)
        pdf.set_font('Helvetica', '', 7.5)
        pdf.set_text_color(*_FOOTER_GRAY)
        pdf.cell(_COL_W, 4, 'Firma y Timbre del Radi\xf3logo', new_x='RIGHT', new_y='TOP')
        pdf.cell(8, 4, '', new_x='RIGHT', new_y='TOP')
        pdf.cell(_COL_W, 4, 'N\xba Registro M\xe9dico / RUT', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(4)
        _sig_y2 = pdf.get_y()
        pdf.set_draw_color(*_TITLE_BLUE)
        pdf.line(pdf.l_margin, _sig_y2, pdf.l_margin + _COL_W, _sig_y2)
        pdf.line(pdf.l_margin + _COL_W + 8, _sig_y2, pdf.l_margin + _COL_W * 2 + 8, _sig_y2)
        pdf.set_y(_sig_y2 + 2)
        pdf.cell(_COL_W, 4, 'Nombre del M\xe9dico Tratante', new_x='RIGHT', new_y='TOP')
        pdf.cell(8, 4, '', new_x='RIGHT', new_y='TOP')
        pdf.cell(_COL_W, 4, 'Fecha de Validaci\xf3n', new_x='LMARGIN', new_y='NEXT')

        # ISO 8601 timestamp for unsigned reports too
        iso_ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        pdf.ln(1)
        pdf.set_font('Helvetica', '', 7)
        pdf.set_text_color(*_FOOTER_GRAY)
        pdf.cell(0, 4, 'Generado: ' + iso_ts, align='L', new_x='LMARGIN', new_y='NEXT')

    # ── QR code (right side of signature block) ───────────────────────────────
    if base_url and informe_id:
        # Strip /api suffix \u2014 QR should point to viewer, not API
        clean_url = base_url.rstrip('/')
        if clean_url.endswith('/api'):
            clean_url = clean_url[:-4]
        qr_url = f'{clean_url}/informes/{informe_id[:8]}/ver'
        qr_buf = _build_qr_image(qr_url)
        if qr_buf:
            QR_SIZE = 24  # mm
            qr_x    = pdf.w - pdf.r_margin - QR_SIZE
            qr_y    = sig_block_y - 2
            pdf.image(qr_buf, x=qr_x, y=qr_y, w=QR_SIZE, h=QR_SIZE)
            # "Escanee para verificar" label below QR
            pdf.set_xy(qr_x, qr_y + QR_SIZE + 1)
            pdf.set_font('Helvetica', '', 5.5)
            pdf.set_text_color(*_FOOTER_GRAY)
            pdf.cell(QR_SIZE, 3, 'Escanee para verificar', align='C', new_x='LMARGIN', new_y='NEXT')

    # ── Footer (v53 \u2014 clean white background, no dark band) ───────────────────
    pdf.set_y(-14)
    # Thin separator line above footer
    pdf.set_draw_color(*_SECTION_SEP)
    pdf.set_line_width(0.3)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(2)

    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    pdf.set_font('Helvetica', '', 6.5)
    pdf.set_text_color(*_FOOTER_GRAY)

    # Left: timestamp
    pdf.set_x(20)
    pdf.cell(90, 4, 'Generado: ' + now_str, align='L', new_x='RIGHT', new_y='TOP')
    # Right: institution + url + confidential
    pdf.cell(80, 4,
             _pdf_safe(institution_name) + '  |  ' + _pdf_safe(institution_url) + '  |  Confidencial',
             align='R', new_x='LMARGIN', new_y='NEXT')

    # Page X de Y
    pdf.set_x(20)
    pdf.set_font('Helvetica', '', 6)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(170, 4, 'P\xe1gina ' + str(pdf.page_no()) + ' de {nb}', align='R',
             new_x='LMARGIN', new_y='NEXT')

    return _as_pdf_bytes(pdf.output())

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — unchanged signatures
# ══════════════════════════════════════════════════════════════════════════════

async def generate_informe_pdf(
    informe: dict[str, Any],
    *,
    institution_name: str = '',
    institution_subtitle: str = '',
    institution_url: str = '',
    doctor_name: str = '',
    doctor_specialty: str = '',
    doctor_license: str = '',
    base_url: str = '',
) -> bytes:
    """Generate PDF for a medical report. Try Gotenberg first, fall back to fpdf2."""
    _log_pdf_template_dir_once()
    # Determine engine
    try:
        from app.core.config import settings as _s
        engine = _s.IMAGIAX_PDF_ENGINE.lower().strip()
    except Exception:
        engine = 'gotenberg'

    if engine == "fpdf2":
        logger.info("PDF_ENGINE=fpdf2 — using fallback engine directly")
        return _generate_fallback_pdf(
            informe,
            institution_name=institution_name,
            institution_subtitle=institution_subtitle,
            institution_url=institution_url,
            doctor_name=doctor_name,
            doctor_specialty=doctor_specialty,
            doctor_license=doctor_license,
            base_url=base_url,
        )

    # Try Gotenberg
    try:
        pdf_bytes = await _generate_gotenberg_pdf(
            informe,
            institution_name=institution_name,
            institution_subtitle=institution_subtitle,
            institution_url=institution_url,
            doctor_name=doctor_name,
            doctor_specialty=doctor_specialty,
            doctor_license=doctor_license,
            base_url=base_url,
        )
        logger.info("PDF generated via Gotenberg (%d bytes)", len(pdf_bytes))
        return pdf_bytes
    except Exception as exc:
        logger.warning("Gotenberg failed (%s), falling back to fpdf2", exc)
        return _generate_fallback_pdf(
            informe,
            institution_name=institution_name,
            institution_subtitle=institution_subtitle,
            institution_url=institution_url,
            doctor_name=doctor_name,
            doctor_specialty=doctor_specialty,
            doctor_license=doctor_license,
            base_url=base_url,
        )


def build_pdf_streaming_response(
    informe: dict[str, Any],
    *,
    mode: str = 'download',
    institution_name: str = '',
    institution_subtitle: str = '',
    institution_url: str = '',
    doctor_name: str = '',
    doctor_specialty: str = '',
    doctor_license: str = '',
    base_url: str = '',
) -> StreamingResponse:
    """Build a StreamingResponse for PDF download.

    This function delegates to generate_informe_pdf() which is now async.
    The caller (router.py) is an async FastAPI endpoint, so we return a
    StreamingResponse that wraps the async PDF generation.
    """
    institution_name, institution_subtitle, institution_url, \
        doctor_name, doctor_specialty, doctor_license, base_url = \
        _resolve_settings_defaults(
            institution_name, institution_subtitle, institution_url,
            doctor_name, doctor_specialty, doctor_license, base_url,
        )

    async def _async_stream():
        pdf_bytes = await generate_informe_pdf(
            informe,
            institution_name=institution_name,
            institution_subtitle=institution_subtitle,
            institution_url=institution_url,
            doctor_name=doctor_name,
            doctor_specialty=doctor_specialty,
            doctor_license=doctor_license,
            base_url=base_url,
        )
        # Starlette encodes non-str/bytes chunks with ``.encode()`` — fpdf2 returns bytearray.
        yield _as_pdf_bytes(pdf_bytes)

    short_id = (informe.get('id') or 'unknown')[:8]
    filename = 'informe-' + short_id + '.pdf'
    mode_normalized = (mode or 'download').strip().lower()
    # Backward-compatible alias: `preview` behaves as `inline`.
    if mode_normalized == 'preview':
        mode_normalized = 'inline'
    disposition = 'inline' if mode_normalized == 'inline' else 'attachment'

    return StreamingResponse(
        _async_stream(),
        media_type='application/pdf',
        headers={
            'Content-Disposition': disposition + '; filename="' + filename + '"',
            'Cache-Control': 'private, no-store, max-age=0, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "frame-ancestors 'self'",
            'X-Frame-Options': 'SAMEORIGIN',
        },
    )

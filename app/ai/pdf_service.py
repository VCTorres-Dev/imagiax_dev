"""
app.admin.pdf_service - PDF generation for medical reports.
Extracted from imagiax_admin.py, no DB access (receives informe dict).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

from fastapi.responses import StreamingResponse

def _pdf_safe(text: str) -> str:
    """Convierte texto a latin-1 aceptable por fpdf2."""
    _replacements = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U", "ñ": "n", "Ñ": "N", "ü": "u", "Ü": "U", "¿": "?", "¡": "!", "’": "'", "‘": "'", "“": """, "”": """, "—": "-", "–": "-", "…": "..."}
    result = []
    for ch in text:
        if ch in _replacements:
            result.append(_replacements[ch])
        else:
            try:
                ch.encode("latin-1")
                result.append(ch)
            except (UnicodeEncodeError, ValueError):
                result.append("?")
    return "".join(result)

def generate_informe_pdf(informe: dict[str, Any]) -> bytes:
    """
    Genera un PDF en memoria para un informe medico.
    Retorna bytes del PDF.
    Requiere fpdf2 instalado.
    """
    try:
        from fpdf import FPDF  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("fpdf2 no esta instalado. Agregue fpdf2 a requirements.txt.") from exc

    informe_generado = (informe.get("informe_generado") or "").strip()
    informe_final = (informe.get("informe_final") or "").strip()

    # Second-line defence: strip any XML tags that may have been stored in DB
    # before the llm_service fix was deployed, or edited manually into informe_final.
    import re as _re
    _xml_re = _re.compile(r"<[^>]+>")
    if "<" in informe_generado:
        informe_generado = _re.sub(r"\n{3,}", "\n\n", _xml_re.sub("", informe_generado)).strip()
    if "<" in informe_final:
        informe_final = _re.sub(r"\n{3,}", "\n\n", _xml_re.sub("", informe_final)).strip()

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    DARK = (10, 15, 30)
    BLUE = (59, 130, 246)
    GRAY = (100, 116, 139)
    LIGHT_BG = (245, 247, 250)

    # Header
    pdf.set_fill_color(*DARK)
    pdf.rect(0, 0, 210, 28, "F")
    pdf.set_y(8)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*BLUE)
    pdf.cell(0, 8, "IMAGIAX", align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*GRAY)
    pdf.set_x(pdf.l_margin)
    pdf.cell(0, 5, "Informe Medico Radiologico  |  imagiax.dev", align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # Datos del paciente
    pdf.set_fill_color(*LIGHT_BG)
    pdf.set_draw_color(*BLUE)
    y0 = pdf.get_y()
    pdf.rect(pdf.l_margin, y0, 190, 28, "F")
    pdf.set_y(y0 + 3)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*DARK)
    pdf.cell(0, 5, "DATOS DEL PACIENTE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    patient_name = _pdf_safe(informe.get("patient_name") or "-")
    patient_id   = _pdf_safe(str(informe.get("patient_id") or "-"))
    study_date   = _pdf_safe(str(informe.get("study_date") or "-"))
    modalidad    = _pdf_safe(str(informe.get("modalidad") or "-"))
    alerta       = _pdf_safe(str(informe.get("ai_nivel_alerta") or "-"))
    estado       = _pdf_safe(str(informe.get("estado") or "-"))
    patient_age  = _pdf_safe(str(informe.get("patient_age") or "-"))
    pdf.cell(95, 5, "Paciente: " + patient_name, new_x="RIGHT", new_y="TOP")
    pdf.cell(95, 5, "ID: " + patient_id + "   Edad: " + patient_age, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(95, 5, "Modalidad: " + modalidad + "   Fecha: " + study_date, new_x="RIGHT", new_y="TOP")
    pdf.cell(95, 5, "Alerta IA: " + alerta + "   Estado: " + estado, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    # Indicación Clínica (nota del médico solicitante)
    nota_clinica = (informe.get("nota_clinica") or "").strip()
    if nota_clinica:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*BLUE)
        pdf.cell(0, 7, "INDICACION CLINICA", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*BLUE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 190, pdf.get_y())
        pdf.ln(3)
        pdf.set_fill_color(240, 245, 250)
        y_nota = pdf.get_y()
        pdf.rect(pdf.l_margin, y_nota, 190, 12, "F")
        pdf.set_y(y_nota + 2)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*DARK)
        pdf.multi_cell(0, 5, _pdf_safe(nota_clinica), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)

    # Informe IA
    if informe_generado:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*BLUE)
        pdf.cell(0, 7, "INFORME GENERADO POR IA", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*BLUE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 190, pdf.get_y())
        pdf.ln(3)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*DARK)
        pdf.multi_cell(0, 5, _pdf_safe(informe_generado), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)

    # Informe final
    if informe_final:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*BLUE)
        pdf.cell(0, 7, "INFORME FINAL (VALIDADO POR MEDICO)", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*BLUE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 190, pdf.get_y())
        pdf.ln(3)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*DARK)
        pdf.multi_cell(0, 5, _pdf_safe(informe_final), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)

    # Footer
    pdf.set_y(-18)
    pdf.set_fill_color(*DARK)
    pdf.rect(0, pdf.get_y() - 2, 210, 20, "F")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*GRAY)
    pdf.cell(95, 6, "Generado: " + now_str, align="L", new_x="RIGHT", new_y="TOP")
    pdf.cell(95, 6, "IMAGIAX  |  imagiax.dev  |  Documento confidencial", align="R", new_x="LMARGIN", new_y="NEXT")

    return pdf.output()

def build_pdf_streaming_response(informe: dict[str, Any]) -> StreamingResponse:
    """Construye StreamingResponse para descarga de PDF."""
    pdf_bytes = generate_informe_pdf(informe)
    short_id = (informe.get("id") or "unknown")[:8]
    filename = "informe-" + short_id + ".pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="' + filename + '"'},
    )

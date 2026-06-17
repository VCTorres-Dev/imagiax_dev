"""
app.ai.pipeline_context — PipelineContext and phase result models for pipeline v1.1.

Created for ai-pipeline-v1.1 (T-01).
These models flow through all pipeline phases carrying intermediate results.
They are JSON-serializable for storage in the pipeline_trace DB column.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


# ============================================================
# Phase result models
# ============================================================


class QualityGateResult(BaseModel):
    """Result from Phase 0.5 — Quality Gate (Gemini Flash)."""

    verdict: Literal["adequate", "suboptimal", "not_analyzable"] = "adequate"
    reason: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class AuditResult(BaseModel):
    """Result from Phase 2 — Selective Auditor (Gemini Pro)."""

    verdict: Literal["sin_hallazgos", "observaciones", "alerta", "sin_auditoria"] = "sin_hallazgos"
    observations: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "low"
    recommendation: str = ""
    model_used: str = ""


class PhaseTrace(BaseModel):
    """Timing and metadata for a single pipeline phase."""

    started: str = ""
    ended: str = ""
    status: Literal["pending", "ok", "failed", "skipped", "timeout"] = "pending"
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""
    # Phase-specific payload (verdict, note hash, etc.)
    payload: dict[str, Any] = Field(default_factory=dict)


# ============================================================
# Pipeline context — flows through all phases
# ============================================================


class PipelineContext(BaseModel):
    """Mutable context passed through all v1.1 pipeline phases.

    Created once per request in generar_informe v1.1 and passed to every
    phase function. Serialized to JSONB as pipeline_trace at the end.
    """

    pipeline_id: str = Field(default_factory=lambda: str(uuid4()))
    version: str = "1.1"
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Clinical note (sanitized)
    nota_clinica: str | None = None
    solicitar_auditoria: bool = False

    # Phase results (None = not executed)
    quality_gate: QualityGateResult | None = None
    audit_result: AuditResult | None = None

    # Per-phase trace data keyed by phase name
    # e.g. "quality_gate", "generation", "auditor"
    phases: dict[str, PhaseTrace] = Field(default_factory=dict)

    # Final flag
    fallback_to_v1: bool = False

    def start_phase(self, phase_name: str, model: str = "") -> None:
        """Record the start of a pipeline phase."""
        self.phases[phase_name] = PhaseTrace(
            started=datetime.now(timezone.utc).isoformat(),
            model=model,
            status="pending",
        )

    def end_phase(
        self,
        phase_name: str,
        status: Literal["ok", "failed", "skipped", "timeout"] = "ok",
        error: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record the completion of a pipeline phase."""
        trace = self.phases.get(phase_name)
        if trace is None:
            trace = PhaseTrace(started=datetime.now(timezone.utc).isoformat())
            self.phases[phase_name] = trace
        trace.ended = datetime.now(timezone.utc).isoformat()
        trace.status = status
        trace.error = error
        trace.tokens_in = tokens_in
        trace.tokens_out = tokens_out
        if payload:
            trace.payload = payload

    def to_trace_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict suitable for the pipeline_trace DB column."""
        return {
            "pipeline_id": self.pipeline_id,
            "version": self.version,
            "started_at": self.started_at,
            "phases": {k: v.model_dump() for k, v in self.phases.items()},
            "quality_gate": self.quality_gate.model_dump() if self.quality_gate else None,
            "audit_result": self.audit_result.model_dump() if self.audit_result else None,
            "fallback_to_v1": self.fallback_to_v1,
        }

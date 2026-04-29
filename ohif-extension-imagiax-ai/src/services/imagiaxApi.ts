/**
 * IMAGIAX API Service
 *
 * Connects the OHIF panel to the IMAGIAX FastAPI backend (port 8010).
 * Pipeline: DICOM → ResNet50-512 inference → Gemini LLM → clinical report.
 */

// ── Configuration ──────────────────────────────────────────────────────────────

const API_BASE_URL =
  (typeof window !== 'undefined' && (window as any).__IMAGIAX_API_URL__) ||
  'http://localhost:8010';

const API_V2_URL =
  (typeof window !== 'undefined' && (window as any).__IMAGIAX_V2_API_URL__) ||
  'http://localhost:8011';

// Timeouts por tipo de operación (en milisegundos)
const TIMEOUTS = {
  DEFAULT: 30_000,        // 30 segundos para operaciones normales
  GENERAR_INFORME: 600_000, // 10 minutos para inicio/confirmación del análisis
  COMPARAR_V1V2: 180_000,   // 3 minutos para comparación V1 vs V2
  JOB_STATUS: 30_000,       // 30 segundos por poll individual del job
} as const;

// ── Types ──────────────────────────────────────────────────────────────────────

export interface PatologiaInfo {
  probabilidad_pct: number;
  positivo: boolean;
  umbral_pct?: number;
}

export interface HallazgoEstudio {
  patologia: string;
  probabilidad_pct_max: number;
}

export interface AIResultados {
  scope: string;
  study_instance_uid: string;
  total_encontradas: number;
  total_analizadas: number;
  hallazgos_estudio: HallazgoEstudio[] | null;
  sin_hallazgos_estudio: boolean;
  nivel_alerta: 'NORMAL' | 'BAJA' | 'MEDIA' | 'ALTA';
  todas_patologias: Record<string, PatologiaInfo>;
  errores: Array<{ orthanc_instance_id: string; error: string }>;
  timestamp_utc: string;
}

export interface GenerarInformeResponse {
  informe_id: string;
  informe_texto: string;
  ai_resultados: AIResultados;
  modelo_llm: string;
  creditos_restantes: number | null;
  pago_automatico: PagoAutomaticoInfo | null;
  advertencia: string;
  // v1.1 pipeline fields
  pipeline_version?: string;
  pipeline_id?: string | null;
  /** Quality gate verdict: adequate | suboptimal | not_analyzable */
  calidad_imagen?: 'adequate' | 'suboptimal' | 'not_analyzable' | null;
  /** Audit state: sin_hallazgos | observaciones | alerta | sin_auditoria */
  auditoria_estado?: 'sin_hallazgos' | 'observaciones' | 'alerta' | 'sin_auditoria' | null;
  /** Manual review required — set when auditor blocks twice */
  requiere_revision_manual?: boolean;
}

export interface GenerarInformeAcceptedResponse {
  job_id: string;
  status: 'processing';
}

export type GenerarInformeResult = GenerarInformeResponse | GenerarInformeAcceptedResponse;

export interface PagoAutomaticoInfo {
  estado: 'approved' | 'rejected' | string;
  payment_id: string | null;
  monto_clp: number;
  error: string | null;
}

export interface MetodoPagoResponse {
  tiene_tarjeta: boolean;
  metodo?: 'oneclick' | 'mp_card';
  card_last4?: string;
  card_brand?: string;
  email?: string;
}

export interface EstadoCreditosResponse {
  installation_id: string;
  creditos_disponibles: number;
  creditos_total: number;
  es_trial: boolean;
  tiene_suscripcion: boolean;
  tiene_tarjeta: boolean;
  card_last4: string | null;
  activa: boolean;
  pagar_url: string;
  mensaje: string;
}

export interface GuardarDecisionResponse {
  ok: boolean;
  informe_id: string;
  estado: string;
  fue_editado: boolean;
  timestamp_decision: string;
  mensaje: string;
}

export interface InformeDB {
  id: string;
  study_instance_uid: string;
  patient_id: string | null;
  patient_name: string | null;
  study_date: string | null;
  modalidad: string | null;
  ai_nivel_alerta: string;
  ai_hallazgos: HallazgoEstudio[];
  ai_todas_patologias: Record<string, PatologiaInfo>;
  informe_generado: string;
  informe_final: string | null;
  modelo_llm: string;
  estado: string;
  fue_editado: boolean;
  notas_rechazo: string | null;
  timestamp_creacion: string;
  timestamp_decision: string | null;
  // Fields from /informes/listado endpoint (created_at may be returned instead of timestamp_creacion)
  created_at?: string;
  usuario_id?: string;
  usuario_email?: string;
  cobro_estado?: string;
  cobro_metodo?: string;
  cobro_monto_clp?: number;
  cobro_ref_id?: string;
  patient_age?: string | null;
  nota_clinica?: string | null;
  short_id?: string | null;
}

export interface ListadoInformesResponse {
  total: number;
  filtro_estado: string | null;
  items: InformeDB[];
  limit: number;
  offset: number | null;
  has_more: boolean;
  next_cursor: string | null;
}

export interface SaludResponse {
  estado: string;
  servicio: string;
  version: string;
  modelo: string;
  llm: string;
  patologias: number;
  dispositivo: string;
  orthanc: { url: string; estado: string; nombre: string | null };
  timestamp_utc: string;
}

// ── API Error ──────────────────────────────────────────────────────────────────

export class ImagiaxApiError extends Error {
  public readonly status: number;
  public readonly detail: string;

  constructor(status: number, detail: string) {
    super(`IMAGIAX API error ${status}: ${detail}`);
    this.name = 'ImagiaxApiError';
    this.status = status;
    this.detail = detail;
  }
}

export class ImagiaxTimeoutError extends Error {
  public readonly timeoutMs: number;

  constructor(timeoutMs: number, endpoint: string) {
    super(
      `La solicitud a ${endpoint} excedió el tiempo límite de ${Math.round(timeoutMs / 1000)} segundos. ` +
      `El análisis puede estar procesándose en el servidor. Por favor, espera un momento e intenta nuevamente.`
    );
    this.name = 'ImagiaxTimeoutError';
    this.timeoutMs = timeoutMs;
  }
}

const PDF_CONTENT_TYPE = 'application/pdf';

function validatePdfBlob(response: Response, blob: Blob, endpoint: string): Blob {
  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  const hasDeclaredPdfType = contentType.includes(PDF_CONTENT_TYPE);

  if (contentType && !hasDeclaredPdfType) {
    throw new ImagiaxApiError(502, `Respuesta inválida desde ${endpoint}: se esperaba PDF.`);
  }

  if (!blob || blob.size <= 0) {
    throw new ImagiaxApiError(502, `Respuesta inválida desde ${endpoint}: PDF vacío.`);
  }

  if (!blob.type || blob.type.toLowerCase() !== PDF_CONTENT_TYPE) {
    return new Blob([blob], { type: PDF_CONTENT_TYPE });
  }

  return blob;
}

export function isGenerarInformeAcceptedResponse(
  response: GenerarInformeResult
): response is GenerarInformeAcceptedResponse {
  return 'job_id' in response && response.status === 'processing';
}

// ── Auth state ─────────────────────────────────────────────────────────────────

export interface AuthUser {
  user_id: string;
  email: string;
  nombre: string;
  rol: string;
}

export async function getCurrentUser(): Promise<AuthUser> {
  const res = await fetch(`${API_BASE_URL}/auth/me`, {
    credentials: 'include',
  });
  if (!res.ok) throw new ImagiaxApiError(res.status, 'No autenticado');
  const data = await res.json();
  return data.user as AuthUser;
}

export async function logoutApi(): Promise<void> {
  await fetch(`${API_BASE_URL}/auth/logout`, {
    method: 'POST',
    credentials: 'include',
  });
}
// ── Helpers ────────────────────────────────────────────────────────────────────

/**
 * Realiza una petición HTTP con timeout usando AbortController.
 * @param path - Ruta del endpoint (ej: "/generar/informe")
 * @param options - Opciones de fetch + timeout personalizado
 * @param timeoutMs - Timeout en milisegundos (default: TIMEOUTS.DEFAULT)
 */
/**
 * Realiza una petición HTTP con timeout usando AbortController.
 * Soporta respuestas 202 Accepted con {job_id, status: 'processing'} —
 * en ese caso retorna el objeto 202 directamente para que el caller haga polling.
 */
async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  timeoutMs: number = TIMEOUTS.DEFAULT
): Promise<T> {
  const url = `${API_BASE_URL}${path}`;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      ...options,
      credentials: 'include',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (response.status === 401) {
      throw new ImagiaxApiError(401, 'Sesión expirada. Por favor, inicia sesión nuevamente.');
    }

    // 202 Accepted: async job — return raw body for caller to handle polling
    if (response.status === 202) {
      return response.json() as Promise<T>;
    }

    if (!response.ok) {
      let detail = response.statusText;
      try {
        const errorBody = await response.json() as { detail?: string; message?: string };
        detail = errorBody.detail || errorBody.message || detail;
      } catch {
        // Non-JSON error body — use statusText
      }
      throw new ImagiaxApiError(response.status, detail);
    }

    return response.json() as Promise<T>;
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ImagiaxTimeoutError(timeoutMs, path);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

// ── API Functions ──────────────────────────────────────────────────────────────

/**
 * Health check — verifies model, Orthanc, and LLM are available.
 */
export async function checkSalud(): Promise<SaludResponse> {
  return apiRequest<SaludResponse>('/salud');
}

/**
 * Full pipeline: DICOM → AI inference → Gemini report → PostgreSQL persistence.
 * Returns either the generated report or a 202 async job token for client polling.
 *
 * ⚠ Este endpoint puede tomar varios minutos. Timeout inicial: 10 minutos.
 *
 * @param studyInstanceUID - DICOM Study Instance UID
 * @param maxInstances     - Max DICOM instances to analyze (default 50)
 * @param notaClinica      - Optional clinical note from physician (max 500 chars, v1.1)
 * @param solicitarAuditoria - Request manual audit pass (v1.1)
 * @param versionPipeline  - Pipeline version override: "1.0" | "1.1" (omit to use server default)
 */
export async function generarInforme(
  studyInstanceUID: string,
  maxInstances: number = 50,
  notaClinica?: string,
  solicitarAuditoria: boolean = false,  // IRONCLAD: Forzado a false para eliminar UI de auditoría
  versionPipeline?: string
): Promise<GenerarInformeResult> {
  return apiRequest<GenerarInformeResult>(
    '/generar/informe',
    {
      method: 'POST',
      body: JSON.stringify({
        study_instance_uid: studyInstanceUID,
        max_instances: maxInstances,
        ...(notaClinica !== undefined && notaClinica !== null && notaClinica.trim() !== ''
          ? { nota_clinica: notaClinica.trim().slice(0, 500) }
          : {}),
        solicitar_auditoria: solicitarAuditoria,
        ...(versionPipeline ? { version_pipeline: versionPipeline } : {}),
      }),
    },
    TIMEOUTS.GENERAR_INFORME
  );
}

export async function getEstadoGeneracionInforme(
  jobId: string
): Promise<GenerarInformeResult> {
  return apiRequest<GenerarInformeResult>(
    `/generar/informe/estado/${encodeURIComponent(jobId)}`,
    {},
    TIMEOUTS.JOB_STATUS
  );
}

/**
 * Save the doctor's decision on a generated report.
 *
 * @param informeId  - UUID of the report
 * @param estado     - 'aceptado' | 'rechazado' | 'editado_aceptado'
 * @param informeFinal  - Modified report text (for editado_aceptado)
 * @param notasRechazo  - Doctor's rejection notes
 */
export async function guardarDecision(
  informeId: string,
  estado: 'aceptado' | 'rechazado' | 'editado_aceptado',
  informeFinal?: string,
  notasRechazo?: string
): Promise<GuardarDecisionResponse> {
  return apiRequest<GuardarDecisionResponse>('/informes/guardar', {
    method: 'POST',
    body: JSON.stringify({
      informe_id: informeId,
      estado,
      informe_final: informeFinal ?? null,
      notas_rechazo: notasRechazo ?? null,
    }),
  });
}

/**
 * Get all reports generated for a specific study.
 */
export async function getInformesPorEstudio(
  studyInstanceUID: string
): Promise<{ study_instance_uid: string; total: number; informes: InformeDB[] }> {
  return apiRequest(`/informes/estudio/${encodeURIComponent(studyInstanceUID)}`);
}

/**
/**
 * List all reports, optionally filtered by estado.
 * @param estado - optional status filter
 * @param limit  - max results to return (default 50 to avoid fetching thousands of records)
 */
export async function listadoInformes(
  estado?: string,
  limit = 25,
  cursorId?: string
): Promise<ListadoInformesResponse> {
  const params = new URLSearchParams();
  if (estado) params.set('estado', estado);
  params.set('limit', String(limit));
  if (cursorId) params.set('cursor_id', cursorId);
  const query = params.toString() ? `?${params.toString()}` : '';
  return apiRequest(`/informes/listado${query}`);
}

/**
 * Download a medical report PDF from the backend.
 * Returns a Blob that can be converted to a download URL via URL.createObjectURL().
 *
 * @param informeId - UUID of the report
 */
export async function downloadInformePdf(informeId: string): Promise<Blob> {
  const endpoint = `/informes/${encodeURIComponent(informeId)}/pdf`;
  const url = `${API_BASE_URL}${endpoint}`;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), TIMEOUTS.DEFAULT);

  try {
    const response = await fetch(url, {
      credentials: 'include',
      signal: controller.signal,
      headers: { 'Accept': 'application/pdf' },
    });

    if (response.status === 401) {
      throw new ImagiaxApiError(401, 'Sesión expirada. Por favor, inicia sesión nuevamente.');
    }

    if (!response.ok) {
      let detail = response.statusText;
      try {
        const errorBody = await response.json() as { detail?: string; message?: string };
        detail = errorBody.detail || errorBody.message || detail;
      } catch { /* Non-JSON error body */ }
      throw new ImagiaxApiError(response.status, detail);
    }

    const blob = await response.blob();
    return validatePdfBlob(response, blob, endpoint);
  } catch (error) {
    if (error instanceof ImagiaxApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ImagiaxTimeoutError(TIMEOUTS.DEFAULT, url);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

export interface InformePdfPreview {
  blob: Blob;
  objectUrl: string;
}

/**
 * Fetches authoritative backend-final PDF and returns both Blob + object URL
 * for embedded preview scenarios.
 */
export async function getInformePdfPreview(informeId: string): Promise<InformePdfPreview> {
  const blob = await downloadInformePdf(informeId);
  const objectUrl = URL.createObjectURL(blob);
  return { blob, objectUrl };
}

export function revokeInformePdfPreviewUrl(objectUrl: string): void {
  if (!objectUrl) return;
  URL.revokeObjectURL(objectUrl);
}

// ══ V2 Experimental — NV-Reason-CXR-3B ═══════════════════════════════════════

export interface V2PatologiaInfo {
  max_pct: number;
  positivo: boolean;
  umbral_pct: number;
}

export interface V2Triage {
  nivel_alerta: string;
  hallazgos: Array<{ patologia: string; probabilidad_pct_max: number }>;
  todas_patologias: Record<string, V2PatologiaInfo>;
}

export interface V2CompararResponse {
  study_instance_uid: string;
  v1_gemini: {
    informe_es: string;
    triage: { nivel_alerta: string; hallazgos: Array<{ patologia: string; probabilidad_pct_max: number }> };
    latencia_s: number;
    modelo_triage: string;
    modelo_llm: string;
  };
  v2_cxr3b: {
    informe_es: string;
    triage: V2Triage;
    thinking_en: string;
    answer_en: string;
    latencia_inferencia_s: number;
    latencia_traduccion_s: number;
    modelo_vision: string;
    modelo_llm: string;
  };
  latencia_total_comparacion_s: number;
}

export interface V2SaludResponse {
  estado: string;
  version: string;
  modelos: {
    densenet121: string;
    cxr3b: string;
    cxr3b_4bit: boolean;
  };
  dispositivo: string;
  timestamp_utc: string;
}

async function apiV2Request<T>(
  path: string,
  options: RequestInit = {},
  timeoutMs: number = TIMEOUTS.DEFAULT
): Promise<T> {
  const url = `${API_V2_URL}${path}`;
  
  // Crear AbortController para timeout
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      ...options,
      credentials: 'include',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (!response.ok) {
      let detail = response.statusText;
      try {
        const errorBody = await response.json();
        detail = errorBody.detail || errorBody.message || detail;
      } catch { /* Non-JSON error body */ }
      throw new ImagiaxApiError(response.status, detail);
    }

    return response.json() as Promise<T>;
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ImagiaxTimeoutError(timeoutMs, path);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

/** Check if the V2 experimental server (port 8011) is running. */
export async function checkSaludV2(): Promise<V2SaludResponse> {
  return apiV2Request<V2SaludResponse>('/v2/salud');
}

/**
 * Compare V1 (DenseNet121 + Gemini) vs V2 (NV-Reason-CXR-3B + Gemini) on the same study.
 * Runs both pipelines independently and returns results side by side.
 * 
 * ⚠ Este endpoint puede tomar 60-120 segundos. Timeout: 3 minutos.
 */
export async function compararV1V2(
  studyInstanceUID: string,
  maxInstances: number = 1
): Promise<V2CompararResponse> {
  return apiV2Request<V2CompararResponse>(
    '/v2/comparar',
    {
      method: 'POST',
      body: JSON.stringify({
        study_instance_uid: studyInstanceUID,
        max_instances: maxInstances,
      }),
    },
    TIMEOUTS.COMPARAR_V1V2 // 3 minutos
  );
}

/**
 * Returns the current credit status for this IMAGIAX installation.
 * Used by the panel to display the credit counter and show payment CTA.
 */
export async function checkEstadoCreditos(): Promise<EstadoCreditosResponse> {
  return apiRequest<EstadoCreditosResponse>('/pagos/estado');
}

/**
 * Returns the registered card info for automatic billing (last4, brand, email).
 */
export async function getMetodoPago(): Promise<MetodoPagoResponse> {
  return apiRequest<MetodoPagoResponse>('/pagos/metodo-pago');
}

/**
 * Removes the registered card — deactivates automatic billing.
 */
export async function borrarMetodoPago(): Promise<{ ok: boolean; mensaje: string }> {
  return apiRequest('/pagos/metodo-pago', { method: 'DELETE' });
}

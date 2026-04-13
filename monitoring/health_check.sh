#!/usr/bin/env bash
# =============================================================================
# IMAGIAX — Health Check & Alert Script
# Runs every 5 minutes via cron. Checks containers, endpoints, resources.
# Sends Telegram alerts only on state transitions (UP→DOWN or DOWN→UP).
# =============================================================================
set -uo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="/root/monitoring"
CONFIG_FILE="${SCRIPT_DIR}/.telegram_config"
STATE_DIR="${SCRIPT_DIR}/state"
LOG_FILE="/root/backups/logs/health.log"
HOSTNAME_LABEL="imagiax.dev"

# ── Logging ──────────────────────────────────────────────────────────────────
log() {
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

# ── Load Telegram config ─────────────────────────────────────────────────────
BOT_TOKEN=""
CHAT_ID=""

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$CONFIG_FILE"
fi

# Validate config is set and not placeholders
TELEGRAM_ENABLED=false
if [[ -n "$BOT_TOKEN" && -n "$CHAT_ID" \
      && "$BOT_TOKEN" != "YOUR_BOT_TOKEN_HERE" \
      && "$CHAT_ID" != "YOUR_CHAT_ID_HERE" ]]; then
  TELEGRAM_ENABLED=true
fi

# ── Send Telegram alert ───────────────────────────────────────────────────────
send_alert() {
  local message="$1"
  if [[ "$TELEGRAM_ENABLED" == "true" ]]; then
    local response
    response=$(curl -s --max-time 10 \
      -d "chat_id=${CHAT_ID}&text=${message}&parse_mode=HTML" \
      "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" 2>&1) || true
    if echo "$response" | grep -q '"ok":true'; then
      log "TELEGRAM: alert sent"
    else
      log "TELEGRAM: failed to send alert — $response"
    fi
  else
    log "TELEGRAM: disabled (config not set or placeholder values)"
  fi
}

# ── State change detection ────────────────────────────────────────────────────
# $1 = check_name (e.g. "container_nginx-imagiax")
# $2 = current_state ("up" or "down")
# $3 = details (human-readable message)
check_state_change() {
  local check_name="$1"
  local current_state="$2"
  local details="$3"
  local state_file="${STATE_DIR}/${check_name}"
  local now
  now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  # First run — initialize state file, no alert
  if [[ ! -f "$state_file" ]]; then
    echo "${current_state}|${now}" > "$state_file"
    log "STATE: ${check_name} initialized as ${current_state}"
    return 0
  fi

  local prev_line
  prev_line=$(cat "$state_file")
  local prev_state="${prev_line%%|*}"
  local prev_ts="${prev_line##*|}"

  if [[ "$current_state" == "$prev_state" ]]; then
    # No change — no alert
    return 0
  fi

  # State changed — update file and alert
  echo "${current_state}|${now}" > "$state_file"

  if [[ "$current_state" == "down" ]]; then
    local msg="🔴 <b>DOWN — ${HOSTNAME_LABEL}</b>%0A<b>${check_name}</b>%0A${details}%0ATime: ${now}"
    log "ALERT: DOWN ${check_name} — ${details}"
    send_alert "$msg"
  else
    # Calculate downtime
    local downtime_msg=""
    if [[ -n "$prev_ts" ]]; then
      local prev_epoch
      local now_epoch
      prev_epoch=$(date -u -d "$prev_ts" '+%s' 2>/dev/null || echo "0")
      now_epoch=$(date -u '+%s')
      if [[ "$prev_epoch" -gt 0 ]]; then
        local diff=$(( now_epoch - prev_epoch ))
        local mins=$(( diff / 60 ))
        downtime_msg=" (down for ~${mins}m)"
      fi
    fi
    local msg="🟢 <b>RECOVERED — ${HOSTNAME_LABEL}</b>%0A<b>${check_name}</b>${downtime_msg}%0A${details}%0ATime: ${now}"
    log "ALERT: RECOVERED ${check_name}${downtime_msg} — ${details}"
    send_alert "$msg"
  fi
}

# ── Ensure state dir exists ───────────────────────────────────────────────────
mkdir -p "$STATE_DIR"

# =============================================================================
# CHECK 1: Container status
# =============================================================================
log "--- Checking containers ---"
CONTAINERS=(
  "fastapi-imagiax"
  "nginx-imagiax"
  "ohif-imagiax"
  "orthanc-imagiax"
  "postgres-imagiax"
)

for container in "${CONTAINERS[@]}"; do
  status=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
  health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || echo "none")

  if [[ "$status" == "running" ]]; then
    # Container is running — if it has a healthcheck, also check health status
    if [[ "$health" == "unhealthy" ]]; then
      log "CONTAINER: ${container} running but UNHEALTHY"
      check_state_change "container_${container}" "down" "Container running but healthcheck failing"
    else
      log "CONTAINER: ${container} OK (status=${status}, health=${health})"
      check_state_change "container_${container}" "up" "Container running normally"
    fi
  else
    log "CONTAINER: ${container} DOWN (status=${status})"
    check_state_change "container_${container}" "down" "Container status: ${status}"
  fi
done

# =============================================================================
# CHECK 2: HTTP Endpoints (through nginx — full request path)
# =============================================================================
log "--- Checking endpoints ---"

# /api/salud — FastAPI health endpoint
API_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "https://imagiax.dev/api/salud" 2>/dev/null || echo "0")
if [[ "$API_CODE" == "200" ]]; then
  log "ENDPOINT: /api/salud OK (${API_CODE})"
  check_state_change "endpoint_api_salud" "up" "HTTP ${API_CODE}"
else
  log "ENDPOINT: /api/salud FAIL (${API_CODE})"
  check_state_change "endpoint_api_salud" "down" "HTTP response: ${API_CODE}"
fi

# /health/db — Database health endpoint
DB_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "https://imagiax.dev/health/db" 2>/dev/null || echo "0")
if [[ "$DB_CODE" == "200" ]]; then
  log "ENDPOINT: /health/db OK (${DB_CODE})"
  check_state_change "endpoint_health_db" "up" "HTTP ${DB_CODE}"
else
  log "ENDPOINT: /health/db FAIL (${DB_CODE})"
  check_state_change "endpoint_health_db" "down" "HTTP response: ${DB_CODE}"
fi

# OHIF root — follows 302 redirect
OHIF_CODE=$(curl -sL -o /dev/null -w "%{http_code}" --max-time 15 "https://imagiax.dev/" 2>/dev/null || echo "0")
if [[ "$OHIF_CODE" == "200" ]]; then
  log "ENDPOINT: OHIF / OK (${OHIF_CODE})"
  check_state_change "endpoint_ohif_root" "up" "HTTP ${OHIF_CODE}"
else
  log "ENDPOINT: OHIF / FAIL (${OHIF_CODE})"
  check_state_change "endpoint_ohif_root" "down" "HTTP response: ${OHIF_CODE}"
fi

# Orthanc internal endpoint — curl from Docker host to container IP on port 8042.
# The host can reach container network directly (172.18.x.x).
# Orthanc returns 401 (auth required) on unauthenticated requests — that is HEALTHY.
# HTTP 000 = connection refused/timeout = container unreachable = DOWN.
log "ENDPOINT: Checking Orthanc internal (/system via host curl) ---"
ORTHANC_IP=$(docker inspect \
  --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  orthanc-imagiax 2>/dev/null || echo "")

if [[ -z "$ORTHANC_IP" ]]; then
  log "ENDPOINT: Orthanc IP not found (container missing or stopped)"
  check_state_change "endpoint_orthanc" "down" "Could not resolve orthanc-imagiax container IP"
else
  ORTHANC_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout 5 --max-time 10 \
    "http://${ORTHANC_IP}:8042/system" 2>/dev/null || echo "000")

  if [[ "$ORTHANC_CODE" == "000" ]]; then
    log "ENDPOINT: Orthanc /system UNREACHABLE (${ORTHANC_IP}:8042 — HTTP 000)"
    check_state_change "endpoint_orthanc" "down" "Orthanc /system connection failed (${ORTHANC_IP}:8042)"
  elif [[ "$ORTHANC_CODE" == "200" || "$ORTHANC_CODE" == "401" ]]; then
    log "ENDPOINT: Orthanc /system OK (${ORTHANC_IP}:8042 — HTTP ${ORTHANC_CODE})"
    check_state_change "endpoint_orthanc" "up" "HTTP ${ORTHANC_CODE} from ${ORTHANC_IP}:8042"
  else
    log "ENDPOINT: Orthanc /system unexpected response (${ORTHANC_IP}:8042 — HTTP ${ORTHANC_CODE})"
    check_state_change "endpoint_orthanc" "down" "HTTP response: ${ORTHANC_CODE} from ${ORTHANC_IP}:8042"
  fi
fi

# =============================================================================
# CHECK 3: Resource usage
# =============================================================================
log "--- Checking resources ---"

# Disk usage — root filesystem
DISK_PCT=$(df / | awk 'NR==2{print $5}' | tr -d '%')
log "RESOURCE: disk usage ${DISK_PCT}%"
if [[ "$DISK_PCT" -ge 85 ]]; then
  check_state_change "resource_disk" "down" "Disk usage at ${DISK_PCT}% (threshold: 85%)"
else
  check_state_change "resource_disk" "up" "Disk usage at ${DISK_PCT}%"
fi

# RAM — available memory
RAM_AVAIL=$(free -m | awk 'NR==2{print $7}')
log "RESOURCE: RAM available ${RAM_AVAIL}MB"
if [[ "$RAM_AVAIL" -le 200 ]]; then
  check_state_change "resource_ram" "down" "RAM available: ${RAM_AVAIL}MB (threshold: 200MB)"
else
  check_state_change "resource_ram" "up" "RAM available: ${RAM_AVAIL}MB"
fi

# =============================================================================
log "--- Health check complete ---"

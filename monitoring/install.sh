#!/usr/bin/env bash
# =============================================================================
# IMAGIAX — Monitoring Installer
# Idempotent: safe to run multiple times.
# Run on server: bash /tmp/install.sh
# =============================================================================
set -euo pipefail

MONITORING_DIR="/root/monitoring"
STATE_DIR="${MONITORING_DIR}/state"
LOG_DIR="/root/backups/logs"
SCRIPT_SRC="${MONITORING_DIR}/health_check.sh"
CRON_ENTRY="*/5 * * * * /usr/bin/flock -n /tmp/.health_check.lock ${MONITORING_DIR}/health_check.sh >> ${LOG_DIR}/health.log 2>&1"

echo "[install] Creating directories..."
mkdir -p "$MONITORING_DIR"
mkdir -p "$STATE_DIR"
mkdir -p "$LOG_DIR"

echo "[install] Setting permissions on health_check.sh..."
if [[ -f "$SCRIPT_SRC" ]]; then
  chmod 750 "$SCRIPT_SRC"
  echo "[install] health_check.sh: chmod 750 OK"
else
  echo "[install] ERROR: ${SCRIPT_SRC} not found — deploy files before running install.sh"
  exit 1
fi

echo "[install] Setting permissions on .telegram_config..."
CONFIG_FILE="${MONITORING_DIR}/.telegram_config"
if [[ -f "$CONFIG_FILE" ]]; then
  chmod 600 "$CONFIG_FILE"
  echo "[install] .telegram_config: chmod 600 OK"
else
  echo "[install] WARNING: .telegram_config not found — create it and set BOT_TOKEN + CHAT_ID"
fi

echo "[install] Configuring cron entry..."
# Remove any existing health_check.sh cron entries first (idempotent)
CURRENT_CRON=$(crontab -l 2>/dev/null || true)
CLEANED_CRON=$(echo "$CURRENT_CRON" | grep -v "health_check.sh" || true)

# Add the new entry
NEW_CRON="${CLEANED_CRON}"
if [[ -n "$NEW_CRON" ]]; then
  NEW_CRON="${NEW_CRON}
${CRON_ENTRY}"
else
  NEW_CRON="${CRON_ENTRY}"
fi

echo "$NEW_CRON" | crontab -
echo "[install] Cron entry added:"
echo "  ${CRON_ENTRY}"

echo "[install] Verifying cron..."
crontab -l | grep "health_check.sh"

echo ""
echo "=========================================================="
echo " IMAGIAX Monitoring installed successfully!"
echo "=========================================================="
echo ""
echo " Next steps:"
echo "   1. Edit ${MONITORING_DIR}/.telegram_config"
echo "      Set your BOT_TOKEN and CHAT_ID (see MONITORING.md)"
echo "   2. Test manually:"
echo "      ${MONITORING_DIR}/health_check.sh"
echo "   3. Watch the log:"
echo "      tail -f ${LOG_DIR}/health.log"
echo "   4. Monitoring runs every 5 minutes automatically."
echo ""
echo " State files: ${STATE_DIR}/"
echo " Log file:    ${LOG_DIR}/health.log"
echo "=========================================================="

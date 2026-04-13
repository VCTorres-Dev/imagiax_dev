#!/usr/bin/env bash
# backup_imagiax.sh — Automated backups for IMAGIAX platform
# Usage: backup_imagiax.sh {db|orthanc|all}
set -euo pipefail

# ── Config ──────────────────────────────────────────────────
BACKUP_ROOT="/root/backups"
DB_DIR="${BACKUP_ROOT}/db"
ORTHANC_DIR="${BACKUP_ROOT}/orthanc"
LOG_FILE="${BACKUP_ROOT}/logs/backup.log"
LOCK_FILE="${BACKUP_ROOT}/.backup.lock"

PG_CONTAINER="postgres-imagiax"
PG_USER="imagiax"
PG_DB="imagiax"

ORTHANC_VOLUME="/var/lib/docker/volumes/orthanc-imagiax_orthanc-data/_data"

DB_RETENTION_DAYS=30
ORTHANC_RETENTION_DAYS=28

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# ── Logging ─────────────────────────────────────────────────
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"
}

die() {
    log "ERROR: $*"
    exit 1
}

# ── Lock ────────────────────────────────────────────────────
exec 200>"${LOCK_FILE}"
flock -n 200 || die "Another backup is already running (lock: ${LOCK_FILE})"

# ── Backup Functions ────────────────────────────────────────
backup_db() {
    local outfile="${DB_DIR}/imagiax_db_${TIMESTAMP}.dump.gz"
    log "Starting PostgreSQL backup..."

    docker exec "${PG_CONTAINER}" pg_dump -U "${PG_USER}" -Fc "${PG_DB}" \
        | gzip > "${outfile}" \
        || die "pg_dump failed"

    # Verify file is non-empty
    [[ -s "${outfile}" ]] || die "Backup file is empty: ${outfile}"

    chmod 600 "${outfile}"
    local size
    size=$(du -h "${outfile}" | cut -f1)
    log "PostgreSQL backup OK: ${outfile} (${size})"

    # Rotate old backups
    local deleted
    deleted=$(find "${DB_DIR}" -name "imagiax_db_*.dump.gz" -mtime +${DB_RETENTION_DAYS} -delete -print | wc -l)
    if [[ "${deleted}" -gt 0 ]]; then
        log "Rotated ${deleted} old DB backup(s)"
    fi
}

backup_orthanc() {
    local outfile="${ORTHANC_DIR}/imagiax_orthanc_${TIMESTAMP}.tar.gz"
    log "Starting Orthanc backup..."

    tar -czf "${outfile}" \
        --exclude='WebViewerCache' \
        -C "${ORTHANC_VOLUME}" . \
        || die "Orthanc tar failed"

    # Verify file is non-empty
    [[ -s "${outfile}" ]] || die "Backup file is empty: ${outfile}"

    chmod 600 "${outfile}"
    local size
    size=$(du -h "${outfile}" | cut -f1)
    log "Orthanc backup OK: ${outfile} (${size})"

    # Rotate old backups
    local deleted
    deleted=$(find "${ORTHANC_DIR}" -name "imagiax_orthanc_*.tar.gz" -mtime +${ORTHANC_RETENTION_DAYS} -delete -print | wc -l)
    if [[ "${deleted}" -gt 0 ]]; then
        log "Rotated ${deleted} old Orthanc backup(s)"
    fi
}

# ── Main ────────────────────────────────────────────────────
CMD="${1:-}"

case "${CMD}" in
    db)
        backup_db
        ;;
    orthanc)
        backup_orthanc
        ;;
    all)
        backup_db
        backup_orthanc
        ;;
    *)
        echo "Usage: $0 {db|orthanc|all}" >&2
        exit 1
        ;;
esac

log "Backup command '${CMD}' completed successfully."

#!/bin/sh
# orthanc-entrypoint.sh
#
# Substitutes environment variables in orthanc.json.template before starting
# Orthanc. This allows secrets (e.g. ORTHANC_PASSWORD) to be injected at
# runtime via Docker environment variables instead of being hardcoded in the
# config file.
#
# Required env vars:
#   ORTHANC_PASSWORD  — password for the 'imagiax' Orthanc user
#
# The rendered config is written to /tmp/orthanc-runtime.json (writable at
# runtime, never committed to git).

set -e

TEMPLATE="/etc/orthanc/orthanc.json.template"
OUTPUT="/tmp/orthanc-runtime.json"

if [ -z "${ORTHANC_PASSWORD}" ]; then
  echo "[orthanc-entrypoint] ERROR: ORTHANC_PASSWORD env var is not set. Aborting." >&2
  exit 1
fi

echo "[orthanc-entrypoint] Rendering Orthanc config from template..."
envsubst '${ORTHANC_PASSWORD}' < "${TEMPLATE}" > "${OUTPUT}"
echo "[orthanc-entrypoint] Config written to ${OUTPUT}"

# Hand off to the official Orthanc entrypoint / binary
exec /docker-entrypoint.sh "${OUTPUT}" "$@"

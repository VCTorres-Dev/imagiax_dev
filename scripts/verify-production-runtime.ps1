param(
    [string]$HostName = "root@imagiax.dev",
    [string]$SshKey = "$env:USERPROFILE\.ssh\do_imagiax",
    [string]$BackendContainer = "fastapi-imagiax",
    [string]$OhifContainer = "ohif-imagiax",
    [string]$ExpectedBackendImage = "imagiax-backend:rollback-precutover-3da43fd",
    [string]$ExpectedOhifImage = "orthanc-imagiax-ohif:prod-18d45af"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $SshKey)) {
    throw "SSH key not found: $SshKey"
}

function ConvertTo-BashSingleQuoted([string]$Value) {
    if ($Value -match "'") {
        throw "Unsupported remote shell value (single quote found): $Value"
    }
    return "'" + $Value + "'"
}

$backendContainerQ = ConvertTo-BashSingleQuoted $BackendContainer
$ohifContainerQ = ConvertTo-BashSingleQuoted $OhifContainer
$expectedBackendImageQ = ConvertTo-BashSingleQuoted $ExpectedBackendImage
$expectedOhifImageQ = ConvertTo-BashSingleQuoted $ExpectedOhifImage

$remoteScript = @'
set -eu

BACKEND_CONTAINER=__BACKEND_CONTAINER__
OHIF_CONTAINER=__OHIF_CONTAINER__
EXPECTED_BACKEND_IMAGE=__EXPECTED_BACKEND_IMAGE__
EXPECTED_OHIF_IMAGE=__EXPECTED_OHIF_IMAGE__

inspect_runtime() {
  local label="$1"
  local container="$2"

  local image_name
  local image_id
  local status
  local restarts

  image_name=$(docker inspect --format '{{.Config.Image}}' "$container" 2>/dev/null || true)
  image_id=$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)
  status=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)
  restarts=$(docker inspect --format '{{.RestartCount}}' "$container" 2>/dev/null || true)

  if [ -z "$image_name" ] || [ -z "$image_id" ]; then
    echo "FAIL: no se pudo inspeccionar contenedor $label ($container)" >&2
    exit 2
  fi

  echo "${label}_CONTAINER=$container"
  echo "${label}_IMAGE_NAME=$image_name"
  echo "${label}_IMAGE_ID=$image_id"
  echo "${label}_STATUS=$status"
  echo "${label}_RESTARTS=$restarts"
}

echo "--- docker ps ---"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

echo "--- backend runtime ---"
inspect_runtime "BACKEND" "$BACKEND_CONTAINER"

echo "--- ohif runtime ---"
inspect_runtime "OHIF" "$OHIF_CONTAINER"

actual_backend_image=$(docker inspect --format '{{.Config.Image}}' "$BACKEND_CONTAINER")
actual_ohif_image=$(docker inspect --format '{{.Config.Image}}' "$OHIF_CONTAINER")

if [ "$actual_backend_image" != "$EXPECTED_BACKEND_IMAGE" ]; then
  echo "FAIL: backend image inesperada. expected=$EXPECTED_BACKEND_IMAGE actual=$actual_backend_image" >&2
  exit 3
fi

if [ "$actual_ohif_image" != "$EXPECTED_OHIF_IMAGE" ]; then
  echo "FAIL: OHIF image inesperada. expected=$EXPECTED_OHIF_IMAGE actual=$actual_ohif_image" >&2
  exit 4
fi

echo "--- internal health ---"
docker exec "$BACKEND_CONTAINER" sh -lc 'curl -s -o /dev/null -w "salud=%{http_code}\n" http://localhost:8010/salud || true'
docker exec "$BACKEND_CONTAINER" sh -lc 'curl -s -o /dev/null -w "ping=%{http_code}\n" http://localhost:8010/ping || true'
docker exec "$BACKEND_CONTAINER" sh -lc 'curl -s -o /dev/null -w "api_salud=%{http_code}\n" http://localhost:8010/api/salud || true'

echo "--- public health ---"
curl -sk -o /dev/null -w 'public_api_salud=%{http_code}\n' https://imagiax.dev/api/salud || true
curl -sk -o /dev/null -w 'public_root=%{http_code}\n' https://imagiax.dev/ || true
curl -sk -o /dev/null -w 'public_frontend_config_unauth=%{http_code}\n' https://imagiax.dev/api/config/frontend || true

echo "VERIFY_RUNTIME=PASS"
'@

$remoteScript = $remoteScript.Replace("__BACKEND_CONTAINER__", $backendContainerQ)
$remoteScript = $remoteScript.Replace("__OHIF_CONTAINER__", $ohifContainerQ)
$remoteScript = $remoteScript.Replace("__EXPECTED_BACKEND_IMAGE__", $expectedBackendImageQ)
$remoteScript = $remoteScript.Replace("__EXPECTED_OHIF_IMAGE__", $expectedOhifImageQ)
$remoteScript = $remoteScript -replace "`r`n", "`n"
$remoteScriptBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($remoteScript))

$sshArgs = @(
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=no",
    "-i", $SshKey,
    $HostName,
    "printf '%s' '$remoteScriptBase64' | base64 -d | bash"
)

$remoteOutput = & ssh @sshArgs 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Remote verification failed: $remoteOutput"
}

$remoteOutput | ForEach-Object { Write-Host $_ }

if (-not ($remoteOutput -contains "VERIFY_RUNTIME=PASS")) {
    throw "Remote verification did not finish correctly (missing VERIFY_RUNTIME=PASS)."
}

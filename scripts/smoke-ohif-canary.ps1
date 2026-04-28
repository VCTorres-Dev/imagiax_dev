param(
    [string]$BaseUrl = "http://127.0.0.1:18080",
    [string]$HostName = "root@imagiax.dev",
    [string]$SshKey = "$env:USERPROFILE\.ssh\do_imagiax",
    [string]$CanaryContainer = "ohif-imagiax-canary"
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) { throw "[FAIL] $Message" }
function Pass([string]$Message) { Write-Host "[PASS] $Message" }

function ConvertTo-BashSingleQuoted([string]$Value) {
    if ($Value -match "'") { Fail "Valor no soportado para shell remoto: contiene comilla simple" }
    return "'" + $Value + "'"
}

if (-not (Test-Path $SshKey)) { Fail "SSH key no encontrada: $SshKey" }

$baseUrlQ = ConvertTo-BashSingleQuoted $BaseUrl
$canaryContainerQ = ConvertTo-BashSingleQuoted $CanaryContainer

$remoteScript = @'
set -eu

BASE_URL=__BASE_URL__
CANARY_CONTAINER=__CANARY_CONTAINER__

docker inspect "$CANARY_CONTAINER" >/dev/null 2>&1 || {
  echo "FAIL: contenedor canary '$CANARY_CONTAINER' no existe" >&2
  exit 2
}

root_code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/" || true)
echo "ROOT_CODE=$root_code"
if [ "$root_code" != "200" ]; then
  echo "FAIL: OHIF canary root devolvió $root_code" >&2
  exit 3
fi

docker exec "$CANARY_CONTAINER" sh -lc 'test -f /usr/share/nginx/html/app-config.js.gz || test -f /usr/share/nginx/html/app-config.js' || {
  echo "FAIL: app-config no existe en canary" >&2
  exit 4
}

docker exec "$CANARY_CONTAINER" sh -lc 'ls /usr/share/nginx/html/*.js /usr/share/nginx/html/*.js.gz >/dev/null 2>&1' || {
  echo "FAIL: no se encontraron assets JS en canary" >&2
  exit 5
}

bundle_stream='({ gzip -cd /usr/share/nginx/html/*.js.gz 2>/dev/null || true; cat /usr/share/nginx/html/*.js 2>/dev/null || true; })'
cargar_mas_token=$(printf 'Cargar m\303\241s informes')

for token in \
  "downloadInformePdf" \
  "getEstadoGeneracionInforme" \
  "pollAnalysisJob" \
  "fetchHistorialFromBackend" \
  "Actualizar historial" \
  "$cargar_mas_token"
do
  docker exec "$CANARY_CONTAINER" sh -lc "$bundle_stream | grep -a -F \"$token\" >/dev/null" || {
    echo "FAIL: bundle canary no contiene token canónico '$token'" >&2
    exit 6
  }
done

echo "REMOTE_OK"
'@

$remoteScript = $remoteScript.Replace("__BASE_URL__", $baseUrlQ)
$remoteScript = $remoteScript.Replace("__CANARY_CONTAINER__", $canaryContainerQ)

$sshArgs = @("-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=no", "-i", $SshKey, $HostName, "bash -s")
$remoteOutput = $remoteScript | & ssh @sshArgs 2>&1
if ($LASTEXITCODE -ne 0) { Fail "Smoke OHIF canary falló: $remoteOutput" }

$rootLine = $remoteOutput | Where-Object { $_ -like "ROOT_CODE=*" } | Select-Object -First 1
if (-not $rootLine) { Fail "Salida remota incompleta: $remoteOutput" }
Pass "/=200 en OHIF canary"

if (-not ($remoteOutput -contains "REMOTE_OK")) { Fail "No se recibió confirmación remota final" }
Pass "Bundle canary contiene tokens del fix OHIF"
Write-Host "OHIF CANARY SMOKE: PASS ($BaseUrl)"

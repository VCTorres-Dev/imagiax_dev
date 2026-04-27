param(
    [string]$HostName = "root@imagiax.dev",
    [string]$SshKey = "$env:USERPROFILE\.ssh\do_imagiax",
    [string]$Network = "orthanc-imagiax_default",
    [string]$ProdContainer = "fastapi-imagiax",
    [int]$CanaryPort = 18010
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "[FAIL] $Message"
}

function Pass([string]$Message) {
    Write-Host "[PASS] $Message"
}

function ConvertTo-BashSingleQuoted([string]$Value) {
    if ($Value -match "'") {
        Fail "Valor no soportado para shell remoto: contiene comilla simple"
    }
    return "'" + $Value + "'"
}

function Run-Git([string[]]$CommandArgs) {
    $output = & git @CommandArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Fail "git $($CommandArgs -join ' ') -> $output"
    }
    return $output
}

if (-not (Test-Path $SshKey)) {
    Fail "SSH key no encontrada: $SshKey"
}
Pass "SSH key presente"

$inside = Run-Git -CommandArgs @("rev-parse", "--is-inside-work-tree")
if (($inside | Select-Object -First 1).Trim() -ne "true") {
    Fail "No estás dentro de un repo git"
}

$status = Run-Git -CommandArgs @("status", "--porcelain=v1", "--untracked-files=all")
if ($status) {
    Fail "Working tree no está limpio"
}
Pass "Working tree limpio"

$branch = (Run-Git -CommandArgs @("branch", "--show-current") | Select-Object -First 1).Trim()
$commit = (Run-Git -CommandArgs @("rev-parse", "HEAD") | Select-Object -First 1).Trim()
Pass "Rama actual: $branch"
Pass "Commit actual: $commit"

$prodContainerQ = ConvertTo-BashSingleQuoted $ProdContainer
$networkQ = ConvertTo-BashSingleQuoted $Network
$canaryPortQ = ConvertTo-BashSingleQuoted ([string]$CanaryPort)

$remoteScript = @'
set -eu

PROD_CONTAINER=__PROD_CONTAINER__
NETWORK_NAME=__NETWORK_NAME__
CANARY_PORT=__CANARY_PORT__

cmd_json=$(docker inspect --format '{{json .Config.Cmd}}' "$PROD_CONTAINER" 2>/dev/null || true)
if [ -z "$cmd_json" ] || [ "$cmd_json" = "null" ]; then
  echo "FAIL: no se pudo inspeccionar contenedor de prod '$PROD_CONTAINER'" >&2
  exit 2
fi

echo "CMD_JSON=$cmd_json"

if ! printf '%s' "$cmd_json" | grep -q 'api.main:create_app'; then
  echo "FAIL: runtime prod no contiene api.main:create_app" >&2
  exit 3
fi

docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 || {
  echo "FAIL: red '$NETWORK_NAME' no existe" >&2
  exit 4
}

if ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)'"$CANARY_PORT"'$'; then
  echo "FAIL: puerto $CANARY_PORT está en uso en el host" >&2
  exit 5
fi

echo "REMOTE_OK"
'@

$remoteScript = $remoteScript.Replace("__PROD_CONTAINER__", $prodContainerQ)
$remoteScript = $remoteScript.Replace("__NETWORK_NAME__", $networkQ)
$remoteScript = $remoteScript.Replace("__CANARY_PORT__", $canaryPortQ)

$sshArgs = @(
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=no",
    "-i", $SshKey,
    $HostName,
    "bash -s"
)

$remoteOutput = $remoteScript | & ssh @sshArgs 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail "Chequeo remoto falló: $remoteOutput"
}

$cmdLine = ($remoteOutput | Where-Object { $_ -like "CMD_JSON=*" } | Select-Object -First 1)
if ($cmdLine) {
    Pass "Runtime prod verificado: $($cmdLine -replace '^CMD_JSON=', '')"
} else {
    Fail "No se recibió CMD_JSON desde host remoto"
}

if (-not ($remoteOutput -contains "REMOTE_OK")) {
    Fail "No se recibió confirmación remota final"
}

Pass "Red remota '$Network' existe"
Pass "Puerto $CanaryPort libre en host remoto"
Write-Host "CANARY PREREQS: PASS"

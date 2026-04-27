param(
    [string]$BaseUrl = "http://127.0.0.1:18010",
    [string]$HostName = "root@imagiax.dev",
    [string]$SshKey = "$env:USERPROFILE\.ssh\do_imagiax"
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "[FAIL] $Message"
}

function Warn([string]$Message) {
    Write-Warning "[WARN] $Message"
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

if (-not (Test-Path $SshKey)) {
    Fail "SSH key no encontrada: $SshKey"
}

$baseUrlQ = ConvertTo-BashSingleQuoted $BaseUrl

$remoteScript = @'
set -eu

BASE_URL=__BASE_URL__

salud_code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/salud" || true)
frontend_code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/config/frontend" || true)

echo "SALUD_CODE=$salud_code"
echo "FRONTEND_CODE=$frontend_code"
'@

$remoteScript = $remoteScript.Replace("__BASE_URL__", $baseUrlQ)

$sshArgs = @(
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=no",
    "-i", $SshKey,
    $HostName,
    "bash -s"
)

$remoteOutput = $remoteScript | & ssh @sshArgs 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail "No se pudo ejecutar smoke remoto: $remoteOutput"
}

$saludLine = $remoteOutput | Where-Object { $_ -like "SALUD_CODE=*" } | Select-Object -First 1
$frontendLine = $remoteOutput | Where-Object { $_ -like "FRONTEND_CODE=*" } | Select-Object -First 1

if (-not $saludLine -or -not $frontendLine) {
    Fail "Salida remota incompleta: $remoteOutput"
}

$saludCode = ($saludLine -replace "^SALUD_CODE=", "").Trim()
$frontendCode = ($frontendLine -replace "^FRONTEND_CODE=", "").Trim()

if ($saludCode -ne "200") {
    Fail "/salud devolvió $saludCode (se requiere 200)"
}
Pass "/salud=200"

if ($frontendCode -ne "200") {
    Warn "/config/frontend devolvió $frontendCode (esperado 200 en este runbook)."
    Fail "/config/frontend no cumple el smoke mínimo"
}
Pass "/config/frontend=200"

Write-Host "SMOKE CANARY: PASS ($BaseUrl)"

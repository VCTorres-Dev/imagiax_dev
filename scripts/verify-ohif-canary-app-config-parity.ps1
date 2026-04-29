param(
  [string]$ComposePath = ".\OHIF Medical Imaging Viewer\orthanc-imagiax\docker-compose.prod.yml",
  [string]$CanaryLogPath = "",
  [string]$AppConfigSnapshotPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Pass([string]$Message) { Write-Host "[PASS] $Message" -ForegroundColor Green }
function Write-Fail([string]$Message) { Write-Host "[FAIL] $Message" -ForegroundColor Red }

$hasErrors = $false

function Add-Error([string]$Message) {
  $script:hasErrors = $true
  Write-Fail $Message
}

if (-not (Test-Path $ComposePath)) {
  Add-Error "No se encontró compose: $ComposePath"
} else {
  $composeRaw = Get-Content -Path $ComposePath -Raw -Encoding UTF8

  if ($composeRaw -notmatch '(?m)^\s*APP_CONFIG\s*:') {
    Add-Error "El compose no declara APP_CONFIG para OHIF."
  } else {
    Write-Pass "APP_CONFIG declarado en compose."
  }

  $requiredTokens = @(
    'window.__IMAGIAX_API_URL__ = ''/api''',
    'window.__IMAGIAX_V2_API_URL__ = ''/api''',
    'window.__IMAGIAX_CONFIG__ = { backend_url: ''/api'' }'
  )

  foreach ($token in $requiredTokens) {
    if ($composeRaw -notlike "*$token*") {
      Add-Error "Falta token de paridad en APP_CONFIG: $token"
    }
  }

  # Verificación defensiva: no exponer literales típicos de secreto en APP_CONFIG.
  $secretPattern = '(?i)(api[_-]?key\s*[:=]|password\s*[:=]|token\s*[:=]|secret\s*[:=])'
  if ($composeRaw -match $secretPattern) {
    Add-Error "APP_CONFIG parece contener literales sensibles (token/password/secret)."
  } else {
    Write-Pass "APP_CONFIG sin literales sensibles obvios."
  }
}

if ($CanaryLogPath) {
  if (-not (Test-Path $CanaryLogPath)) {
    Add-Error "No se encontró log canary: $CanaryLogPath"
  } else {
    $logRaw = Get-Content -Path $CanaryLogPath -Raw -Encoding UTF8
    if ($logRaw -match 'Not using custom APP_CONFIG') {
      Add-Error 'Paridad inválida: el log indica "Not using custom APP_CONFIG".'
    } else {
      Write-Pass 'Log canary sin señal de fallback de APP_CONFIG.'
    }
  }
} else {
  Write-Host '[WARN] Sin -CanaryLogPath: se omitió chequeo de fallback en logs.' -ForegroundColor Yellow
}

if ($AppConfigSnapshotPath) {
  if (-not (Test-Path $AppConfigSnapshotPath)) {
    Add-Error "No se encontró snapshot APP_CONFIG: $AppConfigSnapshotPath"
  } else {
    $snapshot = Get-Content -Path $AppConfigSnapshotPath -Raw -Encoding UTF8
    if ($snapshot -match 'Not using custom APP_CONFIG') {
      Add-Error 'Paridad inválida: el snapshot APP_CONFIG contiene fallback no custom.'
    } else {
      Write-Pass 'Snapshot APP_CONFIG sin fallback inválido.'
    }
  }
}

if ($hasErrors) {
  throw 'OHIF canary APP_CONFIG parity: FAIL'
}

Write-Pass 'OHIF canary APP_CONFIG parity: PASS'

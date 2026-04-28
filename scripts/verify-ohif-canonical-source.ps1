param(
  [string]$ExpectedPanelSha256 = 'a438d53e1db005099c02653b3301493b8efdb7c2ee50071f39ab4e2ef8664815',
  [string]$ExpectedApiSha256 = 'dc56901738f0b8477f17cbc290c1adda5e3c37a8aa1c54a6ad185c6974b8a9d0'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Pass([string]$Message) { Write-Host "[PASS] $Message" -ForegroundColor Green }
function Write-Fail([string]$Message) { Write-Host "[FAIL] $Message" -ForegroundColor Red }

$repoRoot = (& git rev-parse --show-toplevel).Trim()
if (-not $repoRoot) {
  throw 'Could not resolve git repository root.'
}

$srcRoot = Join-Path $repoRoot 'ohif-extension-imagiax-ai/src'
$panelPath = Join-Path $srcRoot 'ImagiaxAiPanel.tsx'
$apiPath = Join-Path $srcRoot 'services/imagiaxApi.ts'

$tokenCargarMasInformes = 'Cargar m' + [char]0x00E1 + 's informes'

$requiredTokens = @(
  'PDF_THEME',
  'fetchHistorialFromBackend',
  'HISTORY_PAGE_SIZE',
  'Actualizar historial',
  $tokenCargarMasInformes,
  'downloadInformePdf',
  'isGenerarInformeAcceptedResponse',
  'getEstadoGeneracionInforme',
  'pollAnalysisJob'
)

$tokenErrors = New-Object System.Collections.Generic.List[string]

function Normalize-Text([string]$Text) {
  if ($null -eq $Text) { return '' }
  return $Text.Normalize([System.Text.NormalizationForm]::FormC)
}

function Get-LfNormalizedSha256([string]$Path) {
  $bytes = [System.IO.File]::ReadAllBytes($Path)
  $text = [System.Text.Encoding]::UTF8.GetString($bytes)
  $normalizedText = $text.Replace("`r`n", "`n").Replace("`r", "`n")
  $normalizedBytes = [System.Text.Encoding]::UTF8.GetBytes($normalizedText)
  $sha256 = [System.Security.Cryptography.SHA256]::Create()
  try {
    return ([System.BitConverter]::ToString($sha256.ComputeHash($normalizedBytes))).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha256.Dispose()
  }
}

$sourceFiles = Get-ChildItem -Path $srcRoot -Recurse -File -Include *.ts,*.tsx
$allSourceText = Normalize-Text (($sourceFiles | ForEach-Object { Get-Content -Path $_.FullName -Raw -Encoding UTF8 }) -join "`n")

foreach ($token in $requiredTokens) {
  $normalizedToken = Normalize-Text $token
  if ($allSourceText.Contains($normalizedToken)) {
    Write-Pass "Token found: $token"
  } else {
    $tokenErrors.Add($token)
    Write-Fail "Token missing: $token"
  }
}
$actualPanelHash = Get-LfNormalizedSha256 $panelPath
$actualApiHash = Get-LfNormalizedSha256 $apiPath

$hashErrors = New-Object System.Collections.Generic.List[string]

if ($actualPanelHash -eq $ExpectedPanelSha256.ToLowerInvariant()) {
  Write-Pass "Panel hash matches mirror snapshot ($actualPanelHash)"
} else {
  $hashErrors.Add('ImagiaxAiPanel.tsx')
  Write-Fail "Panel hash mismatch. expected=$ExpectedPanelSha256 actual=$actualPanelHash"
}

if ($actualApiHash -eq $ExpectedApiSha256.ToLowerInvariant()) {
  Write-Pass "API hash matches mirror snapshot ($actualApiHash)"
} else {
  $hashErrors.Add('imagiaxApi.ts')
  Write-Fail "API hash mismatch. expected=$ExpectedApiSha256 actual=$actualApiHash"
}

if ($tokenErrors.Count -gt 0 -or $hashErrors.Count -gt 0) {
  throw "Verification failed. MissingTokens=$($tokenErrors.Count) HashMismatches=$($hashErrors.Count)"
}

Write-Pass 'OHIF canonical source verification passed.'

param(
    [string]$HostName = "root@imagiax.dev",
    [string]$SshKey = "$env:USERPROFILE\.ssh\do_imagiax",
    [string]$ProdContainer = "ohif-imagiax",
    [string]$ProdImage = "orthanc-imagiax-ohif",
    [string]$RemoteViewersPath = "/root/imagiax/OHIF Medical Imaging Viewer/Viewers",
    [int]$CanaryPort = 18080,
    [switch]$AllowDirtyTree
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) { throw "[FAIL] $Message" }
function Warn([string]$Message) { Write-Warning "[WARN] $Message" }
function Pass([string]$Message) { Write-Host "[PASS] $Message" }

function ConvertTo-BashSingleQuoted([string]$Value) {
    if ($Value -match "'") { Fail "Valor no soportado para shell remoto: contiene comilla simple" }
    return "'" + $Value + "'"
}

function Run-Git([string[]]$CommandArgs) {
    $output = & git @CommandArgs 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "git $($CommandArgs -join ' ') -> $output" }
    return $output
}

if (-not (Test-Path $SshKey)) { Fail "SSH key no encontrada: $SshKey" }
Pass "SSH key presente"

$inside = Run-Git -CommandArgs @("rev-parse", "--is-inside-work-tree")
if (($inside | Select-Object -First 1).Trim() -ne "true") { Fail "No estás dentro de un repo git" }

$repoRoot = (Run-Git -CommandArgs @("rev-parse", "--show-toplevel") | Select-Object -First 1).Trim()
$canonicalVerifier = Join-Path $repoRoot "scripts/verify-ohif-canonical-source.ps1"
if (-not (Test-Path $canonicalVerifier)) { Fail "No se encontró verificador canónico: $canonicalVerifier" }

& powershell -ExecutionPolicy Bypass -File $canonicalVerifier
if ($LASTEXITCODE -ne 0) { Fail "La verificación de fuente canónica OHIF falló" }
Pass "Fuente OHIF canónica validada (hashes + tokens)"

$branch = (Run-Git -CommandArgs @("branch", "--show-current") | Select-Object -First 1).Trim()
$commit = (Run-Git -CommandArgs @("rev-parse", "HEAD") | Select-Object -First 1).Trim()
Pass "Rama actual: $branch"
Pass "Commit HEAD: $commit"

$status = Run-Git -CommandArgs @("status", "--porcelain=v1", "--untracked-files=all")
if ($status -and -not $AllowDirtyTree) {
    Fail "Working tree no está limpio. Usá -AllowDirtyTree solo para revisión previa; nunca para deploy. Cambios: $($status -join '; ')"
}
if ($status -and $AllowDirtyTree) { Warn "Working tree no está limpio; permitido solo para revisión previa." } else { Pass "Working tree limpio" }

if (-not (Test-Path "ohif-extension-imagiax-ai\src\ImagiaxAiPanel.tsx")) { Fail "Falta ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx" }
if (-not (Test-Path "ohif-extension-imagiax-ai\src\services\imagiaxApi.ts")) { Fail "Falta ohif-extension-imagiax-ai/src/services/imagiaxApi.ts" }
Pass "Fuente OHIF canónica presente"

$tokenCargarMasInformes = "Cargar m$([char]0x00E1)s informes"
$canonicalTokens = @(
    "PDF_THEME",
    "fetchHistorialFromBackend",
    "HISTORY_PAGE_SIZE",
    "Actualizar historial",
    $tokenCargarMasInformes,
    "downloadInformePdf",
    "isGenerarInformeAcceptedResponse",
    "getEstadoGeneracionInforme",
    "pollAnalysisJob"
)

$sourceRoot = "ohif-extension-imagiax-ai\src"
$allSourceText = (Get-ChildItem -Path $sourceRoot -Recurse -File -Include *.ts,*.tsx | ForEach-Object { Get-Content -Path $_.FullName -Raw -Encoding UTF8 }) -join "`n"
foreach ($token in $canonicalTokens) {
    if ($allSourceText -notlike "*$token*") { Fail "Fuente canónica no contiene token requerido: $token" }
}
Pass "Tokens canónicos OHIF presentes en fuente local"

$prodContainerQ = ConvertTo-BashSingleQuoted $ProdContainer
$prodImageQ = ConvertTo-BashSingleQuoted $ProdImage
$remoteViewersPathQ = ConvertTo-BashSingleQuoted $RemoteViewersPath
$canaryPortQ = ConvertTo-BashSingleQuoted ([string]$CanaryPort)

$remoteScript = @'
set -eu

PROD_CONTAINER=__PROD_CONTAINER__
PROD_IMAGE=__PROD_IMAGE__
VIEWERS_PATH=__VIEWERS_PATH__
CANARY_PORT=__CANARY_PORT__

image_id=$(docker inspect --format '{{.Image}}' "$PROD_CONTAINER" 2>/dev/null || true)
if [ -z "$image_id" ]; then
  echo "FAIL: no se pudo inspeccionar contenedor OHIF '$PROD_CONTAINER'" >&2
  exit 2
fi
echo "OHIF_IMAGE_ID=$image_id"

image_name=$(docker inspect --format '{{.Config.Image}}' "$PROD_CONTAINER" 2>/dev/null || true)
echo "OHIF_IMAGE_NAME=$image_name"
if [ "$image_name" != "$PROD_IMAGE" ]; then
  echo "WARN: imagen OHIF activa '$image_name' difiere de esperada '$PROD_IMAGE'" >&2
fi

docker image inspect "$PROD_IMAGE" >/dev/null 2>&1 || {
  echo "FAIL: imagen '$PROD_IMAGE' no existe localmente en host" >&2
  exit 3
}

test -d "$VIEWERS_PATH" || { echo "FAIL: Viewers path no existe: $VIEWERS_PATH" >&2; exit 4; }
test -f "$VIEWERS_PATH/Dockerfile" || { echo "FAIL: Viewers Dockerfile no existe" >&2; exit 5; }
test -d "$VIEWERS_PATH/extensions/imagiax-ai/src" || { echo "FAIL: extensión imagiax-ai no existe dentro de Viewers" >&2; exit 6; }

if [ -d "$VIEWERS_PATH/.git" ]; then
  echo "VIEWERS_GIT=present"
else
  echo "VIEWERS_GIT=missing"
fi

if ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)'"$CANARY_PORT"'$'; then
  echo "FAIL: puerto $CANARY_PORT está en uso en el host" >&2
  exit 7
fi

echo "REMOTE_OK"
'@

$remoteScript = $remoteScript.Replace("__PROD_CONTAINER__", $prodContainerQ)
$remoteScript = $remoteScript.Replace("__PROD_IMAGE__", $prodImageQ)
$remoteScript = $remoteScript.Replace("__VIEWERS_PATH__", $remoteViewersPathQ)
$remoteScript = $remoteScript.Replace("__CANARY_PORT__", $canaryPortQ)

$sshArgs = @("-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=no", "-i", $SshKey, $HostName, "bash -s")
$remoteOutput = $remoteScript | & ssh @sshArgs 2>&1
if ($LASTEXITCODE -ne 0) { Fail "Chequeo remoto OHIF falló: $remoteOutput" }

$remoteOutput | ForEach-Object { Write-Host $_ }
if (-not ($remoteOutput -contains "REMOTE_OK")) { Fail "No se recibió confirmación remota final" }

if ($remoteOutput -contains "VIEWERS_GIT=missing") {
    Warn "Viewers remoto no es git/reproducible. Reconciliar antes de construir OHIF."
} else {
    Pass "Viewers remoto tiene .git"
}

Pass "Puerto $CanaryPort libre en host remoto"
Write-Host "OHIF PREREQS: PASS (solo revisión read-only; NO autoriza build/deploy/restart)"

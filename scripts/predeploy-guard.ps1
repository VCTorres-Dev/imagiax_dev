param(
    [string]$ExpectedRuntime = "api.main:create_app",
    [string[]]$AllowedBranches = @("main", "release/*", "hotfix/*", "deploy/*", "prod/*"),
    [switch]$SkipRemoteRuntimeCheck,
    [string]$HostName = "root@imagiax.dev",
    [string]$SshKey = "$env:USERPROFILE\.ssh\do_imagiax",
    [string]$Container = "fastapi-imagiax"
)

$ErrorActionPreference = "Stop"

function Fail($Message) {
    Write-Error "PREDEPLOY BLOCKED: $Message"
    exit 1
}

function Matches-Pattern($Value, $Patterns) {
    foreach ($pattern in $Patterns) {
        if ($Value -like $pattern) { return $true }
    }
    return $false
}

$inside = git rev-parse --is-inside-work-tree 2>$null
if ($inside -ne "true") {
    Fail "not inside a git worktree"
}

$branch = git branch --show-current
if (-not (Matches-Pattern $branch $AllowedBranches)) {
    Fail "branch '$branch' is not allowed. Allowed: $($AllowedBranches -join ', ')"
}

$status = git status --porcelain=v1 --untracked-files=all
if ($status) {
    Fail "working tree is not clean. Commit, stash, or discard changes before deploy."
}

$dangerous = @(
    "(^|/)\.env(\..*)?$",
    "service_account.*\.json$",
    "\.sql$",
    "\.(dcm|dicom)$",
    "(^|/)_quarantine/",
    "(^|/)\.sdd-temp/",
    "(^|/)_backup/",
    "(^|/)nul$"
)

$tracked = git ls-files
foreach ($file in $tracked) {
    foreach ($pattern in $dangerous) {
        if ($file -match $pattern) {
            Fail "dangerous tracked path detected: $file"
        }
    }
}

$opsFiles = @(
    "Dockerfile",
    "OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml",
    "OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-proxy.conf"
)

foreach ($file in $opsFiles) {
    if (Test-Path $file) {
        Write-Host "OPS FILE PRESENT: $file"
    }
}

if (-not (Test-Path "Dockerfile")) {
    Fail "Dockerfile not found. Backend deploy must start from a reproducible image definition."
}

$dockerfile = Get-Content -Raw "Dockerfile"
if ($dockerfile -notlike "*$ExpectedRuntime*") {
    Fail "local Dockerfile does not contain expected runtime '$ExpectedRuntime'. Resolve runtime authority before deploy."
}

if (-not $SkipRemoteRuntimeCheck) {
    if (-not (Test-Path $SshKey)) {
        Fail "SSH key not found: $SshKey"
    }

    $cmd = "docker inspect --format '{{json .Config.Cmd}}' $Container"
    $runtime = & ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no -i $SshKey $HostName $cmd
    if (-not ($runtime -like "*$ExpectedRuntime*")) {
        Fail "live runtime '$runtime' does not contain expected runtime '$ExpectedRuntime'"
    }

    Write-Host "REMOTE RUNTIME OK: $runtime"
}

Write-Host "PREDEPLOY GUARD PASSED for branch '$branch'."

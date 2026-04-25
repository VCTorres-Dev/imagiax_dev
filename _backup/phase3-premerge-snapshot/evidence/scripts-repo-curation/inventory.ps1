Param(
  [string]$ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

function Test-ProtectedPath {
  param([string]$Root, [string]$RelativePath)
  $fullPath = Join-Path $Root $RelativePath
  return Test-Path -LiteralPath $fullPath
}

$protectedPaths = @(
  'app',
  'src',
  'migrations',
  'tests',
  'docs',
  'scripts',
  'OHIF Medical Imaging Viewer',
  'servidor_api_orthanc_v1.py'
)

$forbiddenPatterns = @(
  '^\.env($|\.)',
  '^service_account\.json($|/)',
  '^db-backup-.*\.sql$'
)

$entries = Get-ChildItem -LiteralPath $ProjectRoot -Force | Sort-Object Name
$entryNames = @($entries | ForEach-Object { $_.Name })

$protectedStatus = @()
foreach ($path in $protectedPaths) {
  $protectedStatus += [PSCustomObject]@{
    path = $path
    present = (Test-ProtectedPath -Root $ProjectRoot -RelativePath $path)
  }
}

$forbiddenCandidates = @()
foreach ($name in $entryNames) {
  foreach ($pattern in $forbiddenPatterns) {
    if ($name -match $pattern) {
      $forbiddenCandidates += $name
      break
    }
  }
}

[PSCustomObject]@{
  generated_at = (Get-Date).ToString('s')
  project_root = $ProjectRoot
  root_entry_count = $entryNames.Count
  root_entries = $entryNames
  protected_status = $protectedStatus
  forbidden_candidates = $forbiddenCandidates
} | ConvertTo-Json -Depth 6

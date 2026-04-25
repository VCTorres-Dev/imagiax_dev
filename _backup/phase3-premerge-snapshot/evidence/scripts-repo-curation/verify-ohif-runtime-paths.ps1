Param(
  [string]$ProjectRoot = (Get-Location).Path,
  [string]$OutputJson = "docs/repo-curation/ohif-runtime-path-verification.json"
)

$ErrorActionPreference = 'Stop'

function Resolve-ProjectPath {
  param([string]$Root, [string]$RelativePath)
  if ([System.IO.Path]::IsPathRooted($RelativePath)) {
    return $RelativePath
  }

  return (Join-Path $Root $RelativePath)
}

function Ensure-ParentDirectory {
  param([string]$FilePath)
  $parent = Split-Path -Path $FilePath -Parent
  if (-not [string]::IsNullOrWhiteSpace($parent) -and -not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
}

$projectRootFull = (Resolve-Path -LiteralPath $ProjectRoot).Path
$outputJsonFull = Resolve-ProjectPath -Root $projectRootFull -RelativePath $OutputJson

$checks = @(
  [PSCustomObject]@{
    file = 'deploy_production.sh'
    expected_pattern = 'OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src'
    rationale = 'Deploy script must still copy extension source from nested runtime path.'
  },
  [PSCustomObject]@{
    file = 'start_ohif.bat'
    expected_pattern = 'OHIF Medical Imaging Viewer\\Viewers'
    rationale = 'Local OHIF starter must still cd into nested Viewers runtime folder.'
  },
  [PSCustomObject]@{
    file = 'arrancar_ohif.py'
    expected_pattern = 'OHIF Medical Imaging Viewer\\Viewers\\platform\\app'
    rationale = 'Python launcher must still reference nested Viewers platform app path.'
  },
  [PSCustomObject]@{
    file = 'OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1'
    expected_pattern = 'OHIF Medical Imaging Viewer\\Viewers\\platform\\app'
    rationale = 'Orthanc stack launcher must keep OHIF_DIR in nested Viewers path.'
  }
)

$results = @()

foreach ($check in $checks) {
  $filePath = Resolve-ProjectPath -Root $projectRootFull -RelativePath $check.file
  $exists = Test-Path -LiteralPath $filePath
  $found = $false
  $sample_line = ''

  if ($exists) {
    $content = Get-Content -LiteralPath $filePath
    foreach ($line in $content) {
      if ($line -match $check.expected_pattern) {
        $found = $true
        $sample_line = $line.Trim()
        break
      }
    }
  }

  $results += [PSCustomObject]@{
    file = $check.file
    absolute_path = $filePath
    exists = $exists
    expected_pattern = $check.expected_pattern
    found = $found
    sample_line = $sample_line
    rationale = $check.rationale
  }
}

$missingFiles = @($results | Where-Object { -not $_.exists })
$missingPatterns = @($results | Where-Object { $_.exists -and -not $_.found })

$status = if (($missingFiles.Count -eq 0) -and ($missingPatterns.Count -eq 0)) { 'pass' } else { 'fail' }

$report = [PSCustomObject]@{
  generated_at = (Get-Date).ToString('s')
  project_root = $projectRootFull
  status = $status
  totals = [PSCustomObject]@{
    checks = $results.Count
    files_missing = $missingFiles.Count
    pattern_mismatches = $missingPatterns.Count
  }
  results = $results
}

Ensure-ParentDirectory -FilePath $outputJsonFull
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $outputJsonFull -Encoding UTF8

if ($status -ne 'pass') {
  $report | ConvertTo-Json -Depth 8
  throw "Runtime/deploy nested-path verification failed. See $outputJsonFull"
}

$report | ConvertTo-Json -Depth 8

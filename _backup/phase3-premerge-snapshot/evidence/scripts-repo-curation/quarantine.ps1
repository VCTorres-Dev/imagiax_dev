Param(
  [string]$ProjectRoot = (Get-Location).Path,
  [switch]$Apply,
  [switch]$IncludeManualReview,
  [string]$ApprovalFile = "docs/repo-curation/quarantine-approval.txt",
  [string]$ManifestPath = "_quarantine/manifest.csv",
  [string]$CandidateManifestPath = "_quarantine/candidate-manifest.csv",
  [string]$CandidateReportPath = "docs/repo-curation/quarantine-candidates.json"
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
$quarantineRoot = Join-Path $projectRootFull '_quarantine'
$approvalFileFull = Resolve-ProjectPath -Root $projectRootFull -RelativePath $ApprovalFile
$manifestFileFull = Resolve-ProjectPath -Root $projectRootFull -RelativePath $ManifestPath
$candidateManifestFileFull = Resolve-ProjectPath -Root $projectRootFull -RelativePath $CandidateManifestPath
$candidateReportFileFull = Resolve-ProjectPath -Root $projectRootFull -RelativePath $CandidateReportPath

if (-not (Test-Path -LiteralPath $quarantineRoot)) {
  New-Item -ItemType Directory -Path $quarantineRoot -Force | Out-Null
}

$allowlistNames = @(
  '.agents', '.atl', '.claude', '.git', '.windsurf',
  '_quarantine',
  'app', 'docs', 'migrations', 'openspec', 'scripts', 'sdd', 'src', 'tests',
  'OHIF Medical Imaging Viewer', 'ohif-extension-imagiax-ai',
  'servidor_api_orthanc_v1.py',
  '.gitignore', '.dockerignore',
  'Dockerfile', 'README.md',
  'package.json', 'package-lock.json',
  'pyproject.toml', 'requirements.txt',
  'deploy.sh', 'deploy_production.sh'
)

$manualReviewNames = @(
  'archive', 'models', 'monitoring', 'prompts', 'research', 'env'
)

$forbiddenPreservePatterns = @(
  '^\.env($|\.)',
  '^service_account\.json($|/)$'
)

$candidateRules = @(
  [PSCustomObject]@{ id = 'benchmark-dir'; kind = 'Directory'; regex = '^benchmark_v\d+$'; reason = 'benchmark_root_noise' },
  [PSCustomObject]@{ id = 'benchmark-file'; kind = 'File'; regex = '^bv\d+\.py$'; reason = 'benchmark_loose_script' },
  [PSCustomObject]@{ id = 'debug-file'; kind = 'File'; regex = '^debug_.*'; reason = 'debug_artifact' },
  [PSCustomObject]@{ id = 'fix-file'; kind = 'File'; regex = '^fix_.*'; reason = 'ad_hoc_fix_script' },
  [PSCustomObject]@{ id = 'tmp-check-file'; kind = 'File'; regex = '^check_.*_tmp\.py$'; reason = 'temporary_check_script' },
  [PSCustomObject]@{ id = 'tmp-artifact'; kind = 'Any'; regex = '^tmp_.*'; reason = 'temporary_artifact' },
  [PSCustomObject]@{ id = 'root-test-file'; kind = 'File'; regex = '^test_.*\.(py|js)$'; reason = 'loose_root_test_script' },
  [PSCustomObject]@{ id = 'raw-data-folder'; kind = 'Directory'; regex = '^(datos_.*|RX Dicom Veridicos|rx tx_)$'; reason = 'raw_data_root_folder' },
  [PSCustomObject]@{ id = 'backup-dump'; kind = 'File'; regex = '^db-backup-.*\.sql$'; reason = 'backup_dump' }
)

$mode = if ($Apply) { 'apply' } else { 'dry-run' }

if ($Apply) {
  if (-not (Test-Path -LiteralPath $approvalFileFull)) {
    throw "Apply mode requires approval file: $approvalFileFull"
  }

  $approvalContent = Get-Content -LiteralPath $approvalFileFull -Raw
  if ($approvalContent -notmatch 'APPROVED_FOR_BATCH2\s*=\s*yes') {
    throw "Apply mode blocked. Set APPROVED_FOR_BATCH2=yes in $approvalFileFull after human review."
  }

  if ($IncludeManualReview -and $approvalContent -notmatch 'APPROVED_MANUAL_REVIEW\s*=\s*yes') {
    throw "Manual-review moves blocked. Set APPROVED_MANUAL_REVIEW=yes in $approvalFileFull after explicit review."
  }
}

$entries = Get-ChildItem -LiteralPath $projectRootFull -Force
$evaluationRows = @()
$candidateRows = @()
$moveManifestRows = @()
$movedCount = 0

foreach ($entry in $entries) {
  $name = $entry.Name
  $fullName = $entry.FullName
  $entryKind = if ($entry.PSIsContainer) { 'Directory' } else { 'File' }

  $classification = 'out-of-scope'
  $reason = 'not-targeted'
  $action = 'none'
  $destination = ''
  $candidateRuleId = ''

  if ($allowlistNames -contains $name) {
    $classification = 'allowlist-protected'
    $reason = 'allowlist'
  }
  elseif ($manualReviewNames -contains $name) {
    $classification = 'manual-review-hold'
    $reason = 'manual-review'
  }
  else {
    $isForbiddenPreserve = $false
    foreach ($pattern in $forbiddenPreservePatterns) {
      if ($name -match $pattern) {
        $classification = 'forbidden-preserve-local'
        $reason = 'forbidden-publication'
        $isForbiddenPreserve = $true
        break
      }
    }

    if (-not $isForbiddenPreserve) {
      foreach ($rule in $candidateRules) {
        $kindMatch = ($rule.kind -eq 'Any') -or ($rule.kind -eq $entryKind)
        if ($kindMatch -and $name -match $rule.regex) {
          $classification = 'quarantine-candidate'
          $reason = $rule.reason
          $candidateRuleId = $rule.id
          $destination = Join-Path $quarantineRoot $name

          if ($Apply) {
            $finalDestination = $destination
            if (Test-Path -LiteralPath $finalDestination) {
              $timestampSuffix = (Get-Date).ToString('yyyyMMddHHmmss')
              $finalDestination = "${destination}_$timestampSuffix"
            }

            Move-Item -LiteralPath $fullName -Destination $finalDestination
            $movedCount += 1
            $action = 'moved'
            $destination = $finalDestination

            $moveManifestRows += [PSCustomObject]@{
              timestamp = (Get-Date).ToString('s')
              source_path = $name
              destination_path = [System.IO.Path]::GetFileName($finalDestination)
              reason = $reason
              batch = '2'
            }
          }
          else {
            $action = 'planned'
          }

          $candidateRows += [PSCustomObject]@{
            name = $name
            kind = $entryKind
            reason = $reason
            rule_id = $candidateRuleId
            source_path = $name
            destination_path = [System.IO.Path]::GetFileName($destination)
            action = $action
            mode = $mode
          }

          break
        }
      }
    }
  }

  $evaluationRows += [PSCustomObject]@{
    name = $name
    kind = $entryKind
    classification = $classification
    reason = $reason
    action = $action
    source_path = $name
    destination_path = if ($destination) { [System.IO.Path]::GetFileName($destination) } else { '' }
    mode = $mode
  }
}

if ($Apply -and $moveManifestRows.Count -gt 0) {
  Ensure-ParentDirectory -FilePath $manifestFileFull

  if (-not (Test-Path -LiteralPath $manifestFileFull)) {
    'timestamp,source_path,destination_path,reason,batch' | Set-Content -LiteralPath $manifestFileFull -Encoding UTF8
  }

  $moveManifestRows | Export-Csv -LiteralPath $manifestFileFull -NoTypeInformation -Append
}

Ensure-ParentDirectory -FilePath $candidateManifestFileFull
$candidateRows | Export-Csv -LiteralPath $candidateManifestFileFull -NoTypeInformation

$summary = [PSCustomObject]@{
  generated_at = (Get-Date).ToString('s')
  mode = $mode
  project_root = $projectRootFull
  apply_requires_approval_file = $approvalFileFull
  include_manual_review = [bool]$IncludeManualReview
  totals = [PSCustomObject]@{
    root_entries = $entries.Count
    candidates = $candidateRows.Count
    moved = $movedCount
    allowlist_protected = (@($evaluationRows | Where-Object { $_.classification -eq 'allowlist-protected' })).Count
    manual_review_hold = (@($evaluationRows | Where-Object { $_.classification -eq 'manual-review-hold' })).Count
    forbidden_preserve_local = (@($evaluationRows | Where-Object { $_.classification -eq 'forbidden-preserve-local' })).Count
  }
  candidate_manifest = $candidateManifestFileFull
  move_manifest = $manifestFileFull
  candidates = $candidateRows
  evaluation = $evaluationRows
}

Ensure-ParentDirectory -FilePath $candidateReportFileFull
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $candidateReportFileFull -Encoding UTF8

$summary | ConvertTo-Json -Depth 8

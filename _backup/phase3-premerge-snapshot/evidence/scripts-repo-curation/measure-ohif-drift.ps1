Param(
  [string]$ProjectRoot = (Get-Location).Path,
  [string]$CanonicalRelativePath = "ohif-extension-imagiax-ai/src",
  [string]$MirrorRelativePath = "OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src",
  [string]$OutputJson = "docs/repo-curation/ohif-drift-report.json"
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

function Get-RelativePathKey {
  param([string]$BasePath, [string]$AbsolutePath)
  $trimChars = [char[]]@(92,47)
  $normalizedBase = ((Resolve-Path -LiteralPath $BasePath).Path).TrimEnd($trimChars)
  $normalizedAbsolute = (Resolve-Path -LiteralPath $AbsolutePath).Path

  if (-not $normalizedAbsolute.StartsWith($normalizedBase, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Path '$normalizedAbsolute' is not under base '$normalizedBase'"
  }

  $relative = $normalizedAbsolute.Substring($normalizedBase.Length).TrimStart($trimChars)
  return ($relative -replace '\\', '/')
}

function Get-FileMap {
  param([string]$RootPath)

  $fileMap = @{}
  $files = Get-ChildItem -LiteralPath $RootPath -Recurse -File | Sort-Object FullName
  foreach ($file in $files) {
    $key = Get-RelativePathKey -BasePath $RootPath -AbsolutePath $file.FullName
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $fileMap[$key] = [PSCustomObject]@{
      relative_path = $key
      sha256 = $hash
      bytes = $file.Length
    }
  }

  return $fileMap
}

$projectRootFull = (Resolve-Path -LiteralPath $ProjectRoot).Path
$canonicalPath = Resolve-ProjectPath -Root $projectRootFull -RelativePath $CanonicalRelativePath
$mirrorPath = Resolve-ProjectPath -Root $projectRootFull -RelativePath $MirrorRelativePath
$outputJsonFull = Resolve-ProjectPath -Root $projectRootFull -RelativePath $OutputJson

if (-not (Test-Path -LiteralPath $canonicalPath)) {
  throw "Canonical source path not found: $canonicalPath"
}

if (-not (Test-Path -LiteralPath $mirrorPath)) {
  throw "Runtime mirror path not found: $mirrorPath"
}

$canonicalMap = Get-FileMap -RootPath $canonicalPath
$mirrorMap = Get-FileMap -RootPath $mirrorPath

$canonicalKeys = @($canonicalMap.Keys | Sort-Object)
$mirrorKeys = @($mirrorMap.Keys | Sort-Object)

$canonicalOnly = @($canonicalKeys | Where-Object { -not $mirrorMap.ContainsKey($_) })
$mirrorOnly = @($mirrorKeys | Where-Object { -not $canonicalMap.ContainsKey($_) })

$sharedKeys = @($canonicalKeys | Where-Object { $mirrorMap.ContainsKey($_) })
$hashMismatches = @()
$hashMatches = 0
foreach ($key in $sharedKeys) {
  $canonicalHash = $canonicalMap[$key].sha256
  $mirrorHash = $mirrorMap[$key].sha256
  if ($canonicalHash -ne $mirrorHash) {
    $hashMismatches += [PSCustomObject]@{
      relative_path = $key
      canonical_sha256 = $canonicalHash
      mirror_sha256 = $mirrorHash
      canonical_bytes = $canonicalMap[$key].bytes
      mirror_bytes = $mirrorMap[$key].bytes
    }
  }
  else {
    $hashMatches += 1
  }
}

$report = [PSCustomObject]@{
  generated_at = (Get-Date).ToString('s')
  project_root = $projectRootFull
  canonical_source = $canonicalPath
  runtime_mirror = $mirrorPath
  totals = [PSCustomObject]@{
    canonical_files = $canonicalKeys.Count
    mirror_files = $mirrorKeys.Count
    shared_files = $sharedKeys.Count
    hash_matches = $hashMatches
    hash_mismatches = $hashMismatches.Count
    canonical_only = $canonicalOnly.Count
    mirror_only = $mirrorOnly.Count
  }
  canonical_only = $canonicalOnly
  mirror_only = $mirrorOnly
  hash_mismatches = $hashMismatches
}

Ensure-ParentDirectory -FilePath $outputJsonFull
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $outputJsonFull -Encoding UTF8

$report | ConvertTo-Json -Depth 8

<#
.SYNOPSIS
    Carga masiva de archivos DICOM a Orthanc PACS para IMAGIAX
.DESCRIPTION
    Escanea recursivamente la carpeta de DICOMs chilenos y los sube
    al servidor Orthanc via REST API (POST /instances)
.NOTES
    Autor: IMAGIAX Team
    Fecha: 2024
    Orthanc debe estar corriendo en http://localhost:8042
#>

param(
    [string[]]$DicomPaths = @(
        "C:\Users\WinterOS\Documents\Workspace\IMAGIAX\rx tx_",
        "C:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\RX Dicom Veridicos"
    ),
    [string]$OrthancUrl = "http://localhost:8042",
    [int]$MaxParallel = 4,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  IMAGIAX - Carga Masiva DICOM a Orthanc" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
foreach ($p in $DicomPaths) {
    Write-Host "Origen:  $p"
}
Write-Host "Destino: $OrthancUrl"
Write-Host ""

# Verificar que Orthanc está corriendo
try {
    $response = Invoke-RestMethod -Uri "$OrthancUrl/system" -Method Get -TimeoutSec 5
    Write-Host "[OK] Orthanc v$($response.Version) operativo - '$($response.Name)'" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] No se puede conectar a Orthanc en $OrthancUrl" -ForegroundColor Red
    Write-Host "        Asegurate de que Orthanc esta corriendo: docker compose up -d" -ForegroundColor Yellow
    exit 1
}

# Buscar todos los archivos .dcm en todas las rutas
Write-Host ""
Write-Host "Escaneando archivos DICOM..." -ForegroundColor Yellow
$dcmFiles = @()
foreach ($p in $DicomPaths) {
    if (Test-Path $p) {
        $found = Get-ChildItem -Path $p -Filter "*.dcm" -Recurse -File
        $dcmFiles += $found
        Write-Host "  $($found.Count) archivos en: $p" -ForegroundColor Gray
    } else {
        Write-Host "  [AVISO] Ruta no encontrada: $p" -ForegroundColor Yellow
    }
}
$totalFiles = $dcmFiles.Count

if ($totalFiles -eq 0) {
    Write-Host "[ERROR] No se encontraron archivos .dcm en ninguna ruta" -ForegroundColor Red
    exit 1
}

Write-Host "[OK] Encontrados $totalFiles archivos DICOM" -ForegroundColor Green

# Calcular tamaño total
$totalSizeMB = [math]::Round(($dcmFiles | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "     Tamaño total: ${totalSizeMB} MB" -ForegroundColor Gray

if ($DryRun) {
    Write-Host ""
    Write-Host "[DRY RUN] No se subiran archivos. Usa sin -DryRun para ejecutar." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "Iniciando carga a Orthanc..." -ForegroundColor Yellow
Write-Host "--------------------------------------------"

$uploaded = 0
$failed = 0
$skipped = 0
$startTime = Get-Date

foreach ($file in $dcmFiles) {
    $uploaded++
    $percent = [math]::Round(($uploaded / $totalFiles) * 100, 1)
    $elapsed = (Get-Date) - $startTime
    
    # Calcular ETA
    if ($uploaded -gt 1) {
        $avgTime = $elapsed.TotalSeconds / ($uploaded - 1)
        $remaining = [math]::Round($avgTime * ($totalFiles - $uploaded + 1))
        $eta = "ETA: ${remaining}s"
    } else {
        $eta = "ETA: calculando..."
    }
    
    # Mostrar progreso
    $shortName = $file.Directory.Name + "/" + $file.Name
    if ($shortName.Length -gt 50) { $shortName = "..." + $shortName.Substring($shortName.Length - 47) }
    Write-Host -NoNewline "`r[$uploaded/$totalFiles] ${percent}% | $eta | $shortName                    "
    
    try {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        $response = Invoke-RestMethod -Uri "$OrthancUrl/instances" `
            -Method Post `
            -Body $bytes `
            -ContentType "application/dicom" `
            -TimeoutSec 60
        
        if ($response.Status -eq "AlreadyStored") {
            $skipped++
        }
    } catch {
        $failed++
        $errorMsg = $_.Exception.Message
        Write-Host ""
        Write-Host "  [FALLO] $($file.Name): $errorMsg" -ForegroundColor Red
    }
}

$endTime = Get-Date
$duration = $endTime - $startTime

Write-Host ""
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  CARGA COMPLETADA" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Total archivos:   $totalFiles"
Write-Host "  Subidos OK:       $($uploaded - $failed)" -ForegroundColor Green
Write-Host "  Ya existian:      $skipped" -ForegroundColor Yellow
Write-Host "  Fallidos:         $failed" -ForegroundColor $(if ($failed -gt 0) {"Red"} else {"Green"})
Write-Host "  Tiempo total:     $([math]::Round($duration.TotalMinutes, 1)) minutos"
Write-Host "  Velocidad:        $([math]::Round($totalFiles / $duration.TotalMinutes, 1)) archivos/min"
Write-Host ""

# Verificar estadísticas en Orthanc
try {
    $stats = Invoke-RestMethod -Uri "$OrthancUrl/statistics" -Method Get
    Write-Host "  === Estadisticas Orthanc ===" -ForegroundColor Cyan
    Write-Host "  Pacientes:  $($stats.CountPatients)"
    Write-Host "  Estudios:   $($stats.CountStudies)"
    Write-Host "  Series:     $($stats.CountSeries)"
    Write-Host "  Instancias: $($stats.CountInstances)"
    Write-Host "  Disco:      $([math]::Round($stats.TotalDiskSizeMB, 1)) MB"
} catch {
    Write-Host "  No se pudieron obtener estadisticas" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Puedes verificar en: $OrthancUrl/ui/app/index.html" -ForegroundColor Green
Write-Host ""

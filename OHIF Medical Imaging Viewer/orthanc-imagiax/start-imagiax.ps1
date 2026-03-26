<#
.SYNOPSIS
    Arranca el stack completo IMAGIAX: Docker check â†’ Orthanc PACS â†’ OHIF Viewer
.DESCRIPTION
    Script Ãºnico para levantar todo el entorno de desarrollo local de IMAGIAX.
    Verifica prerequisitos, levanta Orthanc si no estÃ¡ corriendo, y lanza OHIF
    en modo Orthanc con proxy DICOMweb.
.NOTES
    Uso: .\start-imagiax.ps1
    Detener: .\start-imagiax.ps1 -Stop
#>

param(
    [switch]$Stop,
    [switch]$SkipOHIF,
    [switch]$SkipAI
)

$ErrorActionPreference = "Continue"
$ORTHANC_DIR = "c:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\orthanc-imagiax"
$OHIF_DIR = "c:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\Viewers\platform\app"
$IMAGIAX_DIR = "c:\Users\WinterOS\Documents\Workspace\IMAGIAX"
$ORTHANC_URL = "http://localhost:8042"
$OHIF_URL    = "http://localhost:3000"
$AI_API_URL  = "http://localhost:8010"
$PG_PORT     = 5432

# Asegurar PATH completo
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User") + ";" +
            "$env:USERPROFILE\.bun\bin"

function Write-Step($num, $msg) {
    Write-Host "`n[$num] $msg" -ForegroundColor Cyan
    Write-Host ("=" * 50) -ForegroundColor DarkGray
}

function Test-PortOpen($port) {
    $conn = Test-NetConnection -ComputerName localhost -Port $port -WarningAction SilentlyContinue
    return $conn.TcpTestSucceeded
}

# --- MODO STOP ---
if ($Stop) {
    Write-Host "`nDeteniendo IMAGIAX stack..." -ForegroundColor Yellow

    # Detener OHIF (matar procesos node en puerto 3000)
    $nodeProcs = Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue |
                 Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $nodeProcs) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Host "  OHIF (PID $procId) detenido" -ForegroundColor Gray
    }

    # Detener API IA (puerto 8010)
    $apiProcs = Get-NetTCPConnection -LocalPort 8010 -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $apiProcs) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Host "  API IA (PID $procId) detenida" -ForegroundColor Gray
    }

    # Detener Orthanc
    Push-Location $ORTHANC_DIR
    docker compose down 2>$null
    Pop-Location
    Write-Host "  Orthanc detenido" -ForegroundColor Gray

    Write-Host "`nStack IMAGIAX detenido." -ForegroundColor Green
    exit 0
}

# --- MODO START ---
Write-Host ""
Write-Host "  ========================================" -ForegroundColor Cyan
Write-Host "    IMAGIAX - Entorno de Desarrollo Local" -ForegroundColor Cyan
Write-Host "  ========================================" -ForegroundColor Cyan

# PASO 1: Docker
Write-Step 1 "Verificando Docker Engine"
$dockerOk = $false
try {
    $ver = (cmd /c "docker version --format '{{.Server.Version}}'" 2>&1)
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  Docker Engine v$ver operativo" -ForegroundColor Green
        $dockerOk = $true
    }
} catch {}

if (-not $dockerOk) {
    Write-Host "  Docker Engine no responde. Intentando iniciar Docker Desktop..." -ForegroundColor Yellow
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"

    $maxWait = 120
    $waited = 0
    while ($waited -lt $maxWait) {
        Start-Sleep -Seconds 5
        $waited += 5
        Write-Host "  Esperando Docker Engine... ($waited/$maxWait s)" -ForegroundColor Gray -NoNewline
        $check = cmd /c "docker info" 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host ""
            Write-Host "  Docker Engine operativo" -ForegroundColor Green
            $dockerOk = $true
            break
        }
        Write-Host ""
    }

    if (-not $dockerOk) {
        Write-Host "  ERROR: Docker no pudo iniciar en $maxWait segundos." -ForegroundColor Red
        Write-Host "  Abre Docker Desktop manualmente y vuelve a ejecutar este script." -ForegroundColor Yellow
        exit 1
    }
}

# PASO 2: Orthanc
Write-Step 2 "Verificando Orthanc PACS"
$orthancRunning = $false
try {
    $sys = Invoke-RestMethod -Uri "$ORTHANC_URL/system" -Method Get -TimeoutSec 3
    Write-Host "  Orthanc ya corriendo: '$($sys.Name)'" -ForegroundColor Green
    $orthancRunning = $true
} catch {
    Write-Host "  Orthanc no responde. Levantando contenedor..." -ForegroundColor Yellow
    Push-Location $ORTHANC_DIR
    docker compose up -d | Out-Host
    Pop-Location

    # Esperar a que responda
    $maxWait = 30
    $waited = 0
    while ($waited -lt $maxWait) {
        Start-Sleep -Seconds 2
        $waited += 2
        try {
            $sys = Invoke-RestMethod -Uri "$ORTHANC_URL/system" -Method Get -TimeoutSec 2
            $orthancRunning = $true
            break
        } catch {}
    }

    if ($orthancRunning) {
        Write-Host "  Orthanc levantado: '$($sys.Name)'" -ForegroundColor Green
    } else {
        Write-Host "  ERROR: Orthanc no respondio en $maxWait segundos." -ForegroundColor Red
        Write-Host "  Revisa: docker compose logs -f (en $ORTHANC_DIR)" -ForegroundColor Yellow
        exit 1
    }
}

# EstadÃ­sticas Orthanc
$stats = Invoke-RestMethod -Uri "$ORTHANC_URL/statistics" -Method Get
Write-Host "  Pacientes: $($stats.CountPatients) | Estudios: $($stats.CountStudies) | Series: $($stats.CountSeries) | Instancias: $($stats.CountInstances)" -ForegroundColor Gray

if ($stats.CountStudies -eq 0) {
    Write-Host "  Orthanc esta vacio. Cargando DICOMs..." -ForegroundColor Yellow
    Push-Location $ORTHANC_DIR
    PowerShell -ExecutionPolicy Bypass -File .\upload-dicoms.ps1
    Pop-Location
}

# PASO 2b: PostgreSQL
Write-Step "2b" "Verificando PostgreSQL (informes medicos)"
$pgRunning = Test-PortOpen $PG_PORT
if ($pgRunning) {
    Write-Host "  PostgreSQL ya corriendo en localhost:$PG_PORT" -ForegroundColor Green
} else {
    Write-Host "  PostgreSQL no responde; deberia haberse levantado con docker compose." -ForegroundColor Yellow
    Write-Host "  Espera unos segundos y vuelve a verificar o ejecuta: docker compose logs postgres-imagiax" -ForegroundColor Gray
    $maxWait = 20
    $waited = 0
    while ($waited -lt $maxWait) {
        Start-Sleep -Seconds 2
        $waited += 2
        if (Test-PortOpen $PG_PORT) {
            $pgRunning = $true
            break
        }
    }
    if ($pgRunning) {
        Write-Host "  PostgreSQL listo" -ForegroundColor Green
    } else {
        Write-Host "  ADVERTENCIA: PostgreSQL no disponible. Los informes IA no se podrÃ¡n persistir." -ForegroundColor Yellow
    }
}

# PASO 3: OHIF Viewer
if (-not $SkipOHIF) {
    Write-Step 3 "Verificando OHIF Viewer"
    $ohifRunning = Test-PortOpen 3000

    if ($ohifRunning) {
        Write-Host "  OHIF ya corriendo en $OHIF_URL" -ForegroundColor Green
    } else {
        Write-Host "  Iniciando OHIF Viewer (dev:orthanc)..." -ForegroundColor Yellow
        Write-Host "  Esto puede tardar ~30s en compilar webpack." -ForegroundColor Gray

        Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$OHIF_DIR'; yarn run dev:orthanc" -WindowStyle Normal

        # Esperar a que webpack compile
        $maxWait = 120
        $waited = 0
        while ($waited -lt $maxWait) {
            Start-Sleep -Seconds 5
            $waited += 5
            if (Test-PortOpen 3000) {
                $ohifRunning = $true
                break
            }
            Write-Host ("  Compilando webpack... ({0}s)" -f $waited) -ForegroundColor Gray
        }

        if ($ohifRunning) {
            Write-Host "  OHIF Viewer listo" -ForegroundColor Green
        } else {
            Write-Host "  OHIF tardo mas de lo esperado. Revisa la ventana de PowerShell que se abrio." -ForegroundColor Yellow
        }
    }
}

# PASO 4: API IMAGIAX Capa 1 (Orthanc + IA)
if (-not $SkipAI) {
    Write-Step 4 "Verificando API IMAGIAX (Capa 1)"

    $apiRunning = $false
    try {
        $saludApi = Invoke-RestMethod -Uri "$AI_API_URL/salud" -Method Get -TimeoutSec 4
        Write-Host "  API IA ya corriendo en $AI_API_URL ($($saludApi.version))" -ForegroundColor Green
        $apiRunning = $true
    } catch {
        Write-Host "  Iniciando API IA (servidor_api_orthanc_v1.py)..." -ForegroundColor Yellow
        Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$IMAGIAX_DIR'; py -m uvicorn servidor_api_orthanc_v1:app --host 0.0.0.0 --port 8010" -WindowStyle Normal

        $maxWait = 120
        $waited = 0
        while ($waited -lt $maxWait) {
            Start-Sleep -Seconds 4
            $waited += 4
            try {
                $saludApi = Invoke-RestMethod -Uri "$AI_API_URL/salud" -Method Get -TimeoutSec 3
                $apiRunning = $true
                break
            } catch {
                Write-Host ("  Cargando modelo IA... ({0}s)" -f $waited) -ForegroundColor Gray
            }
        }

        if ($apiRunning) {
            Write-Host "  API IA lista" -ForegroundColor Green
        } else {
            Write-Host "  API IA tardÃ³ mÃ¡s de lo esperado. Revisa la ventana de PowerShell abierta." -ForegroundColor Yellow
        }
    }
}

# RESUMEN FINAL
Write-Host ""
Write-Host "  ========================================" -ForegroundColor Green
Write-Host "    IMAGIAX - Stack Listo" -ForegroundColor Green
Write-Host "  ========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  OHIF Viewer:  $OHIF_URL" -ForegroundColor White
Write-Host "  Orthanc PACS: $ORTHANC_URL" -ForegroundColor White
Write-Host "  Orthanc UI:   $ORTHANC_URL/ui/app/index.html" -ForegroundColor White
Write-Host "  API IA:       $AI_API_URL" -ForegroundColor White
Write-Host "  PostgreSQL:   localhost:$PG_PORT  (DB: imagiax)" -ForegroundColor White
Write-Host "  Docs API:     $AI_API_URL/docs" -ForegroundColor White
Write-Host ""
Write-Host "  Datos: $($stats.CountPatients) pacientes / $($stats.CountStudies) estudios" -ForegroundColor Gray
Write-Host ""
Write-Host "  Para detener: .\start-imagiax.ps1 -Stop" -ForegroundColor DarkGray
Write-Host ""

# Abrir navegador
Start-Process $OHIF_URL


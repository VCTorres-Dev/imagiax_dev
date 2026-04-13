# IMAGIAX - Setup Orthanc + OHIF Viewer (Local)

## Requisitos Cumplidos
- [x] Node.js v24.13.0
- [x] Yarn 1.22.22
- [x] Docker Desktop 4.63.0
- [x] WSL2 habilitado (requiere reinicio)
- [x] OHIF Viewer clonado y dependencias instaladas

## Después del Reinicio del Sistema

### Paso 1: Verificar Docker
```powershell
docker version
# Debe mostrar Client Y Server
```

### Paso 2: Levantar Orthanc PACS
```powershell
cd "C:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\orthanc-imagiax"
docker compose up -d
```
Verificar en: http://localhost:8042 (interfaz web de Orthanc)

### Paso 3: Cargar los 714 DICOMs chilenos
```powershell
cd "C:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\orthanc-imagiax"
.\upload-dicoms.ps1
```
Esto sube los 332 estudios de tórax chilenos a Orthanc.

### Paso 4: Iniciar OHIF Viewer conectado a Orthanc
```powershell
cd "C:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\Viewers\platform\app"
yarn run dev:orthanc
```
Abrir en navegador: http://localhost:3000

## Estructura de Archivos
```
orthanc-imagiax/
├── docker-compose.yml      # Contenedor Orthanc
├── config/
│   └── orthanc.json        # Configuración Orthanc + DICOMweb
├── upload-dicoms.ps1       # Script carga masiva DICOM
└── README.md               # Este archivo
```

## URLs Importantes
| Servicio | URL |
|----------|-----|
| OHIF Viewer | http://localhost:3000 |
| Orthanc Web | http://localhost:8042 |
| Orthanc API | http://localhost:8042/dicom-web/ |
| DICOM Port  | localhost:4242 |

## Datos DICOM
- **Origen**: `C:\Users\WinterOS\Documents\Workspace\IMAGIAX\rx tx_\`
- **Estudios**: 332 pacientes chilenos
- **Archivos**: 714 .dcm (tórax)
- **Tamaño aprox**: ~7 GB

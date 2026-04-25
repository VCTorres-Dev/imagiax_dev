# IMAGIAX Backup Restoration Guide

## Prerequisites
- SSH access: `ssh -i ~/.ssh/do_imagiax root@imagiax.dev`
- Docker running with containers `postgres-imagiax` and `orthanc` available
- Backup files in `/root/backups/{db,orthanc}/`

## 1. Restore PostgreSQL Database

### Find your backup
```bash
ls -lht /root/backups/db/
```

### Restore (DESTRUCTIVE — drops and recreates the database)
```bash
# Stop the application to prevent writes during restore
cd "/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax" && docker compose -f docker-compose.prod.yml stop fastapi

# Drop and recreate database
docker exec postgres-imagiax psql -U imagiax -d postgres -c "DROP DATABASE IF EXISTS imagiax;"
docker exec postgres-imagiax psql -U imagiax -d postgres -c "CREATE DATABASE imagiax OWNER imagiax;"

# Restore from backup (replace FILENAME with actual file)
gunzip -c /root/backups/db/FILENAME.dump.gz \
    | docker exec -i postgres-imagiax pg_restore -U imagiax -d imagiax --no-owner --no-privileges

# Restart application
cd "/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax" && docker compose -f docker-compose.prod.yml start fastapi
```

### Verify
```bash
docker exec postgres-imagiax psql -U imagiax -d imagiax -c "\dt"
docker exec postgres-imagiax psql -U imagiax -d imagiax -c "SELECT count(*) FROM usuarios;"
```

## 2. Restore Orthanc DICOM Data

### Find your backup
```bash
ls -lht /root/backups/orthanc/
```

### Restore (DESTRUCTIVE — replaces all Orthanc data)
```bash
# Stop Orthanc
cd "/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax" && docker compose -f docker-compose.prod.yml stop orthanc

# Clear existing data
rm -rf /var/lib/docker/volumes/orthanc-imagiax_orthanc-data/_data/*

# Extract backup (replace FILENAME with actual file)
tar -xzf /root/backups/orthanc/FILENAME.tar.gz \
    -C /var/lib/docker/volumes/orthanc-imagiax_orthanc-data/_data/

# Start Orthanc
cd "/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax" && docker compose -f docker-compose.prod.yml start orthanc
```

### Verify
```bash
curl -s http://localhost:8042/statistics | python3 -m json.tool
```

## 3. Full System Restore (both)
Run steps 1 and 2 in order. Start fastapi AFTER both are complete.

## Notes
- pg_restore requires the same major PostgreSQL version (currently 16)
- Orthanc SQLite index is included in the tar — do NOT restore just DICOM files
- All backup files have 600 permissions (root-only readable)
- Backups contain PHI — handle according to data protection requirements

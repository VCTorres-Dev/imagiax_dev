#!/bin/bash
cd "/mnt/c/Users/WinterOS/Documents/Workspace/IMAGIAX - copia (2)/"

echo "Copying app files..."
scp -i ~/.ssh/do_imagiax imagiax_auth.py imagiax_admin.py imagiax_db.py "root@imagiax.dev:'/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/app/'"

sleep 4

echo "Copying config files..."
scp -i ~/.ssh/do_imagiax .env.production requirements.txt "root@imagiax.dev:'/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/'"

sleep 4

echo "Rebuilding Docker container..."
ssh -i ~/.ssh/do_imagiax root@imagiax.dev 'cd "/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax" && docker compose -f docker-compose.prod.yml build --no-cache fastapi && docker compose -f docker-compose.prod.yml up -d fastapi'

sleep 6

echo "Verifying..."
ssh -i ~/.ssh/do_imagiax root@imagiax.dev 'docker ps --filter name=fastapi-imagiax --format "{{.Status}}" && echo "---LOGS---" && docker logs --tail 30 fastapi-imagiax'

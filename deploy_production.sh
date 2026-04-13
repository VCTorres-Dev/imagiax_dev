#!/bin/bash
set -e

echo "Starting deployment..."

# SCP files
echo "Copying app files..."
scp -o ConnectTimeout=10 -i ~/.ssh/do_imagiax imagiax_auth.py imagiax_admin.py imagiax_db.py root@imagiax.dev:"/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/app/" || { echo "SCP app files failed"; exit 1; }

sleep 5

echo "Copying config files..."
scp -o ConnectTimeout=10 -i ~/.ssh/do_imagiax .env.production requirements.txt root@imagiax.dev:"/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/" || { echo "SCP config files failed"; exit 1; }

sleep 5

echo "Rebuilding Docker container..."
ssh -o ConnectTimeout=10 -i ~/.ssh/do_imagiax root@imagiax.dev 'cd "/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax" && docker compose -f docker-compose.prod.yml build --no-cache fastapi && docker compose -f docker-compose.prod.yml up -d fastapi' || { echo "Docker rebuild failed"; exit 1; }

sleep 5

echo "Checking container status..."
ssh -o ConnectTimeout=10 -i ~/.ssh/do_imagiax root@imagiax.dev 'docker ps --filter name=fastapi-imagiax --format "{{.Status}}"' || { echo "Docker ps failed"; exit 1; }

sleep 5

echo "Checking logs..."
ssh -o ConnectTimeout=10 -i ~/.ssh/do_imagiax root@imagiax.dev 'docker logs --tail 30 fastapi-imagiax' || { echo "Docker logs failed"; exit 1; }

echo "Deployment complete."

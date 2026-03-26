#!/bin/bash
set -e

echo "Starting Docker daemon if not running..."
if ! docker info &>/dev/null; then
  dockerd &>/tmp/dockerd.log &
  echo "Waiting for Docker daemon to start..."
  for i in $(seq 1 30); do
    if docker info &>/dev/null; then
      echo "Docker daemon is ready."
      break
    fi
    sleep 1
  done
fi

echo "Stopping any existing BlobeVM container..."
docker stop BlobeVM 2>/dev/null || true
docker rm BlobeVM 2>/dev/null || true

echo "Building BlobeVM Docker image (this may take several minutes on first run)..."
cd BlobeVM-main
docker build -t blobevm .
cd ..

echo "Starting BlobeVM on port 3000..."
docker run --name=BlobeVM \
  -e PUID=1000 \
  -e PGID=1000 \
  --security-opt seccomp=unconfined \
  -e TZ=Etc/UTC \
  -e SUBFOLDER=/ \
  -e TITLE=BlobeVM \
  -p 5000:3000 \
  --shm-size="2gb" \
  blobevm

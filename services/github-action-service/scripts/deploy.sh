#!/bin/bash
set -e

SERVER="root@de"
APP_DIR="/opt/github-action-service"

echo "=== Syncing project files to server ==="
ssh $SERVER "mkdir -p $APP_DIR"
rsync -avz --exclude '.env' --exclude '__pycache__' --exclude '.pytest_cache' \
    /home/xiaowu/work/github-action-service/ $SERVER:$APP_DIR/

echo "=== Setting .env permissions ==="
ssh $SERVER "chmod 600 $APP_DIR/.env 2>/dev/null || true"

echo "=== Building and starting Docker ==="
ssh $SERVER "cd $APP_DIR && docker compose up -d --build"

echo "=== Done ==="

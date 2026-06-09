#!/bin/bash
# Deploy kiosk frontend to Vesper server
# Usage: bash deploy.sh

SERVER="tanmay@10.0.0.120"
KEY="$HOME/.ssh/vesper_key"
DEST="/home/tanmay/vesper/pipelines/kiosk.html"

echo "→ Deploying kiosk.html to $SERVER..."
scp -i "$KEY" "$(dirname "$0")/kiosk.html" "$SERVER:$DEST"
echo "✓ Done. Open https://10.0.0.120:5000/kiosk"

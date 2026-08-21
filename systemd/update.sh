#!/bin/bash
# /opt/knight-scraper/systemd/update.sh
# Pulls latest code from GitHub and restarts services only when something changed.

set -euo pipefail

REPO_DIR="/opt/knight-scraper"
LOG_TAG="knight-autoupdate"

log() { logger -t "$LOG_TAG" "$*"; echo "$*"; }

cd "$REPO_DIR"

# Fetch latest refs without changing working tree
git fetch origin main 2>&1 | logger -t "$LOG_TAG" || {
    log "ERROR: git fetch failed (no internet?)"
    exit 1
}

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    log "Already up to date ($LOCAL). Nothing to do."
    exit 0
fi

log "Update found: $LOCAL -> $REMOTE. Pulling..."

git pull origin main 2>&1 | logger -t "$LOG_TAG"

# Re-install Node deps only if package.json changed
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "package.json"; then
    log "package.json changed — running npm install..."
    npm install --prefix "$REPO_DIR" 2>&1 | logger -t "$LOG_TAG"
fi

# Re-install Python deps only if requirements.txt changed (if you ever add one)
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "requirements.txt"; then
    log "requirements.txt changed — installing Python deps..."
    pip3 install -r "$REPO_DIR/scraper-server/requirements.txt" --break-system-packages 2>&1 | logger -t "$LOG_TAG"
fi

# Copy updated service files if they changed
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "systemd/"; then
    log "Service files changed — reloading systemd..."
    cp "$REPO_DIR/systemd/scraper.service"   /etc/systemd/system/
    cp "$REPO_DIR/systemd/dashboard.service" /etc/systemd/system/
    systemctl daemon-reload
fi

log "Restarting services..."
systemctl restart scraper dashboard

log "Update complete! Running commit: $(git rev-parse --short HEAD)"

#!/bin/bash
# =============================================================================
#  /home/iden/scraper-dashboard-knightnoel/systemd/update.sh
#  Auto-update script â€” called by knight-update.timer every 5 minutes.
#  Checks GitHub for new commits; if found, pulls + rebuilds + restarts.
# =============================================================================

REPO_DIR="/home/iden/scraper-dashboard-knightnoel"
REPO_URL="https://github.com/alanraiden/scraper-dashboard-knightnoel.git"
BRANCH="main"
LOG_TAG="knight-autoupdate"
VENV_DIR="$REPO_DIR/scraper-server/venv"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { logger -t "$LOG_TAG" "$*"; echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die() { log "FATAL: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
[[ -d "$REPO_DIR/.git" ]] || die "Repo not found at $REPO_DIR. Run setup-autoUpdate.sh first."

cd "$REPO_DIR" || die "Cannot cd into $REPO_DIR"

# Git 2.36+ blocks operations on repos owned by a different user (security check).
# This service runs as root but the repo is owned by the service user — add an
# explicit exception so git fetch/pull work correctly.
git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true

# Make sure we are tracking the right remote
CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
if [[ "$CURRENT_REMOTE" != "$REPO_URL" ]]; then
    log "Fixing remote URL to $REPO_URL"
    git remote set-url origin "$REPO_URL"
fi

# ---------------------------------------------------------------------------
# Fetch â€” only network call; fail gracefully (no internet â†’ skip silently)
# ---------------------------------------------------------------------------
if ! git fetch origin "$BRANCH" 2>&1 | logger -t "$LOG_TAG"; then
    log "WARNING: git fetch failed (no internet?). Skipping this cycle."
    exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [[ "$LOCAL" == "$REMOTE" ]]; then
    log "Already up to date ($(git rev-parse --short HEAD)). Nothing to do."
    exit 0
fi

log "Update found: $(git rev-parse --short HEAD) -> $(git rev-parse --short origin/$BRANCH). Pulling..."

# Stash any local modifications so git pull never aborts.
# This handles cases where the server has uncommitted edits (e.g. config tweaks).
STASH_OUTPUT=$(git stash 2>&1)
STASHED=false
if echo "$STASH_OUTPUT" | grep -q "^Saved"; then
    log "Local changes stashed before pull: $STASH_OUTPUT"
    STASHED=true
fi

# Pull — capture output AND preserve exit code (no pipe-to-logger trick here)
PULL_OUTPUT=$(git pull origin "$BRANCH" 2>&1)
PULL_EXIT=$?
echo "$PULL_OUTPUT" | logger -t "$LOG_TAG"
if [[ $PULL_EXIT -ne 0 ]]; then
    log "FATAL: git pull failed (exit $PULL_EXIT). Restoring stash if any."
    $STASHED && git stash pop 2>&1 | logger -t "$LOG_TAG" || true
    exit 1
fi

# Restore any stashed local changes on top of the new code
if $STASHED; then
    POP_OUTPUT=$(git stash pop 2>&1)
    echo "$POP_OUTPUT" | logger -t "$LOG_TAG"
    log "Stash restored after pull."
fi

# ---------------------------------------------------------------------------
# Re-install Node deps only when package.json changed
# ---------------------------------------------------------------------------
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "^package\.json$"; then
    log "package.json changed â€” running npm install..."
    npm install --prefix "$REPO_DIR" 2>&1 | logger -t "$LOG_TAG" \
        || log "WARNING: npm install reported errors (check logs)"
fi

# ---------------------------------------------------------------------------
# Re-install Python deps if requirements.txt changed
# ---------------------------------------------------------------------------
REQ="$REPO_DIR/scraper-server/requirements.txt"
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "requirements\.txt"; then
    log "requirements.txt changed â€” installing Python deps..."
    if [[ -d "$VENV_DIR" ]]; then
        "$VENV_DIR/bin/pip" install -r "$REQ" 2>&1 | logger -t "$LOG_TAG" \
            || log "WARNING: pip install (venv) reported errors"
    else
        pip3 install -r "$REQ" --break-system-packages 2>&1 | logger -t "$LOG_TAG" \
            || log "WARNING: pip3 install reported errors"
    fi
fi

# ---------------------------------------------------------------------------
# Reload systemd if service files changed
# ---------------------------------------------------------------------------
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "^systemd/"; then
    log "Systemd files changed â€” reloading daemon..."
    cp "$REPO_DIR/systemd/scraper.service"          /etc/systemd/system/
    cp "$REPO_DIR/systemd/dashboard.service"        /etc/systemd/system/
    cp "$REPO_DIR/systemd/knight-update.service"    /etc/systemd/system/
    cp "$REPO_DIR/systemd/knight-update.timer"      /etc/systemd/system/
    chmod +x "$REPO_DIR/systemd/update.sh"
    systemctl daemon-reload
fi

# ---------------------------------------------------------------------------
# Restart services
# ---------------------------------------------------------------------------
log "Restarting scraper and dashboard services..."
systemctl restart scraper dashboard \
    && log "Update complete! Now running commit: $(git rev-parse --short HEAD)" \
    || log "WARNING: systemctl restart returned an error â€” check 'journalctl -u scraper -u dashboard'"

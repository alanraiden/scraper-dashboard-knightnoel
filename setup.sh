#!/bin/bash
# =============================================================================
#  setup.sh â€” Knight Novel Scraper + Dashboard â€” Full Server Bootstrap
#
#  Run ONCE on a fresh Ubuntu server (all steps from UBUNTU_SETUP.md automated):
#
#    sudo bash setup.sh
#
#  What this script does:
#    [1/8] Install system dependencies (python3, nodejs, npm, git, ufw, curl)
#    [2/8] Clone the GitHub repository
#    [3/8] Create Python venv and install Python packages
#    [4/8] Install Node.js dependencies  (npm install)
#    [5/8] Open firewall ports 5174 and 7832
#    [6/8] Install + start systemd services (scraper + dashboard)
#    [7/8] Enable the auto-update timer (every 5 min, no SSH needed after this)
#    [8/8] Print status summary
#
#  After this script finishes:
#    - Dashboard runs at  http://<server-ip>:5174
#    - Scraper API runs at http://<server-ip>:7832
#    - Every push to GitHub -> server pulls + rebuilds + restarts within 5 min
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# * CONFIG â€” edit these if your setup differs
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/alanraiden/scraper-dashboard-knightnoel.git"
REPO_DIR="/home/iden/scraper-dashboard-knightnoel"   # where the repo will live
SERVICE_USER="iden"                                   # Linux user that owns everything
BRANCH="main"
DASHBOARD_PORT=5174
SCRAPER_PORT=7832
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'  # No Colour

ok()   { echo -e "${GREEN}  OK  $*${NC}"; }
info() { echo -e "${CYAN}  ->  $*${NC}"; }
warn() { echo -e "${YELLOW}  !! $*${NC}"; }
fail() { echo -e "${RED}  XX  $*${NC}"; exit 1; }
step() { echo -e "\n${BOLD}${CYAN}$*${NC}"; }

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}================================================================${NC}"
echo -e "${CYAN}     Knight Novel Scraper - Full Server Bootstrap${NC}"
echo -e "${CYAN}  Repo : $REPO_URL${NC}"
echo -e "${CYAN}  Dir  : $REPO_DIR${NC}"
echo -e "${CYAN}  User : $SERVICE_USER  |  Branch: $BRANCH${NC}"
echo -e "${CYAN}================================================================${NC}"
echo ""

# Must run as root
[[ "$EUID" -eq 0 ]] || fail "Please run as root:  sudo bash setup.sh"

# ---------------------------------------------------------------------------
# Verify the SERVICE_USER exists; create if missing
# ---------------------------------------------------------------------------
if ! id "$SERVICE_USER" &>/dev/null; then
    warn "User '$SERVICE_USER' does not exist â€” creating it now..."
    useradd -m -s /bin/bash "$SERVICE_USER"
    ok "User '$SERVICE_USER' created."
fi

# ===========================================================================
# [1/8] System dependencies
# ===========================================================================
step "[1/8] Installing system dependencies..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv nodejs npm git curl ufw
ok "System packages installed: python3, nodejs, npm, git, curl, ufw"

# ===========================================================================
# [2/8] Clone (or refresh) the repository
# ===========================================================================
step "[2/8] Setting up repository at $REPO_DIR..."

PARENT_DIR=$(dirname "$REPO_DIR")
mkdir -p "$PARENT_DIR"
chown "$SERVICE_USER":"$SERVICE_USER" "$PARENT_DIR" 2>/dev/null || true

if [[ -d "$REPO_DIR/.git" ]]; then
    warn "Repo already exists at $REPO_DIR â€” resetting to origin/$BRANCH..."
    sudo -u "$SERVICE_USER" git -C "$REPO_DIR" fetch origin "$BRANCH"
    sudo -u "$SERVICE_USER" git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
else
    info "Cloning $REPO_URL -> $REPO_DIR"
    sudo -u "$SERVICE_USER" git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$REPO_DIR"
ok "Repository ready at $REPO_DIR"

# ===========================================================================
# [3/8] Python virtual environment + packages
# ===========================================================================
step "[3/8] Creating Python virtual environment and installing packages..."
VENV_DIR="$REPO_DIR/scraper-server/venv"

sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip -q

REQ_FILE="$REPO_DIR/scraper-server/requirements.txt"
if [[ -f "$REQ_FILE" ]]; then
    info "Installing from scraper-server/requirements.txt"
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -r "$REQ_FILE" -q
else
    info "No requirements.txt found â€” installing default packages"
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install \
        flask flask-cors requests beautifulsoup4 lxml -q
fi
ok "Python venv ready at $VENV_DIR"

# ---------------------------------------------------------------------------
# Patch scraper.service ExecStart to use venv python + correct paths
# ---------------------------------------------------------------------------
SCRAPER_SVC_SRC="$REPO_DIR/systemd/scraper.service"
if [[ -f "$SCRAPER_SVC_SRC" ]]; then
    info "Patching scraper.service ExecStart -> venv python"
    sed -i \
        "s|ExecStart=.*scraper_server\.py|ExecStart=$VENV_DIR/bin/python3 $REPO_DIR/scraper-server/scraper_server.py|" \
        "$SCRAPER_SVC_SRC"
fi

# Update User=, WorkingDirectory= in all service files so they reflect current config
for svc in scraper.service dashboard.service; do
    SVC_PATH="$REPO_DIR/systemd/$svc"
    [[ -f "$SVC_PATH" ]] || continue
    sed -i "s|^User=.*|User=$SERVICE_USER|"                 "$SVC_PATH"
    sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" "$SVC_PATH"
done

# Fix WorkingDirectory for scraper.service specifically (needs scraper-server subdir)
if [[ -f "$REPO_DIR/systemd/scraper.service" ]]; then
    sed -i "s|WorkingDirectory=$REPO_DIR$|WorkingDirectory=$REPO_DIR/scraper-server|" \
        "$REPO_DIR/systemd/scraper.service"
fi

# Fix knight-update.service ExecStart path
KU_SVC="$REPO_DIR/systemd/knight-update.service"
if [[ -f "$KU_SVC" ]]; then
    sed -i "s|^ExecStart=.*update\.sh|ExecStart=/bin/bash $REPO_DIR/systemd/update.sh|" "$KU_SVC"
fi

# Fix update.sh REPO_DIR variable
UPDATE_SH="$REPO_DIR/systemd/update.sh"
if [[ -f "$UPDATE_SH" ]]; then
    sed -i "s|^REPO_DIR=.*|REPO_DIR=\"$REPO_DIR\"|" "$UPDATE_SH"
    chmod +x "$UPDATE_SH"
fi

ok "Service file paths patched."

# ===========================================================================
# [4/8] Node.js dependencies
# ===========================================================================
step "[4/8] Installing Node.js dependencies (npm install)..."
sudo -u "$SERVICE_USER" npm install --prefix "$REPO_DIR"
ok "npm install complete"

# ===========================================================================
# [5/8] Firewall ports
# ===========================================================================
step "[5/8] Opening firewall ports $DASHBOARD_PORT (dashboard) and $SCRAPER_PORT (scraper)..."
if ufw status | grep -q "Status: active"; then
    ufw allow "$DASHBOARD_PORT/tcp" comment "Knight Dashboard Vite"
    ufw allow "$SCRAPER_PORT/tcp"   comment "Knight Scraper API"
    ok "UFW rules added for ports $DASHBOARD_PORT and $SCRAPER_PORT"
else
    warn "UFW is inactive â€” all ports are open. Skipping ufw rules."
    info "Verify ports are listening later:  ss -tlnp | grep -E '$DASHBOARD_PORT|$SCRAPER_PORT'"
fi

# ===========================================================================
# [6/8] Install systemd service units and start services
# ===========================================================================
step "[6/8] Installing systemd service units..."
SYSTEMD_SRC="$REPO_DIR/systemd"

cp "$SYSTEMD_SRC/scraper.service"         /etc/systemd/system/
cp "$SYSTEMD_SRC/dashboard.service"       /etc/systemd/system/
cp "$SYSTEMD_SRC/knight-update.service"   /etc/systemd/system/
cp "$SYSTEMD_SRC/knight-update.timer"     /etc/systemd/system/

systemctl daemon-reload
systemctl enable  scraper dashboard
systemctl restart scraper dashboard

ok "scraper.service   â€” enabled + started"
ok "dashboard.service â€” enabled + started"

# ===========================================================================
# [7/8] Enable the auto-update timer (pull from GitHub every 5 min)
# ===========================================================================
step "[7/8] Enabling auto-update timer (every 5 minutes)..."
systemctl enable --now knight-update.timer
ok "knight-update.timer is active â€” server will auto-update from GitHub"

# ===========================================================================
# [8/8] Status summary
# ===========================================================================
step "[8/8] Verifying services..."
sleep 3  # give services a moment to fully start

SERVER_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}================================================================${NC}"
echo -e "${GREEN}                  Setup complete!${NC}"
echo -e "${GREEN}================================================================${NC}"
echo ""
echo -e "  ${CYAN}${BOLD}URLs${NC}"
echo -e "    Dashboard   : ${YELLOW}http://$SERVER_IP:$DASHBOARD_PORT${NC}"
echo -e "    Scraper API : ${YELLOW}http://$SERVER_IP:$SCRAPER_PORT${NC}"
echo ""
echo -e "  ${CYAN}${BOLD}Service Status${NC}"
systemctl is-active --quiet scraper \
    && echo -e "    scraper    ${GREEN}[running]${NC}" \
    || echo -e "    scraper    ${RED}[stopped]  <- check: journalctl -u scraper -n 30${NC}"
systemctl is-active --quiet dashboard \
    && echo -e "    dashboard  ${GREEN}[running]${NC}" \
    || echo -e "    dashboard  ${RED}[stopped]  <- check: journalctl -u dashboard -n 30${NC}"
echo ""
echo -e "  ${CYAN}${BOLD}Auto-Update Timer${NC}"
systemctl list-timers knight-update.timer --no-pager 2>/dev/null | tail -n 2
echo ""
echo -e "  ${YELLOW}Push a commit to GitHub -> server auto-updates within 5 min${NC}"
echo ""
echo -e "  Useful commands:"
echo -e "    Force immediate update : sudo systemctl start knight-update.service"
echo -e "    Watch update log live  : sudo journalctl -u knight-update -f"
echo -e "    Watch scraper log      : sudo journalctl -u scraper -f"
echo -e "    Watch dashboard log    : sudo journalctl -u dashboard -f"
echo -e "    Restart services       : sudo systemctl restart scraper dashboard"
echo -e "    Stop everything        : sudo systemctl stop scraper dashboard"
echo ""

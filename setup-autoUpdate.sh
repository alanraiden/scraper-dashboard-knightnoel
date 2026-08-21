#!/bin/bash
# =============================================================================
#  setup-autoUpdate.sh
#  One-shot bootstrap for the Knight Novel Scraper + Dashboard on Ubuntu.
#
#  Run once as root (or with sudo) on a fresh Ubuntu server:
#    sudo bash setup-autoUpdate.sh
#
#  What it does (mirrors every step in UBUNTU_SETUP.md):
#    1. Install system dependencies
#    2. Clone repo from GitHub
#    3. Create Python venv + install Python deps
#    4. Install Node deps (npm install)
#    5. Open firewall ports 5174 & 7832
#    6. Install + enable scraper, dashboard, knight-update systemd units
#    7. Enable + start the auto-update timer
#
#  After this script finishes, every time you push to GitHub the server
#  will automatically pull + rebuild + restart within 5 minutes â€” no SSH needed.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config â€” edit these if your setup differs
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/alanraiden/scraper-dashboard-knightnoel.git"
REPO_DIR="/home/iden/scraper-dashboard-knightnoel"
SERVICE_USER="iden"           # The Linux user that will own the repo & run services
BRANCH="main"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  âœ” $*${NC}"; }
info() { echo -e "${CYAN}  â†’ $*${NC}"; }
warn() { echo -e "${YELLOW}  âš  $*${NC}"; }
fail() { echo -e "${RED}  âœ˜ $*${NC}"; exit 1; }

echo ""
echo -e "${CYAN}â•”â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•—${NC}"
echo -e "${CYAN}â•‘  Knight Novel Scraper â€” Auto-Update Bootstrap Installer  â•‘${NC}"
echo -e "${CYAN}â•šâ•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•${NC}"
echo ""

# Must run as root
[[ "$EUID" -eq 0 ]] || fail "Please run as root: sudo bash setup-autoUpdate.sh"

# ---------------------------------------------------------------------------
# 1. System dependencies
# ---------------------------------------------------------------------------
echo -e "\n${CYAN}[1/7] Installing system dependencies...${NC}"
apt update -qq
apt install -y python3 python3-pip python3-venv nodejs npm git curl ufw
ok "System packages installed."

# ---------------------------------------------------------------------------
# 2. Clone (or update) the repository
# ---------------------------------------------------------------------------
echo -e "\n${CYAN}[2/7] Cloning repository...${NC}"

# Ensure the parent dir exists and is owned by SERVICE_USER
PARENT_DIR=$(dirname "$REPO_DIR")
mkdir -p "$PARENT_DIR"
chown "$SERVICE_USER":"$SERVICE_USER" "$PARENT_DIR" 2>/dev/null || true

if [[ -d "$REPO_DIR/.git" ]]; then
    warn "Repo already exists at $REPO_DIR â€” pulling latest instead of cloning."
    sudo -u "$SERVICE_USER" git -C "$REPO_DIR" pull origin "$BRANCH"
else
    sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$REPO_DIR"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$REPO_DIR"
ok "Repository ready at $REPO_DIR"

# ---------------------------------------------------------------------------
# 3. Python virtual environment + deps
# ---------------------------------------------------------------------------
echo -e "\n${CYAN}[3/7] Setting up Python virtual environment...${NC}"
VENV_DIR="$REPO_DIR/scraper-server/venv"

sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install \
    flask flask-cors requests beautifulsoup4 lxml -q
ok "Python venv created at $VENV_DIR and deps installed."

# Patch scraper.service ExecStart to use the venv python
info "Patching scraper.service to use venv Python..."
sed -i "s|ExecStart=.*scraper_server\.py|ExecStart=$VENV_DIR/bin/python3 $REPO_DIR/scraper-server/scraper_server.py|" \
    "$REPO_DIR/systemd/scraper.service"
ok "scraper.service patched."

# ---------------------------------------------------------------------------
# 4. Node dependencies
# ---------------------------------------------------------------------------
echo -e "\n${CYAN}[4/7] Installing Node.js dependencies...${NC}"
sudo -u "$SERVICE_USER" npm install --prefix "$REPO_DIR"
ok "npm install complete."

# ---------------------------------------------------------------------------
# 5. Firewall ports
# ---------------------------------------------------------------------------
echo -e "\n${CYAN}[5/7] Opening firewall ports...${NC}"
if ufw status | grep -q "Status: active"; then
    ufw allow 5174/tcp   # Vite dashboard
    ufw allow 7832/tcp   # Python scraper API
    ok "UFW rules added for ports 5174 and 7832."
else
    warn "UFW is inactive â€” ports are already open system-wide. Skipping."
fi

# ---------------------------------------------------------------------------
# 6. Install systemd service units
# ---------------------------------------------------------------------------
echo -e "\n${CYAN}[6/7] Installing systemd service units...${NC}"
SYSTEMD_SRC="$REPO_DIR/systemd"

cp "$SYSTEMD_SRC/scraper.service"           /etc/systemd/system/
cp "$SYSTEMD_SRC/dashboard.service"         /etc/systemd/system/
cp "$SYSTEMD_SRC/knight-update.service"     /etc/systemd/system/
cp "$SYSTEMD_SRC/knight-update.timer"       /etc/systemd/system/
chmod +x "$SYSTEMD_SRC/update.sh"

systemctl daemon-reload
systemctl enable scraper dashboard
systemctl start  scraper dashboard
ok "scraper.service and dashboard.service enabled + started."

# ---------------------------------------------------------------------------
# 7. Enable the auto-update timer
# ---------------------------------------------------------------------------
echo -e "\n${CYAN}[7/7] Enabling auto-update timer (every 5 minutes)...${NC}"
systemctl enable --now knight-update.timer
ok "knight-update.timer is active."

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}â•”â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•—${NC}"
echo -e "${GREEN}â•‘              âœ”  Setup complete!                          â•‘${NC}"
echo -e "${GREEN}â•šâ•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•${NC}"
echo ""
echo -e "  ${CYAN}Dashboard:${NC}  http://$(hostname -I | awk '{print $1}'):5174"
echo -e "  ${CYAN}Scraper API:${NC} http://$(hostname -I | awk '{print $1}'):7832"
echo ""
echo -e "  ${CYAN}Service status:${NC}"
systemctl is-active --quiet scraper  && echo -e "    scraper   ${GREEN}â—  running${NC}" \
                                     || echo -e "    scraper   ${RED}â—  stopped${NC}"
systemctl is-active --quiet dashboard && echo -e "    dashboard ${GREEN}â—  running${NC}" \
                                      || echo -e "    dashboard ${RED}â—  stopped${NC}"

echo ""
echo -e "  ${CYAN}Auto-update timer:${NC}"
systemctl list-timers knight-update.timer --no-pager 2>/dev/null | tail -n 2
echo ""
echo -e "  ${YELLOW}Push a commit to GitHub â†’ server updates automatically within 5 min.${NC}"
echo -e "  ${YELLOW}Force an immediate update: sudo systemctl start knight-update.service${NC}"
echo -e "  ${YELLOW}Watch live update log:    sudo journalctl -u knight-update -f${NC}"
echo ""

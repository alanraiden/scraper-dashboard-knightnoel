# Ubuntu Server Setup Guide

## What you'll have when done

```
Ubuntu Home Server (e.g. 192.168.1.3)
  ├── /opt/knight-scraper/          ← project files
  ├── scraper.service (systemd)     ← Python server, auto-starts on boot
  └── dashboard.service (systemd)   ← Vite dashboard, auto-starts on boot

Your Browser (any device on same WiFi)
  └── http://192.168.1.3:5174       ← dashboard
```

The Python server runs 24/7 and automatically checks for new chapters based on
the intervals you configure — **no browser needs to be open**.

---

## 1. Install dependencies on Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv nodejs npm git curl
```

### Python deps — pick one method:

**Method A — system pip (simple):**
```bash
pip3 install flask flask-cors requests beautifulsoup4 lxml
# If Ubuntu 23.04+ gives "externally-managed-environment" error, add:
# pip3 install flask flask-cors requests beautifulsoup4 lxml --break-system-packages
```

**Method B — virtual environment (recommended / cleaner):**
```bash
cd /opt/knight-scraper/scraper-server
python3 -m venv venv
source venv/bin/activate
pip install flask flask-cors requests beautifulsoup4 lxml
```

> ⚠️ If using venv, update `systemd/scraper.service` `ExecStart` to:
> `/opt/knight-scraper/scraper-server/venv/bin/python3 /opt/knight-scraper/scraper-server/scraper_server.py`

---

## 2. Copy the project to the server

**Option A — from your PC via SCP:**
```bash
# Run this from your Windows PC (PowerShell)
scp -r "C:\Users\ALAN\Downloads\scraper-dashboard-knightnovel" ubuntu@192.168.1.3:/opt/knight-scraper
```

**Option B — via Git (recommended for updates):**
```bash
# On the Ubuntu server
sudo mkdir -p /opt/knight-scraper
sudo chown ubuntu:ubuntu /opt/knight-scraper
cd /opt
git clone <your-repo-url> knight-scraper
```

---

## 3. Install Node dependencies

```bash
cd /opt/knight-scraper
npm install
```

---

## 4. Quick test (before setting up systemd)

Open **two terminals** on the Ubuntu server and test manually first:

```bash
# Terminal 1 — Python server
cd /opt/knight-scraper/scraper-server
python3 scraper_server.py
# You should see: "Knight Novel Scraper Server v4 (LAN mode)"

# Terminal 2 — Vite dashboard
cd /opt/knight-scraper
npm run dev
# You should see: "Local: http://localhost:5174"
```

Open `http://192.168.1.3:5174` in your browser — if it loads, proceed to step 5.

---

## 5. Open firewall ports (required for LAN access)

If you get a timeout when accessing from another device, Ubuntu's firewall is blocking the ports:

```bash
sudo ufw allow 5174/tcp   # Vite dashboard
sudo ufw allow 7832/tcp   # Python scraper API
sudo ufw status           # verify
```

If `ufw` shows **Status: inactive**, your firewall is off and ports are already open.
Check if the ports are actually listening:
```bash
ss -tlnp | grep -E '5174|7832'
```

---

## 5. Install as systemd services (auto-start on boot)

```bash
# Copy service files
sudo cp /opt/knight-scraper/systemd/scraper.service  /etc/systemd/system/
sudo cp /opt/knight-scraper/systemd/dashboard.service /etc/systemd/system/

# Reload systemd and enable both services
sudo systemctl daemon-reload
sudo systemctl enable scraper dashboard
sudo systemctl start  scraper dashboard

# Verify they're running
sudo systemctl status scraper
sudo systemctl status dashboard
```

---

## 6. Check logs

```bash
# Python scraper server logs (includes scheduled watch activity)
sudo journalctl -u scraper -f

# Vite dashboard logs
sudo journalctl -u dashboard -f
```

---

## 7. Configure the dashboard

1. Open `http://192.168.1.3:5174` in your browser
2. Click **Connect** (top-right) and enter:
   - **API URL**: `https://your-vercel-app.vercel.app` (your KN site URL)
   - **Scraper Key**: your `SCRAPER_API_KEY` from the KN `.env`
3. Your novels will load automatically

---

## 8. Set up a novel watcher

1. Find a novel in the dashboard
2. Click **Watch** → fill in the scrape URL and interval
3. Click **Start** to enable auto-scheduling
4. The watch config is saved to `/opt/knight-scraper/scraper-server/watches.json`
5. Even if you close the browser, the Python server will check on schedule

---

## 9. Managing the services

```bash
# Restart after code updates
sudo systemctl restart scraper dashboard

# Stop everything
sudo systemctl stop scraper dashboard

# Check if running
sudo systemctl is-active scraper dashboard

# See last 50 lines of scraper log
sudo journalctl -u scraper -n 50
```

---

## 10. Updating the code

```bash
# If you used Git
cd /opt/knight-scraper
git pull
npm install                    # if package.json changed
sudo systemctl restart scraper dashboard
```

If you used SCP, re-copy the changed files and restart the services.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Dashboard not loading from LAN | Check `ufw`: `sudo ufw allow 5174/tcp && sudo ufw allow 7832/tcp` |
| Python server won't start | Check logs: `journalctl -u scraper -n 30` |
| "Module not found" errors | Re-run `pip3 install flask flask-cors requests beautifulsoup4 lxml` |
| Watches not auto-running | Check `watches.json` has `"active": true` and `intervalHours` is set |
| Can't reach Vercel API | Your server needs internet access — check with `curl https://google.com` |

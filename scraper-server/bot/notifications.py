"""
bot/notifications.py — Discord webhook notifications.

Sends structured embed messages for:
  - Server down / restart alerts
  - Stale watch alerts
  - Auto-recovery actions
  - Daily digest reports
"""

import time
import json
import requests
from datetime import datetime

from bot.config import get_config
from bot import logger


def _get_webhook_url():
    cfg = get_config()
    return cfg.get("notifications", {}).get("discord_webhook_url", "").strip()


def is_configured():
    """Check if Discord notifications are configured."""
    return bool(_get_webhook_url())


def _send(payload):
    """Send a payload to the Discord webhook."""
    url = _get_webhook_url()
    if not url:
        return False

    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 204:
            return True
        elif r.status_code == 429:
            # Rate limited — wait and retry once
            retry_after = r.json().get("retry_after", 5)
            logger.warn(f"Discord rate limited — waiting {retry_after}s")
            time.sleep(retry_after)
            r = requests.post(url, json=payload, timeout=15)
            return r.status_code == 204
        else:
            logger.error(f"Discord webhook failed: HTTP {r.status_code}")
            return False
    except Exception as e:
        logger.error(f"Discord webhook error: {e}")
        return False


def _embed(title, description, color, fields=None, footer=None):
    """Build a Discord embed payload."""
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    if fields:
        embed["fields"] = fields
    if footer:
        embed["footer"] = {"text": footer}
    return {"embeds": [embed]}


# ── Color constants (Discord uses decimal, not hex) ──────────────────────────
COLOR_RED    = 0xFF4444   # errors, server down
COLOR_ORANGE = 0xFF8800   # warnings, stale watches
COLOR_GREEN  = 0x44FF44   # success, recovery
COLOR_BLUE   = 0x4488FF   # info, daily digest
COLOR_PURPLE = 0x8844FF   # power cut recovery


# ── Notification functions ───────────────────────────────────────────────────

def notify_server_down(service, error, consecutive_failures):
    """Alert that the scraper server or KN API is down."""
    if not is_configured():
        return

    severity = "🔴 CRITICAL" if consecutive_failures >= 3 else "⚠️ Warning"
    _send(_embed(
        title=f"{severity}: {service} is DOWN",
        description=f"**Error:** {error}\n**Consecutive failures:** {consecutive_failures}",
        color=COLOR_RED,
        footer="Knight Novel Scraper Bot",
    ))


def notify_server_recovered(service):
    """Alert that a previously-down service is back up."""
    if not is_configured():
        return

    _send(_embed(
        title=f"✅ {service} is back online",
        description="Service has recovered and is responding normally.",
        color=COLOR_GREEN,
        footer="Knight Novel Scraper Bot",
    ))


def notify_power_cut_detected(stale_count):
    """Alert that a server restart (possible power cut) was detected."""
    if not is_configured():
        return

    _send(_embed(
        title="🔌 Server Restart Detected",
        description=(
            f"The scraper server appears to have restarted (possible power cut).\n"
            f"**{stale_count}** watches are stale and need recovery.\n\n"
            f"Starting staggered recovery..."
        ),
        color=COLOR_PURPLE,
        footer="Knight Novel Scraper Bot",
    ))


def notify_power_recovery_complete(summary):
    """Report the result of post-crash recovery."""
    if not is_configured():
        return

    _send(_embed(
        title="🔧 Power Recovery Complete",
        description=(
            f"**{summary.get('recovered', 0)}** watches re-triggered\n"
            f"**{summary.get('failed', 0)}** failed\n"
            f"**{summary.get('skipped', 0)}** skipped (blocked/unreachable)"
        ),
        color=COLOR_GREEN if summary.get("failed", 0) == 0 else COLOR_ORANGE,
        footer="Knight Novel Scraper Bot",
    ))


def notify_stale_watches(diagnosed_list):
    """Send a summary of stale watches with diagnoses."""
    if not is_configured() or not diagnosed_list:
        return

    diag_icons = {
        "overdue":        "🕐",
        "chain_url_dead": "🔗",
        "site_blocked":   "🚫",
        "site_down":      "💀",
        "kn_drift":       "📊",
        "unknown":        "❓",
    }

    lines = []
    for entry in diagnosed_list[:15]:  # Discord embed has a ~4096 char limit
        diag  = entry.get("diagnosis", "unknown")
        icon  = diag_icons.get(diag, "❓")
        title = entry.get("novel_title", "?")
        ch    = entry.get("last_chapter", "?")
        hrs   = entry.get("hours_overdue", "?")
        lines.append(f"{icon} **{title}** — Ch.{ch}, {hrs}h overdue")

    if len(diagnosed_list) > 15:
        lines.append(f"\n*...and {len(diagnosed_list) - 15} more*")

    _send(_embed(
        title=f"⚠ {len(diagnosed_list)} Stale Watcher(s)",
        description="\n".join(lines),
        color=COLOR_ORANGE,
        footer="Knight Novel Scraper Bot",
    ))


def notify_auto_recovery(novel_title, action, success, message=""):
    """Report an auto-recovery action."""
    if not is_configured():
        return

    action_labels = {
        "retry":         "🔄 Re-triggered watch",
        "chain_repair":  "🔗 Repaired chain URL + re-triggered",
        "drift_fix":     "📊 Fixed KN drift + re-triggered",
        "retry_unknown": "🔄 Re-triggered (unknown diagnosis)",
    }

    label = action_labels.get(action, f"🔧 {action}")

    _send(_embed(
        title=f"{'✅' if success else '❌'} Auto-Recovery: {novel_title}",
        description=f"**Action:** {label}\n**Result:** {'Success' if success else 'Failed'}\n{message}",
        color=COLOR_GREEN if success else COLOR_RED,
        footer="Knight Novel Scraper Bot",
    ))


def notify_daily_digest(report_text):
    """Send the daily digest report."""
    if not is_configured():
        return

    _send(_embed(
        title=f"📊 Daily Scraper Report — {datetime.now().strftime('%b %d, %Y')}",
        description=report_text,
        color=COLOR_BLUE,
        footer="Knight Novel Scraper Bot",
    ))


def notify_stuck_job(job_id, novel_slug, running_minutes):
    """Alert about a job that has been running too long."""
    if not is_configured():
        return

    _send(_embed(
        title="⏰ Stuck Job Detected",
        description=(
            f"**Job:** `{job_id}`\n"
            f"**Novel:** {novel_slug}\n"
            f"**Running for:** {running_minutes:.0f} minutes\n\n"
            f"This job may be stuck. Consider cancelling it."
        ),
        color=COLOR_ORANGE,
        footer="Knight Novel Scraper Bot",
    ))

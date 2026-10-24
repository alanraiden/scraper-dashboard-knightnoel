"""
bot/reporter.py — Daily summary report generation.

Aggregates stats from bot_state, watches, and KN API into a formatted report
suitable for Discord and logging.
"""

import time
from datetime import datetime

from bot.config import get_config
from bot import logger, health, state
from bot.staleness import find_stale_watches


def generate_daily_report():
    """
    Generate a daily summary report string.
    Pulls data from watches, daily stats, and KN API.
    """
    cfg = get_config()
    watches = health.get_watches()
    daily = state.get_daily_stats()

    # Count watch statuses
    total_watches   = 0
    active_watches  = 0
    stale_watches   = 0
    inactive_watches = 0

    if watches:
        total_watches = len(watches)
        for w in watches:
            if w.get("active"):
                active_watches += 1
            else:
                inactive_watches += 1

        stale = find_stale_watches(watches)
        stale_watches = len(stale)

    up_to_date = active_watches - stale_watches

    # KN API stats
    kn_status = health.check_kn_api()
    kn_novels = kn_status.get("total_novels", "?") if kn_status.get("alive") else "unavailable"

    # Scraper server stats
    scraper_status = health.check_scraper_server()
    server_alive = scraper_status.get("alive", False)

    # Daily action stats
    retries      = daily.get("retries_triggered", 0)
    chain_repairs = daily.get("chain_repairs", 0)
    drift_fixes  = daily.get("drift_fixes", 0)
    power_recs   = daily.get("power_recoveries", 0)
    chapters     = daily.get("chapters_uploaded", 0)
    stuck_jobs   = daily.get("stuck_jobs_detected", 0)

    # Build report
    lines = []
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Watch overview
    if up_to_date > 0:
        lines.append(f"✅ **{up_to_date}** novels up-to-date")
    if stale_watches > 0:
        lines.append(f"⚠️ **{stale_watches}** novels stale")
    if inactive_watches > 0:
        lines.append(f"⏸ **{inactive_watches}** watches paused/inactive")

    lines.append("")

    # Server health
    lines.append(f"🖥️ Scraper server: {'🟢 online' if server_alive else '🔴 DOWN'}")
    lines.append(f"📚 KN API: {kn_novels} novels in database")

    # Actions taken
    if retries or chain_repairs or drift_fixes or power_recs or stuck_jobs:
        lines.append("")
        lines.append("**Bot actions today:**")
        if retries:
            lines.append(f"  🔄 {retries} auto-retries triggered")
        if chain_repairs:
            lines.append(f"  🔗 {chain_repairs} chain URLs repaired")
        if drift_fixes:
            lines.append(f"  📊 {drift_fixes} chapter drift fixes")
        if power_recs:
            lines.append(f"  🔌 {power_recs} power-cut recoveries")
        if stuck_jobs:
            lines.append(f"  ⏰ {stuck_jobs} stuck jobs detected")

    # Stale watch details (top 5)
    if stale_watches > 0 and watches:
        stale_list = find_stale_watches(watches)
        lines.append("")
        lines.append("**Stale watches (top 5):**")
        for entry in stale_list[:5]:
            title = entry.get("novel_title", "?")
            ch    = entry.get("last_chapter", "?")
            hrs   = entry.get("hours_overdue", "?")
            lines.append(f"  • {title} — Ch.{ch}, {hrs}h overdue")
        if stale_watches > 5:
            lines.append(f"  *...and {stale_watches - 5} more*")

    lines.append("")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


def should_send_digest():
    """Check if it's time to send the daily digest."""
    cfg = get_config()
    digest_hour = cfg.get("notifications", {}).get("daily_digest_hour", 9)
    now = datetime.now()

    # Only send if we're in the digest hour and haven't sent today
    if now.hour != digest_hour:
        return False

    last_date = state.get_last_digest_date()
    today = now.strftime("%Y-%m-%d")

    return last_date != today


def mark_digest_sent():
    """Record that today's digest has been sent."""
    state.set_last_digest_date(datetime.now().strftime("%Y-%m-%d"))

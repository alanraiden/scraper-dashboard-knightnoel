"""
bot/staleness.py — Detect stale watches and diagnose why they stopped updating.

A watch is "stale" when lastChecked is older than (threshold_multiplier × intervalHours).
For each stale watch, we run diagnostics to determine the likely root cause.
"""

import time
import requests

from bot.config import get_config
from bot import logger, health


# Diagnosis result codes
DIAG_OVERDUE         = "overdue"           # Just needs to be triggered
DIAG_CHAIN_URL_DEAD  = "chain_url_dead"    # lastChapterUrl returns 404
DIAG_SITE_BLOCKED    = "site_blocked"      # Source site returning 403
DIAG_SITE_DOWN       = "site_down"         # Source site unreachable
DIAG_KN_DRIFT        = "kn_drift"          # KN chapter count doesn't match watch
DIAG_WATCH_INACTIVE  = "watch_inactive"    # Watch is not active
DIAG_UNKNOWN         = "unknown"           # Could not determine cause


def find_stale_watches(watches=None):
    """
    Identify all stale watches.

    Args:
        watches: List of watch dicts from GET /watches (if None, will fetch).

    Returns:
        List of dicts: [{ watch, novel_id, staleness_hours, ... }, ...]
    """
    cfg = get_config()
    threshold = cfg.get("staleness_threshold_multiplier", 2.0)

    if watches is None:
        watches = health.get_watches()
    if not watches:
        return []

    now = time.time()
    stale = []

    for w in watches:
        if not w.get("active"):
            continue

        novel_id      = w.get("novelId", "")
        interval_hrs  = float(w.get("intervalHours", 6))
        last_checked  = w.get("lastChecked", 0)

        if last_checked == 0:
            # Never checked — definitely stale
            hours_overdue = float("inf")
        else:
            age_hours     = (now - last_checked) / 3600
            threshold_hrs = interval_hrs * threshold
            if age_hours < threshold_hrs:
                continue  # Not stale
            hours_overdue = age_hours

        stale.append({
            "watch":         w,
            "novel_id":      novel_id,
            "novel_title":   w.get("novelTitle", novel_id),
            "novel_slug":    w.get("novelSlug", ""),
            "hours_overdue": round(hours_overdue, 1),
            "last_chapter":  w.get("lastChapter", 0),
            "interval_hrs":  interval_hrs,
            "mode":          w.get("mode", "chain"),
        })

    # Sort by most overdue first
    stale.sort(key=lambda x: x["hours_overdue"], reverse=True)
    return stale


def diagnose_stale_watch(stale_entry):
    """
    Run diagnostics on a single stale watch to determine why it's not updating.

    Args:
        stale_entry: Dict from find_stale_watches()

    Returns:
        Updated stale_entry with 'diagnosis' and 'details' keys.
    """
    w         = stale_entry["watch"]
    novel_id  = stale_entry["novel_id"]
    slug      = stale_entry["novel_slug"]
    mode      = stale_entry["mode"]

    diagnosis = DIAG_OVERDUE
    details   = {}

    # 1. Check if chain URL is dead (chain mode only)
    if mode == "chain":
        chain_url = w.get("lastChapterUrl", "")
        if chain_url:
            status, blocked, err = health.check_source_url(chain_url)
            if blocked:
                diagnosis = DIAG_SITE_BLOCKED
                details["chain_url"] = chain_url
                details["status_code"] = 403
            elif status and status >= 400:
                diagnosis = DIAG_CHAIN_URL_DEAD
                details["chain_url"] = chain_url
                details["status_code"] = status
            elif err:
                diagnosis = DIAG_SITE_DOWN
                details["chain_url"] = chain_url
                details["error"] = err

    # 2. Check if source site (index page) is reachable
    if diagnosis == DIAG_OVERDUE:
        start_url = w.get("startUrl", "")
        if start_url:
            status, blocked, err = health.check_source_url(start_url)
            if blocked:
                diagnosis = DIAG_SITE_BLOCKED
                details["start_url"] = start_url
                details["status_code"] = 403
            elif err:
                diagnosis = DIAG_SITE_DOWN
                details["start_url"] = start_url
                details["error"] = err

    # 3. Check KN API for chapter count drift
    if slug and diagnosis in (DIAG_OVERDUE, DIAG_CHAIN_URL_DEAD):
        kn_data = health.get_kn_novel(slug)
        if kn_data:
            kn_chapters = kn_data.get("chapterCount", 0)
            watch_last  = w.get("lastChapter", 0)
            if kn_chapters > 0:
                details["kn_chapter_count"] = kn_chapters
                details["watch_last_chapter"] = watch_last
                if kn_chapters > watch_last:
                    # KN has more chapters than the watch knows about
                    # The watch state is outdated
                    diagnosis = DIAG_KN_DRIFT
                    details["drift"] = kn_chapters - watch_last

    stale_entry["diagnosis"] = diagnosis
    stale_entry["details"]   = details
    return stale_entry


def diagnose_all(stale_list):
    """Run diagnostics on all stale watches."""
    results = []
    for entry in stale_list:
        try:
            results.append(diagnose_stale_watch(entry))
        except Exception as e:
            entry["diagnosis"] = DIAG_UNKNOWN
            entry["details"] = {"error": str(e)}
            results.append(entry)
    return results


def format_staleness_report(diagnosed_list):
    """
    Format a human-readable staleness report.
    Returns a string suitable for logging or Discord.
    """
    if not diagnosed_list:
        return "✅ All watches are up-to-date."

    lines = [f"⚠ {len(diagnosed_list)} stale watcher(s) detected:\n"]

    diag_icons = {
        DIAG_OVERDUE:        "🕐",
        DIAG_CHAIN_URL_DEAD: "🔗",
        DIAG_SITE_BLOCKED:   "🚫",
        DIAG_SITE_DOWN:      "💀",
        DIAG_KN_DRIFT:       "📊",
        DIAG_UNKNOWN:        "❓",
    }

    diag_labels = {
        DIAG_OVERDUE:        "Overdue (just needs re-trigger)",
        DIAG_CHAIN_URL_DEAD: "Chain URL dead (404)",
        DIAG_SITE_BLOCKED:   "Site blocked (403)",
        DIAG_SITE_DOWN:      "Site unreachable",
        DIAG_KN_DRIFT:       "KN chapter count drift",
        DIAG_UNKNOWN:        "Unknown cause",
    }

    for entry in diagnosed_list:
        diag  = entry.get("diagnosis", DIAG_UNKNOWN)
        icon  = diag_icons.get(diag, "❓")
        label = diag_labels.get(diag, "Unknown")
        title = entry.get("novel_title", entry.get("novel_id", "?"))
        hrs   = entry.get("hours_overdue", "?")
        ch    = entry.get("last_chapter", "?")

        line = f"  {icon} {title} — Ch.{ch}, {hrs}h overdue"
        if diag != DIAG_OVERDUE:
            line += f" [{label}]"
        lines.append(line)

        # Add detail hints
        details = entry.get("details", {})
        if details.get("drift"):
            lines.append(f"      ↳ KN has Ch.{details['kn_chapter_count']} but watch thinks Ch.{details['watch_last_chapter']}")
        if details.get("chain_url"):
            lines.append(f"      ↳ Dead URL: {details['chain_url']}")

    return "\n".join(lines)

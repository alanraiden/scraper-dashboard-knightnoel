#!/usr/bin/env python3
"""
scraper_bot.py — Knight Novel Scraper Management Bot
=====================================================
A standalone daemon that monitors the scraper server, detects stale watches,
auto-recovers failed scraping jobs, repairs chain URLs, and sends Discord
notifications.

Run alongside the scraper server on the same machine:
    python scraper_bot.py

Or via systemd:
    systemctl start scraper-bot

Configuration:  bot_config.json  (auto-created on first run)
State file:     bot_state.json   (auto-managed)
Logs:           bot_logs/YYYY-MM-DD.jsonl
"""

import sys
import os
import time
import signal
import traceback

# Ensure the bot package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.config import load_config, get_config
from bot import logger, health, state
from bot.staleness import find_stale_watches, diagnose_all, format_staleness_report
from bot.recovery import recover_watch, run_power_recovery
from bot import notifications
from bot.reporter import generate_daily_report, should_send_digest, mark_digest_sent


# ── Globals ───────────────────────────────────────────────────────────────────

_running = True
_last_staleness_check = 0
_scraper_was_down     = False
_kn_was_down          = False

# Stuck job tracking: job_id → first_seen_timestamp
_stuck_job_tracker    = {}
STUCK_JOB_THRESHOLD   = 30 * 60  # 30 minutes


def _signal_handler(sig, frame):
    global _running
    logger.info(f"Received signal {sig} — shutting down gracefully...")
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═════════════════════════════════════════════════════════════════════════════

def main():
    global _last_staleness_check, _scraper_was_down, _kn_was_down

    cfg = load_config()
    logger.info("=" * 56)
    logger.info("  Knight Novel Scraper Bot starting...")
    logger.info("=" * 56)
    logger.info(f"  Scraper server: {cfg['scraper_url']}")
    logger.info(f"  KN API:         {cfg['kn_api_url']}")
    logger.info(f"  Discord:        {'configured' if notifications.is_configured() else 'not configured'}")
    logger.info(f"  Tick interval:  {cfg['tick_interval_seconds']}s")
    logger.info(f"  Auto-retry:     {'enabled' if cfg['auto_retry']['enabled'] else 'disabled'}")
    logger.info("=" * 56)

    # Record startup and check if this is a crash recovery scenario
    prev_startup = state.record_startup()
    _check_crash_recovery(cfg)

    tick = 0
    while _running:
        try:
            _tick(cfg, tick)
        except Exception as e:
            logger.error(f"Tick error: {e}")
            logger.error(traceback.format_exc())

        tick += 1
        # Sleep in small increments so we respond to SIGTERM quickly
        sleep_total = cfg["tick_interval_seconds"]
        slept = 0
        while slept < sleep_total and _running:
            time.sleep(min(5, sleep_total - slept))
            slept += 5

    logger.info("Bot shut down cleanly.")


def _tick(cfg, tick_number):
    """One iteration of the main loop."""
    global _last_staleness_check, _scraper_was_down, _kn_was_down

    # ── 1. Health checks (every tick) ─────────────────────────────────────

    scraper = health.check_scraper_server()
    if not scraper.get("alive"):
        consecutive = scraper.get("consecutive_failures", 1)
        logger.error(f"Scraper server DOWN: {scraper.get('error')} (failure #{consecutive})")
        # Alert on first failure, then every 5th
        if consecutive == 1 or consecutive % 5 == 0:
            notifications.notify_server_down("Scraper Server", scraper.get("error", "?"), consecutive)
        _scraper_was_down = True
        return  # Can't do anything else if scraper is down
    else:
        if _scraper_was_down:
            logger.info("Scraper server recovered!")
            notifications.notify_server_recovered("Scraper Server")
            _scraper_was_down = False
        if tick_number % 10 == 0:  # Log health every 10 ticks (~10 min)
            logger.debug(
                f"Scraper healthy — {scraper.get('active_jobs', 0)} active, "
                f"{scraper.get('queued_jobs', 0)} queued"
            )

    # KN API check (every 5 ticks to avoid spamming)
    if tick_number % 5 == 0:
        kn = health.check_kn_api()
        if kn.get("alive") is False:
            consecutive = kn.get("consecutive_failures", 1)
            logger.warn(f"KN API issue: {kn.get('error')} (failure #{consecutive})")
            if consecutive == 1 or consecutive % 5 == 0:
                notifications.notify_server_down("Knight Novel API", kn.get("error", "?"), consecutive)
            _kn_was_down = True
        elif kn.get("alive") is True and _kn_was_down:
            logger.info("KN API recovered!")
            notifications.notify_server_recovered("Knight Novel API")
            _kn_was_down = False

    # ── 2. Stuck job detection (every tick) ───────────────────────────────

    _check_stuck_jobs()

    # ── 3. Staleness check (periodic) ─────────────────────────────────────

    staleness_interval = cfg.get("staleness_check_interval_minutes", 30) * 60
    now = time.time()

    if now - _last_staleness_check >= staleness_interval:
        _last_staleness_check = now
        _run_staleness_check(cfg)

    # ── 4. Daily digest ───────────────────────────────────────────────────

    if should_send_digest():
        _send_daily_digest()


# ═════════════════════════════════════════════════════════════════════════════
#  STALENESS CHECK
# ═════════════════════════════════════════════════════════════════════════════

def _run_staleness_check(cfg):
    """Full staleness check with diagnosis and auto-recovery."""
    logger.info("Running staleness check...")

    watches = health.get_watches()
    if watches is None:
        logger.error("Could not fetch watches from scraper server")
        return

    stale = find_stale_watches(watches)
    if not stale:
        logger.info(f"All {len([w for w in watches if w.get('active')])} active watches are up-to-date")
        return

    logger.warn(f"Found {len(stale)} stale watch(es) — running diagnostics...")

    # Diagnose each stale watch
    diagnosed = diagnose_all(stale)

    # Log the report
    report = format_staleness_report(diagnosed)
    for line in report.split("\n"):
        logger.info(line)

    # Send Discord alert if there are new stale watches
    notifications.notify_stale_watches(diagnosed)

    # Auto-recover where possible
    if cfg["auto_retry"]["enabled"]:
        logger.info("Attempting auto-recovery for recoverable watches...")
        for entry in diagnosed:
            try:
                result = recover_watch(entry)
                if result.get("success"):
                    notifications.notify_auto_recovery(
                        entry["novel_title"],
                        result["action"],
                        True,
                        result.get("message", ""),
                    )
                elif result.get("action") == "alert":
                    notifications.notify_auto_recovery(
                        entry["novel_title"],
                        result["action"],
                        False,
                        result.get("message", ""),
                    )
            except Exception as e:
                logger.error(f"Recovery failed for {entry.get('novel_title')}: {e}")

            # Small delay between recovery attempts
            time.sleep(3)


# ═════════════════════════════════════════════════════════════════════════════
#  CRASH RECOVERY
# ═════════════════════════════════════════════════════════════════════════════

def _check_crash_recovery(cfg):
    """
    Check if we're starting up after a crash/power cut and run staggered
    recovery if needed.
    """
    if state.is_recovery_done():
        logger.debug("Recovery already done for this startup — skipping")
        return

    recovery_cfg = cfg.get("recovery", {})
    if not recovery_cfg.get("enabled", True):
        logger.info("Power recovery is disabled in config")
        state.mark_recovery_done()
        return

    # Wait for the scraper server to come up
    logger.info("Waiting for scraper server to become available...")
    for i in range(30):  # Try for up to 5 minutes
        scraper = health.check_scraper_server()
        if scraper.get("alive"):
            break
        time.sleep(10)
    else:
        logger.error("Scraper server did not come up within 5 minutes — skipping recovery")
        state.mark_recovery_done()
        return

    logger.info("Scraper server is up — checking for stale watches...")

    # Give the server a moment to fully initialize
    time.sleep(5)

    watches = health.get_watches()
    if watches is None:
        logger.error("Could not fetch watches — skipping recovery")
        state.mark_recovery_done()
        return

    stale = find_stale_watches(watches)
    if not stale:
        logger.info("No stale watches found — no recovery needed")
        state.mark_recovery_done()
        return

    # If more than 5 watches are stale, this is likely a crash recovery scenario
    is_crash = len(stale) >= 5
    if is_crash:
        logger.alert(f"Detected {len(stale)} stale watches — likely a server restart/power cut")
        notifications.notify_power_cut_detected(len(stale))

        # Run diagnostics
        diagnosed = diagnose_all(stale)
        report = format_staleness_report(diagnosed)
        for line in report.split("\n"):
            logger.info(line)

        # Staggered recovery
        summary = run_power_recovery(diagnosed)
        notifications.notify_power_recovery_complete(summary)
    else:
        # Just a few stale — normal staleness handling will pick them up
        logger.info(f"Only {len(stale)} stale watches — will handle in normal staleness check cycle")

    state.mark_recovery_done()


# ═════════════════════════════════════════════════════════════════════════════
#  STUCK JOB DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def _check_stuck_jobs():
    """Detect jobs that have been running for too long."""
    global _stuck_job_tracker

    all_jobs = health.get_all_jobs()
    if not all_jobs:
        return

    now = time.time()
    active_ids = set()

    # all_jobs may be a dict of {job_id: job_data} or similar
    items = all_jobs.items() if isinstance(all_jobs, dict) else []

    for job_id, job_data in items:
        status = job_data.get("status", "")
        if status not in ("running", "starting"):
            continue

        active_ids.add(job_id)
        started_at = job_data.get("started_at", 0)
        if not started_at:
            continue

        running_secs = now - started_at

        if running_secs >= STUCK_JOB_THRESHOLD:
            if job_id not in _stuck_job_tracker:
                _stuck_job_tracker[job_id] = now
                novel_slug = job_data.get("novel_slug", job_data.get("novel_id", "unknown"))
                running_mins = running_secs / 60
                logger.warn(f"Stuck job detected: {job_id} ({novel_slug}) running for {running_mins:.0f} min")
                notifications.notify_stuck_job(job_id, novel_slug, running_mins)
                state.add_daily_stat("stuck_jobs_detected")

    # Clean up tracker for jobs that are no longer active
    _stuck_job_tracker = {k: v for k, v in _stuck_job_tracker.items() if k in active_ids}


# ═════════════════════════════════════════════════════════════════════════════
#  DAILY DIGEST
# ═════════════════════════════════════════════════════════════════════════════

def _send_daily_digest():
    """Generate and send the daily digest report."""
    logger.info("Generating daily digest report...")
    try:
        report = generate_daily_report()
        logger.info("Daily digest:\n" + report)
        notifications.notify_daily_digest(report)
        mark_digest_sent()
        logger.info("Daily digest sent successfully")
    except Exception as e:
        logger.error(f"Failed to generate/send daily digest: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()

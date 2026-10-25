#!/usr/bin/env python3
"""
scraper_bot.py — Knight Novel Scraper Management Bot v3
=======================================================
A standalone daemon that monitors the scraper server, detects stale watches,
auto-recovers failed scraping jobs, repairs chain URLs, and sends Discord
notifications.

v3 improvements:
  - Auto-cancel stuck jobs after 30 min + pause watch after 3 timeouts
  - Dead man's switch via healthchecks.io
  - Internet connectivity check before recovery
  - Proper error logging for trigger failures
  - Alert deduplication (no repeat Discord spam)

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
import requests

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
_last_heartbeat       = 0
_scraper_was_down     = False
_kn_was_down          = False

# Stuck job tracking: job_id → {"first_seen": timestamp, "novel_slug": str, "novel_id": str, "alerted": bool}
_stuck_job_tracker    = {}
STUCK_JOB_THRESHOLD   = 30 * 60   # 30 minutes — alert
AUTO_CANCEL_THRESHOLD = 35 * 60   # 35 minutes — auto-cancel (5 min grace after alert)
MAX_TIMEOUTS_BEFORE_PAUSE = 3     # Pause watch after 3 timeouts

# Alert deduplication: set of novel_ids we already alerted about this session.
# Cleared when a watch recovers (becomes non-stale).
_alerted_stale_ids    = set()


def _signal_handler(sig, frame):
    global _running
    logger.info(f"Received signal {sig} — shutting down gracefully...")
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ═════════════════════════════════════════════════════════════════════════════
#  DEAD MAN'S SWITCH (healthchecks.io)
# ═════════════════════════════════════════════════════════════════════════════

def _ping_heartbeat():
    """Ping healthchecks.io to confirm the bot is alive.
    If this stops, healthchecks.io alerts you from OUTSIDE the server."""
    global _last_heartbeat
    cfg = get_config()
    hc_url = cfg.get("healthcheck_url", "")
    if not hc_url:
        return

    now = time.time()
    interval = cfg.get("healthcheck_interval_seconds", 300)  # default 5 min
    if now - _last_heartbeat < interval:
        return

    try:
        requests.get(hc_url, timeout=10)
        _last_heartbeat = now
    except Exception as e:
        logger.warn(f"Healthcheck ping failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═════════════════════════════════════════════════════════════════════════════

def main():
    global _last_staleness_check, _scraper_was_down, _kn_was_down

    cfg = load_config()
    logger.info("=" * 56)
    logger.info("  Knight Novel Scraper Bot v3 starting...")
    logger.info("=" * 56)
    logger.info(f"  Scraper server: {cfg['scraper_url']}")
    logger.info(f"  KN API:         {cfg['kn_api_url']}")
    logger.info(f"  Discord:        {'configured' if notifications.is_configured() else 'not configured'}")
    logger.info(f"  Heartbeat:      {'configured' if cfg.get('healthcheck_url') else 'not configured'}")
    logger.info(f"  Tick interval:  {cfg['tick_interval_seconds']}s")
    logger.info(f"  Auto-retry:     {'enabled' if cfg['auto_retry']['enabled'] else 'disabled'}")
    logger.info(f"  Auto-cancel:    stuck jobs after {AUTO_CANCEL_THRESHOLD // 60} min")
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

    # ── 0. Dead man's switch ping ─────────────────────────────────────────
    _ping_heartbeat()

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

    # ── 2. Stuck job detection + auto-cancel (every tick) ─────────────────

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
    global _alerted_stale_ids
    logger.info("Running staleness check...")

    watches = health.get_watches()
    if watches is None:
        logger.error("Could not fetch watches from scraper server")
        return

    stale = find_stale_watches(watches)
    if not stale:
        active_count = len([w for w in watches if w.get('active')])
        logger.info(f"All {active_count} active watches are up-to-date")
        # Clear dedup set — everything recovered
        if _alerted_stale_ids:
            _alerted_stale_ids.clear()
        return

    # Diagnose each stale watch (includes queue check)
    diagnosed = diagnose_all(stale)

    # Separate already-queued from truly needing attention
    from bot.staleness import DIAG_ALREADY_QUEUED
    queued_count = sum(1 for e in diagnosed if e.get('diagnosis') == DIAG_ALREADY_QUEUED)
    truly_stale  = [e for e in diagnosed if e.get('diagnosis') != DIAG_ALREADY_QUEUED]

    if queued_count > 0:
        logger.info(f"{queued_count} stale watch(es) already queued in scraper — scheduler is handling them")

    if not truly_stale:
        logger.info(f"All {len(stale)} stale watches are already queued/running — no action needed")
        return

    logger.warn(f"{len(truly_stale)} watch(es) need attention (plus {queued_count} already queued)")

    # Log the report (only truly stale)
    report = format_staleness_report(diagnosed)
    for line in report.split("\n"):
        if line.strip():
            logger.info(line)

    # Only send Discord alerts for NEWLY stale watches (not already alerted)
    new_stale = [e for e in truly_stale if e["novel_id"] not in _alerted_stale_ids]
    if new_stale:
        notifications.notify_stale_watches(new_stale)
        for e in new_stale:
            _alerted_stale_ids.add(e["novel_id"])
    else:
        logger.debug(f"No new stale watches to alert about ({len(truly_stale)} already reported)")

    # Clear dedup entries for watches that are no longer stale
    current_stale_ids = {e["novel_id"] for e in truly_stale}
    recovered = _alerted_stale_ids - current_stale_ids
    if recovered:
        logger.info(f"{len(recovered)} previously stale watch(es) have recovered")
        _alerted_stale_ids -= recovered

    # Auto-recover truly stale watches only
    if cfg["auto_retry"]["enabled"] and truly_stale:
        recoverable = [e for e in truly_stale if e.get('diagnosis') != DIAG_ALREADY_QUEUED]
        if recoverable:
            # Internet check before recovery — avoid false 403s when router is still booting
            if not health.check_internet():
                logger.warn("No internet connectivity — deferring recovery until connection is restored")
                return

            logger.info(f"Attempting auto-recovery for {len(recoverable)} watch(es)...")
            for entry in recoverable:
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
                    elif result.get("action") == "skip":
                        # Silently skip (budget exhausted, already queued, etc.)
                        pass
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

    # ── Internet connectivity check ──────────────────────────────────────
    # After a power cut, the router often comes back 1-3 min after the server.
    if not health.check_internet():
        logger.warn("Server is up but no internet — waiting for router...")
        for attempt in range(12):  # Try for up to 2 more minutes
            time.sleep(10)
            if health.check_internet():
                logger.info("Internet is back!")
                break
        else:
            logger.error("No internet after 2 minutes — proceeding anyway (may get false 403s)")

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
            if line.strip():
                logger.info(line)

        # Staggered recovery (fully automated — no user intervention needed)
        summary = run_power_recovery(diagnosed)
        notifications.notify_power_recovery_complete(summary)
    else:
        # Just a few stale — normal staleness handling will pick them up
        logger.info(f"Only {len(stale)} stale watches — will handle in normal staleness check cycle")

    state.mark_recovery_done()


# ═════════════════════════════════════════════════════════════════════════════
#  STUCK JOB DETECTION + AUTO-CANCEL
# ═════════════════════════════════════════════════════════════════════════════

def _check_stuck_jobs():
    """
    Detect jobs running too long, auto-cancel them, and track timeouts per novel.
    After 3 timeouts for the same novel, pause its watch and alert once.
    """
    global _stuck_job_tracker

    all_jobs = health.get_all_jobs()
    if not all_jobs:
        return

    now = time.time()
    active_ids = set()

    # all_jobs is a dict of {job_id: job_data}
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
        novel_slug = job_data.get("novel_slug", job_data.get("novel_id", "unknown"))
        novel_id   = job_data.get("novel_id", novel_slug)

        # ── Phase 1: Alert at 30 min ────────────────────────────────────
        if running_secs >= STUCK_JOB_THRESHOLD and job_id not in _stuck_job_tracker:
            _stuck_job_tracker[job_id] = {
                "first_seen": now,
                "novel_slug": novel_slug,
                "novel_id":   novel_id,
                "alerted":    True,
                "cancelled":  False,
            }
            running_mins = running_secs / 60
            logger.warn(f"Stuck job detected: {job_id} ({novel_slug}) running for {running_mins:.0f} min — will auto-cancel in 5 min")
            notifications.notify_stuck_job(job_id, novel_slug, running_mins)
            state.add_daily_stat("stuck_jobs_detected")

        # ── Phase 2: Auto-cancel at 35 min ──────────────────────────────
        if running_secs >= AUTO_CANCEL_THRESHOLD and job_id in _stuck_job_tracker:
            tracker = _stuck_job_tracker[job_id]
            if not tracker.get("cancelled"):
                tracker["cancelled"] = True
                running_mins = running_secs / 60
                logger.warn(f"Auto-cancelling stuck job {job_id} ({novel_slug}) after {running_mins:.0f} min")

                success = health.cancel_job(job_id)
                if success:
                    logger.info(f"Job {job_id} cancelled successfully")
                    state.add_daily_stat("jobs_auto_cancelled")

                    # Track timeouts for this novel
                    timeout_count = state.increment_timeout(novel_id)
                    logger.info(f"[{novel_slug}] Timeout #{timeout_count} of {MAX_TIMEOUTS_BEFORE_PAUSE}")

                    # ── Phase 3: Pause watch after 3 timeouts ───────────
                    if timeout_count >= MAX_TIMEOUTS_BEFORE_PAUSE:
                        logger.warn(f"[{novel_slug}] Timed out {timeout_count}× — pausing watch (possible pagination loop?)")
                        health.patch_watch(novel_id, {"active": False})
                        state.add_paused_watch(novel_id, novel_slug, f"Auto-paused: timed out {timeout_count}× — check for pagination loop or site changes")
                        state.add_daily_stat("watches_auto_paused")
                        notifications.notify_watch_paused(
                            novel_slug,
                            f"Timed out {timeout_count} times. Paused to prevent resource waste. "
                            f"Possible causes: pagination loop, site layout changed, or Cloudflare blocking.\n"
                            f"Check manually and re-enable from the dashboard."
                        )
                else:
                    logger.error(f"Failed to cancel job {job_id}")

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

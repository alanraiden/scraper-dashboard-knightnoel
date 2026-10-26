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
_last_staleness_check  = 0
_last_heartbeat        = 0
_last_gap_check        = 0
_last_adaptive_check   = 0
_scraper_was_down      = False
_kn_was_down           = False

# Stuck job tracking: job_id → {"no_progress_since": ts, novel_slug, alerted, cancelled}
_stuck_job_tracker    = {}
# v4: detect stuck by LACK OF PROGRESS, not wall-clock age.
NO_PROGRESS_ALERT  = 10 * 60   # 10 min no progress → alert
NO_PROGRESS_CANCEL = 13 * 60   # 13 min no progress → auto-cancel (3 min grace)
ABS_MAX_JOB_TIME   = 6 * 60 * 60  # 6 h absolute cap — catches true infinite loops
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
    logger.info("  Knight Novel Scraper Bot v5 starting...")
    logger.info("=" * 56)
    logger.info(f"  Scraper server: {cfg['scraper_url']}")
    logger.info(f"  KN API:         {cfg['kn_api_url']}")
    logger.info(f"  Discord:        {'configured' if notifications.is_configured() else 'not configured'}")
    logger.info(f"  Heartbeat:      {'configured' if cfg.get('healthcheck_url') else 'not configured'}")
    logger.info(f"  Tick interval:  {cfg['tick_interval_seconds']}s")
    logger.info(f"  Auto-retry:     {'enabled' if cfg['auto_retry']['enabled'] else 'disabled'}")
    logger.info(f"  Auto-resume:    {'enabled' if cfg.get('auto_resume', {}).get('enabled') else 'disabled'}")
    logger.info(f"  Circuit breaker:{'enabled' if cfg.get('circuit_breaker', {}).get('enabled') else 'disabled'}")
    logger.info(f"  Adaptive ivl:   {'enabled' if cfg.get('adaptive_intervals', {}).get('enabled') else 'disabled'}")
    logger.info(f"  Gap detection:  {'enabled' if cfg.get('gap_detection', {}).get('enabled') else 'disabled'}")
    logger.info("=" * 56)

    # v5: Purge old log files at startup
    _purge_old_logs(cfg)

    # Record startup and check if this is a crash recovery scenario
    prev_startup = state.record_startup()
    _check_crash_recovery(cfg)

    tick = 0
    while _running:
        try:
            # v5: Config hot-reload — re-read bot_config.json every N ticks
            reload_every = cfg.get("config_reload_ticks", 10)
            if reload_every > 0 and tick > 0 and tick % reload_every == 0:
                cfg = load_config()

            _tick(cfg, tick)
        except Exception as e:
            logger.error(f"Tick error: {e}")
            logger.error(traceback.format_exc())

        tick += 1
        sleep_total = cfg["tick_interval_seconds"]
        slept = 0
        while slept < sleep_total and _running:
            time.sleep(min(5, sleep_total - slept))
            slept += 5

    logger.info("Bot shut down cleanly.")


def _tick(cfg, tick_number):
    """One iteration of the main loop."""
    global _last_staleness_check, _last_gap_check, _last_adaptive_check
    global _scraper_was_down, _kn_was_down

    # ── 0. Dead man's switch ping ─────────────────────────────────────────
    _ping_heartbeat()

    # ── 1. Health checks (every tick) ─────────────────────────────────────
    scraper = health.check_scraper_server()
    if not scraper.get("alive"):
        consecutive = scraper.get("consecutive_failures", 1)
        logger.error(f"Scraper server DOWN: {scraper.get('error')} (failure #{consecutive})")
        if consecutive == 1 or consecutive % 5 == 0:
            notifications.notify_server_down("Scraper Server", scraper.get("error", "?"), consecutive)
        _scraper_was_down = True
        return
    else:
        if _scraper_was_down:
            logger.info("Scraper server recovered!")
            notifications.notify_server_recovered("Scraper Server")
            _scraper_was_down = False
        if tick_number % 10 == 0:
            logger.debug(
                f"Scraper healthy — {scraper.get('active_jobs', 0)} active, "
                f"{scraper.get('queued_jobs', 0)} queued"
            )

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

    # ── 4. v5: Auto-resume bot-paused watches (every tick, cheap check) ───
    if cfg.get("auto_resume", {}).get("enabled", True):
        _check_auto_resume(cfg)

    # ── 5. v5: Adaptive interval & completed-novel detection (hourly) ──────
    adaptive_interval = 60 * 60  # 1 hour
    if cfg.get("adaptive_intervals", {}).get("enabled", True):
        if now - _last_adaptive_check >= adaptive_interval:
            _last_adaptive_check = now
            _check_adaptive_intervals(cfg)

    # ── 6. v5: Gap detection (configurable, default 24 h) ─────────────────
    gap_cfg = cfg.get("gap_detection", {})
    if gap_cfg.get("enabled", True):
        gap_interval = gap_cfg.get("check_interval_hours", 24) * 3600
        if now - _last_gap_check >= gap_interval:
            _last_gap_check = now
            _check_chapter_gaps(cfg)

    # ── 7. Daily digest ───────────────────────────────────────────────────
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
    v4: Detect stuck jobs by lack of progress (last_progress_at not moving),
    NOT by wall-clock age.  A 1000-chapter job running for 2 h is healthy;
    a job that scraped 0 chapters in the last 10 min is broken.

    Stages:
      1. NO_PROGRESS_ALERT  (10 min) — alert via Discord, add to tracker.
      2. NO_PROGRESS_CANCEL (13 min) — auto-cancel, track timeout count.
      3. ABS_MAX_JOB_TIME   (6 h)   — force-cancel regardless of progress
                                       (guards infinite-redirect loops).
      4. After MAX_TIMEOUTS_BEFORE_PAUSE timeouts, deactivate the watch.
    If progress is made (last_progress_at advances), remove from tracker.
    """
    global _stuck_job_tracker

    all_jobs = health.get_all_jobs()
    if not all_jobs:
        return

    now = time.time()
    active_ids = set()

    items = all_jobs.items() if isinstance(all_jobs, dict) else []

    for job_id, job_data in items:
        status = job_data.get("status", "")
        if status not in ("running", "starting"):
            continue

        active_ids.add(job_id)
        started_at = job_data.get("started_at", 0)
        if not started_at:
            continue

        novel_slug = job_data.get("novel_slug", job_data.get("novel_id", "unknown"))
        novel_id   = job_data.get("novel_id", novel_slug)
        running_secs = now - started_at

        # ── Absolute safety cap (infinite-redirect / runaway loop) ───────────
        if running_secs >= ABS_MAX_JOB_TIME:
            if job_id not in _stuck_job_tracker or not _stuck_job_tracker[job_id].get("cancelled"):
                logger.warn(f"Force-cancelling {job_id} ({novel_slug}) — hit {ABS_MAX_JOB_TIME/3600:.0f}h absolute cap")
                _do_cancel(job_id, novel_slug, novel_id, running_secs / 60, force=True)
            continue

        # ── Progress-based detection ─────────────────────────────────────────
        # Prefer last_progress_at; fall back to started_at if the server is
        # older and doesn't send it yet.
        last_progress = job_data.get("last_progress_at") or started_at
        no_progress_secs = now - last_progress

        if no_progress_secs < NO_PROGRESS_ALERT:
            # Job is making progress — clear any stale tracker entry
            if job_id in _stuck_job_tracker:
                logger.info(f"Job {job_id} ({novel_slug}) resumed making progress — cleared stuck flag")
                _stuck_job_tracker.pop(job_id, None)
            continue

        # ── Phase 1: Alert at 10 min no-progress ────────────────────────────
        if job_id not in _stuck_job_tracker:
            _stuck_job_tracker[job_id] = {
                "no_progress_since": last_progress,
                "novel_slug":        novel_slug,
                "novel_id":          novel_id,
                "alerted":           False,
                "cancelled":         False,
            }

        tracker = _stuck_job_tracker[job_id]

        if not tracker.get("alerted"):
            tracker["alerted"] = True
            np_mins = no_progress_secs / 60
            logger.warn(
                f"Stuck job: {job_id} ({novel_slug}) — no progress for {np_mins:.0f} min "
                f"(total running {running_secs/60:.0f} min) — will cancel in "
                f"{(NO_PROGRESS_CANCEL - NO_PROGRESS_ALERT) / 60:.0f} min"
            )
            notifications.notify_stuck_job(job_id, novel_slug, running_secs / 60)
            state.add_daily_stat("stuck_jobs_detected")

        # ── Phase 2: Auto-cancel at 13 min no-progress ──────────────────────
        if not tracker.get("cancelled") and no_progress_secs >= NO_PROGRESS_CANCEL:
            tracker["cancelled"] = True
            _do_cancel(job_id, novel_slug, novel_id, running_secs / 60, force=False)

    # Clean up tracker entries for jobs that finished / were cancelled externally
    _stuck_job_tracker = {k: v for k, v in _stuck_job_tracker.items() if k in active_ids}


def _do_cancel(job_id, novel_slug, novel_id, running_mins, *, force=False):
    """Cancel a stuck job, track the timeout, and auto-pause if needed."""
    reason = "absolute time cap" if force else f"no progress for {NO_PROGRESS_CANCEL // 60} min"
    logger.warn(f"Auto-cancelling {job_id} ({novel_slug}) after {running_mins:.0f} min total ({reason})")

    success = health.cancel_job(job_id)
    if success:
        logger.info(f"Job {job_id} cancelled successfully")
        state.add_daily_stat("jobs_auto_cancelled")

        timeout_count = state.increment_timeout(novel_id)
        logger.info(f"[{novel_slug}] Timeout #{timeout_count} of {MAX_TIMEOUTS_BEFORE_PAUSE}")

        if timeout_count >= MAX_TIMEOUTS_BEFORE_PAUSE:
            logger.warn(f"[{novel_slug}] Timed out {timeout_count}\u00d7 — pausing watch")
            health.patch_watch(novel_id, {"active": False})
            state.add_paused_watch(
                novel_id, novel_slug,
                f"Auto-paused: no progress {timeout_count}\u00d7 — "
                f"check for pagination loop, site layout change, or Cloudflare block.",
                source_url=job_data.get("last_progress_url", ""),
            )
            state.add_daily_stat("watches_auto_paused")
            notifications.notify_watch_paused(
                novel_slug,
                f"No scraping progress detected {timeout_count} times. "
                f"Paused to prevent resource waste.\n"
                f"Possible causes: pagination loop, site layout changed, Cloudflare blocking.\n"
                f"Check manually and re-enable from the dashboard."
            )
    else:
        logger.error(f"Failed to cancel job {job_id}")


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
#  v5: LOG PURGE
# ═════════════════════════════════════════════════════════════════════════════

def _purge_old_logs(cfg):
    """Delete bot_logs/*.jsonl files older than log_retention_days at startup."""
    retention_days = cfg.get("log_retention_days", 30)
    log_dir = cfg.get("log_dir", "bot_logs")
    if not os.path.isdir(log_dir):
        return
    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for fname in os.listdir(log_dir):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(log_dir, fname)
        try:
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                deleted += 1
        except Exception:
            pass
    if deleted:
        logger.info(f"Purged {deleted} log file(s) older than {retention_days} days from {log_dir}/")


# ═════════════════════════════════════════════════════════════════════════════
#  v5: AUTO-RESUME BOT-PAUSED WATCHES
# ═════════════════════════════════════════════════════════════════════════════

def _check_auto_resume(cfg):
    """
    Re-enable watches that were auto-paused by the bot (after repeated timeouts).

    A watch is eligible for auto-resume if:
      1. It is recorded in bot_state["bot_paused"].
      2. It has been paused for >= auto_resume.resume_after_hours.
      3. The source site is reachable (HEAD request succeeds).
      4. It hasn't exceeded auto_resume.max_resume_attempts.

    On successful resume: resets the timeout counter, removes from paused list,
    re-enables the watch via PATCH /watches/<id>, and notifies Discord.
    """
    resume_cfg = cfg.get("auto_resume", {})
    resume_after = resume_cfg.get("resume_after_hours", 6) * 3600
    max_attempts = resume_cfg.get("max_resume_attempts", 3)

    paused = state.get("bot_paused", {})
    if not paused:
        return

    now = time.time()
    for novel_id, info in list(paused.items()):
        paused_at      = info.get("paused_at", 0)
        resume_count   = info.get("resume_attempts", 0)
        novel_slug     = info.get("novel_slug", novel_id)
        source_url     = info.get("source_url", "")

        if now - paused_at < resume_after:
            continue  # Too early

        if resume_count >= max_attempts:
            # Give up — needs manual intervention
            logger.warn(
                f"[{novel_slug}] Paused watch has hit max auto-resume attempts ({max_attempts}) "
                f"— needs manual review and re-enable from the dashboard."
            )
            continue

        # Check if the source site is reachable before re-enabling
        if source_url:
            try:
                r = requests.head(source_url, timeout=10,
                                  headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code >= 500:
                    logger.info(f"[{novel_slug}] Source site returned {r.status_code} — deferring auto-resume")
                    continue
            except Exception as e:
                logger.info(f"[{novel_slug}] Source site unreachable ({e}) — deferring auto-resume")
                continue

        # Re-enable the watch
        logger.info(f"[{novel_slug}] Auto-resuming after {(now - paused_at)/3600:.1f}h pause (attempt {resume_count + 1}/{max_attempts})")
        success = health.patch_watch(novel_id, {"active": True})
        if success:
            # Reset timeout counter so it gets 3 fresh chances
            state.reset_timeout(novel_id)
            state.update_paused_watch_attempts(novel_id, resume_count + 1)
            state.add_daily_stat("watches_auto_resumed")
            notifications.notify_watch_resumed(
                novel_slug,
                f"Auto-resumed after {(now - paused_at)/3600:.1f}h pause "
                f"(attempt {resume_count + 1}/{max_attempts}). "
                f"Will re-pause if timeouts recur."
            )
        else:
            logger.error(f"[{novel_slug}] Failed to re-enable watch via API")


# ═════════════════════════════════════════════════════════════════════════════
#  v5: ADAPTIVE INTERVALS + COMPLETED NOVEL DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def _check_adaptive_intervals(cfg):
    """
    Runs hourly.  For each active watch:

    1. If the KN novel is marked 'completed', deactivate the watch and notify.
    2. If no new chapter has appeared in > inactive_days, double the check
       interval (up to max_interval_hours) to reduce wasted scrape attempts.
    """
    adaptive = cfg.get("adaptive_intervals", {})
    inactive_threshold = adaptive.get("inactive_days", 30) * 86400
    multiplier         = adaptive.get("interval_multiplier", 2.0)
    max_hours          = adaptive.get("max_interval_hours", 48)
    stop_on_completed  = adaptive.get("stop_on_completed", True)

    watches = health.get_watches()
    if not watches:
        return

    now = time.time()
    for w in watches:
        if not w.get("active"):
            continue

        novel_id   = w.get("novelId") or w.get("_id") or w.get("id")
        novel_slug = w.get("novelSlug") or w.get("novelTitle", novel_id)
        if not novel_id:
            continue

        # ── 1. Completed novel detection ──────────────────────────────────
        if stop_on_completed and w.get("novelSlug"):
            try:
                novel_info = health.get_kn_novel(w["novelSlug"])
                if novel_info and str(novel_info.get("status", "")).lower() in ("completed", "complete"):
                    logger.info(f"[{novel_slug}] KN status is 'completed' — deactivating watch")
                    health.patch_watch(novel_id, {"active": False})
                    state.add_daily_stat("watches_stopped_completed")
                    notifications.notify_watch_completed(novel_slug)
                    continue
            except Exception:
                pass  # API error — skip, try again next hourly cycle

        # ── 2. Slow-down for inactive novels ──────────────────────────────
        last_new_ch_at = w.get("lastNewChapterAt", w.get("lastChecked", 0))
        if not last_new_ch_at:
            continue
        inactive_secs = now - last_new_ch_at
        if inactive_secs < inactive_threshold:
            continue

        current_interval = float(w.get("intervalHours", 1))
        new_interval     = min(current_interval * multiplier, max_hours)
        if new_interval <= current_interval + 0.01:
            continue  # already at max or negligible change

        logger.info(
            f"[{novel_slug}] No new chapters for {inactive_secs/86400:.0f} days — "
            f"slowing interval {current_interval}h → {new_interval}h"
        )
        health.patch_watch(novel_id, {"intervalHours": new_interval})
        state.add_daily_stat("intervals_slowed")


# ═════════════════════════════════════════════════════════════════════════════
#  v5: CHAPTER GAP DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def _check_chapter_gaps(cfg):
    """
    Compare the chapter sequence stored in KN for each watched novel against the
    expected 1..N sequence.  Report any missing chapters via Discord.

    Automated backfill is NOT attempted — finding the correct source URL for an
    arbitrary chapter N is unreliable in chain mode.  The Discord alert tells
    you exactly which chapters are missing so you can fix manually or trigger a
    targeted one-shot scrape from the dashboard.
    """
    gap_cfg       = cfg.get("gap_detection", {})
    alert_threshold = gap_cfg.get("alert_threshold", 1)

    watches = health.get_watches()
    if not watches:
        return

    total_gaps = []
    for w in watches:
        if not w.get("active"):
            continue
        novel_slug = w.get("novelSlug", "")
        novel_title = w.get("novelTitle", novel_slug)
        if not novel_slug:
            continue

        try:
            chapters = health.get_kn_chapters(novel_slug)
            if not chapters:
                continue
            stored_nums = sorted({int(c.get("number", 0)) for c in chapters if c.get("number")})
            if not stored_nums:
                continue
            max_ch = stored_nums[-1]
            stored_set = set(stored_nums)
            missing = [i for i in range(1, max_ch + 1) if i not in stored_set]
            if len(missing) >= alert_threshold:
                total_gaps.append({
                    "novel_title": novel_title,
                    "novel_slug":  novel_slug,
                    "missing":     missing,
                    "max_ch":      max_ch,
                })
        except Exception as e:
            logger.warn(f"Gap check failed for {novel_slug}: {e}")

    if total_gaps:
        logger.warn(f"Gap detection found missing chapters in {len(total_gaps)} novel(s)")
        notifications.notify_chapter_gaps(total_gaps)
        state.add_daily_stat("gap_checks_with_gaps")
    else:
        logger.debug("Gap detection: no missing chapters found")


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()


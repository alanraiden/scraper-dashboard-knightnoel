"""
bot/recovery.py — Auto-retry failed watches and repair chain URLs.

Handles:
  1. Simple re-trigger for overdue watches
  2. Chain URL repair when lastChapterUrl is dead (404)
  3. KN drift correction (update watch state to match actual DB)
  4. Post-crash staggered recovery (feed stale watches in small batches)
"""

import time
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from bot.config import get_config
from bot import logger, health, state
from bot.staleness import (
    DIAG_OVERDUE, DIAG_CHAIN_URL_DEAD, DIAG_SITE_BLOCKED,
    DIAG_SITE_DOWN, DIAG_KN_DRIFT, DIAG_ALREADY_QUEUED, DIAG_UNKNOWN,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}


def can_retry(novel_id):
    """Check if we're allowed to retry this watch today."""
    cfg = get_config()
    max_retries = cfg["auto_retry"]["max_retries_per_day"]
    current = state.get_retry_count(novel_id)

    if current >= max_retries:
        return False

    # Respect retry delay
    delay_mins = cfg["auto_retry"]["retry_delay_minutes"]
    last_retry = state.get_last_retry_time(novel_id)
    if last_retry and (time.time() - last_retry) < (delay_mins * 60):
        return False

    return True


def recover_watch(diagnosed_entry):
    """
    Attempt to recover a single stale watch based on its diagnosis.

    Returns:
        dict with keys: action, success, message, job_id (if triggered)
    """
    cfg       = get_config()
    novel_id  = diagnosed_entry["novel_id"]
    title     = diagnosed_entry["novel_title"]
    slug      = diagnosed_entry["novel_slug"]
    diag      = diagnosed_entry.get("diagnosis", DIAG_UNKNOWN)
    details   = diagnosed_entry.get("details", {})
    watch     = diagnosed_entry["watch"]

    if not cfg["auto_retry"]["enabled"]:
        return {"action": "skip", "success": False, "message": "Auto-retry disabled in config"}

    if not can_retry(novel_id):
        return {"action": "skip", "success": False, "message": "Retry budget exhausted for today"}

    # ── Already queued — scheduler is handling it, don't interfere ─────────
    if diag == DIAG_ALREADY_QUEUED:
        queue_reason = details.get("queue_reason", "unknown")
        logger.debug(f"[{title}] Already in scraper queue ({queue_reason}) — skipping", novel_id=novel_id)
        return {"action": "skip", "success": False, "message": f"Already queued: {queue_reason}"}

    # ── Site blocked / down — can't do anything automated ─────────────────
    if diag == DIAG_SITE_BLOCKED:
        logger.warn(f"[{title}] Site is blocking us (403) — cannot auto-recover", novel_id=novel_id)
        return {"action": "alert", "success": False, "message": f"Site returning 403 for {title}"}

    if diag == DIAG_SITE_DOWN:
        logger.warn(f"[{title}] Source site is down — will retry later", novel_id=novel_id)
        return {"action": "defer", "success": False, "message": f"Site unreachable for {title}"}

    # ── KN drift — update watch state to match actual DB ──────────────────
    if diag == DIAG_KN_DRIFT:
        kn_chapters = details.get("kn_chapter_count", 0)
        if kn_chapters > 0:
            logger.info(f"[{title}] Fixing KN drift: watch thinks Ch.{details.get('watch_last_chapter')}, KN has Ch.{kn_chapters}", novel_id=novel_id)
            success = health.patch_watch(novel_id, {"lastChapter": kn_chapters})
            if success:
                state.add_daily_stat("drift_fixes")
                # Now trigger a fresh check
                return _trigger_and_track(novel_id, slug, title, "drift_fix")
            else:
                return {"action": "error", "success": False, "message": f"Failed to patch watch state for {title}"}

    # ── Chain URL dead — attempt to repair ────────────────────────────────
    if diag == DIAG_CHAIN_URL_DEAD:
        logger.info(f"[{title}] Chain URL is dead — attempting repair...", novel_id=novel_id)
        new_url, repaired_ch = _repair_chain_url(watch)
        if new_url:
            logger.info(f"[{title}] Repaired chain URL → {new_url} (Ch.{repaired_ch})", novel_id=novel_id)
            # Patch both URL and chapter number so next run won't re-upload old chapters
            patch = {"lastChapterUrl": new_url}
            if repaired_ch > 0:
                patch["lastChapter"] = repaired_ch
            success = health.patch_watch(novel_id, patch)
            if success:
                state.add_daily_stat("chain_repairs")
                return _trigger_and_track(novel_id, slug, title, "chain_repair")
            else:
                return {"action": "error", "success": False, "message": f"Failed to update chain URL for {title}"}
        else:
            logger.warn(f"[{title}] Could not find a replacement chain URL", novel_id=novel_id)
            return {"action": "alert", "success": False, "message": f"Chain URL dead and could not auto-repair for {title}"}


    # ── Simple overdue — just re-trigger ──────────────────────────────────
    if diag == DIAG_OVERDUE:
        return _trigger_and_track(novel_id, slug, title, "retry")

    # ── Unknown — try a simple re-trigger ─────────────────────────────────
    logger.warn(f"[{title}] Unknown diagnosis — attempting simple re-trigger", novel_id=novel_id)
    return _trigger_and_track(novel_id, slug, title, "retry_unknown")


def _trigger_and_track(novel_id, novel_slug, title, action_type):
    """Trigger a watch and track the retry."""
    # Double-check queue before triggering — avoids wasting retries
    already_queued, queue_reason = health.is_watch_already_queued(
        novel_id, novel_slug=novel_slug
    )
    if already_queued:
        logger.debug(f"[{title}] Already in scraper queue ({queue_reason}) — not re-triggering", novel_id=novel_id)
        return {"action": "skip", "success": False, "message": f"Already queued: {queue_reason}"}

    retry_num = state.increment_retry(novel_id)
    state.add_daily_stat("retries_triggered")

    job_id, error_reason = health.trigger_watch(novel_id)
    if job_id:
        logger.info(f"[{title}] Triggered watch check (retry #{retry_num}) → job {job_id}", novel_id=novel_id)
        return {"action": action_type, "success": True, "message": f"Triggered for {title}", "job_id": job_id}
    else:
        logger.warn(f"[{title}] Failed to trigger: {error_reason}", novel_id=novel_id)
        return {"action": "error", "success": False, "message": f"Trigger failed for {title}: {error_reason}"}


def _repair_chain_url(watch):
    """
    Attempt to find the correct latest chapter URL by scraping the novel's
    index page (startUrl) and finding the highest chapter link.

    Returns (url, chapter_num) or (None, 0).
    """
    start_url = watch.get("startUrl", "")
    if not start_url:
        return None, 0

    try:
        r = requests.get(start_url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None, 0

        soup = BeautifulSoup(r.text, "lxml")

        # Try common chapter list selectors
        selectors = [
            ".wp-manga-chapter a", ".chapter-list li a", ".chapters li a",
            "ul.chapter-list a", "ul.row-content-chapter li a",
            ".listing-chapters_wrap li a", ".eph-num a", ".chbox a",
            "li.chapter a", "li[class*='chapter'] a", "a[href*='chapter']",
        ]

        best_num = 0
        best_url = None

        for sel in selectors:
            try:
                for a in soup.select(sel):
                    txt  = a.get_text(strip=True)
                    href = a.get("href", "")
                    m = (re.search(r"chapter[\s\-_#]?(\d+)", txt, re.I) or
                         re.search(r"ch[\s\-_.]?(\d+)", txt, re.I) or
                         re.search(r"chapter[\-_]?(\d+)", href, re.I))
                    if m:
                        num = int(m.group(1))
                        if num > best_num:
                            best_num = num
                            best_url = urljoin(start_url, href)
            except Exception:
                continue

        # Fallback: brute-force all links
        if not best_url:
            for a in soup.find_all("a", href=True):
                txt  = a.get_text(strip=True)
                href = a["href"]
                m = (re.search(r"chapter[\s\-_#]?(\d+)", txt, re.I) or
                     re.search(r"chapter[\-_\/]?(\d+)", href, re.I))
                if m:
                    num = int(m.group(1))
                    if num > best_num:
                        best_num = num
                        best_url = urljoin(start_url, href)

        if best_url:
            logger.debug(f"Chain repair: found Ch.{best_num} at {best_url}")
        return best_url, best_num

    except Exception as e:
        logger.error(f"Chain URL repair failed: {e}")
        return None, 0


def run_power_recovery(stale_diagnosed):
    """
    Post-crash staggered recovery: process stale watches in small batches
    with gaps between them to avoid overwhelming the scraper.

    Args:
        stale_diagnosed: List of diagnosed stale entries (already sorted by urgency)

    Returns:
        dict with recovery summary
    """
    cfg = get_config()
    recovery_cfg = cfg.get("recovery", {})

    if not recovery_cfg.get("enabled", True):
        logger.info("Power recovery is disabled in config")
        return {"skipped": True}

    batch_size = recovery_cfg.get("batch_size", 3)
    gap_mins   = recovery_cfg.get("batch_gap_minutes", 10)

    # Filter to only recoverable watches
    recoverable = [
        e for e in stale_diagnosed
        if e.get("diagnosis") not in (DIAG_SITE_BLOCKED, DIAG_SITE_DOWN)
    ]

    if not recoverable:
        logger.info("No recoverable watches found after power cut")
        return {"total": 0, "recovered": 0, "failed": 0}

    logger.info(f"Power recovery: {len(recoverable)} watches to process in batches of {batch_size}")
    state.add_daily_stat("power_recoveries")

    total     = len(recoverable)
    recovered = 0
    failed    = 0

    for i in range(0, total, batch_size):
        batch = recoverable[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (total + batch_size - 1) // batch_size

        logger.info(f"Recovery batch {batch_num}/{total_batches}: processing {len(batch)} watches...")

        # Check queue depth before adding more
        queue = health.get_queue_status()
        if queue and queue.get("queued", 0) >= 8:
            logger.warn(f"Queue already has {queue['queued']} items — pausing recovery until queue drains")
            time.sleep(gap_mins * 60)
            # Re-check
            queue = health.get_queue_status()
            if queue and queue.get("queued", 0) >= 8:
                logger.warn("Queue still full after waiting — stopping recovery for now")
                break

        for entry in batch:
            result = recover_watch(entry)
            if result.get("success"):
                recovered += 1
            elif result.get("action") != "skip":
                failed += 1

            # Small delay between individual triggers within a batch
            time.sleep(5)

        # Wait between batches (but not after the last one)
        if i + batch_size < total:
            logger.info(f"Waiting {gap_mins} minutes before next batch...")
            time.sleep(gap_mins * 60)

    summary = {
        "total": total,
        "recovered": recovered,
        "failed": failed,
        "skipped": total - recovered - failed,
    }
    logger.info(f"Power recovery complete: {recovered} triggered, {failed} failed, {summary['skipped']} skipped")
    return summary

"""
bot/state.py — Persist bot runtime state across restarts.

Tracks per-watch retry counts, crash history, last digest time, etc.
Stored in bot_state.json next to the bot.
"""

import json
import os
import time
import threading

_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bot_state.json",
)
_lock = threading.Lock()


def _load():
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save(state):
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Bot State] Error saving state: {e}")


def get(key, default=None):
    with _lock:
        return _load().get(key, default)


def put(key, value):
    with _lock:
        state = _load()
        state[key] = value
        _save(state)


def get_all():
    with _lock:
        return _load()


# ── Per-watch retry tracking ─────────────────────────────────────────────────

def get_retry_count(novel_id):
    """Get today's retry count for a watch."""
    with _lock:
        state = _load()
        retries = state.get("retries", {})
        entry = retries.get(novel_id, {})
        # Reset if the date has changed
        today = time.strftime("%Y-%m-%d")
        if entry.get("date") != today:
            return 0
        return entry.get("count", 0)


def increment_retry(novel_id):
    """Increment today's retry count for a watch. Returns new count."""
    with _lock:
        state = _load()
        retries = state.setdefault("retries", {})
        today = time.strftime("%Y-%m-%d")
        entry = retries.get(novel_id, {})
        if entry.get("date") != today:
            entry = {"date": today, "count": 0}
        entry["count"] += 1
        entry["last_retry"] = time.time()
        retries[novel_id] = entry
        _save(state)
        return entry["count"]


def get_last_retry_time(novel_id):
    """Get timestamp of last retry for a watch."""
    with _lock:
        state = _load()
        entry = state.get("retries", {}).get(novel_id, {})
        return entry.get("last_retry", 0)


# ── Crash / restart tracking ─────────────────────────────────────────────────

def record_startup():
    """Record that the bot started. Returns previous startup time (0 if first run)."""
    with _lock:
        state = _load()
        prev = state.get("last_startup", 0)
        state["last_startup"] = time.time()
        state["startup_count"] = state.get("startup_count", 0) + 1
        _save(state)
        return prev


def is_recovery_done():
    """Check if post-crash recovery has already been completed for this startup."""
    with _lock:
        state = _load()
        return state.get("recovery_done_for_startup") == state.get("last_startup")


def mark_recovery_done():
    """Mark that recovery for the current startup is complete."""
    with _lock:
        state = _load()
        state["recovery_done_for_startup"] = state.get("last_startup", 0)
        _save(state)


# ── Daily digest tracking ────────────────────────────────────────────────────

def get_last_digest_date():
    with _lock:
        return _load().get("last_digest_date", "")


def set_last_digest_date(date_str):
    put("last_digest_date", date_str)


# ── Stats accumulator (for daily digest) ─────────────────────────────────────

def add_daily_stat(key, increment=1):
    """Increment a daily stat counter (e.g. 'chapters_uploaded', 'retries_triggered')."""
    with _lock:
        state = _load()
        today = time.strftime("%Y-%m-%d")
        daily = state.setdefault("daily_stats", {})
        if daily.get("_date") != today:
            daily.clear()
            daily["_date"] = today
        daily[key] = daily.get(key, 0) + increment
        _save(state)


def get_daily_stats():
    with _lock:
        state = _load()
        daily = state.get("daily_stats", {})
        today = time.strftime("%Y-%m-%d")
        if daily.get("_date") != today:
            return {"_date": today}
        return dict(daily)

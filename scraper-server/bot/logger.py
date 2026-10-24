"""
bot/logger.py — Structured logging for the scraper bot.

Writes to:
  - stdout (for journalctl when running via systemd)
  - bot_logs/YYYY-MM-DD.jsonl (daily rotating JSON log)
"""

import json
import os
import time
import sys
from datetime import datetime

from bot.config import get_config

_log_dir = None


def _ensure_log_dir():
    global _log_dir
    if _log_dir is not None:
        return
    cfg = get_config()
    _log_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        cfg.get("log_dir", "bot_logs"),
    )
    os.makedirs(_log_dir, exist_ok=True)


def _log_file_path():
    _ensure_log_dir()
    return os.path.join(_log_dir, f"{time.strftime('%Y-%m-%d')}.jsonl")


# ── Severity levels ──────────────────────────────────────────────────────────

LEVEL_DEBUG = "debug"
LEVEL_INFO  = "info"
LEVEL_WARN  = "warn"
LEVEL_ERROR = "error"
LEVEL_ALERT = "alert"   # triggers notification

# Color codes for terminal output
_COLORS = {
    LEVEL_DEBUG: "\033[90m",      # grey
    LEVEL_INFO:  "\033[36m",      # cyan
    LEVEL_WARN:  "\033[33m",      # yellow
    LEVEL_ERROR: "\033[31m",      # red
    LEVEL_ALERT: "\033[1;31m",    # bold red
}
_RESET = "\033[0m"
_ICONS = {
    LEVEL_DEBUG: "·",
    LEVEL_INFO:  "→",
    LEVEL_WARN:  "⚠",
    LEVEL_ERROR: "✗",
    LEVEL_ALERT: "🔴",
}


def log(message, level=LEVEL_INFO, context=None):
    """
    Log a message to both stdout and the daily log file.

    Args:
        message:  Human-readable log message
        level:    One of debug/info/warn/error/alert
        context:  Optional dict with structured data (novel_id, job_id, etc.)
    """
    now = time.time()
    ts_human = datetime.fromtimestamp(now).strftime("%H:%M:%S")
    ts_iso   = datetime.fromtimestamp(now).isoformat()

    # Terminal output (colored)
    icon  = _ICONS.get(level, "·")
    color = _COLORS.get(level, "")
    print(f"{color}[{ts_human}] {icon} {message}{_RESET}", flush=True)

    # JSON log file
    entry = {
        "ts": ts_iso,
        "level": level,
        "msg": message,
    }
    if context:
        entry["ctx"] = context

    try:
        with open(_log_file_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Logger] Failed to write log: {e}", file=sys.stderr)


# ── Convenience aliases ──────────────────────────────────────────────────────

def debug(msg, **ctx):
    log(msg, LEVEL_DEBUG, ctx if ctx else None)

def info(msg, **ctx):
    log(msg, LEVEL_INFO, ctx if ctx else None)

def warn(msg, **ctx):
    log(msg, LEVEL_WARN, ctx if ctx else None)

def error(msg, **ctx):
    log(msg, LEVEL_ERROR, ctx if ctx else None)

def alert(msg, **ctx):
    """Error-level log that also triggers a notification."""
    log(msg, LEVEL_ALERT, ctx if ctx else None)

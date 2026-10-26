"""
bot/config.py — Load and validate bot configuration.

Secret precedence (highest first):
  1. Environment variables: KN_API_KEY, DISCORD_WEBHOOK, HEALTHCHECK_URL
  2. bot_config.json  (copy bot_config.example.json → bot_config.json to start)
  3. Built-in defaults
"""

import json
import os
import sys

DEFAULT_CONFIG = {
    "scraper_url": "http://localhost:7832",
    "kn_api_url": "http://localhost:3000",
    "kn_api_key": "",

    # How often the main loop ticks (seconds)
    "tick_interval_seconds": 60,

    # How often staleness checks run (minutes)
    "staleness_check_interval_minutes": 30,

    # A watch is considered stale if lastChecked > multiplier × intervalHours
    "staleness_threshold_multiplier": 2.0,

    # Power-cut recovery
    "recovery": {
        "enabled": True,
        "batch_size": 3,
        "batch_gap_minutes": 10,
        "restart_uptime_threshold_seconds": 600,
    },

    # Auto-retry failed / overdue watches
    "auto_retry": {
        "enabled": True,
        "max_retries_per_day": 3,
        "retry_delay_minutes": 15,
    },

    # v5: Auto-resume watches that were bot-paused (after repeated timeouts).
    # Checks that the source site is reachable before re-enabling.
    "auto_resume": {
        "enabled": True,
        "resume_after_hours": 6,    # hours to wait before trying
        "max_resume_attempts": 3,   # give up after N auto-resumes (needs manual review)
    },

    # v5: Per-domain circuit breaker — prevents burning retries on a site that
    # is rate-limiting or blocking.  Triggered by consecutive 403 responses
    # from the same domain within a single scrape job.
    "circuit_breaker": {
        "enabled": True,
        "failure_threshold": 3,     # consecutive 403s to trip the breaker
        "open_minutes": 30,         # minutes to stay open before auto-reset
    },

    # v5: Adaptive check intervals — slow down or stop watching novels that
    # haven't had new content in a long time, or are marked completed on KN.
    "adaptive_intervals": {
        "enabled": True,
        "inactive_days": 30,        # days since last new chapter → slow down
        "interval_multiplier": 2.0, # multiply intervalHours by this factor
        "max_interval_hours": 48,   # cap so it never exceeds this
        "stop_on_completed": True,  # deactivate watch when KN status == "completed"
    },

    # v5: Gap detection — compare chapter sequence on KN and report holes.
    # Automated backfill is NOT attempted (chain-mode URL resolution is risky).
    "gap_detection": {
        "enabled": True,
        "check_interval_hours": 24, # how often to run gap checks
        "alert_threshold": 1,       # only alert if >= this many chapters are missing
    },

    # Notifications
    "notifications": {
        "discord_webhook_url": "",
        "daily_digest_hour": 9,
    },

    # v5: Delete bot_logs/*.jsonl files older than this many days at startup
    "log_retention_days": 30,

    # Logging
    "log_dir": "bot_logs",

    # v5: Config hot-reload — re-read bot_config.json every N ticks (0 = off)
    "config_reload_ticks": 10,
}


_config = None


def load_config(config_path=None):
    """Load config from disk, merging with defaults."""
    global _config

    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bot_config.json",
        )

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
        except Exception as e:
            print(f"[Bot Config] Error reading {config_path}: {e}")
            user_config = {}
    else:
        user_config = {}
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
            print(f"[Bot Config] Created default config at {config_path}")
        except Exception as e:
            print(f"[Bot Config] Could not create config file: {e}")

    _config = _deep_merge(DEFAULT_CONFIG, user_config)

    # ── Override secrets from environment variables (highest priority) ────────
    env_kn_key  = os.environ.get("KN_API_KEY", "").strip()
    env_webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    env_hc_url  = os.environ.get("HEALTHCHECK_URL", "").strip()

    if env_kn_key:
        _config["kn_api_key"] = env_kn_key
    if env_webhook:
        _config.setdefault("notifications", {})["discord_webhook_url"] = env_webhook
    if env_hc_url:
        _config["healthcheck_url"] = env_hc_url

    # ── Validate critical fields ──────────────────────────────────────────────
    if not _config.get("kn_api_key"):
        print("[Bot Config] WARNING: kn_api_key is empty — KN API calls will fail.")
        print("             Set env var KN_API_KEY=<key> or edit bot_config.json.")

    if not _config.get("notifications", {}).get("discord_webhook_url"):
        print("[Bot Config] WARNING: discord_webhook_url is empty — no Discord alerts will be sent.")
        print("             Set env var DISCORD_WEBHOOK=<url> or edit bot_config.json.")

    return _config


def get_config():
    """Return the loaded config. Call load_config() first."""
    if _config is None:
        return load_config()
    return _config


def _deep_merge(defaults, overrides):
    """Recursively merge overrides into defaults."""
    result = dict(defaults)
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result

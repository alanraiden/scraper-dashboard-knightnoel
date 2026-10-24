"""
bot/config.py — Load and validate bot configuration.
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
        # Feed stale watches in batches after a detected restart
        "batch_size": 3,
        # Minutes to wait between recovery batches
        "batch_gap_minutes": 10,
        # Server uptime (seconds) below which we consider it a fresh restart
        "restart_uptime_threshold_seconds": 600,
    },

    # Auto-retry
    "auto_retry": {
        "enabled": True,
        # Max retries per watch per day
        "max_retries_per_day": 3,
        # Minutes to wait before retrying a failed watch
        "retry_delay_minutes": 15,
    },

    # Notifications
    "notifications": {
        "discord_webhook_url": "",
        # Send daily digest at this hour (24h format, server local time)
        "daily_digest_hour": 9,
    },

    # Logging
    "log_dir": "bot_logs",
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
        # Create default config file so user can edit it
        user_config = {}
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
            print(f"[Bot Config] Created default config at {config_path}")
        except Exception as e:
            print(f"[Bot Config] Could not create config file: {e}")

    _config = _deep_merge(DEFAULT_CONFIG, user_config)

    # Validate critical fields
    if not _config.get("kn_api_key"):
        print("[Bot Config] WARNING: kn_api_key is empty — KN API calls will fail.")
        print("             Edit bot_config.json and set your scraper API key.")

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

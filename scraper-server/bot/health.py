"""
bot/health.py — Health checks for the scraper server and KN API.

Returns structured results that the main loop uses to decide actions.
"""

import time
import requests

from bot.config import get_config
from bot import logger

# Track consecutive failures for escalation
_consecutive_scraper_failures = 0
_consecutive_kn_failures      = 0


def check_scraper_server():
    """
    Check if the scraper server is alive and responsive.
    Returns dict with status info.
    """
    global _consecutive_scraper_failures
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/health"

    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            data = r.json()
            _consecutive_scraper_failures = 0
            return {
                "alive": True,
                "active_jobs": data.get("active_jobs", 0),
                "queued_jobs": data.get("queued_jobs", 0),
                "concurrency": data.get("concurrency", 1),
            }
        else:
            _consecutive_scraper_failures += 1
            return {"alive": False, "error": f"HTTP {r.status_code}", "consecutive_failures": _consecutive_scraper_failures}
    except requests.ConnectionError:
        _consecutive_scraper_failures += 1
        return {"alive": False, "error": "Connection refused", "consecutive_failures": _consecutive_scraper_failures}
    except requests.Timeout:
        _consecutive_scraper_failures += 1
        return {"alive": False, "error": "Timeout (10s)", "consecutive_failures": _consecutive_scraper_failures}
    except Exception as e:
        _consecutive_scraper_failures += 1
        return {"alive": False, "error": str(e), "consecutive_failures": _consecutive_scraper_failures}


def check_kn_api():
    """
    Check if the Knight Novel API is alive.
    Returns dict with status info.
    """
    global _consecutive_kn_failures
    cfg = get_config()

    if not cfg.get("kn_api_key"):
        return {"alive": None, "error": "No API key configured"}

    url  = f"{cfg['kn_api_url'].rstrip('/')}/api/scraper/novels"
    hdrs = {"x-scraper-key": cfg["kn_api_key"]}

    try:
        r = requests.get(url, headers=hdrs, timeout=15, params={"limit": 1})
        if r.ok:
            _consecutive_kn_failures = 0
            data = r.json()
            return {
                "alive": True,
                "total_novels": data.get("total", 0),
            }
        else:
            _consecutive_kn_failures += 1
            return {"alive": False, "error": f"HTTP {r.status_code}", "consecutive_failures": _consecutive_kn_failures}
    except requests.ConnectionError:
        _consecutive_kn_failures += 1
        return {"alive": False, "error": "Connection refused", "consecutive_failures": _consecutive_kn_failures}
    except requests.Timeout:
        _consecutive_kn_failures += 1
        return {"alive": False, "error": "Timeout (15s)", "consecutive_failures": _consecutive_kn_failures}
    except Exception as e:
        _consecutive_kn_failures += 1
        return {"alive": False, "error": str(e), "consecutive_failures": _consecutive_kn_failures}


def get_queue_status():
    """Get the current job queue status from the scraper server."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/queue"
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def get_watches():
    """Fetch all watches from the scraper server."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/watches"
    try:
        r = requests.get(url, timeout=15)
        if r.ok:
            return r.json().get("watches", [])
    except Exception as e:
        logger.error(f"Failed to fetch watches: {e}")
    return None


def get_job_status(job_id):
    """Fetch status of a specific job."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/jobs/{job_id}"
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def get_all_jobs():
    """Fetch all jobs from the scraper server."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/jobs"
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            data = r.json()
            return data.get("jobs", data)
    except Exception:
        pass
    return None


def trigger_watch(novel_id):
    """Trigger an immediate watch check. Returns job_id or None."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/watches/{novel_id}/run"
    try:
        r = requests.post(url, timeout=15)
        if r.ok:
            data = r.json()
            return data.get("job_id")
        else:
            data = r.json()
            logger.warn(f"Failed to trigger watch {novel_id}: {data.get('error', f'HTTP {r.status_code}')}")
    except Exception as e:
        logger.error(f"Error triggering watch {novel_id}: {e}")
    return None


def patch_watch(novel_id, updates):
    """Update watch fields (lastChapter, lastChecked, lastChapterUrl, active)."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/watches/{novel_id}"
    try:
        r = requests.patch(url, json=updates, timeout=10)
        return r.ok
    except Exception as e:
        logger.error(f"Error patching watch {novel_id}: {e}")
        return False


def get_kn_novel(slug):
    """Fetch novel details from the KN API by slug."""
    cfg = get_config()
    if not cfg.get("kn_api_key"):
        return None
    url  = f"{cfg['kn_api_url'].rstrip('/')}/api/scraper/novels/{slug}"
    hdrs = {"x-scraper-key": cfg["kn_api_key"]}
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def check_source_url(url):
    """
    Quick HEAD/GET check on a source novel URL to see if it's reachable.
    Returns: (status_code, is_blocked, error_msg)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        r = requests.head(url, headers=headers, timeout=15, allow_redirects=True)
        is_blocked = r.status_code == 403
        return r.status_code, is_blocked, None
    except requests.Timeout:
        return None, False, "Timeout"
    except requests.ConnectionError:
        return None, False, "Connection refused"
    except Exception as e:
        return None, False, str(e)

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


def is_watch_already_queued(novel_id, novel_slug=None):
    """
    Check if a watch already has a job running or queued in the scraper server.
    This prevents the bot from trying to trigger watches the scheduler already handles.

    The queue stores novel_slug (e.g. 'the-failed-swordmaster') while watches
    are keyed by novel_id (MongoDB ObjectId). We check both.
    """
    identifiers = {novel_id}
    if novel_slug:
        identifiers.add(novel_slug)

    # Check the queue endpoint first (cheaper)
    queue = get_queue_status()
    if queue:
        for item in queue.get("queue", []):
            if item.get("novel") in identifiers:
                return True, f"already queued ({item.get('novel')})"

    # Check running jobs
    all_jobs = get_all_jobs()
    if all_jobs and isinstance(all_jobs, dict):
        for job_id, job_data in all_jobs.items():
            job_ids = set()
            if job_data.get("novel_slug"):
                job_ids.add(job_data["novel_slug"])
            if job_data.get("novel_id"):
                job_ids.add(job_data["novel_id"])
            status = job_data.get("status", "")
            if identifiers & job_ids and status in ("running", "starting", "queued", "pending"):
                return True, f"job {job_id} is {status}"

    return False, None


def trigger_watch(novel_id):
    """
    Trigger an immediate watch check.
    Returns (job_id_or_None, error_reason_or_None).
    """
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/watches/{novel_id}/run"
    try:
        r = requests.post(url, timeout=15)
        if r.ok:
            data = r.json()
            return data.get("job_id"), None
        else:
            try:
                data = r.json()
                reason = data.get("error", f"HTTP {r.status_code}")
            except Exception:
                reason = f"HTTP {r.status_code}"
            return None, reason
    except Exception as e:
        return None, str(e)


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


def get_kn_chapters(slug):
    """Fetch the chapter list for a novel from the KN API (used for gap detection)."""
    cfg = get_config()
    if not cfg.get("kn_api_key"):
        return None
    url  = f"{cfg['kn_api_url'].rstrip('/')}/api/scraper/novels/{slug}/chapters"
    hdrs = {"x-scraper-key": cfg["kn_api_key"]}
    try:
        r = requests.get(url, headers=hdrs, timeout=30)
        if r.ok:
            data = r.json()
            # Accept both {chapters: [...]} and a direct list
            return data.get("chapters", data) if isinstance(data, dict) else data
    except Exception as e:
        logger.error(f"Failed to fetch chapters for {slug}: {e}")
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


def cancel_job(job_id):
    """Cancel a running or queued job. Returns True if successful."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/jobs/{job_id}"
    try:
        r = requests.delete(url, timeout=10)
        return r.ok
    except Exception as e:
        logger.error(f"Error cancelling job {job_id}: {e}")
        return False


def get_concurrency():
    """Get current concurrency config from the scraper server."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/config"
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def set_concurrency(concurrency):
    """Update the scraper server's concurrency limit. Returns True if successful."""
    cfg = get_config()
    url = f"{cfg['scraper_url'].rstrip('/')}/config"
    try:
        r = requests.patch(url, json={"concurrency": concurrency}, timeout=10)
        return r.ok
    except Exception as e:
        logger.error(f"Error setting concurrency to {concurrency}: {e}")
        return False


def check_internet():
    """
    Quick connectivity check. Returns True if the server has internet access.
    Used before recovery to avoid misdiagnosing sites as 'blocked' when
    the router hasn't come back yet after a power cut.
    """
    test_urls = [
        "https://1.1.1.1",
        "https://dns.google",
        "https://www.google.com",
    ]
    for url in test_urls:
        try:
            r = requests.head(url, timeout=5)
            if r.status_code < 500:
                return True
        except Exception:
            continue
    return False


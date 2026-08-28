"""
scraper_server.py — Local Scraper API Server (v3 — adapter architecture)
=========================================================================
Runs on http://0.0.0.0:7832  (LAN-accessible)
The React dashboard connects to this to run scraping jobs server-side.

Start it with:
    python scraper_server.py

Requirements:
    pip install flask requests beautifulsoup4 lxml flask-cors
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import threading, time, re, os, json, uuid
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# -- Adapter system -----------------------------------------------------------
from adapters import registry
from adapters.utils import (
    clean_text, strip_watermarks, infer_chapter_number,
    find_next_url_generic, find_prev_url_generic,
    infer_next_url_from_pattern, CONTENT_SELECTORS,
    is_junk_page,
)

registry.load()

# Gemini cleaner removed — not in use

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=False)

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    return response

PORT = 7832

# -- Active jobs store --------------------------------------------------------
jobs      = {}
jobs_lock = threading.Lock()

# -- Concurrency-limited job queue --------------------------------------------
# Only WATCH_CONCURRENCY jobs run at the same time; the rest wait in job_queue.
# Can be changed at runtime via PATCH /config without restarting the server.
WATCH_CONCURRENCY = 1          # default — 1 is safest for low-spec servers; frontend slider overrides
_concurrency_lock  = threading.Lock()
_semaphore         = threading.Semaphore(WATCH_CONCURRENCY)  # real gate
job_queue          = []         # list of (job_id, params) waiting to run
job_queue_lock     = threading.Lock()

# -- Stagger delay ------------------------------------------------------------
# Seconds to sleep BEFORE starting each new job from the queue (after a slot
# becomes free). Spreads server load over time. 0 = no delay.
# Can be changed at runtime via PATCH /config.
STAGGER_DELAY      = 0.0       # seconds (0 = off)


def _dispatch_loop():
    """Background thread: pulls jobs off job_queue as semaphore slots free up."""
    while True:
        with job_queue_lock:
            pending = job_queue[:]
        if not pending:
            time.sleep(0.2)
            continue
        # Try to acquire a slot (non-blocking peek so we don't hold the lock)
        acquired = _semaphore.acquire(blocking=False)
        if not acquired:
            time.sleep(0.3)
            continue
        # Got a slot — pop the first job and run it
        with job_queue_lock:
            if not job_queue:
                _semaphore.release()   # nothing to run, release immediately
                continue
            job_id, params = job_queue.pop(0)
        with jobs_lock:
            if jobs.get(job_id, {}).get("status") == "cancelled":
                _semaphore.release()   # cancelled while queued — skip it
                continue
            jobs[job_id]["status"] = "starting"

        # ── Stagger delay: sleep before actually launching the thread ──────
        stagger = STAGGER_DELAY
        if stagger > 0:
            time.sleep(stagger)
            # Re-check cancellation after sleeping
            with jobs_lock:
                if jobs.get(job_id, {}).get("status") == "cancelled":
                    _semaphore.release()
                    continue

        def _run(jid=job_id, p=params):
            try:
                run_scrape_job(jid, p)
            finally:
                _semaphore.release()   # always free the slot when done

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["thread"] = t


# Start dispatcher once at import time
_dispatcher = threading.Thread(target=_dispatch_loop, daemon=True, name="job-dispatcher")
_dispatcher.start()


# -- Watches store (server-side 24/7 watcher) ----------------------------------
_WATCHES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watches.json")
_watches_lock = threading.Lock()

def _load_watches():
    """Load watches from disk. Returns dict keyed by novelId."""
    try:
        if os.path.exists(_WATCHES_FILE):
            with open(_WATCHES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_watches(watches):
    """Persist watches to disk."""
    with open(_WATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(watches, f, indent=2, ensure_ascii=False)

# -- Scraper config -----------------------------------------------------------
DEFAULT_DELAY      = 1.2
MAX_CONSECUTIVE_SKIPS = 5   # stop crawl after this many locked/empty chapters in a row
MAX_CHAPTERS       = 2000
# Minimum word count for a page to be considered real chapter content.
# Pages below this threshold (paywall walls, login pages, UI chrome, nav pages)
# are skipped. Can be overridden per-job via the "min_words" param.
MIN_CONTENT_WORDS  = 150
# Number of chapters to accumulate before sending a bulk upload request.
# Larger = fewer HTTP round trips; smaller = more frequent progress updates.
BATCH_SIZE         = 5   # chapters per bulk upload (smaller = more frequent progress + less chance of timeout)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# =============================================================================
#  PAGE FETCH
# =============================================================================

def fetch_page(url, session, log_fn=None):
    max_retries = 3
    backoff     = [5, 15, 30]   # seconds to wait before each retry

    for attempt in range(max_retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            if r.status_code in (502, 503, 504) and attempt < max_retries:
                wait = backoff[attempt]
                if log_fn:
                    log_fn(f"HTTP {r.status_code} on attempt {attempt+1} — retrying in {wait}s", "warn")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml"), r.text
        except requests.RequestException as e:
            if attempt < max_retries and any(
                x in str(e) for x in ("502", "503", "504", "timed out", "Connection")
            ):
                wait = backoff[attempt]
                if log_fn:
                    log_fn(f"Fetch error attempt {attempt+1} — retrying in {wait}s: {e}", "warn")
                time.sleep(wait)
                continue
            if log_fn:
                log_fn(f"Fetch failed: {url} -- {e}", "err")
            return None, None

    if log_fn:
        log_fn(f"Fetch failed after {max_retries} retries: {url}", "err")
    return None, None


# =============================================================================
#  CORE EXTRACTION  (adapter-aware)
# =============================================================================

def extract_content(soup, html, url, session, log_fn=None):
    # -- Quick selector pass (fast path for simple static sites) --------------
    soup2 = BeautifulSoup(html, "lxml")
    for tag in ["script", "style", "nav", "header", "footer", "aside",
                "figure", "figcaption", "iframe", "ins", "noscript", "form", "button"]:
        for el in soup2.find_all(tag):
            el.decompose()

    for sel in CONTENT_SELECTORS:
        try:
            block = soup2.select_one(sel)
            if block:
                text = clean_text(block.get_text(separator="\n"))
                if len(text) > 200:
                    return strip_watermarks(text)
        except Exception:
            continue

    divs = soup2.find_all("div")
    if divs:
        biggest = max(divs, key=lambda d: len(d.get_text()))
        text    = clean_text(biggest.get_text(separator="\n"))
        if len(text) > 200:
            return strip_watermarks(text)

    if not html:
        return ""

    # -- Adapter dispatch ------------------------------------------------------
    adapter = registry.get_best(url, html)
    if adapter:
        if log_fn:
            log_fn(f"[{adapter.name}] adapter matched", "info")
        result = adapter.extract_content(soup, html, url, session, log_fn)
        if result:
            return result
        if log_fn:
            log_fn(f"[{adapter.name}] returned no content -- falling back", "warn")

    from adapters.generic import GenericAdapter
    return GenericAdapter().extract_content(soup, html, url, session, log_fn) or ""


def find_next_url(soup, current_url, html="", log_fn=None):
    adapter = registry.get_best(current_url, html) if html else None
    if adapter:
        result = adapter.find_next_url(soup, current_url, html, log_fn)
        if result:
            return result
    result = find_next_url_generic(soup, current_url)
    if result:
        return result
    return infer_next_url_from_pattern(current_url, soup, log_fn)


def find_prev_url(soup, current_url, html="", log_fn=None):
    adapter = registry.get_best(current_url, html) if html else None
    if adapter:
        result = adapter.find_prev_url(soup, current_url, html, log_fn)
        if result:
            return result
    return find_prev_url_generic(soup, current_url)


def extract_title(soup, fallback_num, url="", html=""):
    adapter = registry.get_best(url, html) if html else None
    if adapter:
        result = adapter.extract_title(soup, fallback_num)
        if result:
            return result
    title_tag = soup.find("title")
    if title_tag:
        raw = clean_text(title_tag.get_text(strip=True))
        if raw and len(raw) > 2:
            return raw
    for tag in [soup.find("h1"), soup.find("h2"),
                soup.find(class_=re.compile(r"chapter[_-]?title|entry-title", re.I))]:
        if tag and tag.get_text(strip=True):
            return clean_text(tag.get_text(strip=True))
    return f"Chapter {fallback_num}"


# =============================================================================
#  CHAPTER DETECTION
# =============================================================================

def detect_latest_chapter(index_url, check_selector, session, log_fn=None):
    soup, html = fetch_page(index_url, session, log_fn)
    if soup is None:
        return 0, ""

    if html:
        adapter = registry.get_best(index_url, html)
        if adapter:
            result = adapter.detect_latest_chapter(index_url, check_selector, session, log_fn)
            if result:
                return result

    if check_selector:
        try:
            el = soup.select_one(check_selector)
            if el:
                m = re.search(r"(\d+)", el.get_text())
                if m:
                    return int(m.group(1)), urljoin(index_url, el.get("href", ""))
        except Exception:
            pass

    for sel in [".wp-manga-chapter a", ".chapter-list li a", ".chapters li a",
                "ul.chapter-list a", "ul.row-content-chapter li a",
                ".listing-chapters_wrap li a", ".eph-num a", ".chbox a",
                "li.chapter a", "li[class*='chapter'] a", "a[href*='chapter']"]:
        try:
            links      = soup.select(sel)
            candidates = []
            for a in links:
                txt  = a.get_text(strip=True)
                href = a.get("href", "")
                m    = (re.search(r"chapter[\s\-_#]?(\d+)", txt, re.I) or
                        re.search(r"ch[\s\-_.]?(\d+)", txt, re.I) or
                        re.search(r"chapter[\-_]?(\d+)", href, re.I))
                if m:
                    candidates.append((int(m.group(1)), urljoin(index_url, href)))
            if candidates:
                best = max(candidates, key=lambda x: x[0])
                if log_fn:
                    log_fn(f"Detected latest via '{sel}': Ch.{best[0]}", "dim")
                return best
        except Exception:
            continue

    candidates = []
    for a in soup.find_all("a", href=True):
        txt  = a.get_text(strip=True)
        href = a.get("href", "")
        m    = (re.search(r"chapter[\s\-_#]?(\d+)", txt, re.I) or
                re.search(r"ch[\s\-_.]?(\d+)", txt, re.I) or
                re.search(r"chapter[\-_\/]?(\d+)", href, re.I))
        if m:
            candidates.append((int(m.group(1)), urljoin(index_url, href)))
    if candidates:
        best = max(candidates, key=lambda x: x[0])
        if log_fn:
            log_fn(f"Detected latest (brute-force): Ch.{best[0]}", "dim")
        return best

    if log_fn:
        log_fn("Could not auto-detect latest chapter number", "warn")
    return 0, ""


# =============================================================================
#  UPLOAD
# =============================================================================

def nextauth_login(session, api_url, email, password, log_fn=None):
    """
    Logs into a NextAuth-based site (email/password credentials provider,
    e.g. Knight Novel's admin) and leaves the resulting session cookie on
    `session` for subsequent requests. Session cookies are per-domain, so
    reusing the same `requests.Session` used for scraping the source site
    is safe — it won't leak cookies across domains.

    NextAuth's credentials callback always responds 200/302 with
    `json: "true"`, whether login succeeded or failed, so success is
    checked by looking for the session-token cookie it sets rather than
    the status code.
    """
    base = api_url.rstrip("/")
    try:
        csrf_resp = session.get(f"{base}/api/auth/csrf", timeout=15)
        csrf_resp.raise_for_status()
        csrf_token = csrf_resp.json().get("csrfToken")
        if not csrf_token:
            if log_fn:
                log_fn("Login failed: could not fetch CSRF token from /api/auth/csrf", "err")
            return False

        session.post(
            f"{base}/api/auth/callback/credentials",
            data={"csrfToken": csrf_token, "email": email, "password": password, "json": "true"},
            timeout=15,
            allow_redirects=False,
        )
        has_session_cookie = any(
            "next-auth.session-token" in name or "__Secure-next-auth.session-token" in name
            for name in session.cookies.keys()
        )
        if has_session_cookie:
            if log_fn:
                log_fn("Logged in to Knight Novel admin session.", "ok")
            return True
        if log_fn:
            log_fn("Login failed — check the admin email/password (and that the account has role: admin).", "err")
        return False
    except Exception as e:
        if log_fn:
            log_fn(f"Login error: {e}", "err")
        return False


def upload_chapters_knight_novel(novel_slug, chapters, api_url, session, log_fn=None):
    """
    Push one or more chapters to a Knight Novel-style admin endpoint:
        POST {api_url}/api/admin/novels/{slug}/chapters
        body: {chapterNumber, title, content}  -- or an array of those
    The route computes slug/wordCount/status/timestamps itself and upserts
    on (novelId, chapterNumber), so there's no separate bulk endpoint and
    no client-side duplicate handling needed -- every accepted chapter
    counts as "added" whether it was new or an update.
    Returns (added_count, errors_list, auth_expired: bool)
    """
    url = f"{api_url.rstrip('/')}/api/admin/novels/{novel_slug}/chapters"
    payload = [
        {"chapterNumber": c["number"], "title": c["title"], "content": c["content"]}
        for c in chapters
    ]
    body = payload[0] if len(payload) == 1 else payload
    try:
        r = session.post(url, json=body, timeout=120)
        if r.status_code == 403:
            return 0, [{"reason": "403 Forbidden -- admin session expired or not an admin account"}], True
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.ok:
            added = data.get("added", len(payload))
            if log_fn:
                log_fn(f"Uploaded {added} chapter(s) -- novel now has {data.get('totalChapters', '?')} total", "ok")
            return added, [], False
        err = data.get("error", f"HTTP {r.status_code}")
        if log_fn:
            log_fn(f"Upload failed: {err}", "err")
        return 0, [{"reason": err}], False
    except Exception as e:
        if log_fn:
            log_fn(f"Upload error: {e}", "err")
        return 0, [{"reason": str(e)}], False


def upload_chapter(novel_slug, chapter, api_url, token, log_fn=None):
    """Upload a single chapter via the scraper API bridge. Used as fallback."""
    url  = f"{api_url.rstrip('/')}/api/scraper/novels/{novel_slug}/chapters"
    hdrs = {"Content-Type": "application/json", "x-scraper-key": token}
    try:
        r    = requests.post(url, headers=hdrs, json={"chapters": [chapter], "skipDuplicates": False}, timeout=30)
        data = r.json()
        if r.ok:
            created = data.get("created", 0)
            skipped = data.get("skipped", 0)
            if created > 0:
                if log_fn:
                    log_fn(f"Uploaded Ch.{chapter['number']} -- {chapter['title']}", "ok")
                return True, None
            if skipped > 0:
                if log_fn:
                    log_fn(f"~ Ch.{chapter['number']} already exists, skipped", "dim")
                return True, "exists"
        err = data.get("error", f"HTTP {r.status_code}")
        if log_fn:
            log_fn(f"Ch.{chapter['number']} failed: {err}", "err")
        return False, err
    except Exception as e:
        if log_fn:
            log_fn(f"Ch.{chapter['number']} upload error: {e}", "err")
        return False, str(e)


def _fetch_max_stored_chapter(api_url, novel_slug, token):
    """Query the KN API for all stored chapters and return the highest chapter number.
    Returns 0 if none are stored, or if the request fails (fail-safe — crawl from start)."""
    if not api_url or not novel_slug:
        return 0
    try:
        url = f"{api_url.rstrip('/')}/api/scraper/novels/{novel_slug}/chapters"
        headers = {"x-scraper-key": token} if token else {}
        r = requests.get(url, headers=headers, timeout=15)
        if not r.ok:
            return 0
        data = r.json()
        # The endpoint may return a list directly or {chapters: [...]}
        chapters = data if isinstance(data, list) else data.get("chapters", [])
        if not chapters:
            return 0
        numbers = [int(c.get("number", 0)) for c in chapters if c.get("number")]
        return max(numbers) if numbers else 0
    except Exception:
        return 0


def bulk_upload_chapters(novel_slug, chapters, api_url, token, log_fn=None):
    """Upload multiple chapters via the KN scraper API bridge.
    POST /api/scraper/novels/:slug/chapters  (existing compiled route)
    Header: x-scraper-key: <token>
    Body: { chapters: [{number, title, content}], skipDuplicates: true }
    Returns: (created, skipped, errors_list)
    """
    url  = f"{api_url.rstrip('/')}/api/scraper/novels/{novel_slug}/chapters"
    hdrs = {"Content-Type": "application/json", "x-scraper-key": token}
    try:
        r = requests.post(url, headers=hdrs,
                          json={"chapters": chapters, "skipDuplicates": True},
                          timeout=120)
        # --- safe JSON parse: KN dev server can return empty body on first
        # compile of a new route, or on a 5xx crash. Log exactly what arrived.
        try:
            data = r.json()
        except ValueError:
            body_preview = r.text[:300].strip() if r.text else "(empty body)"
            msg = f"Bulk upload failed: HTTP {r.status_code} — non-JSON response: {body_preview!r}"
            if log_fn:
                log_fn(msg, "err")
                log_fn("Tip: restart your KN dev server (Ctrl-C → npm run dev) then retry", "warn")
            return 0, 0, [{"reason": msg}]

        if r.ok:
            created = data.get("created", 0)
            skipped = data.get("skipped", 0)
            errors  = data.get("errors", [])
            if log_fn:
                log_fn(f"Bulk upload: {created} uploaded, {skipped} already existed, {len(errors)} errors", "ok")
                for e in errors:
                    log_fn(f"  Ch.{e.get('number','?')} error: {e.get('reason','?')}", "err")
            return created, skipped, errors

        err = data.get("error", f"HTTP {r.status_code}")
        if log_fn:
            log_fn(f"Bulk upload failed: {err}", "err")
            if "not found" in err.lower():
                log_fn(f"  → The novel slug '{novel_slug}' was not found in the database.", "err")
                log_fn(f"  → Go to Knight Novel admin and CREATE the novel first, then retry.", "warn")
        return 0, 0, [{"reason": err}]
    except requests.Timeout:
        msg = f"Bulk upload timed out after 120s for {len(chapters)} chapters"
        if log_fn:
            log_fn(msg, "err")
        return 0, 0, [{"reason": msg}]
    except Exception as e:
        if log_fn:
            log_fn(f"Bulk upload error: {e}", "err")
        return 0, 0, [{"reason": str(e)}]



# =============================================================================
#  JOB RUNNER
# =============================================================================

def run_scrape_job(job_id, params):
    def update(patch):
        with jobs_lock:
            jobs[job_id].update(patch)

    def log(msg, type_="info"):
        with jobs_lock:
            jobs[job_id]["logs"].append({
                "msg":  msg,
                "type": type_,
                "ts":   int(time.time() * 1000),
            })

    update({"status": "running", "logs": [], "stats": {"scraped": 0, "uploaded": 0, "skipped": 0, "errors": 0}, "started_at": time.time()})

    mode          = params.get("mode", "scrape")
    start_url     = params["start_url"]
    api_url       = params["api_url"]
    # novel_slug: KN slug used by the scraper API bridge (e.g. "shadow-slave")
    # novel_id is a MongoDB ObjectId hex string — NOT usable as a slug.
    # Always prefer novel_slug; only fall back to novel_id as a last resort.
    novel_slug    = params.get("novel_slug") or ""
    novel_id      = params.get("novel_id") or ""
    if not novel_slug and novel_id:
        # Caller did not provide a slug — use the _id as the URL segment (will
        # work ONLY if the KN route accepts _id lookups, which it doesn't by
        # default, so this is likely to fail with "Novel not found").
        log(f"WARNING: no novel_slug provided — falling back to novel_id '{novel_id}' as slug.", "warn")
        log("  → Open the scraper dashboard, find the novel, and confirm it has a slug set.", "warn")
        novel_slug = novel_id
    token         = params.get("token", "")
    from_chapter  = int(params.get("from_chapter", 0))
    index_offset  = int(params.get("index_offset", 0))  # chain mode: index starts here not at 1
    delay         = float(params.get("delay", DEFAULT_DELAY))
    max_chapters  = int(params.get("max_chapters", MAX_CHAPTERS))
    min_words     = int(params.get("min_words", MIN_CONTENT_WORDS))

    log(f"Upload target: {api_url.rstrip('/')}/api/scraper/novels/{novel_slug}/chapters", "dim")

    session = requests.Session()
    stats   = {"scraped": 0, "uploaded": 0, "skipped": 0, "errors": 0}

    def push_stats():
        update({"stats": dict(stats)})

    def push_batch(batch):
        """Upload a batch via the KN scraper API bridge (x-scraper-key, slug-based)."""
        created, _, errs = bulk_upload_chapters(novel_slug, batch, api_url, token, log)
        return created, errs

    try:
        chapter_one_url   = params.get("chapter_one_url") or start_url
        custom_watermarks = params.get("custom_watermarks") or []

        def _normalise(entry):
            if isinstance(entry, str):
                return {"phrase": entry, "titleReplace": None}
            return entry

        custom_watermarks = [_normalise(e) for e in custom_watermarks if e]
        custom_entries    = []
        for e in custom_watermarks:
            ph = (e.get("phrase") or "").strip()
            if not ph:
                continue
            custom_entries.append({
                "pattern":      re.compile(re.escape(ph), re.I),
                "titleReplace": e.get("titleReplace") or "",
            })

        extra_junk = [ce["pattern"] for ce in custom_entries]

        def strip_watermarks_job(text):
            return strip_watermarks(text, extra_patterns=extra_junk)

        def clean_title(title):
            t = title
            for ce in custom_entries:
                t = ce["pattern"].sub(ce["titleReplace"], t)
            t = re.sub(r"[ \t]{2,}", " ", t)
            t = re.sub(r"^\s*[-\u2013\u2014|:,. ]+", "", t)   # leading separators incl. en/em dash
            t = re.sub(r"[-\u2013\u2014|:,. ]+\s*$", "", t)   # trailing separators
            return t.strip() or title

        if mode == "watch_check":
            # Query the KN backend for the real highest stored chapter so we never
            # re-crawl from Ch.1. Overrides whatever from_chapter was passed in.
            # Gaps (deleted chapters) are handled by the separate gap-detection feature.
            api_max = _fetch_max_stored_chapter(api_url, novel_slug, token)
            if api_max > from_chapter:
                log(f"KN API reports {api_max} chapters stored — starting after Ch.{api_max}", "dim")
                from_chapter  = api_max
                index_offset  = api_max   # chain mode: keep index numbering aligned
            log(f"-- Watch check started (stored up to Ch.{from_chapter}) --", "info")

        log(f"Starting forward crawl from: {chapter_one_url}", "info")
        upload_batch = []   # chapters accumulated for bulk upload
        # Reset any per-job adapter state (e.g. consecutive-skip counters)
        for adapter in registry.all():
            if hasattr(adapter, "reset_state"):
                adapter.reset_state()
        url     = chapter_one_url
        index   = index_offset + 1  # chain mode: start index at the right offset so infer_chapter_number fallback is correct
        visited = set()
        consecutive_skips = 0
        last_real_chapter_url = None  # tracks highest chapter URL for chain mode

        while url and stats["scraped"] < max_chapters:
            # Check if job was cancelled or paused
            with jobs_lock:
                job_status = jobs.get(job_id, {}).get("status")
                if job_status == "cancelled":
                    log("Job cancelled by user.", "warn")
                    break
                if job_status == "pause_requested":
                    # Save checkpoint so the job can be resumed later
                    jobs[job_id]["checkpoint"] = {
                        "from_chapter": max(stats.get("uploaded", 0), from_chapter),
                        "last_url":     url,
                    }
                    jobs[job_id]["status"] = "paused"
                    log(f"Job paused at Ch.~{jobs[job_id]['checkpoint']['from_chapter']} — will resume from {url}", "warn")
                    # Flush any pending batch before pausing
                    if upload_batch:
                        created, errs = push_batch(upload_batch)
                        stats["uploaded"] += created
                        stats["errors"]   += len(errs)
                        upload_batch.clear()
                        push_stats()
                    update({"stats": dict(stats)})
                    return  # exit run_scrape_job; semaphore released by _dispatch_loop's finally

            if url in visited:
                break
            visited.add(url)
            log(f"[{index}] {url}", "info")

            soup, html = fetch_page(url, session, log)
            if soup is None:
                break

            title   = clean_title(extract_title(soup, index, url, html or ""))
            ch_num  = infer_chapter_number(title, index)
            content = strip_watermarks_job(extract_content(soup, html, url, session, log) or "")



            if ch_num > from_chapter:
                stats["scraped"] += 1
                last_real_chapter_url = url  # update on every new chapter seen
                wc = len((content or "").split())

                # Check for paywall pages, UI chrome, login walls, and
                # genuinely short pages before uploading
                junk, reason = is_junk_page(content or "", min_words=min_words)
                if junk:
                    log(f"Ch.{ch_num} -- {title} skipped: {reason}", "warn")
                    stats["skipped"] += 1
                    consecutive_skips += 1
                    if consecutive_skips >= MAX_CONSECUTIVE_SKIPS:
                        log(f"-- {consecutive_skips} consecutive locked/empty chapters -- stopping crawl --", "warn")
                        break
                    push_stats()
                else:
                    consecutive_skips = 0
                    log(f"Ch.{ch_num} -- {title} ({wc}w)", "ok")
                    # Accumulate into batch — flush every BATCH_SIZE or at end
                    upload_batch.append({"number": ch_num, "title": title, "content": content})
                    if len(upload_batch) >= BATCH_SIZE:
                        created, errs = push_batch(upload_batch)
                        stats["uploaded"] += created
                        stats["errors"]   += len(errs)
                        upload_batch.clear()
                    push_stats()
            else:
                log(f"~ Ch.{ch_num} already stored, skipping", "dim")
                consecutive_skips = 0  # already-stored is not a lock skip

            next_url = find_next_url(soup, url, html or "", log)
            # Strip fragment-only URLs like chapter-237/#respond — these are comment anchors not chapters
            if next_url and '#' in next_url:
                base = next_url.split('#')[0].rstrip('/')
                cur  = url.split('#')[0].rstrip('/')
                if base == cur:
                    next_url = None  # same page, just an anchor — treat as end of crawl
            if not next_url or next_url == url:
                log("No more chapters found.", "info")
                # Emit the last real chapter URL for chain mode tracking
                emit_url = last_real_chapter_url or url
                log(f"__last_chapter_url__:{emit_url}", "dim")
                break
            url    = next_url
            index += 1
            time.sleep(delay)

        # Emit last chapter URL if loop ended due to max_chapters (not via the inner break)
        if last_real_chapter_url:
            log(f"__last_chapter_url__:{last_real_chapter_url}", "dim")

        # Flush any remaining chapters that didn't fill a full batch
        if upload_batch:
            created, errs = push_batch(upload_batch)
            stats["uploaded"] += created
            stats["errors"]   += len(errs)
            upload_batch.clear()
            push_stats()

        log(
            f"-- Finished: {stats['scraped']} found, {stats['uploaded']} uploaded, "
            f"{stats['skipped']} skipped, {stats['errors']} errors --",
            "ok" if stats["uploaded"] > 0 else "warn",
        )

    except Exception as e:
        log(f"Fatal error: {e}", "err")

    update({"status": "done", "stats": stats})

    # ── Watch post-run update ─────────────────────────────────────────────────
    # If this job was triggered by the WatchScheduler, write back lastChecked
    # and lastChapterUrl so the next scheduled run starts from the right place.
    watch_novel_id = params.get("_watch_novel_id")
    if watch_novel_id:
        with jobs_lock:
            job_logs = jobs.get(job_id, {}).get("logs", [])
        with _watches_lock:
            watches = _load_watches()
            if watch_novel_id in watches:
                w = watches[watch_novel_id]
                w["lastChecked"] = time.time()
                # Chain: update lastChapterUrl so next run starts where we left off
                if w.get("mode") == "chain":
                    for entry in reversed(job_logs):
                        if "__last_chapter_url__:" in entry.get("msg", ""):
                            w["lastChapterUrl"] = entry["msg"].split("__last_chapter_url__:")[1].strip()
                            break
                _save_watches(watches)


# =============================================================================
#  FLASK ROUTES
# =============================================================================

@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    with job_queue_lock:
        queued = len(job_queue)
    with _concurrency_lock:
        limit   = WATCH_CONCURRENCY
        stagger = STAGGER_DELAY
    active = max(0, limit - _semaphore._value)
    return jsonify({
        "status":        "ok",
        "port":          PORT,
        "min_words":     MIN_CONTENT_WORDS,
        "concurrency":   limit,
        "stagger_delay": stagger,
        "active_jobs":   active,
        "queued_jobs":   queued,
    })


@app.route("/jobs", methods=["POST"])
def create_job():
    params = request.json
    novel_ref_ok = params and (params.get("novel_id") or params.get("novel_slug"))
    if not params or not params.get("start_url") or not novel_ref_ok:
        return jsonify({"error": "start_url and (novel_id or novel_slug) are required"}), 400
    if params.get("auth_mode") == "nextauth" and not (params.get("admin_email") and params.get("admin_password")):
        return jsonify({"error": "admin_email and admin_password are required for auth_mode=nextauth"}), 400
    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {
            "status":     "queued",
            "logs":       [],
            "stats":      {},
            "started_at": None,
            "params":     params,   # saved for resume
            "checkpoint": None,     # { from_chapter, last_url } set on pause
        }
    with job_queue_lock:
        job_queue.append((job_id, params))
    return jsonify({"job_id": job_id})


@app.route("/queue", methods=["GET"])
def get_queue():
    """Return current queue depth, active job count, and concurrency limit."""
    with job_queue_lock:
        queue_items = [(jid, p.get("novel_slug") or p.get("novel_id", "?"))
                       for jid, p in job_queue]
    with _concurrency_lock:
        limit = WATCH_CONCURRENCY
    active = max(0, limit - _semaphore._value)
    return jsonify({
        "concurrency":  limit,
        "active":       active,
        "queued":       len(queue_items),
        "queue":        [{"job_id": jid, "novel": nov} for jid, nov in queue_items],
    })


@app.route("/config", methods=["GET", "PATCH"])
def config():
    """GET returns current config; PATCH updates concurrency limit and/or stagger delay."""
    global WATCH_CONCURRENCY, STAGGER_DELAY, _semaphore
    if request.method == "GET":
        with _concurrency_lock:
            return jsonify({
                "concurrency":   WATCH_CONCURRENCY,
                "stagger_delay": STAGGER_DELAY,
            })

    body = request.json or {}

    # ── Update concurrency if provided ────────────────────────────────────
    new_val = body.get("concurrency")
    if new_val is not None:
        try:
            new_val = int(new_val)
            if not (1 <= new_val <= 20):
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "concurrency must be an integer 1-20"}), 400

        with _concurrency_lock:
            old = WATCH_CONCURRENCY
            WATCH_CONCURRENCY = new_val
            # Adjust semaphore capacity by releasing/acquiring delta slots
            diff = new_val - old
            if diff > 0:
                for _ in range(diff):
                    _semaphore.release()   # add slots
            elif diff < 0:
                # Reduce capacity: acquire slots (non-blocking — existing running jobs
                # finish normally, we just stop new ones from starting until level drops)
                for _ in range(-diff):
                    if _semaphore.acquire(blocking=False):
                        pass
                    else:
                        break          # semaphore already at 0 — running jobs will drain

    # ── Update stagger delay if provided ──────────────────────────────────
    new_stagger = body.get("stagger_delay")
    if new_stagger is not None:
        try:
            new_stagger = float(new_stagger)
            if not (0.0 <= new_stagger <= 120.0):
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "stagger_delay must be a number 0-120"}), 400
        with _concurrency_lock:
            STAGGER_DELAY = new_stagger

    with _concurrency_lock:
        return jsonify({
            "ok":            True,
            "concurrency":   WATCH_CONCURRENCY,
            "stagger_delay": STAGGER_DELAY,
        })


@app.route("/jobs", methods=["GET"])
def list_jobs():
    """Return all known jobs with their status (used by Run Now to find what to pause)."""
    with jobs_lock:
        snapshot = {
            jid: {
                "status":     j.get("status"),
                "started_at": j.get("started_at"),
                "novel_slug": (j.get("params") or {}).get("novel_slug") or (j.get("params") or {}).get("novel_id"),
                "novel_id":   (j.get("params") or {}).get("novel_id"),
                "stats":      j.get("stats", {}),
            }
            for jid, j in jobs.items()
        }
    return jsonify({"jobs": snapshot})


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    # Compute queue position if queued
    queue_pos = None
    with job_queue_lock:
        for i, (qid, _) in enumerate(job_queue):
            if qid == job_id:
                queue_pos = i + 1   # 1-based
                break
    return jsonify({
        "status":         job["status"],
        "stats":          job.get("stats", {}),
        "logs":           job.get("logs", []),
        "started_at":     job.get("started_at"),
        "checkpoint":     job.get("checkpoint"),
        "queue_position": queue_pos,
    })


@app.route("/jobs/<job_id>/stream", methods=["GET"])
def stream_job(job_id):
    def generate():
        sent = 0
        while True:
            with jobs_lock:
                job    = jobs.get(job_id)
                if not job:
                    yield 'data: {"error":"not found"}\n\n'
                    return
                logs   = job.get("logs", [])
                status = job.get("status", "pending")
                stats  = job.get("stats", {})
            for entry in logs[sent:]:
                yield f"data: {json.dumps({'type':'log','entry':entry})}\n\n"
            sent = len(logs)
            yield f"data: {json.dumps({'type':'stats','stats':stats})}\n\n"
            if status == "done":
                yield f"data: {json.dumps({'type':'done','stats':stats})}\n\n"
                return
            time.sleep(0.4)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/jobs/<job_id>", methods=["DELETE"])
def cancel_job(job_id):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = "cancelled"
    # Also remove from queue if it hasn't started yet
    with job_queue_lock:
        global job_queue
        job_queue = [(jid, p) for jid, p in job_queue if jid != job_id]
    return jsonify({"ok": True})


@app.route("/jobs/<job_id>/pause", methods=["POST"])
def pause_job(job_id):
    """Gracefully pause a running job at the next chapter boundary."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] not in ("running", "starting"):
            return jsonify({"error": f"Cannot pause job in status '{job['status']}'"}), 409
        job["status"] = "pause_requested"
    return jsonify({"ok": True, "message": "Pause requested — job will stop at next chapter boundary"})


@app.route("/jobs/<job_id>/resume", methods=["POST"])
def resume_job(job_id):
    """Re-queue a paused job, starting from its saved checkpoint."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] != "paused":
            return jsonify({"error": f"Cannot resume job in status '{job['status']}'"}), 409
        checkpoint = job.get("checkpoint") or {}
        orig_params = dict(job.get("params") or {})
        # Build resumed params — override from_chapter and start_url from checkpoint
        if checkpoint.get("last_url"):
            orig_params["chapter_one_url"] = checkpoint["last_url"]
            orig_params["start_url"]       = checkpoint["last_url"]
        if checkpoint.get("from_chapter") is not None:
            orig_params["from_chapter"] = checkpoint["from_chapter"]
        # Create a new job for the resume so the old one is kept in history
        new_job_id = str(uuid.uuid4())[:8]
        jobs[new_job_id] = {
            "status":     "queued",
            "logs":       [],
            "stats":      {},
            "started_at": None,
            "params":     orig_params,
            "checkpoint": None,
        }
        job["status"] = "resumed"   # mark old job as handled
    with job_queue_lock:
        job_queue.append((new_job_id, orig_params))
    return jsonify({"ok": True, "job_id": new_job_id, "resumed_from": job_id})


@app.route("/detect", methods=["POST"])
def detect_chapter():
    body     = request.json or {}
    url      = body.get("url", "")
    selector = body.get("check_selector", "")
    if not url:
        return jsonify({"error": "url is required"}), 400
    session   = requests.Session()
    num, href = detect_latest_chapter(url, selector, session)
    return jsonify({"latest_num": num, "chapter_url": href})


@app.route("/test-url", methods=["POST"])
def test_url():
    body = request.json or {}
    url  = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    session  = requests.Session()
    warnings = []
    result   = {
        "reachable":       False,
        "status_code":     None,
        "title":           "",
        "content_preview": "",
        "content_length":  0,
        "word_count":      0,
        "next_url":        None,
        "prev_url":        None,
        "selector_used":   None,
        "framework":       "Unknown/Static",
        "adapter_matched": None,
        "adapter_scores":  [],
        "warnings":        [],
    }

    try:
        r = session.get(url, headers=HEADERS, timeout=20)
        result["status_code"] = r.status_code
        if r.status_code == 403:
            warnings.append("HTTP 403 -- site is blocking scrapers (Cloudflare or anti-bot)")
        elif r.status_code == 404:
            warnings.append("HTTP 404 -- page not found")
        elif r.status_code != 200:
            warnings.append(f"HTTP {r.status_code} -- unexpected status")

        if r.status_code != 200:
            result["warnings"] = warnings
            return jsonify(result)

        result["reachable"] = True
        html = r.text
        soup = BeautifulSoup(html, "lxml")

        scores = registry.score_all(url, html)
        result["adapter_scores"] = scores
        if scores and scores[0]["score"] >= 0.5:
            result["adapter_matched"] = scores[0]["name"]
            result["framework"]       = scores[0]["name"]
        else:
            result["framework"] = "Unknown/Static"

        result["title"]    = extract_title(soup, 1, url, html)
        result["next_url"] = find_next_url(soup, url, html)
        result["prev_url"] = find_prev_url(soup, url, html)

        if not result["next_url"]:
            warnings.append("No 'next chapter' link found -- crawler may stop after this chapter")

        content = extract_content(soup, html, url, session)
        if not content:
            warnings.append("No content extracted -- may need a custom adapter")
        else:
            junk, reason = is_junk_page(content, min_words=MIN_CONTENT_WORDS)
            if junk:
                warnings.append(f"Page would be SKIPPED during scrape: {reason}")
            elif len(content.split()) < 300:
                warnings.append(f"Content is short ({len(content.split())} words) -- confirm this is a real chapter")

        result["content_length"]  = len(content)
        result["word_count"]      = len(content.split()) if content else 0
        result["content_preview"] = content
        result["warnings"]        = warnings

    except requests.exceptions.ConnectionError:
        warnings.append("Connection refused -- site may be down or blocking your IP")
        result["warnings"] = warnings
    except requests.exceptions.Timeout:
        warnings.append("Request timed out after 20s")
        result["warnings"] = warnings
    except Exception as e:
        warnings.append(f"Unexpected error: {e}")
        result["warnings"] = warnings

    return jsonify(result)


# -- Adapter management endpoints ---------------------------------------------


@app.route("/adapters", methods=["GET"])
def list_adapters():
    """List all loaded adapters."""
    return jsonify({"adapters": registry.list_adapters(), "count": len(registry)})


@app.route("/adapters/test", methods=["POST"])
def test_adapter():
    """Score a URL against all adapters. Body: { url }"""
    body = request.json or {}
    url  = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    session = requests.Session()
    try:
        r = session.get(url, headers=HEADERS, timeout=20)
        if not r.ok:
            return jsonify({"error": f"HTTP {r.status_code}"}), 400
        html    = r.text
        scores  = registry.score_all(url, html)
        matched = scores[0]["name"] if scores and scores[0]["score"] >= 0.5 else None
        return jsonify({"scores": scores, "matched": matched})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
#  WATCH SCHEDULER — runs 24/7, fires scrape jobs for active watches
# =============================================================================

class WatchScheduler:
    """
    Background thread that wakes every 60 seconds and triggers a scrape job
    for any watch whose interval has elapsed.  Completely independent of the
    browser / React dashboard.
    """

    def __init__(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            time.sleep(60)
            try:
                self._tick()
            except Exception as e:
                print(f"[Scheduler] Unhandled error: {e}")

    def _tick(self):
        now = time.time()
        with _watches_lock:
            watches = _load_watches()

        for novel_id, w in watches.items():
            if not w.get("active"):
                continue
            interval_secs = float(w.get("intervalHours", 1)) * 3600
            last_checked  = w.get("lastChecked", 0)
            # Skip if a job for this novel is already running
            already_running = any(
                j.get("novel_id") == novel_id and j.get("status") in ("pending", "running")
                for j in jobs.values()
            )
            if already_running:
                continue
            if now - last_checked >= interval_secs:
                print(f"[Scheduler] Triggering watch for: {w.get('novelTitle', novel_id)}")
                self._trigger(novel_id, w)

    def _trigger(self, novel_id, w):
        mode = w.get("mode", "chain")
        # Chain mode: crawl from last known chapter URL
        if mode == "chain":
            crawl_start = w.get("lastChapterUrl") or w.get("chapterOneUrl") or w.get("startUrl", "")
        else:
            crawl_start = w.get("chapterOneUrl") or w.get("startUrl", "")

        params = {
            "mode":             "watch_check",
            "start_url":        w.get("startUrl", "") if mode != "chain" else crawl_start,
            "chapter_one_url":  crawl_start,
            "novel_id":         novel_id,
            "novel_slug":       w.get("novelSlug", ""),
            "api_url":          w.get("apiUrl", ""),
            "token":            w.get("token", ""),
            "from_chapter":     w.get("lastChapter", 0),
            "index_offset":     w.get("lastChapter", 0) if mode == "chain" else 0,
            "delay":            w.get("delay", DEFAULT_DELAY),
            "max_chapters":     500,
            "watch_mode":       mode,
            "check_selector":   w.get("checkSelector", ""),
            "_watch_novel_id":  novel_id,   # signal for post-run update
        }

        job_id = str(uuid.uuid4())[:8]
        with jobs_lock:
            jobs[job_id] = {"status": "queued", "logs": [], "stats": {}, "novel_id": novel_id}
        # Route through the shared concurrency queue so watch jobs respect the limit
        with job_queue_lock:
            job_queue.append((job_id, params))

    def _run_and_update(self, job_id, params, novel_id):
        """Run the scrape job then write lastChecked/lastChapter back to watches.json."""
        run_scrape_job(job_id, params)

        # Read back the job stats to find the highest chapter uploaded
        with jobs_lock:
            job   = jobs.get(job_id, {})
            stats = job.get("stats", {})
            logs  = job.get("logs", [])

        new_last = stats.get("last_chapter", 0)

        with _watches_lock:
            watches = _load_watches()
            if novel_id in watches:
                w = watches[novel_id]
                w["lastChecked"] = time.time()
                if new_last and new_last > w.get("lastChapter", 0):
                    w["lastChapter"] = new_last
                # Chain: update lastChapterUrl so next run starts where we left off
                if w.get("mode") == "chain":
                    for log in reversed(logs):
                        if "__last_chapter_url__:" in log.get("msg", ""):
                            w["lastChapterUrl"] = log["msg"].split("__last_chapter_url__:")[1].strip()
                            break
                _save_watches(watches)


# =============================================================================
#  WATCHES REST API
# =============================================================================

@app.route("/watches", methods=["GET"])
def list_watches():
    """List all watches with their current status."""
    with _watches_lock:
        watches = _load_watches()
    # Annotate each watch with whether a job is currently running for it
    result = []
    for novel_id, w in watches.items():
        running_job = next(
            (j for j in jobs.values()
             if j.get("novel_id") == novel_id and j.get("status") in ("pending", "running")),
            None,
        )
        result.append({
            **w,
            "novelId":   novel_id,
            "isRunning": running_job is not None,
        })
    return jsonify({"watches": result, "count": len(result)})


@app.route("/watches", methods=["POST"])
def upsert_watch():
    """
    Add or update a watch.
    Body: { novelId, novelSlug, novelTitle, startUrl, chapterOneUrl,
            mode, intervalHours, apiUrl, token, checkSelector?,
            lastChapter?, lastChecked?, active? }
    """
    body     = request.json or {}
    novel_id = body.get("novelId", "").strip()
    if not novel_id:
        return jsonify({"error": "novelId is required"}), 400
    if not body.get("novelSlug", "").strip():
        return jsonify({"error": "novelSlug is required"}), 400
    if not body.get("startUrl", "").strip():
        return jsonify({"error": "startUrl is required"}), 400

    with _watches_lock:
        watches = _load_watches()
        existing = watches.get(novel_id, {})
        watches[novel_id] = {
            # Preserve runtime fields if they exist
            "lastChapter":    existing.get("lastChapter",    body.get("lastChapter",    0)),
            "lastChecked":    existing.get("lastChecked",    body.get("lastChecked",    0)),
            "lastChapterUrl": existing.get("lastChapterUrl", body.get("lastChapterUrl", "")),
            # Config from request
            "novelSlug":      body.get("novelSlug", "").strip(),
            "novelTitle":     body.get("novelTitle", "").strip(),
            "startUrl":       body.get("startUrl", "").strip(),
            "chapterOneUrl":  body.get("chapterOneUrl", body.get("startUrl", "")).strip(),
            "mode":           body.get("mode", "chain"),
            "intervalHours":  float(body.get("intervalHours", 6)),
            "checkSelector":  body.get("checkSelector", ""),
            "active":         bool(body.get("active", existing.get("active", False))),
            "apiUrl":         body.get("apiUrl", existing.get("apiUrl", "")),
            "token":          body.get("token",  existing.get("token",  "")),
            "delay":          float(body.get("delay", existing.get("delay", DEFAULT_DELAY))),
        }
        _save_watches(watches)
    return jsonify({"ok": True, "novelId": novel_id})


@app.route("/watches/<novel_id>", methods=["DELETE"])
def delete_watch(novel_id):
    """Remove a watch."""
    with _watches_lock:
        watches = _load_watches()
        removed = novel_id in watches
        watches.pop(novel_id, None)
        _save_watches(watches)
    return jsonify({"ok": True, "removed": removed})


@app.route("/watches/<novel_id>/start", methods=["POST"])
def start_watch(novel_id):
    """Enable auto-scheduling for a watch."""
    with _watches_lock:
        watches = _load_watches()
        if novel_id not in watches:
            return jsonify({"error": "Watch not found"}), 404
        watches[novel_id]["active"] = True
        _save_watches(watches)
    return jsonify({"ok": True})


@app.route("/watches/<novel_id>/stop", methods=["POST"])
def stop_watch(novel_id):
    """Disable auto-scheduling for a watch."""
    with _watches_lock:
        watches = _load_watches()
        if novel_id not in watches:
            return jsonify({"error": "Watch not found"}), 404
        watches[novel_id]["active"] = False
        _save_watches(watches)
    return jsonify({"ok": True})


@app.route("/watches/<novel_id>/run", methods=["POST"])
def run_watch_now(novel_id):
    """Trigger an immediate watch check for a novel (regardless of interval)."""
    with _watches_lock:
        watches = _load_watches()
    if novel_id not in watches:
        return jsonify({"error": "Watch not found"}), 404

    # Refuse if already running
    already = any(
        j.get("novel_id") == novel_id and j.get("status") in ("pending", "running")
        for j in jobs.values()
    )
    if already:
        return jsonify({"error": "A check is already running for this novel"}), 409

    scheduler._trigger(novel_id, watches[novel_id])

    # Find the newly created job_id
    job_id = next(
        (jid for jid, j in jobs.items() if j.get("novel_id") == novel_id and j.get("status") == "pending"),
        None,
    )
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/watches/<novel_id>", methods=["PATCH"])
def patch_watch(novel_id):
    """Update runtime fields: lastChapter, lastChecked, lastChapterUrl."""
    body = request.json or {}
    with _watches_lock:
        watches = _load_watches()
        if novel_id not in watches:
            return jsonify({"error": "Watch not found"}), 404
        w = watches[novel_id]
        if "lastChapter"    in body: w["lastChapter"]    = body["lastChapter"]
        if "lastChecked"    in body: w["lastChecked"]    = body["lastChecked"]
        if "lastChapterUrl" in body: w["lastChapterUrl"] = body["lastChapterUrl"]
        if "active"         in body: w["active"]         = bool(body["active"])
        _save_watches(watches)
    return jsonify({"ok": True})


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    adapter_count  = len(registry)
    scheduler      = WatchScheduler()
    active_watches = len([w for w in _load_watches().values() if w.get("active")])
    print(f"""
+--------------------------------------------------+
|   Knight Novel Scraper Server v4 (LAN mode)      |
|   Running on http://0.0.0.0:{PORT}               |
|                                                  |
|   {adapter_count} adapters loaded                           |
|   {active_watches} watch(es) active (scheduler running)     |
|                                                  |
|   Access from LAN: http://<your-ip>:{PORT}       |
|   Press Ctrl+C to stop.                          |
+--------------------------------------------------+
""")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

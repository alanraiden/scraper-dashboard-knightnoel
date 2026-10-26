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
import threading, time, re, os, json, uuid, random
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
            # Periodically clean up old completed jobs when idle
            if len(jobs) > 50:
                _cleanup_old_jobs()
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
    """Persist watches to disk atomically (write to .tmp then os.replace).
    Prevents a half-written file if the process crashes mid-write."""
    tmp = _WATCHES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(watches, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _WATCHES_FILE)


# =============================================================================
#  CHAIN-URL VALIDATION
# =============================================================================

# These path fragments indicate a non-chapter page that must never be saved
# as a chain checkpoint even if the scraper emitted it as __last_chapter_url__.
_JUNK_PATH_FRAGMENTS = (
    "/sign-up", "/signup", "/register", "/login", "/cart",
    "/checkout", "/account", "/profile", "/members",
    "/search", "/tag", "/category", "/genre",
)


def validate_chain_url(url, watch, invalid_url=False):
    """
    Return True iff *url* is an acceptable chain checkpoint for *watch*.

    Checks:
      1. URL must be non-empty and parseable.
      2. Domain must match the watch's startUrl domain.
      3. URL must NOT be the same as startUrl (index page).
      4. URL path must NOT be a known non-chapter path.
      5. URL must contain the novel slug somewhere in the path.
      6. URL must NOT contain a fragment-only anchor on the same page as startUrl.
    """
    if invalid_url:
        return False
    if not url:
        return False

    try:
        parsed      = urlparse(url)
        start_parsed = urlparse(watch.get("startUrl", ""))
    except Exception:
        return False

    # 1. Must have a scheme and netloc
    if not parsed.scheme or not parsed.netloc:
        return False

    # 2. Domain must match
    if parsed.netloc.lower() != start_parsed.netloc.lower():
        return False

    path = parsed.path.rstrip("/").lower()

    # 3. Must not be the index/start page itself
    start_path = start_parsed.path.rstrip("/").lower()
    if path == start_path:
        return False

    # 4. Must not be a known non-chapter path
    for frag in _JUNK_PATH_FRAGMENTS:
        if frag in path:
            return False

    # 5. Must contain the novel slug somewhere in the path
    slug = (watch.get("novelSlug") or "").lower().strip("/")
    if slug and slug not in path:
        return False

    # 6. Fragment-only anchor on the index page (e.g. startUrl/#comments)
    if parsed.fragment and path == start_path:
        return False

    return True


# =============================================================================
#  POST-RUN WATCH UPDATE  (extracted so tests can call it directly)
# =============================================================================

def _apply_post_run_watch_update(novel_id, job_logs, job_stats, watch_mode):
    """
    Write lastChecked, lastChapter, and lastChapterUrl back to watches.json
    after a job finishes.  Called by run_scrape_job (for scheduler-triggered
    jobs) and exposed as a standalone function so regression tests can call it
    without running a real scrape job.

    Rules:
      - lastChecked is always updated.
      - lastChapter is ONLY advanced when job_stats["uploaded"] > 0 (confirmed
        successful upload).  Scraped-but-not-uploaded chapters do NOT count.
      - lastChapterUrl is only saved if validate_chain_url() passes.
    """
    with _watches_lock:
        watches = _load_watches()
        if novel_id not in watches:
            return
        w = watches[novel_id]
        w["lastChecked"] = time.time()

        # ── lastChapter: only advance if uploads were confirmed ───────────────
        uploaded = job_stats.get("uploaded", 0)
        if uploaded > 0:
            # Derive the highest chapter number from "ok" log lines
            highest_ch = 0
            for entry in reversed(job_logs):
                msg = entry.get("msg", "")
                if entry.get("type") == "ok" and msg.startswith("Ch."):
                    m = re.match(r"Ch\.(\d+)", msg)
                    if m:
                        highest_ch = max(highest_ch, int(m.group(1)))
                        break  # reversed → first match is the highest
            if highest_ch > w.get("lastChapter", 0):
                w["lastChapter"] = highest_ch

        # ── lastChapterUrl: validate before saving ────────────────────────────
        if watch_mode == "chain" or w.get("mode") == "chain":
            for entry in reversed(job_logs):
                msg = entry.get("msg", "")
                if "__last_chapter_url__:" in msg:
                    candidate = msg.split("__last_chapter_url__:")[1].strip()
                    if validate_chain_url(url=candidate, watch=w):
                        w["lastChapterUrl"] = candidate
                    else:
                        print(
                            f"[WatchUpdate] Rejected chain URL for {novel_id}: "
                            f"{candidate!r} failed validation"
                        )
                    break

        _save_watches(watches)

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

# -- Per-domain circuit breaker -----------------------------------------------
# Tracks consecutive 403 responses per domain. After CIRCUIT_THRESHOLD failures
# the circuit opens for CIRCUIT_OPEN_SECS seconds; no new watch jobs are queued
# for that domain until it resets. Settings can be overridden per-run via the
# scheduler (which reads watches.json, not from this global) but the defaults
# here apply to the gate in WatchScheduler._tick.
_domain_circuit      = {}     # domain → {"failures": N, "open_until": float}
_domain_circuit_lock = threading.Lock()
CIRCUIT_THRESHOLD    = 3      # consecutive 403s before opening
CIRCUIT_OPEN_SECS   = 30 * 60  # 30 minutes open


def _domain_of(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return url


def _circuit_record_failure(url):
    """Record a 403 failure for the domain. Opens circuit after threshold."""
    domain = _domain_of(url)
    with _domain_circuit_lock:
        entry = _domain_circuit.setdefault(domain, {"failures": 0, "open_until": 0})
        entry["failures"] += 1
        if entry["failures"] >= CIRCUIT_THRESHOLD:
            entry["open_until"] = time.time() + CIRCUIT_OPEN_SECS
            print(f"[CircuitBreaker] {domain} tripped after {entry['failures']} 403s — "
                  f"closed for {CIRCUIT_OPEN_SECS//60} min")


def _circuit_record_success(url):
    """Reset failure counter on a successful fetch."""
    domain = _domain_of(url)
    with _domain_circuit_lock:
        if domain in _domain_circuit:
            _domain_circuit[domain]["failures"] = 0
            # Leave open_until as-is; it will expire naturally


def is_domain_circuit_open(url):
    """Return True if the domain's circuit is currently open (no new jobs)."""
    domain = _domain_of(url)
    with _domain_circuit_lock:
        entry = _domain_circuit.get(domain)
        if not entry:
            return False
        return time.time() < entry.get("open_until", 0)



# =============================================================================
#  PAGE FETCH
# =============================================================================

def fetch_page(url, session, log_fn=None):
    max_retries = 3
    backoff     = [5, 15, 30]   # seconds to wait before each retry

    for attempt in range(max_retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 403:
                _circuit_record_failure(url)
                if log_fn:
                    log_fn(f"HTTP 403 — site may be blocking scrapers (circuit breaker updated for {_domain_of(url)})", "warn")
                return None, None
            if r.status_code in (502, 503, 504) and attempt < max_retries:
                wait = backoff[attempt]
                if log_fn:
                    log_fn(f"HTTP {r.status_code} on attempt {attempt+1} — retrying in {wait}s", "warn")
                time.sleep(wait)
                continue
            r.raise_for_status()
            _circuit_record_success(url)
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

    update({"status": "running", "logs": [], "stats": {"scraped": 0, "uploaded": 0, "skipped": 0, "errors": 0}, "started_at": time.time(), "last_progress_at": time.time()})

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
            # Query the KN backend for the real highest stored chapter.
            # Only advance from_chapter if the API max is HIGHER than what the
            # frontend already computed.  If the frontend sent a LOWER value it
            # means gap-detection found deleted / missing chapters and we must
            # honour that lower start so those chapters get re-scraped.
            api_max = _fetch_max_stored_chapter(api_url, novel_slug, token)
            if api_max > from_chapter:
                # Bug C4 fix: cap how far ahead api_max can push from_chapter.
                # If the KN chapterCount field was set too high (stale/corrupt) and
                # empty chapter records exist for those phantom numbers, api_max could
                # skip an entire unpublished range.  We only trust api_max if it
                # doesn't jump more than MAX_API_JUMP chapters ahead of what the
                # watch already knows about.
                MAX_API_JUMP = 50
                if api_max - from_chapter > MAX_API_JUMP:
                    log(
                        f"KN API reports Ch.{api_max} but watch only knows Ch.{from_chapter} "
                        f"— jump of {api_max - from_chapter} exceeds MAX_API_JUMP ({MAX_API_JUMP}). "
                        f"Capping at Ch.{from_chapter + MAX_API_JUMP} to avoid skipping real chapters.",
                        "warn",
                    )
                    api_max = from_chapter + MAX_API_JUMP
                log(f"KN API reports {api_max} chapters stored — starting after Ch.{api_max}", "dim")
                from_chapter = api_max
                index_offset = api_max   # chain mode: keep index numbering aligned
            else:
                if api_max < from_chapter:
                    log(f"Gap-fill mode: frontend detected missing chapters — scanning from Ch.{from_chapter} (DB max is Ch.{api_max})", "warn")
                # from_chapter stays as-is (already correct from frontend gap detection)
            log(f"-- Watch check started (scanning from Ch.{from_chapter}) --", "info")

        log(f"Starting forward crawl from: {chapter_one_url}", "info")
        upload_batch = []   # chapters accumulated for bulk upload
        # Reset any per-job adapter state (e.g. consecutive-skip counters)
        for adapter in registry.all():
            if hasattr(adapter, "reset_state"):
                adapter.reset_state()
        url     = chapter_one_url
        index   = index_offset + 1  # chain mode: start index at the right offset so infer_chapter_number fallback is correct
        visited = set()
        last_real_chapter_url = None  # tracks highest chapter URL for chain mode
        # Bug C1 fix: track whether __last_chapter_url__ was already emitted inside
        # the loop so the post-loop block doesn't emit it a second time.
        _chain_url_emitted = False
        # Bug C3 fix: initialise consecutive_skips (was referenced but never declared,
        # causing a NameError that silently aborted the crawl at the first skipped chapter).
        consecutive_skips = 0

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
                    # Bug C5 fix: emit __last_chapter_url__ so the watch scheduler
                    # can update lastChapterUrl for the NEXT fresh job it triggers.
                    # Without this, the scheduler re-starts from the old checkpoint
                    # and re-uploads already-stored chapters.
                    if last_real_chapter_url and not _chain_url_emitted:
                        log(f"__last_chapter_url__:{last_real_chapter_url}", "dim")
                        _chain_url_emitted = True
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
                wc = len((content or "").split())

                # Check for paywall pages, UI chrome, login walls, and
                # genuinely short pages before uploading
                junk, reason = is_junk_page(content or "", min_words=min_words)
                if junk:
                    log(f"Ch.{ch_num} -- {title}: locked/unavailable ({reason}) — stopping here", "warn")
                    stats["skipped"] += 1
                    consecutive_skips += 1
                    push_stats()
                    # Flush whatever was already batched up to this point
                    if upload_batch:
                        created, errs = push_batch(upload_batch)
                        stats["uploaded"] += created
                        stats["errors"]   += len(errs)
                        upload_batch.clear()
                        push_stats()
                    # Emit the PREVIOUS good chapter URL so chain resumes correctly.
                    # Bug C2 fix: last_real_chapter_url is only set for real content
                    # pages, so it never points to a junk/paywall page here.
                    if last_real_chapter_url and not _chain_url_emitted:
                        log(f"__last_chapter_url__:{last_real_chapter_url}", "dim")
                        _chain_url_emitted = True
                    log("-- Stopped at first locked chapter (content unavailable) --", "warn")
                    break

                else:
                    # Bug C2 fix: only record URL after confirming real content
                    last_real_chapter_url = url
                    consecutive_skips = 0
                    log(f"Ch.{ch_num} -- {title} ({wc}w)", "ok")
                    # Record that progress was made — bot uses this for stuck-job detection
                    update({"last_progress_at": time.time()})
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
                consecutive_skips = 0  # already-stored chapters don't count as lock skips

            next_url = find_next_url(soup, url, html or "", log)
            # Strip fragment-only URLs like chapter-237/#respond — these are comment anchors not chapters
            if next_url and '#' in next_url:
                base = next_url.split('#')[0].rstrip('/')
                cur  = url.split('#')[0].rstrip('/')
                if base == cur:
                    next_url = None  # same page, just an anchor — treat as end of crawl
            if not next_url or next_url == url:
                log("No more chapters found.", "info")
                # Emit the last real chapter URL for chain mode tracking.
                # Bug C1 fix: use the flag so the post-loop block below doesn't emit again.
                if not _chain_url_emitted:
                    emit_url = last_real_chapter_url or url
                    log(f"__last_chapter_url__:{emit_url}", "dim")
                    _chain_url_emitted = True
                break
            url    = next_url
            index += 1
            time.sleep(delay)

        # Emit last chapter URL when loop exited due to max_chapters limit.
        # Bug C1 fix: only emit if the inner loop didn't already emit it.
        if last_real_chapter_url and not _chain_url_emitted:
            log(f"__last_chapter_url__:{last_real_chapter_url}", "dim")
            _chain_url_emitted = True

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
    # If this job was triggered by the WatchScheduler, write back lastChecked,
    # lastChapter, and lastChapterUrl.  Uses _apply_post_run_watch_update() so
    # the logic is testable independently and consistent with the validation rules:
    #   - lastChapter only advances when uploaded > 0 (confirmed upload)
    #   - lastChapterUrl is validated against the novel's domain + slug before saving
    watch_novel_id = params.get("_watch_novel_id")
    if watch_novel_id:
        with jobs_lock:
            job_logs  = list(jobs.get(job_id, {}).get("logs", []))
            job_stats = dict(jobs.get(job_id, {}).get("stats", {}))
        _apply_post_run_watch_update(
            novel_id=watch_novel_id,
            job_logs=job_logs,
            job_stats=job_stats,
            watch_mode=params.get("watch_mode", ""),
        )


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
    # Expose open circuit breakers so the bot and dashboard can see them
    now = time.time()
    with _domain_circuit_lock:
        open_circuits = [
            {"domain": d, "failures": e["failures"],
             "open_until": e["open_until"],
             "minutes_remaining": max(0, round((e["open_until"] - now) / 60, 1))}
            for d, e in _domain_circuit.items()
            if now < e.get("open_until", 0)
        ]
    return jsonify({
        "status":         "ok",
        "port":           PORT,
        "min_words":      MIN_CONTENT_WORDS,
        "concurrency":    limit,
        "stagger_delay":  stagger,
        "active_jobs":    active,
        "queued_jobs":    queued,
        "open_circuits":  open_circuits,
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
                "status":           j.get("status"),
                "started_at":       j.get("started_at"),
                "last_progress_at": j.get("last_progress_at"),   # v4: progress-based stuck detection
                "novel_slug":       (j.get("params") or {}).get("novel_slug") or (j.get("params") or {}).get("novel_id"),
                "novel_id":         (j.get("params") or {}).get("novel_id"),
                "stats":            j.get("stats", {}),
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


def _cleanup_old_jobs():
    """Remove completed/cancelled/failed jobs older than 1 hour to prevent memory buildup."""
    cutoff = time.time() - 3600  # 1 hour
    terminal_states = ("done", "cancelled", "failed", "error")
    with jobs_lock:
        to_remove = [
            jid for jid, j in jobs.items()
            if j.get("status") in terminal_states
            and j.get("started_at", 0) < cutoff
        ]
        for jid in to_remove:
            del jobs[jid]
    if to_remove:
        print(f"[Cleanup] Removed {len(to_remove)} old completed jobs")


@app.route("/jobs/cleanup", methods=["POST"])
def cleanup_jobs():
    """Manually trigger cleanup of old completed jobs."""
    _cleanup_old_jobs()
    with jobs_lock:
        remaining = len(jobs)
    return jsonify({"ok": True, "remaining_jobs": remaining})


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
        # After a restart (e.g. power cut), wait before firing watches to avoid
        # a thundering herd where all stale watches queue simultaneously and
        # overwhelm the concurrency-limited job queue.
        print("[Scheduler] Warming up — will start checking watches in 5 minutes...")
        time.sleep(300)  # 5-minute warmup
        print("[Scheduler] Warmup complete — now monitoring watches.")
        while True:
            time.sleep(60)
            try:
                self._tick()
            except Exception as e:
                print(f"[Scheduler] Unhandled error: {e}")

    # Maximum number of watches to trigger in a single tick.
    # Prevents queue flooding after a restart when many watches are overdue.
    MAX_TRIGGERS_PER_TICK = 5
    # Don't queue more watches if the job queue already has this many items.
    MAX_QUEUE_DEPTH       = 8

    def _tick(self):
        now = time.time()
        with _watches_lock:
            watches = _load_watches()

        # Collect all due watches, then shuffle + cap to avoid thundering herd
        due = []
        for novel_id, w in watches.items():
            if not w.get("active"):
                continue
            interval_secs = float(w.get("intervalHours", 1)) * 3600
            last_checked  = w.get("lastChecked", 0)
            # Skip if a job for this novel is already running or queued
            already_running = any(
                j.get("novel_id") == novel_id and j.get("status") in ("pending", "running", "queued", "starting")
                for j in jobs.values()
            )
            if already_running:
                continue
            if now - last_checked >= interval_secs:
                # v5: skip if the source domain is circuit-breaker open
                source_url = w.get("startUrl", "") or w.get("lastChapterUrl", "")
                if source_url and is_domain_circuit_open(source_url):
                    domain = _domain_of(source_url)
                    print(f"[Scheduler] Skipping {w.get('novelTitle', novel_id)} — domain {domain} circuit is open")
                    continue
                due.append((novel_id, w))

        if not due:
            return

        # Randomise order so we don't always favour the same novels
        random.shuffle(due)

        triggered = 0
        for novel_id, w in due:
            # Queue depth guard — don't pile up if queue is already busy
            with job_queue_lock:
                if len(job_queue) >= self.MAX_QUEUE_DEPTH:
                    print(f"[Scheduler] Queue full ({len(job_queue)} pending) — deferring remaining {len(due) - triggered} watches to next tick")
                    break
            # Per-tick cap
            if triggered >= self.MAX_TRIGGERS_PER_TICK:
                print(f"[Scheduler] Hit per-tick cap ({self.MAX_TRIGGERS_PER_TICK}) — deferring {len(due) - triggered} watches to next tick")
                break

            print(f"[Scheduler] Triggering watch for: {w.get('novelTitle', novel_id)}")
            self._trigger(novel_id, w)
            triggered += 1

            # Stagger between triggers to spread load
            if triggered < len(due):
                time.sleep(random.uniform(3, 10))

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
        """
        DEAD CODE — never called.

        Jobs are now dispatched via _dispatch_loop → run_scrape_job, and the
        post-run watch update is handled inside run_scrape_job itself via
        _apply_post_run_watch_update().  This method is preserved for historical
        context only and will be removed in a future cleanup.
        """
        raise RuntimeError(
            "_run_and_update is dead code and must not be called. "
            "Post-run watch updates are handled inside run_scrape_job()."
        )


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

        # ── lastChapterUrl resolution ────────────────────────────────────────
        # Normally we prefer the server's existing value (so a corrected URL
        # is not silently overwritten by a stale push from the browser).
        # Exception: when the caller sends force_lastChapterUrl=true, they are
        # explicitly fixing a wrong value — honour their URL.
        body_url   = body.get("lastChapterUrl", "")
        force_url  = bool(body.get("force_lastChapterUrl", False))
        if force_url and body_url:
            resolved_chain_url = body_url
        else:
            resolved_chain_url = existing.get("lastChapterUrl") or body_url

        watches[novel_id] = {
            # Runtime fields — preserved from existing unless explicitly forced
            "lastChapter":    existing.get("lastChapter",    body.get("lastChapter",    0)),
            "lastChecked":    existing.get("lastChecked",    body.get("lastChecked",    0)),
            "lastChapterUrl": resolved_chain_url,
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

    # Refuse if already running or queued
    already = any(
        j.get("novel_id") == novel_id and j.get("status") in ("pending", "running", "queued", "starting")
        for j in jobs.values()
    )
    if already:
        return jsonify({"error": "A check is already running or queued for this novel"}), 409

    scheduler._trigger(novel_id, watches[novel_id])

    # Find the newly created job_id (scheduler creates with status="queued")
    job_id = next(
        (jid for jid, j in jobs.items() if j.get("novel_id") == novel_id and j.get("status") in ("queued", "pending")),
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

"""
scraper_server.py — Local Scraper API Server (v3 — adapter architecture)
=========================================================================
Runs on http://localhost:7832
The React dashboard connects to this to run scraping jobs server-side,
avoiding all CORS proxy limitations and JS-rendering issues.

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

# ── Gemini cleaner (optional — only active when GEMINI_API_KEY is set) ────────
from gemini_cleaner import maybe_clean as gemini_maybe_clean, status as gemini_status

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
        # This spreads load over time even when multiple slots are available.
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

def upload_chapter(novel_slug, chapter, api_url, token, log_fn=None):
    """Upload a single chapter via the scraper API. Used as fallback when bulk is not appropriate."""
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


def bulk_upload_chapters(novel_slug, chapters, api_url, token, log_fn=None):
    """
    Upload multiple chapters in a single API call using the Knight Novel scraper API.
    POST /api/scraper/novels/:slug/chapters/bulk
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
        # Safe JSON parse: KN dev server can return empty body on first compile
        # of a new route, or on a 5xx crash. Log exactly what arrived.
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

    update({"status": "running", "logs": [], "stats": {"scraped": 0, "uploaded": 0, "skipped": 0, "errors": 0}})

    mode          = params.get("mode", "scrape")
    start_url     = params["start_url"]
    novel_id      = params.get("novel_id", "")
    novel_slug    = params.get("novel_slug", "")  # KN slug-based API — preferred
    api_url       = params["api_url"]
    token         = params["token"]
    from_chapter  = int(params.get("from_chapter", 0))
    index_offset  = int(params.get("index_offset", 0))  # chain mode: index starts here not at 1
    delay         = float(params.get("delay", DEFAULT_DELAY))
    max_chapters  = int(params.get("max_chapters", MAX_CHAPTERS))
    min_words     = int(params.get("min_words", MIN_CONTENT_WORDS))

    if not novel_slug and not novel_id:
        log("ERROR: neither novel_slug nor novel_id was provided", "err")
        update({"status": "done", "stats": {"scraped": 0, "uploaded": 0, "skipped": 0, "errors": 1}})
        return

    session = requests.Session()
    stats   = {"scraped": 0, "uploaded": 0, "skipped": 0, "errors": 0}

    def push_stats():
        update({"stats": dict(stats)})

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
        last_real_chapter_url = chapter_one_url  # tracks last crawled URL for chain mode (initialised to start so it's never None)

        while url and stats["scraped"] < max_chapters:
            # Check if job was cancelled via DELETE /jobs/<id>
            with jobs_lock:
                if jobs.get(job_id, {}).get("status") == "cancelled":
                    log("Job cancelled by user.", "warn")
                    break

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

            # Optional Gemini cleaning — only triggers when content looks suspicious
            # (nav chrome mixed in, soft UI signals, encoding artifacts).
            # Skipped entirely when GEMINI_API_KEY is not set.
            content = gemini_maybe_clean(content, log_fn=log)

            last_real_chapter_url = url  # always track the last crawled URL regardless of chapter number

            if ch_num > from_chapter:
                stats["scraped"] += 1
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
                        # Use slug if available, fall back to novel_id
                        target = novel_slug or novel_id
                        created, _, errs = bulk_upload_chapters(target, upload_batch, api_url, token, log)
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
            target = novel_slug or novel_id
            created, _, errs = bulk_upload_chapters(target, upload_batch, api_url, token, log)
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


# =============================================================================
#  FLASK ROUTES
# =============================================================================

@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    with job_queue_lock:
        queued = len(job_queue)
    with _concurrency_lock:
        limit = WATCH_CONCURRENCY
        stagger = STAGGER_DELAY
    active = limit - _semaphore._value   # slots in use
    return jsonify({
        "status":       "ok",
        "port":         PORT,
        "min_words":    MIN_CONTENT_WORDS,
        "gemini":       gemini_status(),
        "concurrency":  limit,
        "stagger_delay": stagger,
        "active_jobs":  max(0, active),
        "queued_jobs":  queued,
    })


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



@app.route("/jobs", methods=["POST"])
def create_job():
    params = request.json
    if not params or not params.get("start_url"):
        return jsonify({"error": "start_url is required"}), 400
    if not params.get("novel_slug") and not params.get("novel_id"):
        return jsonify({"error": "novel_slug or novel_id is required"}), 400
    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "logs": [], "stats": {}}
    with job_queue_lock:
        job_queue.append((job_id, params))
    return jsonify({"job_id": job_id})



@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "stats":  job.get("stats", {}),
        "logs":   job.get("logs", []),
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
    return jsonify({"ok": True})


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

@app.route("/gemini/test", methods=["POST"])
def test_gemini():
    """
    Test Gemini cleaning on a sample text.
    Body: { text: string }
    Returns: { original_words, cleaned_words, removed, score, cleaned_text }
    """
    from gemini_cleaner import suspicion_score, clean, is_available
    body = request.json or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    if not is_available():
        return jsonify({"error": "Gemini not configured — set GEMINI_API_KEY"}), 503

    score   = suspicion_score(text)
    cleaned = clean(text)
    orig_wc = len(text.split())
    clean_wc= len(cleaned.split())
    return jsonify({
        "suspicion_score":  round(score, 3),
        "original_words":   orig_wc,
        "cleaned_words":    clean_wc,
        "removed_words":    orig_wc - clean_wc,
        "removed_ratio":    round((orig_wc - clean_wc) / max(orig_wc, 1), 3),
        "cleaned_text":     cleaned,
    })


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


if __name__ == "__main__":
    adapter_count = len(registry)
    print(f"""
+--------------------------------------------------+
|      NovaSphere Local Scraper Server v3          |
|      Running on http://localhost:{PORT}           |
|                                                  |
|  {adapter_count} adapters loaded                            |
|  Keep this terminal open while using the         |
|  React dashboard.  Press Ctrl+C to stop.         |
+--------------------------------------------------+
""")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)

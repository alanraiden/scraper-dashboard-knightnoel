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

    update({"status": "running", "logs": [], "stats": {"scraped": 0, "uploaded": 0, "skipped": 0, "errors": 0}})

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


# =============================================================================
#  FLASK ROUTES
# =============================================================================

@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    return jsonify({
        "status":    "ok",
        "port":      PORT,
        "min_words": MIN_CONTENT_WORDS,
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
        jobs[job_id] = {"status": "pending", "logs": [], "stats": {}}
    t = threading.Thread(target=run_scrape_job, args=(job_id, params), daemon=True)
    t.start()
    with jobs_lock:
        jobs[job_id]["thread"] = t
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

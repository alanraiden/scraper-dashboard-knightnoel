"""
scraper_bridge.py — Local Python bridge for the NovaSphere Scraper Dashboard
=============================================================================
Run this alongside `npm run dev` to let the React dashboard trigger scraping
via Python (no CORS proxy, no JS rendering issues).

Usage:
    pip install flask flask-cors requests beautifulsoup4 lxml
    python scraper_bridge.py

Then in the dashboard, enable "Use Local Python Scraper" in settings.
The bridge runs on http://localhost:5174
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import threading, queue, json, time, re, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173", "http://127.0.0.1:5173"])

# ── Active jobs: job_id → { status, log, result, stop_flag } ─────────────────
jobs = {}
jobs_lock = threading.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ═══════════════════════════════════════════════════════════════════════
#  TEXT HELPERS  (same logic as novel_scraper.py)
# ═══════════════════════════════════════════════════════════════════════

def clean_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(l.strip() for l in text.splitlines()).strip()

JUNK_PATTERNS = [
    r"^prev(ious)?\s+chapter$", r"^next\s+chapter$",
    r"^←\s*(prev|previous|back)", r"^(next|forward)\s*→",
    r"^chapter\s+navigation", r"^(prev|previous|next)\s*$",
    r"use arrow keys", r"\(or\s+a\s*/\s*d\)",
    r"^add\s+to\s+(library|bookmarks?|reading\s+list)$",
    r"^\d+\s+comments?$", r"^comments?$", r"^reply$", r"^like$",
    r"^rate\s+this\s+(chapter|novel)$", r"^table\s+of\s+contents?$",
    r"translated\s+by", r"translation\s+by", r"translator[:\s]",
    r"t\.?l\.?\s*note", r"tl\s*note", r"to\s+support\s+us",
    r"support\s+the\s+(translation|translator|author)",
    r"read\s+(more|ahead|the\s+latest)\s+(at|on)",
    r"https?://", r"\w+\.(com|net|org|io|xyz|online|site)\b",
    r"patreon\.com", r"ko-?fi\.com", r"buy\s+me\s+a\s+coffee",
    r"if\s+you('re|\s+are)\s+reading\s+this",
    r"this\s+chapter\s+was\s+(stolen|scraped|taken)",
    r"join\s+our\s+(discord|group|server)", r"discord\.gg/",
    r"^[\-_\*=~]{3,}$", r"lunox\s*scans?", r"lunoxteam",
]
BLOCK_MARKERS = [
    r"(translator|tl|editor|proofreader)'?s?\s+note",
    r"t\.?l\.?\s*note",
    r"note\s+from\s+(the\s+)?(translator|editor)",
]
_junk_re   = [re.compile(p, re.I) for p in JUNK_PATTERNS]
_block_re  = [re.compile(p, re.I) for p in BLOCK_MARKERS]

def strip_watermarks(text):
    lines = text.splitlines()
    out, skip = [], False
    for line in lines:
        s = line.strip()
        if any(p.search(s) for p in _block_re): skip = True
        if skip and s == "": skip = False; continue
        if skip: continue
        if s and any(p.search(s) for p in _junk_re): continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

CONTENT_SELECTORS = [
    "div.reading-content", "div.text-left", "div#chapter-content",
    "div.chapter-content", "div.entry-content", "article.post-content",
    "div.chapter-body", "div#content", "div.storytext", "div.chapter",
    "div.post-content", "div.main-content",
]
STRIP_TAGS = [
    "script","style","nav","header","footer","aside",
    "figure","figcaption","iframe","ins","noscript","form","button",
]
NEXT_TEXTS = {"next chapter","next","next chap","next →"}
PREV_TEXTS = {"previous chapter","previous","prev chapter","prev chap","← prev"}

# ═══════════════════════════════════════════════════════════════════════
#  PAGE FETCHING + EXTRACTION
# ═══════════════════════════════════════════════════════════════════════

def fetch_soup(url, session):
    try:
        r = session.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml"), r.text
    except Exception as e:
        raise RuntimeError(f"Fetch failed for {url}: {e}")

def extract_title(soup, fallback):
    for tag in ["h1", "h2"]:
        el = soup.find(tag)
        if el and el.get_text(strip=True):
            return clean_text(el.get_text(strip=True))
    for cls in ["chapter-title", "entry-title"]:
        el = soup.find(class_=re.compile(cls, re.I))
        if el and el.get_text(strip=True):
            return clean_text(el.get_text(strip=True))
    return f"Chapter {fallback}"

def extract_content_dom(soup):
    for tag in STRIP_TAGS:
        for el in soup.find_all(tag): el.decompose()
    for sel in CONTENT_SELECTORS:
        try:
            block = soup.select_one(sel)
            if block:
                text = clean_text(block.get_text(separator="\n"))
                if len(text) > 200:
                    return strip_watermarks(text)
        except: pass
    divs = soup.find_all("div")
    if divs:
        raw = clean_text(max(divs, key=lambda d: len(d.get_text())).get_text(separator="\n"))
        if len(raw) > 200:
            return strip_watermarks(raw)
    return ""

def is_madara(soup, url):
    html = str(soup)
    return (
        "wp-manga" in html or "madara" in html or
        "admin-ajax.php" in html or
        bool(re.search(r"/series/[^/]+/chapter-\d+", url, re.I))
    )

def extract_nonce(html):
    for p in [
        r'"nonce"\s*:\s*"([a-f0-9]{10})"',
        r"nonce\s*=\s*['\"]([a-f0-9]{10})['\"]",
        r'"ajaxNonce"\s*:\s*"([a-f0-9]{10})"',
    ]:
        m = re.search(p, html, re.I)
        if m: return m.group(1)
    return None

def extract_chapter_id(html):
    for p in [
        r'"chapter_id"\s*:\s*"?(\d+)',
        r'data-id=["\'](\d+)["\'][^>]*class=["\'][^"\']*chapter',
        r'wp_manga_chapter_id\s*=\s*(\d+)',
        r'"chapter":\{"id":(\d+)',
    ]:
        m = re.search(p, html, re.I)
        if m: return m.group(1)
    return None

def try_madara_ajax(origin, html, session, log):
    nonce      = extract_nonce(html)
    chapter_id = extract_chapter_id(html)
    if not chapter_id:
        log("  [Madara] No chapter_id found in page HTML", "dim")
        return ""
    log(f"  [Madara] Trying AJAX (chapter_id={chapter_id})", "dim")
    data = {
        "action": "manga_get_reading_page",
        "manga_chapter_id": chapter_id,
        "chapter_id": chapter_id,
    }
    if nonce: data["nonce"] = nonce
    try:
        r = session.post(
            f"{origin}/wp-admin/admin-ajax.php",
            data=data, headers=HEADERS, timeout=15
        )
        content_html = r.text
        try:
            j = r.json()
            content_html = j.get("data") or j.get("content") or j.get("html") or r.text
        except: pass
        soup = BeautifulSoup(content_html, "lxml")
        text = clean_text(soup.get_text(separator="\n"))
        if len(text) > 100:
            log("  [Madara] AJAX succeeded", "dim")
            return strip_watermarks(text)
    except Exception as e:
        log(f"  [Madara] AJAX failed: {e}", "dim")
    return ""

def try_wp_rest(origin, chapter_slug, session, log):
    log(f"  [Madara] Trying WP REST API for slug: {chapter_slug}", "dim")
    url = f"{origin}/wp-json/wp/v2/posts?slug={chapter_slug}&_fields=content,title"
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        data = r.json()
        if isinstance(data, list) and data and data[0].get("content", {}).get("rendered"):
            soup = BeautifulSoup(data[0]["content"]["rendered"], "lxml")
            text = clean_text(soup.get_text(separator="\n"))
            if len(text) > 100:
                log("  [Madara] WP REST succeeded", "dim")
                return strip_watermarks(text)
    except Exception as e:
        log(f"  [Madara] WP REST failed: {e}", "dim")
    return ""

def extract_content(url, soup, html, session, log):
    # Try standard DOM first
    content = extract_content_dom(soup)
    if len(content) >= 50:
        return content

    # Madara-specific fallbacks
    if is_madara(soup, url):
        log("  [Madara] DOM empty — trying direct API extraction", "info")
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

        # Strategy 1: AJAX
        content = try_madara_ajax(origin, html, session, log)
        if len(content) >= 50: return content

        # Strategy 2: WP REST API
        slug_m = re.search(r"/series/[^/]+/(chapter-\d+)", url, re.I) or \
                 re.search(r"/(chapter-\d+)/?$", url, re.I)
        if slug_m:
            content = try_wp_rest(origin, slug_m.group(1), session, log)
            if len(content) >= 50: return content

        log("  [Madara] All API strategies failed", "warn")

    return content

def find_next_url(soup, current_url):
    for sel in ["a.next_page","a[rel='next']","a.next-chap","a.btn-next"]:
        try:
            a = soup.select_one(sel)
            if a and a.get("href"):
                return urljoin(current_url, a["href"])
        except: pass
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() in NEXT_TEXTS:
            return urljoin(current_url, a["href"])
    return None

def find_prev_url(soup, current_url):
    for sel in ["a.prev_page","a[rel='prev']","a.prev-chap","a.btn-prev"]:
        try:
            a = soup.select_one(sel)
            if a and a.get("href"):
                return urljoin(current_url, a["href"])
        except: pass
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() in PREV_TEXTS:
            return urljoin(current_url, a["href"])
    return None

def infer_chapter_number(title, fallback):
    for p in [
        r"chapter[\s\-_#]?(\d+)",
        r"ch[\s\-_.]?(\d+)",
        r"#(\d+)",
        r"(\d+)",
    ]:
        m = re.search(p, title, re.I)
        if m: return int(m.group(1))
    return fallback

# ═══════════════════════════════════════════════════════════════════════
#  JOB RUNNER
# ═══════════════════════════════════════════════════════════════════════

def run_scrape_job(job_id, start_url, from_chapter, max_chapters, delay_ms, novel_id, api_url, token):
    job = jobs[job_id]
    log = job["log"]
    session = requests.Session()

    def add_log(msg, type_="info"):
        log.append({"msg": msg, "type": type_, "ts": int(time.time() * 1000)})

    add_log(f"Job started: {start_url}", "info")
    add_log(f"Scraping from Ch.{from_chapter} | max={max_chapters} | delay={delay_ms}ms", "dim")

    url   = start_url
    index = 1
    uploaded = 0
    skipped  = 0
    errors   = 0
    visited  = set()

    while url and not job["stop"]:
        if url in visited: break
        if uploaded + skipped >= max_chapters:
            add_log(f"Max chapters ({max_chapters}) reached", "warn"); break
        visited.add(url)

        add_log(f"[{index}] {url}", "info")

        try:
            soup, html = fetch_soup(url, session)
        except Exception as e:
            add_log(f"Fetch failed: {e}", "err"); break

        title   = extract_title(soup, index)
        ch_num  = infer_chapter_number(title, index)
        content = extract_content(url, soup, html, session, add_log)

        if ch_num > from_chapter:
            wc = len(content.split()) if content else 0
            if len(content.strip()) < 50:
                add_log(f"⚠ Ch.{ch_num} skipped — no text content ({wc}w)", "warn")
                skipped += 1
            else:
                # Push to backend
                try:
                    hdrs = {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    }
                    r = requests.post(
                        f"{api_url.rstrip('/')}/api/novels/{novel_id}/chapters",
                        json={"number": ch_num, "title": title, "content": content},
                        headers=hdrs, timeout=30
                    )
                    data = r.json()
                    if r.ok:
                        add_log(f"✓ Ch.{ch_num} uploaded — {title} ({wc}w)", "ok")
                        uploaded += 1
                    elif "already exists" in str(data.get("error","")).lower():
                        add_log(f"~ Ch.{ch_num} already exists", "dim")
                    else:
                        add_log(f"✗ Ch.{ch_num} upload failed: {data.get('error')}", "err")
                        errors += 1
                except Exception as e:
                    add_log(f"✗ Ch.{ch_num} upload error: {e}", "err")
                    errors += 1
        else:
            add_log(f"~ Ch.{ch_num} already stored, skipping", "dim")

        next_url = find_next_url(soup, url)
        if not next_url or next_url == url:
            add_log("No more chapters found.", "info"); break

        url = next_url
        index += 1
        time.sleep(delay_ms / 1000)

    job["status"]   = "done"
    job["uploaded"] = uploaded
    job["skipped"]  = skipped
    job["errors"]   = errors
    add_log(f"── Done: {uploaded} uploaded · {skipped} skipped · {errors} errors ──",
            "ok" if uploaded > 0 else "warn")


def run_watch_check(job_id, series_url, from_chapter, novel_id, api_url, token, check_selector=""):
    job = jobs[job_id]
    log = job["log"]
    session = requests.Session()

    def add_log(msg, type_="info"):
        log.append({"msg": msg, "type": type_, "ts": int(time.time() * 1000)})

    add_log(f"Watch check: {series_url}", "info")

    # ── Detect latest chapter on series page ──────────────────────────────────
    try:
        soup, html = fetch_soup(series_url, session)
    except Exception as e:
        add_log(f"Could not fetch series page: {e}", "err")
        job["status"] = "done"; return

    site_latest = 0
    chapter_start_url = series_url

    # Try user selector first
    if check_selector:
        try:
            el = soup.select_one(check_selector)
            if el:
                m = re.search(r"(\d+)", el.get_text())
                if m:
                    site_latest = int(m.group(1))
                    chapter_start_url = urljoin(series_url, el.get("href","")) or series_url
        except: pass

    # Auto-detect from chapter list
    if not site_latest:
        list_sels = [
            ".wp-manga-chapter a", ".chapter-list li a", ".chapters li a",
            "ul.chapter-list a", "ul.row-content-chapter li a",
            ".listing-chapters_wrap li a", ".eph-num a",
            "li.chapter a", "li[class*='chapter'] a",
        ]
        best = (0, "")
        for sel in list_sels:
            try:
                for a in soup.select(sel):
                    txt  = a.get_text(strip=True)
                    href = a.get("href","")
                    m    = re.search(r"chapter[\s\-_#]?(\d+)", txt, re.I) or \
                           re.search(r"ch[\s\-_.]?(\d+)", txt, re.I) or \
                           re.search(r"chapter[\-_]?(\d+)", href, re.I)
                    if m and int(m.group(1)) > best[0]:
                        best = (int(m.group(1)), urljoin(series_url, href))
            except: pass
        if best[0]:
            site_latest = best[0]
            chapter_start_url = best[1]

    # Brute force all links
    if not site_latest:
        best = (0, "")
        for a in soup.find_all("a", href=True):
            txt  = a.get_text(strip=True)
            href = a["href"]
            m    = re.search(r"chapter[\s\-_#]?(\d+)", txt, re.I) or \
                   re.search(r"chapter[\-_\/]?(\d+)", href, re.I)
            if m and int(m.group(1)) > best[0]:
                best = (int(m.group(1)), urljoin(series_url, href))
        if best[0]:
            site_latest = best[0]
            chapter_start_url = best[1]

    add_log(f"Site latest: Ch.{site_latest} | Stored: Ch.{from_chapter}", "info")

    if site_latest <= from_chapter:
        add_log("No new chapters.", "dim")
        job["status"] = "done"; job["newChapters"] = 0; return

    new_count = site_latest - from_chapter
    add_log(f"{new_count} new chapter(s) detected! Fetching from {chapter_start_url}", "ok")

    # Scrape and upload
    run_scrape_job(
        job_id, chapter_start_url,
        from_chapter, new_count + 5,  # small buffer
        1200, novel_id, api_url, token
    )
    job["newChapters"] = job.get("uploaded", 0)

# ═══════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "1.0"})


@app.route("/scrape", methods=["POST"])
def scrape():
    """Start a one-shot scrape job."""
    body       = request.json or {}
    job_id     = f"scrape_{int(time.time()*1000)}"
    start_url  = body.get("startUrl","")
    from_ch    = int(body.get("fromChapter", 0))
    max_ch     = int(body.get("maxChapters", 500))
    delay_ms   = int(body.get("delay", 1200))
    novel_id   = body.get("novelId","")
    api_url    = body.get("apiUrl","")
    token      = body.get("token","")

    if not all([start_url, novel_id, api_url, token]):
        return jsonify({"error": "startUrl, novelId, apiUrl and token are required"}), 400

    with jobs_lock:
        jobs[job_id] = {"status":"running","log":[],"stop":False,"uploaded":0,"skipped":0,"errors":0}

    t = threading.Thread(
        target=run_scrape_job,
        args=(job_id, start_url, from_ch, max_ch, delay_ms, novel_id, api_url, token),
        daemon=True
    )
    t.start()
    return jsonify({"jobId": job_id})


@app.route("/watch", methods=["POST"])
def watch():
    """Run a single watch check for a novel."""
    body       = request.json or {}
    job_id     = f"watch_{int(time.time()*1000)}"
    series_url = body.get("startUrl","")
    from_ch    = int(body.get("fromChapter", 0))
    novel_id   = body.get("novelId","")
    api_url    = body.get("apiUrl","")
    token      = body.get("token","")
    selector   = body.get("checkSelector","")

    if not all([series_url, novel_id, api_url, token]):
        return jsonify({"error": "startUrl, novelId, apiUrl and token are required"}), 400

    with jobs_lock:
        jobs[job_id] = {"status":"running","log":[],"stop":False,"uploaded":0,"skipped":0,"errors":0}

    t = threading.Thread(
        target=run_watch_check,
        args=(job_id, series_url, from_ch, novel_id, api_url, token, selector),
        daemon=True
    )
    t.start()
    return jsonify({"jobId": job_id})


@app.route("/job/<job_id>", methods=["GET"])
def job_status(job_id):
    """Poll job status and logs."""
    since = int(request.args.get("since", 0))  # log index to start from
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":   job["status"],
        "log":      job["log"][since:],
        "uploaded": job.get("uploaded", 0),
        "skipped":  job.get("skipped",  0),
        "errors":   job.get("errors",   0),
        "newChapters": job.get("newChapters", 0),
    })


@app.route("/job/<job_id>/stop", methods=["POST"])
def stop_job(job_id):
    """Signal a running job to stop."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["stop"] = True
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("=" * 52)
    print("  NovaSphere Scraper Bridge  •  localhost:5174")
    print("=" * 52)
    print("  Keep this running alongside: npm run dev")
    print("  Then enable 'Use Local Python Scraper' in the dashboard.")
    print("-" * 52)
    app.run(host="127.0.0.1", port=5174, debug=False)

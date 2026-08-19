"""
gemini_cleaner.py — Optional Gemini-powered content cleaner
============================================================
This module sits between the scraper's content extraction and the
upload step. When content passes is_junk_page() (i.e. it's long enough
and has no hard paywall signals) but still looks suspicious — too many
nav-like lines, mixed UI chrome, encoding junk — Gemini is asked to
strip the noise without touching the actual story text.

It is OPTIONAL. If GEMINI_API_KEY is not set in the environment, this
module does nothing and the content passes through unchanged.

HOW IT WORKS
------------
1. is_suspicious(text) scores the content on several heuristics:
   - Ratio of very short lines (< 4 words) — nav chrome has many
   - Presence of known UI phrases not caught by the hard blacklist
   - Encoding artifacts (????, garbled Unicode runs, bracket noise)
   - Ratio of lines that look like prose vs lines that look like labels

2. If the suspicion score exceeds SUSPICION_THRESHOLD, clean(text)
   is called. It sends the content to Gemini with a strict prompt that:
   - Instructs it to ONLY remove non-story lines
   - Explicitly forbids re-writing, paraphrasing, or adding content
   - Returns the cleaned text or the original if Gemini fails/errors

3. The cleaned text is diffed against the original:
   - If Gemini removed > MAX_REMOVAL_RATIO of the original word count,
     we assume it over-cleaned and fall back to the original
   - If it added words (net increase), we assume hallucination and
     fall back to the original

SAFETY GUARANTEES
-----------------
- Never used for chapters that already passed is_junk_page() as junk
- Never replaces more than MAX_REMOVAL_RATIO (40%) of word count
- Never adds words (net word count can only decrease or stay same)
- Falls back to original on any API error, timeout, or parse failure
- Costs are minimised: only triggered when suspicion score >= threshold
  and content is sent as plain text (not JSON, not multi-turn)

SETUP
-----
Set the following env var before starting the Python server:
    GEMINI_API_KEY=your_key_here

Get a free key at: https://aistudio.google.com/apikey
The free tier (Gemini 1.5 Flash) is sufficient — each chapter is one
API call, and cleaning is only triggered on suspicious content.

CONFIGURATION (environment variables)
--------------------------------------
GEMINI_API_KEY             required  Your Gemini API key
GEMINI_MODEL               optional  Default: gemini-2.0-flash
GEMINI_SUSPICION_THRESHOLD optional  Default: 0.30  (0.0-1.0)
GEMINI_MAX_REMOVAL_RATIO   optional  Default: 0.40  (fraction of words)
GEMINI_TIMEOUT             optional  Default: 20 seconds
"""

import os
import re
import logging

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

GEMINI_MODEL        = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
SUSPICION_THRESHOLD = float(os.getenv("GEMINI_SUSPICION_THRESHOLD", "0.30"))
MAX_REMOVAL_RATIO   = float(os.getenv("GEMINI_MAX_REMOVAL_RATIO", "0.40"))
GEMINI_TIMEOUT      = int(os.getenv("GEMINI_TIMEOUT", "20"))


def _api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "")


_genai = None
_genai_import_failed = False

def _get_genai():
    global _genai, _genai_import_failed
    if _genai is not None:
        _genai.configure(api_key=_api_key())
        return _genai
    if _genai_import_failed:
        return None
    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=_api_key())
        _genai = genai
        return _genai
    except ImportError:
        _genai_import_failed = True
        return None


def is_available() -> bool:
    """Return True if Gemini cleaning is configured and the library is installed."""
    return bool(_api_key()) and _get_genai() is not None


# ── Suspicion scoring ─────────────────────────────────────────────────────────

_SOFT_UI_SIGNALS = [
    r"\bhome\b.*\blatest\b",
    r"\bchapter list\b",
    r"\btable of contents\b",
    r"\bnext chapter\b",
    r"\bprev(ious)? chapter\b",
    r"\bcomments?\s*\(\d+\)",
    r"\brate this\b",
    r"\bsign (in|up)\b.*\bregist",
    r"\blog(in|out)\b",
    r"\bbookmark(s)?\b",
    r"\bnotification(s)?\b",
    r"\bfollow(ing)?\b.*\bnovel\b",
    r"\bshare\b.*\bchapter\b",
    r"\bread(ing)?\s+(settings?|mode)\b",
    r"\bfont\s+size\b",
    r"\bnight\s+mode\b",
    r"\[tl\s*note",
    r"\[editor",
    r"\[pr\b",
    r"\[t/n",
    r"^\s*\*\s*\*\s*\*\s*$",
    r"^\s*[-=_~]{4,}\s*$",
    r"read.*latest.*chapter.*at",
    r"support.*patreon",
    r"discord\.gg/",
    r"chapters?\s+ahead",
    r"early\s+access",
    # Blog / author metadata — the main gap that was missing
    r"\brecent\s+posts?\b",
    r"\blatest\s+posts?\s+by\b",
    r"\bsee\s+all\s+posts?\b",
    r"\bposted\s+by\b",
    r"\bwritten\s+by\b",
    r"\bauthor\s*:\s*\S",
    r"\bview\s+all\s+posts?\b",
    r"\ball\s+posts?\s+by\b",
    r"\brelated\s+posts?\b",
    r"\byou\s+may\s+also\s+like\b",
    # Reader UI widget signals
    r"\bfont\s+size\b",
    r"\bline\s+height\b",
    r"\bpage\s+width\b",
    r"\btts\s+control\b",
    r"\btext\s+indent\b",
    r"\brate\b.*\bnovel\s+updates\b",
    r"\bkeyboard\s+arrow\b",
    r"\bnavigate\s+between\s+chapters?\b",
    r"\bwhat\s+do\s+you\s+think\b",
    r"\breactions?\b.*\bcomment\b",
    r"\bpr\s*/\s*ed\s*:",
]

_soft_ui_compiled = [re.compile(p, re.I | re.M) for p in _SOFT_UI_SIGNALS]

_ARTIFACT_PATTERNS = [
    re.compile(r"[â€™â€œâ€]{2,}"),
    re.compile(r"\?\?\?+"),
    re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]"),
    re.compile(r"&(?:amp|lt|gt|nbsp|quot);"),
    re.compile(r"\[\s*\?\s*\]"),
]

# ── Pre-clean: strip blog metadata blocks with regex BEFORE sending to Gemini ─
# These patterns are unambiguous enough that we don't need AI to remove them.
# Running this first means Gemini gets cleaner input and doesn't need to guess.
_BLOG_METADATA_BLOCKS = [
    # ── Whole trailing blog-widget block ──────────────────────────────────────
    # Catches the full mikan-style block at end of chapter:
    #   Author\n\nRecent Posts\n\nmikan\n\nLatest posts by mikan\n(\nsee all\n)
    # Anchored to end-of-string so mid-chapter "Author's Note" is never touched.
    re.compile(
        r"\n[ \t]*Author[ \t]*\n"
        r"(?:[ \t]*\n)*"
        r"(?:.*\n){0,20}"
        r".*(?:recent\s+posts?|latest\s+posts?\s+by|see\s+all)"
        r"[\s\S]*$",
        re.I
    ),

    # ── "Recent Posts" header + following list ────────────────────────────────
    re.compile(
        r"(?:^|\n)[ \t]*[Rr]ecent\s+[Pp]osts?[ \t]*\n(?:.*\n?){0,15}",
        re.I
    ),

    # ── "Latest posts by <n>" — handles both inline AND multi-line split forms ─
    # Inline:  "Latest posts by mikan (see all)"
    # Split:   "Latest posts by mikan\n(\nsee all\n)"
    re.compile(
        r"latest\s+posts?\s+by\s+\S+"
        r"(?:"
          r"\s*\([^)]{0,30}\)"
          r"|"
          r"(?:\s*\n\s*){1,4}\(\s*\n?\s*see\s+all\s*\n?\s*\)"
          r"|"
          r"\s*\n?\s*see\s+all"
        r")?",
        re.I
    ),

    # ── Standalone "(see all)" — own line, or split across lines ─────────────
    re.compile(r"^\s*\(\s*\n?\s*see\s+all\s*\n?\s*\)\s*$", re.I | re.M),
    re.compile(r"^\s*\(\s*see\s+all\s*\)\s*$",             re.I | re.M),

    # ── "See all posts by <n>" ────────────────────────────────────────────────
    re.compile(r"see\s+all\s+posts?\s+by\s+\S+.*?\n?", re.I),

    # ── "Posted by" / "Written by" ────────────────────────────────────────────
    re.compile(r"^\s*(?:posted|written)\s+by\s+.{1,60}$", re.I | re.M),

    # ── "Author: <n>" (colon form — safe, specific) ────────────────────────
    re.compile(r"^\s*[Aa]uthor\s*:\s*.{1,60}$", re.I | re.M),

    # ── Bare "Author" line at very end of text (fallback) ────────────────────
    re.compile(r"\n[ \t]*Author[ \t]*\n[ \t]*$", re.I),

    # ── "Related Posts" section ───────────────────────────────────────────────
    re.compile(r"related\s+posts?\s*\n(?:.*\n){0,10}", re.I),

    # ── "You may also like" section ───────────────────────────────────────────
    re.compile(r"you\s+may\s+also\s+like\s*\n(?:.*\n){0,10}", re.I),

    # ── Reader UI / settings widget block at end of chapter ───────────────────
    # Catches trailing blocks like:
    #   Note :\nRate/Review on Novel Updates\nTheme\nFont\nFont Size\n
    #   Line Height\nTTS Control\n...\nPlease\nlogin\nto comment.
    # Anchored to end-of-string to avoid touching mid-chapter content.
    re.compile(
        r"\n[ \t]*Note\s*:?\s*\n"
        r"(?:[ \t]*\n)*"
        r"(?:.*\n){0,5}"
        r".*(?:Novel Updates|Rate|Click Here)"
        r"[\s\S]*$",
        re.I
    ),
    re.compile(
        r"\n[ \t]*(?:Theme|Font(?:\s+Size)?|Line Height|Alignment|Page Width"
        r"|Text Indent|Paragraph Action|TTS Control|Sign in to save"
        r"|keyboard arrow|navigate between chapters|What do you think"
        r"|reactions?|Please\s+login\s+to\s+comment)"
        r"[\s\S]*$",
        re.I | re.M
    ),

    # ── Duplicate / repeated chapter header at top ─────────────────────────────
    # Catches patterns like:
    #   Ch. 165\nChapter 165\n: Chapter 165\nPr/Ed: Sol IX
    # Removes everything up to (but not including) the last "Chapter N" header
    # only when it appears 2+ times in the first 20 lines.
]


def _remove_duplicate_chapter_header(text: str) -> str:
    """
    Remove repeated chapter header lines at the very top of the text.

    Scrapers often produce:
        Ch. 165
        Chapter 165
        : Chapter 165
        Pr/Ed: Sol IX

    We want to keep exactly ONE clean "Chapter N" line and drop the rest.
    Only operates on the first 15 lines to avoid touching story content.
    """
    lines = text.splitlines()

    # Patterns that identify a chapter-header line (not story prose)
    header_pat = re.compile(
        r"^\s*(?:"
        r"ch(?:apter)?\.?\s*\d+"           # Ch. 165 / Chapter 165
        r"|:\s*chapter\s*\d+"              # : Chapter 165
        r"|pr\s*/\s*ed\s*:.*"              # Pr/Ed: Sol IX
        r"|chapter\s+\d+\s*:?\s*chapter"  # Chapter 165 : Chapter 165
        r")\s*$",
        re.I
    )

    # Look only at the first 15 lines
    scan = lines[:15]
    header_indices = [i for i, l in enumerate(scan) if header_pat.match(l)]

    if len(header_indices) < 2:
        return text  # Nothing to deduplicate

    # Find the last "Chapter N" line among the header block — keep only that one
    chapter_num_pat = re.compile(r"^\s*chapter\s+\d+\s*$", re.I)
    keep_idx = None
    for i in reversed(header_indices):
        if chapter_num_pat.match(scan[i]):
            keep_idx = i
            break
    if keep_idx is None:
        keep_idx = header_indices[-1]  # fallback: keep the last header line

    # Remove all header lines except the kept one
    remove_set = set(header_indices) - {keep_idx}
    cleaned_lines = [l for i, l in enumerate(lines) if i not in remove_set]
    return "\n".join(cleaned_lines)


def _pre_clean(text: str) -> str:
    """
    Strip unambiguous blog/metadata blocks with regex before sending to Gemini.
    This removes patterns that are certain noise — no risk of touching story text.
    """
    # Step 0: deduplicate repeated chapter headers at the top
    text = _remove_duplicate_chapter_header(text)

    for pattern in _BLOG_METADATA_BLOCKS:
        text = pattern.sub("", text)
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def suspicion_score(text: str) -> float:
    """
    Return a score from 0.0 (clean prose) to 1.0 (very suspicious).
    """
    if not text or len(text.split()) < 50:
        return 0.0

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0

    short = sum(1 for l in lines if len(l.split()) < 4)
    short_ratio = short / len(lines)

    ui_hits = sum(1 for p in _soft_ui_compiled if p.search(text))
    ui_score = min(ui_hits / 5.0, 1.0)

    artifact_score = min(
        sum(1 for p in _ARTIFACT_PATTERNS if p.search(text)) / 3.0,
        1.0
    )

    score = (short_ratio * 0.40) + (ui_score * 0.40) + (artifact_score * 0.20)
    return round(min(score, 1.0), 3)


def is_suspicious(text: str, threshold: float = None) -> tuple:
    t = threshold if threshold is not None else SUSPICION_THRESHOLD
    score = suspicion_score(text)
    return score >= t, score


# ── Gemini prompt ─────────────────────────────────────────────────────────────
# FIX: Completely restructured. The old prompt had:
#   1. Formatting errors (missing newlines between sections)
#   2. Rule 5 "if ambiguous KEEP it" was causing Gemini to keep blog metadata
#      because short names/labels look ambiguous out of context
#   3. No concrete examples of the blog metadata pattern to remove
#
# The new prompt:
#   - Uses clear numbered sections with blank lines between them
#   - Gives CONCRETE EXAMPLES of the exact patterns we see (mikan-style)
#   - Removes the "if ambiguous keep it" rule for the EXPLICIT removal list
#   - Keeps the ambiguity rule only for content that is clearly prose-like
#   - Uses a TWO-PASS instruction: first identify noise blocks, then remove

_PROMPT_TEMPLATE = """\
You are a text cleaning assistant for a novel chapter archiver.

The text below was scraped from a web page. It contains the actual novel \
chapter text BUT may also contain website junk mixed in — navigation, ads, \
blog sidebars, author widgets, etc.

REMOVE THESE — they are NEVER part of the story:

1. Website navigation lines
   Example: "Home | Browse | Login | Register | Search"
   Example: "Previous Chapter | Next Chapter"

2. Blog/author metadata widgets — IMPORTANT, pay close attention
   These appear as short standalone lines, often at the top or bottom.
   Remove any block that matches these patterns:
     - "Recent Posts" (as a heading, followed by a list of titles)
     - "Latest posts by <any name> (see all)" or similar
     - "See all posts by <any name>"
     - "(see all)" standing alone
     - "Posted by <name>" / "Written by <name>" / "Author: <name>"
     - "Related Posts" / "You may also like"
     - Lists of article/chapter titles that are clearly NOT the current chapter
   EXAMPLE of what to remove:
     mikan
     Latest posts by mikan (see all)
     Recent Posts
     How I Became the Dark Lord's Bride Chapter 5
     The Villainess Reverses the Hourglass Chapter 12

3. Advertisement and promotional lines
   Example: "Read ahead on Patreon | Support us on Ko-fi"
   Example: "Join our Discord: discord.gg/xxx"
   Example: "X chapters ahead on Patreon"

4. UI widget lines
   Example: "Rate this chapter: ★★★★★"
   Example: "Comments (12)" / "Bookmark" / "Follow Novel"

5. Translator/editor credit lines in square brackets
   Example: "[TL Note: xxx]" / "[Editor: xxx]"

6. Garbled encoding (mojibake, ??? sequences, HTML entities in plain text)

7. Separator-only lines (lines that are ONLY dashes, stars, or equals signs)
   Example: "---" / "***" / "===="

KEEP EVERYTHING ELSE — especially:
- All actual story text: dialogue, narration, scene descriptions
- Chapter titles and numbers that are part of the chapter itself
- Any line that is clearly prose, even if it seems short or odd

AMBIGUITY RULE:
- For items in the list above (navigation, blog widgets, ads, UI): REMOVE them
  even if you are not 100% sure — it is better to remove a rare false positive
  than to leave website junk in the story
- For lines that look like prose or story content: KEEP them if uncertain

OUTPUT RULES:
- Return ONLY the cleaned text
- Do NOT rewrite, paraphrase, or change any story text
- Do NOT add any words, commentary, or preamble
- Do NOT fix grammar or style

TEXT TO CLEAN:
---
{text}
---
"""


def clean(text: str, log_fn=None) -> str:
    """
    Send text to Gemini for cleaning. Returns cleaned text on success,
    or the original text if Gemini is unavailable, errors, or over-cleans.
    """
    if not is_available():
        return text

    genai = _get_genai()
    if genai is None:
        return text

    original_words = len(text.split())

    try:
        model  = genai.GenerativeModel(GEMINI_MODEL)
        prompt = _PROMPT_TEMPLATE.format(text=text[:15000])

        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.0,
                max_output_tokens=8192,
            ),
            request_options={"timeout": GEMINI_TIMEOUT},
        )

        cleaned = response.text.strip() if response.text else ""

        if not cleaned:
            if log_fn:
                log_fn("[Gemini] Empty response — keeping original", "warn")
            return text

        cleaned_words = len(cleaned.split())

        if cleaned_words > original_words:
            if log_fn:
                log_fn(
                    f"[Gemini] Response added words ({original_words} -> {cleaned_words}) — keeping original",
                    "warn"
                )
            return text

        removed_ratio = (original_words - cleaned_words) / max(original_words, 1)
        if removed_ratio > MAX_REMOVAL_RATIO:
            if log_fn:
                log_fn(
                    f"[Gemini] Removed {removed_ratio:.0%} of words (limit {MAX_REMOVAL_RATIO:.0%}) — keeping original",
                    "warn"
                )
            return text

        if log_fn and cleaned_words < original_words:
            removed = original_words - cleaned_words
            log_fn(
                f"[Gemini] Cleaned {removed} words of noise ({removed_ratio:.0%} removed)",
                "dim"
            )

        return cleaned

    except Exception as e:
        if log_fn:
            log_fn(f"[Gemini] Error: {e} — keeping original", "warn")
        log.warning("Gemini cleaning failed: %s", e)
        return text


# ── Adaptive learning ─────────────────────────────────────────────────────────
#
# Every time Gemini successfully removes noise, we diff the before/after text
# and extract the removed lines. Lines that appear as noise repeatedly across
# multiple chapters are promoted into compiled regex patterns that run for FREE
# in _pre_clean() on all future chapters — no API call needed.
#
# Storage: a JSON file next to this module (or GEMINI_LEARN_PATH env var).
# Format:
#   {
#     "noise_lines": {
#       "rate/review on novel updates": 4,   ← seen 4 times, promoted at 3
#       "font size": 12,
#       ...
#     },
#     "learned_patterns": [
#       "rate\\/review on novel updates",     ← regex strings already promoted
#       ...
#     ]
#   }

import json
import pathlib

_LEARN_PATH = pathlib.Path(
    os.getenv("GEMINI_LEARN_PATH",
              str(pathlib.Path(__file__).parent / "learned_patterns.json"))
)
_PROMOTE_THRESHOLD = int(os.getenv("GEMINI_PROMOTE_THRESHOLD", "3"))

# In-memory cache so we don't read the file on every chapter
_learn_cache: dict | None = None
_learned_compiled: list[re.Pattern] = []


def _load_learn_db() -> dict:
    global _learn_cache, _learned_compiled
    if _learn_cache is not None:
        return _learn_cache
    if _LEARN_PATH.exists():
        try:
            data = json.loads(_LEARN_PATH.read_text("utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("noise_lines", {})
    data.setdefault("learned_patterns", [])
    _learn_cache = data
    _learned_compiled = [
        re.compile(p, re.I | re.M) for p in data["learned_patterns"]
    ]
    return _learn_cache


def _save_learn_db(data: dict) -> None:
    try:
        _LEARN_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    except Exception as e:
        log.warning("Could not save learned patterns: %s", e)


def _apply_learned(text: str) -> str:
    """Apply all learned regex patterns (free, no API)."""
    db = _load_learn_db()
    if not _learned_compiled:
        return text
    for pat in _learned_compiled:
        text = pat.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _learn_from_diff(original: str, cleaned: str, log_fn=None) -> None:
    """
    Compare original vs Gemini-cleaned text. Lines that were removed are
    candidate noise patterns. Accumulate counts; promote to learned patterns
    once seen >= _PROMOTE_THRESHOLD times across different chapters.
    """
    orig_lines  = set(l.strip().lower() for l in original.splitlines() if l.strip())
    clean_lines = set(l.strip().lower() for l in cleaned.splitlines()  if l.strip())
    removed = orig_lines - clean_lines

    if not removed:
        return

    db = _load_learn_db()
    newly_promoted = []

    for line in removed:
        # Skip very short lines (single words) and very long lines (prose)
        words = line.split()
        if len(words) < 2 or len(words) > 12:
            continue
        # Skip lines with story-like punctuation — probably prose, not UI
        if any(c in line for c in ['"', "'", ".", "!", "?", "—", "…"]):
            continue

        count = db["noise_lines"].get(line, 0) + 1
        db["noise_lines"][line] = count

        already_learned = line in db["learned_patterns"]
        if count >= _PROMOTE_THRESHOLD and not already_learned:
            # Escape for regex and anchor to full line
            escaped = re.escape(line)
            pattern_str = rf"^\s*{escaped}\s*$"
            db["learned_patterns"].append(pattern_str)
            newly_promoted.append(line)

    if newly_promoted:
        global _learn_cache, _learned_compiled
        _learn_cache = db  # update cache
        _learned_compiled = [
            re.compile(p, re.I | re.M) for p in db["learned_patterns"]
        ]
        if log_fn:
            for p in newly_promoted:
                log_fn(f"[Learn] Promoted to regex: {repr(p)}", "info")

    _save_learn_db(db)


def learn_stats() -> dict:
    """Return learning stats for the /health endpoint."""
    db = _load_learn_db()
    return {
        "learned_pattern_count": len(db["learned_patterns"]),
        "noise_line_candidates": len(db["noise_lines"]),
        "promote_threshold":     _PROMOTE_THRESHOLD,
        "storage_path":          str(_LEARN_PATH),
        "top_candidates": sorted(
            db["noise_lines"].items(), key=lambda x: -x[1]
        )[:10],
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def maybe_clean(text: str, log_fn=None) -> str:
    """
    1. Always run _pre_clean() — strips hardcoded + learned patterns (free).
    2. Score what's left; if still suspicious and Gemini is available, clean further.
    3. Diff Gemini's output to learn new noise patterns for future chapters.

    Always returns a string — never raises.
    """
    if not text:
        return text

    # Step 1a: hardcoded regex pre-clean
    pre_cleaned = _pre_clean(text)

    # Step 1b: apply learned patterns (free, grows over time)
    pre_cleaned = _apply_learned(pre_cleaned)

    if pre_cleaned != text and log_fn:
        removed = len(text.split()) - len(pre_cleaned.split())
        log_fn(f"[Cleaner] Pre-clean removed {removed} words of noise", "dim")

    # Step 2: if Gemini is available and content still looks suspicious, clean further
    if not is_available():
        return pre_cleaned

    suspicious, score = is_suspicious(pre_cleaned)
    if not suspicious:
        return pre_cleaned

    if log_fn:
        log_fn(f"[Gemini] Suspicious content (score={score:.2f}) — cleaning…", "info")

    gemini_result = clean(pre_cleaned, log_fn=log_fn)

    # Step 3: learn from what Gemini removed
    if gemini_result != pre_cleaned:
        _learn_from_diff(pre_cleaned, gemini_result, log_fn=log_fn)

    return gemini_result


# ── Status for /health endpoint ───────────────────────────────────────────────

def status() -> dict:
    """Return status info for the /health endpoint."""
    library_installed = _get_genai() is not None
    return {
        "enabled":             is_available(),
        "api_key_set":         bool(_api_key()),
        "library_installed":   library_installed,
        "model":               GEMINI_MODEL if is_available() else None,
        "suspicion_threshold": SUSPICION_THRESHOLD,
        "max_removal_ratio":   MAX_REMOVAL_RATIO,
        "setup_hint": (
            None if is_available() else
            "pip install google-generativeai and set GEMINI_API_KEY env var to enable"
        ),
        "learning":            learn_stats(),
    }

# Writing a new adapter

Drop a `.py` file into `scraper-server/adapters/`. It will be loaded
automatically at server startup — no registration step needed.

## Minimal example

```python
# adapters/my_site.py
import re
from .base import BaseAdapter
from .utils import clean_text, strip_watermarks, find_next_url_generic

class MySiteAdapter(BaseAdapter):
    name     = "my_site"
    priority = 100          # site-specific = high priority

    def can_handle(self, url, html):
        if "my-novel-site.com" in url:
            return 1.0      # exact domain match — maximum confidence
        return 0.0

    def extract_content(self, soup, html, url, session, log_fn=None):
        block = soup.select_one("div.chapter-text")
        if not block:
            return None     # signal failure — server will try next layer
        return strip_watermarks(clean_text(block.get_text(separator="\n")))

    def find_next_url(self, soup, url, html, log_fn=None):
        return find_next_url_generic(soup, url)
```

## Priority guide

| Value | When to use                                      |
|-------|--------------------------------------------------|
| 100   | Exact domain match (my-site.com only)            |
| 70–90 | Sub-framework (Next.js RSC, specific CMS theme)  |
| 50–60 | Broad framework (Next.js, SvelteKit, Nuxt)       |
| 10–40 | Genre/pattern heuristics                         |
| 0     | Generic fallback (only one of these)             |

## `can_handle` scoring tips

Combine signals and cap at 1.0:

```python
def can_handle(self, url, html):
    score = 0.0
    if "mysite.com" in url:          score += 0.6   # domain is a strong signal
    if "my-custom-class" in html:    score += 0.3
    if re.search(r"/chapter/\d+", url): score += 0.2
    return min(score, 1.0)
```

Return 0.0 for sites you definitely don't handle. The adapter is skipped
entirely if it returns 0.0, so it's free to be conservative.

## Available utilities (`adapters/utils.py`)

```python
from .utils import (
    clean_text,               # normalise whitespace
    strip_watermarks,         # remove promo/nav lines
    infer_chapter_number,     # parse chapter number from title string
    walk_json_for_text,       # find prose string in arbitrary JSON
    collect_text_values,      # collect all long strings from JSON
    pick_best_candidate,      # pick longest string, strip HTML tags
    extract_tiptap_doc,       # convert TipTap doc JSON to plain text
    find_next_url_generic,    # standard CSS selector + text scan
    find_prev_url_generic,
    infer_next_url_from_pattern,  # infer /chapter-N+1/ from current URL
    CONTENT_SELECTORS,        # shared list of content div selectors
)
```

## Testing your adapter without restarting the server

```bash
curl -s -X POST http://localhost:7832/adapters/test \
  -H "Content-Type: application/json" \
  -d '{"url":"https://my-novel-site.com/novel/story/chapter-1"}' | python -m json.tool
```

Returns every adapter's score for that URL, so you can see exactly
why your adapter did or didn't win.

Also available at `/adapters` (GET) to list all loaded adapters.

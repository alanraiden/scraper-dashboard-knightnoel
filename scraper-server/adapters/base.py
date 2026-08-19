"""
base.py — BaseAdapter
=====================
Every site adapter inherits from this class and overrides the methods it needs.
Methods that return None signal "I didn't handle this — fall through to the next layer".
"""


class BaseAdapter:
    # Human-readable name shown in logs and the /adapters API endpoint
    name: str = "base"

    # Higher priority adapters are evaluated first.
    # Use 100 for site-specific adapters (exact domain match),
    # 50 for framework-level adapters (Next.js, SvelteKit, etc.),
    # 10 for broad/generic adapters.
    priority: int = 50

    def can_handle(self, url: str, html: str) -> float:
        """
        Return a confidence score between 0.0 and 1.0.
          0.0  = definitely not my site, skip me
          0.5  = minimum threshold to be selected
          1.0  = I am certain this is my site

        The registry picks the adapter with the highest score >= 0.5.
        If no adapter reaches 0.5, the generic fallback is used.
        """
        return 0.0

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        """
        Extract chapter text from the page.
        Return the cleaned text string, or None to signal failure.
        The caller will try the next layer if None is returned.
        """
        return None

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        """
        Find the URL of the next chapter.
        Return the absolute URL string, or None if not found.
        """
        return None

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        """
        Find the URL of the previous chapter.
        Return the absolute URL string, or None if not found.
        """
        return None

    def extract_title(self, soup, fallback_num: int) -> str | None:
        """
        Extract the chapter title from the page.
        Return the title string, or None to let the server use its default logic.
        """
        return None

    def detect_latest_chapter(self, index_url: str, check_selector: str, session, log_fn=None):
        """
        Detect the latest chapter number from a novel index page.
        Return (chapter_number, chapter_url) tuple, or None to use default detection.
        """
        return None

    def __repr__(self):
        return f"<Adapter:{self.name} priority={self.priority}>"

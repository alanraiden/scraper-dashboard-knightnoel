"""
__init__.py — Adapter Registry
================================
Auto-discovers every adapter in this package at startup.
Drop a new .py file in this folder that defines a class inheriting BaseAdapter
and it will be picked up automatically — no registration step needed.

Usage:
    from adapters import registry
    registry.load()

    adapter = registry.get_best(url, html)
    if adapter:
        content = adapter.extract_content(soup, html, url, session, log_fn)
"""

import importlib
import pkgutil
import pathlib
import logging

from .base import BaseAdapter

log = logging.getLogger(__name__)


class AdapterRegistry:
    def __init__(self):
        self._adapters: list[BaseAdapter] = []
        self._loaded = False

    def load(self):
        """
        Scan this package for adapter modules and instantiate all BaseAdapter subclasses.
        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._loaded:
            return

        pkg_dir = pathlib.Path(__file__).parent
        skip = {"base", "__init__"}

        for _, module_name, _ in pkgutil.iter_modules([str(pkg_dir)]):
            if module_name in skip:
                continue
            try:
                mod = importlib.import_module(f".{module_name}", package=__name__)
                for attr_name in vars(mod):
                    attr = getattr(mod, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseAdapter)
                        and attr is not BaseAdapter
                    ):
                        instance = attr()
                        self._adapters.append(instance)
                        log.debug("Loaded adapter: %s (priority=%d)", instance.name, instance.priority)
            except Exception as e:
                log.warning("Failed to load adapter module '%s': %s", module_name, e)

        # Sort highest priority first so get_best short-circuits early
        self._adapters.sort(key=lambda a: a.priority, reverse=True)
        self._loaded = True
        log.info("Adapter registry ready: %d adapters loaded", len(self._adapters))

    def get_best(self, url: str, html: str, threshold: float = 0.5) -> BaseAdapter | None:
        """
        Run can_handle() on every adapter and return the highest-scoring one.
        Returns None if no adapter scores >= threshold.
        """
        best_adapter = None
        best_score = 0.0

        for adapter in self._adapters:
            try:
                score = adapter.can_handle(url, html)
                if score > best_score:
                    best_score = score
                    best_adapter = adapter
            except Exception as e:
                log.warning("Adapter %s raised in can_handle: %s", adapter.name, e)

        if best_score >= threshold:
            return best_adapter
        return None

    def score_all(self, url: str, html: str) -> list[dict]:
        """
        Return scores from every adapter — used by the /adapters/test endpoint
        so the dashboard can show which adapter would be selected for a URL.
        """
        results = []
        for adapter in self._adapters:
            try:
                score = adapter.can_handle(url, html)
            except Exception as e:
                score = 0.0
                log.warning("Adapter %s raised in can_handle: %s", adapter.name, e)
            results.append({
                "name": adapter.name,
                "priority": adapter.priority,
                "score": round(score, 3),
            })
        return sorted(results, key=lambda r: r["score"], reverse=True)

    def list_adapters(self) -> list[dict]:
        """Return metadata for all loaded adapters (for the /adapters endpoint)."""
        return [
            {"name": a.name, "priority": a.priority, "class": type(a).__name__}
            for a in self._adapters
        ]

    def __len__(self):
        return len(self._adapters)

    def all(self) -> list:
        """Return all loaded adapter instances."""
        return list(self._adapters)


# Singleton — import and use this everywhere
registry = AdapterRegistry()

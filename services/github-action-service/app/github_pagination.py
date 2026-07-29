"""Bounded pagination helpers for PyGithub paginated collections."""

from __future__ import annotations

import itertools
from typing import Iterable


def normalize_page(limit: int, page: int, *, max_limit: int = 100) -> tuple[int, int]:
    return min(max(int(limit), 1), max_limit), max(int(page), 1)


def bounded_page(items: Iterable, limit: int, page: int) -> tuple[list, int, int, int]:
    limit, page = normalize_page(limit, page)
    total = getattr(items, "totalCount", None)
    if not isinstance(total, int):
        total = len(items)  # Lists and test doubles; PyGithub uses totalCount.
    start = (page - 1) * limit
    return list(itertools.islice(items, start, start + limit)), total, limit, page


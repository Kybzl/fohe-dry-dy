"""Filesystem containment helpers (Milestone 4, section 31).

The library UI serves and deletes files by path, so every path must be proven
to live inside a configured root before it is opened, played or removed.
``pathlib`` does the resolution; these helpers only answer the containment
question and never touch the filesystem beyond ``resolve()``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

LOGGER = logging.getLogger(__name__)


def resolve_candidate(path: Path | str) -> Path | None:
    """Absolute, symlink-resolved path, or ``None`` when it cannot be resolved."""

    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover - defensive
        LOGGER.debug("could not resolve %r: %s", path, exc)
        return None


def is_within(path: Path | str, root: Path | str) -> bool:
    """True when ``path`` resolves to ``root`` or stays inside it."""

    candidate = resolve_candidate(path)
    base = resolve_candidate(root)
    if candidate is None or base is None:
        return False
    if candidate == base:
        return True
    try:
        return base in candidate.parents
    except OSError:  # pragma: no cover - defensive
        return False


def is_within_any(path: Path | str, roots: Iterable[Path | str]) -> bool:
    return any(is_within(path, root) for root in roots)


def resolve_within(path: Path | str, roots: Iterable[Path | str]) -> Path | None:
    """Resolved path when it lives in one of ``roots``, else ``None``.

    ``None`` means "refuse": the caller must not open, serve or delete the file.
    """

    resolved = resolve_candidate(path)
    if resolved is None:
        return None
    for root in roots:
        if is_within(resolved, root):
            return resolved
    LOGGER.warning("refusing path outside the configured roots: %s", resolved)
    return None

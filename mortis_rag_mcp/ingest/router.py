"""Explainable conservative routing and process-shared admission budget."""
from __future__ import annotations

import threading
import math
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .local import LocalUnsupported, _good_text, office_admission, pdf_backend, validate_source
from .models import ResourceLimits


@dataclass(frozen=True)
class RouteDecision:
    route: str
    confidence: float
    reasons: tuple[str, ...]
    sampled_pages: tuple[int, ...] = ()
    probe_capabilities: dict[str, bool] = field(default_factory=dict)
    estimated_resources: dict[str, int] = field(default_factory=dict)
    initial_route: str = ""
    upgrade_count: int = 0
    partial: bool = True
    failed_pages: tuple[int, ...] = ()

    def as_dict(self):
        return asdict(self)


class ParseBudget:
    """One controlled parser at a time across vaults; not a native RSS guarantee."""
    def __init__(self, capacity: int = 128 * 1024 * 1024):
        self.capacity = capacity
        self._gate = threading.Semaphore(1)

    @contextmanager
    def reserve(self, amount: int, cancelled=lambda: False):
        if amount < 0 or amount > self.capacity:
            raise LocalUnsupported("shared parse allocation admission exceeded")
        while not self._gate.acquire(timeout=.1):
            if cancelled():
                raise LocalUnsupported("parse cancelled before admission")
        try:
            if cancelled():
                raise LocalUnsupported("parse cancelled before admission")
            yield
        finally:
            self._gate.release()


DEFAULT_PARSE_BUDGET = ParseBudget()


def sample_pages(count: int) -> tuple[int, ...]:
    return tuple(sorted({i for i in (0, 1, 2, count // 4 - 1, count // 4,
                                    count // 2 - 1, count // 2, count // 2 + 1,
                                    count * 3 // 4 - 1, count * 3 // 4, count - 1)
                         if 0 <= i < count}))


_QUALITY_REASONS = frozenset({"empty_or_garbled", "geometry_unknown", "columns", "images",
                              "formula", "table", "probe_unavailable"})


def pdf_page_quality(page: Any) -> tuple[str, ...]:
    """Page-local rule shared with E09's full parse revalidation; sampling is not proof."""
    reasons = []
    try:
        text = page.get_text("text")
        if not _good_text(text):
            reasons.append("empty_or_garbled")
        blocks = [b for b in page.get_text("blocks") if len(b) > 6 and b[6] == 0]
        width = page.rect.width
        if (not isinstance(width, (int, float)) or not math.isfinite(width) or width <= 0
                or not blocks or any(not all(isinstance(x, (int, float)) and math.isfinite(x)
                                            for x in b[:4]) for b in blocks)):
            reasons.append("geometry_unknown")
        else:
            left = [b for b in blocks if b[2] < width * .58]
            right = [b for b in blocks if b[0] > width * .42]
            if left and right:
                reasons.append("columns")
        if page.get_images():
            reasons.append("images")
        if any(c in text for c in "∑∫√"):
            reasons.append("formula")
        if "<table" in text.lower() or any(line.count("|") >= 3 for line in text.splitlines()):
            reasons.append("table")
    except (AttributeError, TypeError, ValueError, RuntimeError):
        reasons.append("probe_unavailable")
    return tuple(dict.fromkeys(reasons))


def upgrade_route_once(decision: RouteDecision, config: Any, *,
                       quality_reasons: tuple[str, ...], failed_pages: tuple[int, ...] = ()) -> RouteDecision:
    """Pure E10 decision; E09 executes/persists the returned single local→cloud upgrade.

    Budget/cancellation/format errors are not quality reasons. Forced local and
    local_only never upgrade; cloud never falls back to local.
    """
    if (decision.route != "local" or decision.upgrade_count != 0
            or getattr(config, "routing", "auto") != "auto"
            or getattr(config, "network_policy", "configured") == "local_only"
            or not getattr(config, "enabled", False)):
        raise LocalUnsupported("quality route upgrade unavailable or already used")
    if not quality_reasons or any(reason not in _QUALITY_REASONS for reason in quality_reasons):
        raise LocalUnsupported("only explicit page quality failures may upgrade")
    return replace(decision, route="mineru", confidence=.4,
                   initial_route=decision.initial_route or decision.route,
                   upgrade_count=1, partial=True, failed_pages=tuple(failed_pages),
                   reasons=decision.reasons + ("local quality failed: " + ",".join(quality_reasons),))


def decide_route(path: Path, config: Any, *, limits: ResourceLimits | None = None) -> RouteDecision:
    limits = limits or ResourceLimits.from_config(config)
    mode = getattr(config, "routing", "auto")
    policy = getattr(config, "network_policy", "configured")
    if mode not in {"auto", "local", "mineru"} or policy not in {"configured", "local_only"}:
        raise LocalUnsupported("invalid routing/network policy")
    validate_source(path, limits)
    reasons: list[str] = []
    pages: tuple[int, ...] = ()
    simple = False
    available = False
    failed_pages = []
    suffix = path.suffix.lower()
    if suffix in {".docx", ".pptx", ".xlsx"}:
        office_admission(path, limits)
    if mode != "mineru":
        try:
            if suffix == ".pdf":
                backend, module = pdf_backend()
                available = True
                if backend == "pymupdf":
                    with module.open(path) as document:
                        if document.is_encrypted or len(document) > getattr(config, "local_max_pages", 300):
                            raise LocalUnsupported("PDF encrypted or local page budget exceeded")
                        indices = sample_pages(len(document))
                        pages = tuple(i + 1 for i in indices)
                        simple = bool(indices)
                        for index in indices:
                            quality = pdf_page_quality(document[index])
                            if quality:
                                failed_pages.append(index + 1)
                                reasons.append(f"page {index + 1}: " + ",".join(quality))
                            simple &= not quality
                        reasons.append("sampled text/geometry/image probe; unsampled pages not proven")
                else:
                    reasons.append("pypdf text available but geometry/image probe unavailable")
            else:
                import importlib.util
                name = {".docx": "docx", ".pptx": "pptx", ".xlsx": "openpyxl"}.get(suffix)
                available = bool(name and importlib.util.find_spec(name))
                reasons.append("Office text extraction is partial; complex content not proven simple")
        except (LocalUnsupported, ValueError, RuntimeError) as exc:
            reasons.append(str(exc))
    route = "local" if (mode == "local" or (mode == "auto" and simple)) and available else "mineru"
    if mode == "local" and not available:
        raise LocalUnsupported("forced local parser dependency/capability unavailable")
    if route == "mineru" and policy == "local_only":
        raise LocalUnsupported("local_only forbids cloud route; choose explicit supported local extraction")
    if route == "mineru" and not getattr(config, "enabled", False):
        raise LocalUnsupported("cloud ingest requires enabled authorization")
    reasons.append("forced route" if mode != "auto" else "simple sampled PDF" if simple else "unknown/complex uses authorized MinerU")
    return RouteDecision(route, .9 if simple else .4, tuple(reasons), pages,
                         {"text": available, "geometry": simple},
                         {"source_bytes": path.stat().st_size, "controlled_buffer_estimate": path.stat().st_size * 4},
                         initial_route=route, partial=True, failed_pages=tuple(failed_pages))

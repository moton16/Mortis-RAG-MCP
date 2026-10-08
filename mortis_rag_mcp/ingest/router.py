"""Explainable conservative routing and process-shared admission budget."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
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
    return tuple(sorted({i for i in (0, 1, 2, count // 2 - 1, count // 2, count // 2 + 1, count - 1)
                         if 0 <= i < count}))


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
                            page = document[index]
                            text = page.get_text("text")
                            blocks = [b for b in page.get_text("blocks") if len(b) > 6 and b[6] == 0]
                            # Conservative: any separated left/right body columns are deep-channel candidates.
                            width = page.rect.width
                            left = [b for b in blocks if b[2] < width * .58]
                            right = [b for b in blocks if b[0] > width * .42]
                            complex_page = bool(left and right) or bool(page.get_images())
                            complex_page |= any(c in text for c in "∑∫√")
                            simple &= _good_text(text) and not complex_page
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
                         {"source_bytes": path.stat().st_size, "controlled_buffer_estimate": path.stat().st_size * 4})

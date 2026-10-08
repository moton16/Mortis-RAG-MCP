from types import SimpleNamespace
import zipfile

import pytest

from mortis_rag_mcp.ingest import local, router


def config(**kwargs):
    return SimpleNamespace(enabled=True, routing="auto", network_policy="configured", **kwargs)


def test_sample_pages_deduplicated():
    assert router.sample_pages(1) == (0,)
    assert router.sample_pages(2) == (0, 1)
    assert router.sample_pages(0) == ()
    assert router.sample_pages(100) == (0, 1, 2, 24, 25, 49, 50, 51, 74, 75, 99)


def test_missing_docs_uses_only_authorized_cloud(tmp_path, monkeypatch):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(router, "pdf_backend", lambda: (_ for _ in ()).throw(local.LocalUnsupported("missing")))
    assert router.decide_route(path, config()).route == "mineru"
    blocked = config()
    blocked.network_policy = "local_only"
    with pytest.raises(local.LocalUnsupported, match="local_only"):
        router.decide_route(path, blocked)
    blocked.network_policy = "configured"
    blocked.enabled = False
    with pytest.raises(local.LocalUnsupported, match="authorization"):
        router.decide_route(path, blocked)


def test_magic_mismatch_never_uploads(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"not a pdf")
    with pytest.raises(local.LocalUnsupported, match="magic"):
        router.decide_route(path, config())


def test_office_xml_dtd_and_nested_archive_rejected(tmp_path):
    path = tmp_path / "a.docx"
    for name, body in [("word/document.xml", b'<!DOCTYPE x [<!ENTITY x "bad">]><x/>'),
                       ("word/embedded.zip", b"zip")]:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<x/>")
            archive.writestr(name, body)
        with pytest.raises(local.LocalUnsupported):
            local.office_admission(path, local.ResourceLimits())


def test_shared_budget_exception_and_cancel_release():
    budget = router.ParseBudget(100)
    with pytest.raises(RuntimeError):
        with budget.reserve(100):
            raise RuntimeError("failure")
    with budget.reserve(100):
        pass
    with pytest.raises(local.LocalUnsupported):
        with budget.reserve(101):
            pass
    with pytest.raises(local.LocalUnsupported, match="cancelled"):
        with budget.reserve(1, lambda: True):
            pass
    with budget.reserve(1):
        pass


def test_forced_local_missing_extra_does_not_fallback(tmp_path, monkeypatch):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(router, "pdf_backend", lambda: (_ for _ in ()).throw(local.LocalUnsupported("missing")))
    cfg = config()
    cfg.routing = "local"
    with pytest.raises(local.LocalUnsupported, match="forced local"):
        router.decide_route(path, cfg)


def test_forced_local_with_available_backend_routes_local(tmp_path, monkeypatch):
    """routing=local 且有可用本地后端 → 本地路由（不回云）。"""
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(router, "pdf_backend", lambda: ("pypdf", object()))
    cfg = config()
    cfg.routing = "local"
    decision = router.decide_route(path, cfg)
    assert decision.route == "local"
    assert decision.confidence == 0.4


def test_auto_without_geometry_probe_routes_deep(tmp_path, monkeypatch):
    """auto 模式下仅有纯文本后端、无几何/图像探测 → 保守走深通道，绝不假 simple。"""
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(router, "pdf_backend", lambda: ("pypdf", object()))
    decision = router.decide_route(path, config())
    assert decision.route == "mineru"
    assert decision.probe_capabilities == {"text": True, "geometry": False}


def test_forced_mineru_mode_skips_local_probe(tmp_path, monkeypatch):
    """routing=mineru：不探测本地能力，直接深通道。"""
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(router, "pdf_backend", lambda: (_ for _ in ()).throw(local.LocalUnsupported("missing")))
    cfg = config()
    cfg.routing = "mineru"
    decision = router.decide_route(path, cfg)
    assert decision.route == "mineru"
    assert decision.confidence == 0.4


def test_invalid_routing_policy_rejected(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    cfg = config()
    cfg.routing = "bogus"
    with pytest.raises(local.LocalUnsupported, match="invalid routing"):
        router.decide_route(path, cfg)


def test_quality_upgrade_decision_exactly_once():
    first = router.RouteDecision("local", .9, ("sampled",))
    second = router.upgrade_route_once(first, config(), quality_reasons=("columns",), failed_pages=(75,))
    assert first.route == "local" and first.upgrade_count == 0
    assert second.route == "mineru" and second.upgrade_count == 1
    assert second.initial_route == "local" and second.failed_pages == (75,) and second.partial
    assert second.as_dict()["upgrade_count"] == 1
    with pytest.raises(local.LocalUnsupported, match="already used"):
        router.upgrade_route_once(second, config(), quality_reasons=("columns",))


@pytest.mark.parametrize("changes,reasons", [
    ({"routing": "local"}, ("columns",)),
    ({"network_policy": "local_only"}, ("columns",)),
    ({"enabled": False}, ("columns",)),
    ({}, ("cancelled",)),
    ({}, ("budget",)),
    ({}, ("format",)),
])
def test_quality_upgrade_does_not_expand_other_errors(changes, reasons):
    cfg = config()
    for name, value in changes.items():
        setattr(cfg, name, value)
    with pytest.raises(local.LocalUnsupported):
        router.upgrade_route_once(router.RouteDecision("local", .9, ()), cfg, quality_reasons=reasons)


def test_mixed_pdf_late_page_quality_and_partial_sampling(tmp_path, monkeypatch):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    class Page:
        rect = SimpleNamespace(width=100)
        def __init__(self, number):
            self.number = number
        def get_text(self, kind):
            if kind == "text":
                return "printable body"
            if self.number == 75:
                return [(0, 0, 30, 20, "left", 0, 0), (60, 0, 90, 20, "right", 1, 0)]
            return [(0, 0, 100, 20, "body", 0, 0)]
        def get_images(self):
            return []
    class Document:
        is_encrypted = False
        def __len__(self):
            return 100
        def __getitem__(self, index):
            return Page(index + 1)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(router, "pdf_backend", lambda: ("pymupdf", SimpleNamespace(open=lambda p: Document())))
    result = router.decide_route(path, config())
    assert result.route == "mineru" and 75 in result.failed_pages and result.partial
    assert any("columns" in reason for reason in result.reasons)
    assert len(result.sampled_pages) < 100


@pytest.mark.parametrize("text,blocks,images,reason", [
    ("∑ formula", [(0, 0, 100, 20, "body", 0, 0)], [], "formula"),
    ("| a | b |", [(0, 0, 100, 20, "body", 0, 0)], [], "table"),
    ("body", [], [], "geometry_unknown"),
    ("body", [(0, 0, 100, 20, "body", 0, 0)], [object()], "images"),
    ("", [(0, 0, 100, 20, "body", 0, 0)], [], "empty_or_garbled"),
])
def test_page_quality_distinct_reasons(text, blocks, images, reason):
    page = SimpleNamespace(rect=SimpleNamespace(width=100),
        get_text=lambda kind: text if kind == "text" else blocks, get_images=lambda: images)
    assert reason in router.pdf_page_quality(page)

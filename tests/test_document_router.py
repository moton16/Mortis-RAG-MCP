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
    assert router.sample_pages(100) == (0, 1, 2, 49, 50, 51, 99)


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

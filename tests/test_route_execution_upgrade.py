"""E09：路由执行（最多一次质量升级）与本地 PDF 有界分卷（NEW）。

真实 store + 真实 worker 编排；远端一律 mock。分卷用例需要本地 PDF 后端
（pymupdf/pypdf），核心解释器缺依赖时按 `importorskip` 诚实跳过，并在有依赖的
PATH 解释器上另行靶向运行（不把 skip 记 PASS）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import DocumentStore, resolve_storage_layout
from mortis_rag_mcp.ingest import models as ingest_models
from mortis_rag_mcp.ingest.local import LocalUnsupported, parse_local_volumes
from mortis_rag_mcp.ingest.models import ResourceLimits
from mortis_rag_mcp.ingest.router import RouteDecision
from mortis_rag_mcp.ingest.worker import VirtualIngestWorker
from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture()
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


class _FakeClient:
    """parse_structured 打桩：记录调用并返回完整 ParseResult（实例级计数）。"""

    def __init__(self, *args, **kwargs):
        self.calls: list[str] = []

    def parse_structured(self, path, *, sink=None, intent_recorder=None, request_id="", **kwargs):
        self.calls.append(request_id)
        return ingest_models.ParseResult(
            markdown="# cloud body\n", parser_fingerprint="fake-cloud-v1",
            channel="v4", model="vlm", quality="full")


def build_server(tmp_path: Path, *, ingest_extra: str = ""):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        'mode = "static"\n\n[ingest]\nenabled = true\nstorage = "virtual"\n'
        f'{ingest_extra}\n',
        encoding="utf-8",
    )
    server = VaultMcpServer(config_path)
    server.config.cache.dir = str(tmp_path / "cache")
    server.config.cache.enabled = True
    server.config.cache.placement = "home"
    server.registry.add(str(vault), name="Vault")
    return server, vault


def _ingest_ns(**overrides):
    base = {"enabled": True, "audio_enabled": False, "storage": "virtual",
            "network_policy": "configured", "routing": "auto", "auto_watch": False,
            "output_dirname": ".mortis-parsed"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _store(tmp_path: Path, name: str = "vault") -> DocumentStore:
    vault = tmp_path / name
    vault.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / f"cache-{name}"), enabled=True),
    )
    store = DocumentStore(resolve_storage_layout(cfg, vault), cfg)
    store.open(write=True)
    return store


def _expire_lease(store: DocumentStore, job_id: str) -> None:
    import sqlite3
    import time

    conn = sqlite3.connect(str(store._store_path(store.generation_id)))
    try:
        conn.execute("UPDATE ingest_jobs SET lease_until = ? WHERE job_id = ?",
                     (time.time() - 1.0, job_id))
        conn.commit()
    finally:
        conn.close()


def _pdf_job(store: DocumentStore, vault: Path, *, name: str = "doc.pdf"):
    path = vault / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 fixture")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    job, _ = store.enqueue_job(source=name, source_sha256=sha, parser_fingerprint="fixture")
    claimed = store.claim_job("A")
    assert claimed.job_id == job.job_id
    return path, sha, claimed


def test_quality_failure_upgrades_exactly_once(isolated_registry, tmp_path, monkeypatch):
    """本地质量复检失败 → E10 最多一次升级；云解析只调用一次，决策完整可见。"""
    import mortis_rag_mcp.ingest.worker as worker_mod

    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    store = _store(tmp_path)
    try:
        claimed = _pdf_job(store, vault)[2]
        worker = VirtualIngestWorker(vault, _ingest_ns(), lambda: store)

        def _fake_decision(path, config, *, limits=None):
            return RouteDecision(route="local", confidence=.9, reasons=("fixture",),
                                 initial_route="local")

        def _boom(*args, **kwargs):
            raise LocalUnsupported("local quality revalidation failed: empty/garbled page")

        monkeypatch.setattr(worker_mod, "decide_route", _fake_decision)
        monkeypatch.setattr(worker_mod, "parse_local", _boom)
        fake = _FakeClient()
        worker._client = fake
        worker._run_job(store, claimed, "A")

        active = store.get_active("doc.pdf")
        assert active is not None
        decision = active.revision.capabilities["route_decision"]
        assert decision["route"] == "mineru" and decision["upgrade_count"] == 1
        assert decision["initial_route"] == "local"
        assert "local quality failed: empty_or_garbled" in decision["reasons"][-1]
        assert fake.calls == [claimed.job_id], "升级最多一次，绝不重复上传"
    finally:
        store.close()


@pytest.mark.parametrize("routing", ["local", "auto"])
def test_non_quality_local_failure_never_upgrades(isolated_registry, tmp_path, monkeypatch,
                                                  routing):
    """取消/预算/依赖缺失等非质量失败：如实失败，不升级、不掩盖、0 云调用。"""
    import mortis_rag_mcp.ingest.worker as worker_mod

    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    store = _store(tmp_path)
    try:
        claimed = _pdf_job(store, vault)[2]
        worker = VirtualIngestWorker(vault, _ingest_ns(routing=routing), lambda: store)

        def _fake_decision(path, config, *, limits=None):
            return RouteDecision(route="local", confidence=.9, reasons=("fixture",),
                                 initial_route="local")

        def _boom(*args, **kwargs):
            raise LocalUnsupported("optional docs dependency unavailable: pypdf")

        monkeypatch.setattr(worker_mod, "decide_route", _fake_decision)
        monkeypatch.setattr(worker_mod, "parse_local", _boom)
        fake = _FakeClient()
        worker._client = fake
        with pytest.raises(LocalUnsupported):
            worker._run_job(store, claimed, "A")
        assert fake.calls == []
        assert store.get_active("doc.pdf") is None
    finally:
        store.close()


def test_local_only_refuses_upgrade(isolated_registry, tmp_path, monkeypatch):
    """local_only 不以升级绕过外发策略：质量失败也必须如实失败。"""
    import mortis_rag_mcp.ingest.worker as worker_mod

    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    store = _store(tmp_path)
    try:
        claimed = _pdf_job(store, vault)[2]
        worker = VirtualIngestWorker(vault, _ingest_ns(network_policy="local_only"),
                                     lambda: store)

        def _fake_decision(path, config, *, limits=None):
            return RouteDecision(route="local", confidence=.9, reasons=("fixture",),
                                 initial_route="local")

        def _boom(*args, **kwargs):
            raise LocalUnsupported("local quality revalidation failed: empty/garbled page")

        monkeypatch.setattr(worker_mod, "decide_route", _fake_decision)
        monkeypatch.setattr(worker_mod, "parse_local", _boom)
        fake = _FakeClient()
        worker._client = fake
        with pytest.raises(LocalUnsupported, match="empty/garbled"):
            worker._run_job(store, claimed, "A")
        assert fake.calls == []
    finally:
        store.close()


# ---------------------------------------------------------------- 有界分卷（E09-D）

def make_pdf(path: Path, pages: int = 5) -> str:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    try:
        for index in range(pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"page {index + 1} bounded volume fixture text")
        doc.save(str(path))
    finally:
        doc.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_volume_parse_global_offsets_and_checkpoints(tmp_path):
    path = tmp_path / "book.pdf"
    sha = make_pdf(path, pages=5)
    limits = ResourceLimits()
    marks: list[tuple[int, tuple[int, int]]] = []
    result = parse_local_volumes(path, limits=limits, max_pages=2, source_sha256=sha,
                                 checkpoint=lambda ordinal, rng, payload: marks.append((ordinal, rng)))
    assert result.capabilities["volumes_total"] == 3
    assert result.capabilities["volumes_done"] == [1, 2, 3]
    assert marks == [(1, (0, 2)), (2, (2, 4)), (3, (4, 5))]
    assert [span.page for span in result.page_map] == [1, 2, 3, 4, 5], "全局页码不重排"
    offsets = [span.char_start for span in result.page_map]
    assert offsets == sorted(offsets), "拼接卷的字符偏移必须单调"
    assert offsets[2] > offsets[1], "第 3 页属于第 2 卷，偏移已跨卷累计"
    assert "page 5" in result.markdown


def test_volume_resume_reuses_confirmed_volumes(tmp_path):
    path = tmp_path / "book.pdf"
    sha = make_pdf(path, pages=5)
    limits = ResourceLimits()
    payloads: dict[int, dict] = {}
    parse_local_volumes(path, limits=limits, max_pages=2, source_sha256=sha,
                        checkpoint=lambda ordinal, rng, payload: payloads.update({ordinal: payload}))

    # 全量 resume：零重提 checkpoint、卷内容一致
    marks: list[int] = []
    again = parse_local_volumes(path, limits=limits, max_pages=2, source_sha256=sha,
                                resume=payloads,
                                checkpoint=lambda ordinal, rng, payload: marks.append(ordinal))
    assert again.capabilities["volumes_resumed"] == [1, 2, 3]
    assert marks == []
    assert again.markdown  # 与首跑一致（同指纹）

    # 部分 resume：只补缺卷
    marks2: list[int] = []
    partial = parse_local_volumes(path, limits=limits, max_pages=2, source_sha256=sha,
                                  resume={1: payloads[1]},
                                  checkpoint=lambda ordinal, rng, payload: marks2.append(ordinal))
    assert partial.capabilities["volumes_resumed"] == [1]
    assert marks2 == [2, 3], "恢复只补缺卷，不重复已完成卷"

    # 父源 SHA 变化：全部重算（不误复用旧卷）
    stale = parse_local_volumes(path, limits=limits, max_pages=2, source_sha256="0" * 64,
                                resume=payloads)
    assert stale.capabilities["volumes_resumed"] == []


def test_worker_volume_split_on_page_budget(isolated_registry, tmp_path):
    """本地 PDF 超页数预算：worker 走有界分卷发布（不升云），并逐卷写 E02 子记录。"""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    path = vault / "book.pdf"
    sha = make_pdf(path, pages=5)
    store = _store(tmp_path)
    try:
        job, _ = store.enqueue_job(source="book.pdf", source_sha256=sha,
                                   parser_fingerprint="fixture")
        worker = VirtualIngestWorker(vault, _ingest_ns(routing="local", local_max_pages=2),
                                     lambda: store)
        fake = _FakeClient()
        worker._client = fake
        claimed = store.claim_job("A")
        worker._run_job(store, claimed, "A")
        active = store.get_active("book.pdf")
        assert active is not None
        caps = active.revision.capabilities
        assert caps["volumes_total"] == 3
        assert caps["route_decision"]["route"] == "local"
        assert caps["route_decision"]["upgrade_count"] == 0, "分卷不是质量升级"
        rows = {row.ordinal: row for row in store.list_subjobs(job.job_id)}
        assert sorted(rows) == [1, 2, 3]
        assert all(rows[o].range_kind == "document_pages" for o in rows)
        assert rows[3].range_end == 5, "页范围是全局 0-based 半开区间"
        assert fake.calls == [], "分卷是纯本地路径，不触云"
    finally:
        store.close()


def test_worker_volume_crash_resume_only_missing_volumes(isolated_registry, tmp_path,
                                                         monkeypatch):
    """卷 3 崩溃 → 失败保 1/2 子记录 → 租约过期重领 → 只补缺卷发布。"""
    import mortis_rag_mcp.ingest.worker as worker_mod

    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    path = vault / "book.pdf"
    sha = make_pdf(path, pages=5)
    store = _store(tmp_path)
    try:
        job, _ = store.enqueue_job(source="book.pdf", source_sha256=sha,
                                   parser_fingerprint="fixture")
        claimed = store.claim_job("A")
        worker = VirtualIngestWorker(vault, _ingest_ns(routing="local", local_max_pages=2),
                                     lambda: store)

        real_volumes = worker_mod.parse_local_volumes

        def _crash_on_third(path, *, checkpoint=None, **kwargs):
            def _checkpoint(ordinal, rng, payload):
                if ordinal >= 3:
                    raise RuntimeError("injected crash on volume 3")
                checkpoint(ordinal, rng, payload)
            return real_volumes(path, checkpoint=_checkpoint, **kwargs)

        monkeypatch.setattr(worker_mod, "parse_local_volumes", _crash_on_third)
        with pytest.raises(RuntimeError):
            worker._run_job(store, claimed, "A")
        done = sorted(row.ordinal for row in store.list_subjobs(job.job_id)
                      if row.state == "done")
        assert done == [1, 2], "已确认卷的 checkpoint 必须保留"
        assert store.get_active("book.pdf") is None, "失败不发布半成品"

        # 崩溃：租约过期 → 同 job 重领 → 只补卷 3
        _expire_lease(store, job.job_id)
        reclaimed = store.claim_job("B")
        assert reclaimed.job_id == job.job_id
        monkeypatch.setattr(worker_mod, "parse_local_volumes", real_volumes)
        worker2 = VirtualIngestWorker(vault, _ingest_ns(routing="local", local_max_pages=2),
                                      lambda: store)
        worker2._run_job(store, reclaimed, "B")
        active = store.get_active("book.pdf")
        assert active is not None
        assert active.revision.capabilities["volumes_resumed"] == [1, 2]
        rows = {row.ordinal: row for row in store.list_subjobs(job.job_id)}
        assert sorted(rows) == [1, 2, 3], "补缺卷后子记录完整"
    finally:
        store.close()


def test_non_pdf_page_limit_still_refuses(isolated_registry, tmp_path, monkeypatch):
    """无页映射能力的格式（Office）：超限有界拒绝，不承诺分卷。"""
    from mortis_rag_mcp.ingest.local import parse_local_volumes as volumes

    docx = tmp_path / "book.docx"
    docx.write_bytes(b"PK\x03\x04 fixture")
    with pytest.raises(LocalUnsupported, match="refuse rather than promise paging"):
        volumes(docx, max_pages=2)

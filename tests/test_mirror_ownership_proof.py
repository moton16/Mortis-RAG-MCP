"""E03-a：单项镜像归属 proof 的只读契约（NEW）。

重点：`legacy_mirror` 标记或路径吻合**不能**单独作为归属证明；缺证据时保留
普通文件（sync 侧反例见 `test_mirror_exclusion.py`）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mortis_rag_mcp.ingest.migration import (
    MirrorOwnershipProof,
    load_legacy_ledger,
    prove_mirror_ownership,
    resolve_excluded_mirrors,
)

QUOTA = 10 * 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PLAIN = "# Legacy\n\nparsed body\n"
BODY = "# Legacy\n\n![fig](paper.assets/fig.png)\n\nparsed body\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "papers").mkdir(parents=True)
    (vault / ".mortis-parsed" / "papers").mkdir(parents=True)
    (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 payload")
    return vault


def _sha(vault: Path, source: str = "papers/paper.pdf") -> str:
    return hashlib.sha256((vault / source).read_bytes()).hexdigest()


def _write_mirror(vault: Path, *, source: str = "papers/paper.pdf", sha: str | None = None,
                  body: str = PLAIN, rel: str = "papers/paper.md", parsed_by: str = "mineru-v4") -> str:
    digest = sha if sha is not None else _sha(vault, source)
    target = vault / ".mortis-parsed" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        f"source_pdf: {source}\n"
        f"source_sha256: {digest}\n"
        f"parsed_by: {parsed_by}\n"
        "---\n" + body,
        encoding="utf-8",
    )
    return ".mortis-parsed/" + rel


def _write_ledger(vault: Path, entries: list[dict]) -> None:
    jobs = {f"j{i}": entry for i, entry in enumerate(entries, start=1)}
    (vault / ".mortis-parsed" / ".ingest_state.json").write_text(
        json.dumps({"jobs": jobs}), encoding="utf-8")


def test_proven_mirror_returns_proof_with_media_evidence(tmp_path: Path):
    vault = _vault(tmp_path)
    (vault / ".mortis-parsed" / "papers" / "paper.assets").mkdir()
    (vault / ".mortis-parsed" / "papers" / "paper.assets" / "fig.png").write_bytes(PNG)
    mirror = _write_mirror(vault, body=BODY)
    _write_ledger(vault, [{"source": "papers/paper.pdf", "state": "done", "sha256": _sha(vault)}])

    proof = prove_mirror_ownership(vault, mirror, quota_bytes=QUOTA)
    assert isinstance(proof, MirrorOwnershipProof)
    assert proof.source == "papers/paper.pdf"
    assert proof.source_sha256 == _sha(vault)
    assert proof.parser.endswith(":mineru-v4")
    assert proof.media_assets == 1


@pytest.mark.parametrize("case", ["no_ledger", "sha_changed", "duplicate_ledger",
                                  "ledger_not_done", "no_frontmatter", "bad_media"])
def test_unprovable_mirror_is_rejected(tmp_path: Path, case: str):
    vault = _vault(tmp_path)
    (vault / ".mortis-parsed" / "papers" / "paper.assets").mkdir()
    mirror = _write_mirror(vault)
    if case == "no_ledger":
        pass
    elif case == "sha_changed":
        (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 CHANGED")
        _write_ledger(vault, [{"source": "papers/paper.pdf", "state": "done", "sha256": _sha(vault)}])
    elif case == "duplicate_ledger":
        entry = {"source": "papers/paper.pdf", "state": "done", "sha256": _sha(vault)}
        _write_ledger(vault, [entry, dict(entry)])
    elif case == "ledger_not_done":
        _write_ledger(vault, [{"source": "papers/paper.pdf", "state": "running", "sha256": _sha(vault)}])
    elif case == "no_frontmatter":
        (vault / ".mortis-parsed" / "papers" / "paper.md").write_text(
            "# user notes\n\nnot a mirror\n", encoding="utf-8")
        _write_ledger(vault, [{"source": "papers/paper.pdf", "state": "done", "sha256": _sha(vault)}])
    elif case == "bad_media":
        # 引用了远程图片：资产归属无法证明
        _write_mirror(vault, body="# Legacy\n\n![fig](https://example.invalid/f.png)\n")
        _write_ledger(vault, [{"source": "papers/paper.pdf", "state": "done", "sha256": _sha(vault)}])

    with pytest.raises(ValueError):
        prove_mirror_ownership(vault, mirror, quota_bytes=QUOTA)


def test_header_source_mismatch_is_not_excluded(tmp_path: Path):
    """header 声明的 source 与候选 source 不一致 → 该候选不排除。"""
    vault = _vault(tmp_path)
    (vault / "other").mkdir()
    (vault / "other" / "other.pdf").write_bytes(b"%PDF-1.4 other")
    mirror = _write_mirror(vault, source="other/other.pdf")
    _write_ledger(vault, [{"source": "other/other.pdf", "state": "done",
                           "sha256": _sha(vault, "other/other.pdf")}])

    excluded, reasons = resolve_excluded_mirrors(
        vault, {"papers/paper.pdf": mirror}, quota_bytes=QUOTA)
    assert excluded == set()
    assert "does not match" in reasons[mirror]


def test_resolve_skips_missing_candidates_and_keeps_physical_source(tmp_path: Path):
    vault = _vault(tmp_path)
    mirror = _write_mirror(vault)
    _write_ledger(vault, [{"source": "papers/paper.pdf", "state": "done", "sha256": _sha(vault)}])
    before = _sha(vault)

    excluded, reasons = resolve_excluded_mirrors(
        vault, {"papers/paper.pdf": mirror, "missing/x.pdf": ".mortis-parsed/missing/x.md"},
        quota_bytes=QUOTA)
    assert excluded == {mirror}
    assert reasons == {}
    assert _sha(vault) == before, "proof 只读，物理源 SHA 不得改变"
    assert (vault / ".mortis-parsed" / "papers" / "paper.md").is_file(), "镜像文件不得被删除"


def test_load_legacy_ledger_returns_none_when_missing(tmp_path: Path):
    assert load_legacy_ledger(tmp_path) is None

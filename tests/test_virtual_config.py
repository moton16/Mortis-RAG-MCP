"""T01 虚拟存储的配置、路径与身份（v0.9.0 C91 验收，§19.2 T01 / §11.2 / §12.2）。

锁四件事，缺一不可：
1. **默认口径**：`cache.placement` 默认 home，文档库路径落在缓存根（库外）之下；
   解析布局本身**零副作用**——不合格的路径不能靠"顺手建个目录"暴露出来。
2. **真实归属**：cache 根通过 symlink/junction 落进库（或任一已注册库）时必须
   判定为不可写虚拟存储，给出修复建议，而不是偷偷改道。
3. **入队前失败**：subdir 上跳/绝对路径、`max_size_mb` 的 bool/NaN、cache 关闭
   都必须在**写任何东西之前**报错/拒绝。
4. **身份稳定**：vault key 对路径拼写（大小写、尾分隔符、symlink）稳定；显式
   `cache.id` 相同但实际库不同的两个库不能共享同一个文档库（§12.2）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mortis_rag_mcp.config import (
    AppConfig,
    CacheConfig,
    DocStoreConfig,
    EmbeddingConfig,
    _fallback_toml,
    load_config,
)
from mortis_rag_mcp.doc_store import (
    ControlStore,
    DocumentStore,
    DocStoreError,
    StoreBindingMismatch,
    VirtualStorageDisabled,
    is_within,
    normalize_source_path,
    resolve_storage_layout,
    vault_cache_key,
)
from mortis_rag_mcp.indexer import MarkdownIndexer


def _config(cache_dir: Path, **cache_kwargs) -> AppConfig:
    cache_kwargs.setdefault("enabled", True)
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(cache_dir), **cache_kwargs),
    )


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """写门禁会读「已注册库」：把注册表钉到用例临时目录，绝不读宿主 ~/.mortis_rag_mcp。

    （宿主注册表默认落点的隔离缺口属 v0.8.2 的 DCGFH-F；这里只保证**本 Lane 新增
    的读写路径**不引入对真实用户配置的依赖。）
    """
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(tmp_path / "vaults.toml"))
    monkeypatch.delenv("VAULT_MCP_REGISTRY", raising=False)


def _make_dir_link(link: Path, target: Path) -> None:
    """建一个指向 target 的目录链接；Windows 无权限时退到 junction，再不行 skip。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0 and link.exists():
            return
    pytest.skip("当前环境无法创建目录符号链接/junction（不影响产品行为）")


# ------------------------------------------------------------------ 默认与路径

def test_default_placement_is_home_and_docstore_is_under_cache_root(tmp_path):
    """默认 home：文档库落在缓存根下，而不是库内。"""
    cfg = _config(tmp_path / "cache")
    assert cfg.cache.placement == "home"
    assert cfg.doc_store.max_size_mb == 2048

    vault = tmp_path / "vault"
    vault.mkdir()
    layout = resolve_storage_layout(cfg, vault)

    assert layout.placement == "home"
    assert layout.enabled is True
    assert layout.blocked_reason == ""
    assert is_within(layout.doc_store_dir, layout.cache_root)
    assert vault.resolve() not in layout.doc_store_dir.parents
    # control 与 mutation 锁是同级兄弟文件，不落进 generation 目录
    assert layout.control_path.parent == layout.doc_store_dir
    assert layout.mutation_lock_path.parent == layout.doc_store_dir
    assert layout.generations_dir.parent == layout.vault_dir


def test_layout_resolution_has_no_filesystem_side_effects(tmp_path):
    """解析布局不得建库：只有真正要写的时候才允许产生文件（§11.2 零污染）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    layout = resolve_storage_layout(_config(tmp_path / "cache"), vault)
    assert not layout.doc_store_dir.exists()
    assert not layout.control_path.exists()


def test_vault_placement_keeps_docstore_inside_declared_subdir(tmp_path):
    """显式 vault：文档库落在库内声明的缓存子树（显式例外，不是"库内零污染"）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    layout = resolve_storage_layout(_config(tmp_path / "unused", placement="vault"), vault)

    assert layout.placement == "vault"
    assert is_within(layout.doc_store_dir, vault)
    assert layout.doc_store_dir.relative_to(vault).parts[0] == ".mcp_cache"


def test_home_root_inside_current_vault_is_blocked(tmp_path):
    """home 根落在**本库**内：判定不可写并给修复建议，不改道、不写盘。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    inside = vault / ".cache"
    cfg = _config(inside)

    layout = resolve_storage_layout(cfg, vault)

    assert layout.blocked_reason
    assert "vault" in layout.blocked_reason or "placement" in layout.blocked_reason
    assert not layout.doc_store_dir.exists()


def test_home_root_inside_other_registered_vault_is_blocked(tmp_path):
    """home 根落在**任一已注册库**内同样不可写（§11.2 不得只查当前库）。"""
    other = tmp_path / "other"
    other.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = _config(other / "shared_cache")

    layout = resolve_storage_layout(cfg, vault, registered_vaults=[str(vault), str(other)])

    assert layout.blocked_reason
    # 只注册当前库时看不到这个冲突（说明检测确实按注册库集合工作）
    assert resolve_storage_layout(cfg, vault, registered_vaults=[str(vault)]).blocked_reason == ""


def test_write_gate_blocks_cache_root_inside_other_registered_vault(tmp_path, monkeypatch):
    """写门禁补齐跨库维度：cache 根落在**另一个已注册库**内时必须拒绝写入。

    布局解析（读路径）只检测当前库、不读宿主注册表；跨库归属在写门禁显式补查，
    因此这里的读判决为空、写判决必须失败，且不得先建目录。
    """
    from mortis_rag_mcp.registry import VaultRegistry

    other = tmp_path / "other"
    other.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    registry_file = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(registry_file))
    registry = VaultRegistry(registry_file)
    registry.add(str(vault))
    registry.add(str(other))

    cfg = _config(other / "shared_cache")
    layout = resolve_storage_layout(cfg, vault)
    assert layout.blocked_reason == ""

    with pytest.raises(VirtualStorageDisabled):
        _new_store(layout, cfg, write=True)
    assert not layout.doc_store_dir.exists()


def test_symlinked_cache_root_is_detected_as_inside_vault(tmp_path):
    """cache 根经 symlink/junction 落进库：必须按**真实路径**判定为库内。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    real = vault / "hidden_cache"
    real.mkdir()
    link = tmp_path / "cache_link"
    _make_dir_link(link, real)

    layout = resolve_storage_layout(_config(link), vault)

    assert layout.blocked_reason


def test_is_within_uses_real_paths(tmp_path):
    """归属判定按真实路径 + normcase，而不是字符串前缀。"""
    vault = tmp_path / "vault"
    (vault / "a").mkdir(parents=True)
    assert is_within(vault / "a", vault)
    assert not is_within(tmp_path / "vault_sibling", vault)
    assert not is_within(vault, vault / "a")


# ---------------------------------------------------------------------- 身份

def test_vault_cache_key_is_stable_across_path_spellings(tmp_path, monkeypatch):
    """vault key：大小写/尾部分隔符/symlink 拼写差异不得产生第二个缓存身份。"""
    vault = tmp_path / "MyVault"
    vault.mkdir()
    cfg = _config(tmp_path / "cache")

    key_plain = vault_cache_key(cfg, vault)
    key_trailing = vault_cache_key(cfg, Path(str(vault) + os.sep))
    assert key_plain == key_trailing

    link = tmp_path / "link_to_vault"
    _make_dir_link(link, vault)
    assert vault_cache_key(cfg, link) == key_plain


def test_explicit_cache_id_makes_key_path_independent(tmp_path):
    """显式 cache.id：路径拼写差异不改变身份（跨 agent 找到同一份缓存）。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = _config(tmp_path / "cache", id="portable-id")

    assert vault_cache_key(cfg, a) == vault_cache_key(cfg, b)
    assert vault_cache_key(_config(tmp_path / "cache"), a) != vault_cache_key(
        _config(tmp_path / "cache"), b
    )


def test_explicit_cache_id_bound_to_two_real_vaults_is_rejected(tmp_path):
    """同一显式 cache.id 被两个实际库绑定：第二个写入必须被拒（§12.2）。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = _config(tmp_path / "cache", id="shared-id")
    layout_a = resolve_storage_layout(cfg, a)
    layout_b = resolve_storage_layout(cfg, b)
    assert layout_a.control_path == layout_b.control_path

    first = ControlStore(layout_a)
    first.open()
    first.close()

    second = ControlStore(layout_b)
    with pytest.raises(StoreBindingMismatch):
        second.open(write=True)
    # 只读检视可以打开，但降级不是「共享」：任何写入路径都必须继续被拒
    probe = ControlStore(layout_b)
    probe.open()
    with pytest.raises(StoreBindingMismatch):
        with probe.mutation():
            pass
    # 库 A 自己仍然可写（拒绝只针对被误绑的第二方）
    assert first.state().epoch >= 1


# ------------------------------------------------------------ 写入前的拒绝

def _new_store(layout, cfg, *, write: bool = False) -> DocumentStore:
    """write 开关允许落在构造或 open()（实现自选其一，这里两种都接受）。"""
    import inspect

    if "write" in inspect.signature(DocumentStore.__init__).parameters:
        return DocumentStore(layout, cfg, write=write)
    store = DocumentStore(layout, cfg)
    if "write" in inspect.signature(store.open).parameters:
        store.open(write=write)
    else:
        store.open()
    return store


def test_cache_disabled_blocks_virtual_write_before_creating_files(tmp_path):
    """cache.enabled=false：虚拟摄取在写任何东西之前就被拒（物理文本不受影响）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = _config(tmp_path / "cache", enabled=False)
    layout = resolve_storage_layout(cfg, vault)
    assert layout.enabled is False

    with pytest.raises(VirtualStorageDisabled):
        _new_store(layout, cfg, write=True)
    assert not layout.doc_store_dir.exists()


def test_home_inside_vault_blocks_virtual_write_and_keeps_config_untouched(tmp_path):
    """home 落库内：拒绝虚拟写入 + 给出修复建议，且**不改**用户的显式配置。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    cache_dir = vault / ".cache"
    cfg = _config(cache_dir)
    layout = resolve_storage_layout(cfg, vault)

    with pytest.raises(VirtualStorageDisabled):
        _new_store(layout, cfg, write=True)

    assert cfg.cache.dir == str(cache_dir)          # 不静默改道
    assert cfg.cache.placement == "home"
    assert not cache_dir.exists()


# ---------------------------------------------------------- 源路径规范化

def test_normalize_source_path_keeps_suffix_and_normalizes_separators(tmp_path):
    vault = tmp_path / "vault"
    (vault / "docs").mkdir(parents=True)
    assert normalize_source_path("docs/a.pdf", vault) == "docs/a.pdf"
    assert normalize_source_path("docs\\a.pdf", vault) == "docs/a.pdf"
    assert normalize_source_path("a.docx", vault) == "a.docx"


@pytest.mark.parametrize(
    "bad",
    ["", ".", "..", "/abs/a.pdf", "C:\\evil.pdf", "//host/share/a.pdf", "../a.pdf", "a/../../b.pdf"],
)
def test_normalize_source_path_rejects_escapes(tmp_path, bad):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(DocStoreError):
        normalize_source_path(bad, vault)


def test_normalize_source_path_rejects_windows_device_names(tmp_path):
    """Win32 设备名（NUL/CON/aux.txt）不是库内普通文件：必须拒绝（POSIX 不适用）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    if os.name != "nt":
        pytest.skip("Windows 专属限制；POSIX 下 aux.pdf 是合法文件名")
    for bad in ("NUL", "con/out.pdf", "aux.txt", "COM1.pdf"):
        with pytest.raises(DocStoreError):
            normalize_source_path(bad, vault)


def test_normalize_source_path_rejects_nul(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(DocStoreError):
        normalize_source_path("a\x00.pdf", vault)


# ------------------------------------------------------------ 配置层拒绝

def test_unsafe_vault_subdir_rejected_at_load(tmp_path):
    """subdir 上跳/绝对路径：加载期即拒绝（错误发生在入队之前）。"""
    for raw in ('"../escape"', '"/abs"', '"C:\\\\x"'):
        toml = tmp_path / "app.toml"
        toml.write_text(
            f'[cache]\nenabled = true\nplacement = "vault"\nsubdir = {raw}\n', encoding="utf-8"
        )
        with pytest.raises(ValueError):
            load_config(toml)


def test_windows_device_name_subdir_rejected():
    """Windows 设备名不能当库内缓存子树（Win32 下会解析到设备而不是目录）。"""
    if os.name != "nt":
        pytest.skip("Windows 专属限制")
    for bad in ("NUL", "con", "aux.txt"):
        with pytest.raises(ValueError):
            AppConfig(cache=CacheConfig(placement="vault", subdir=bad))


def test_doc_store_bool_and_nan_rejected(tmp_path):
    """bool / NaN 不能冒充容量数字（bool 是 int 子类，NaN 与任何值比较都是 False）。"""
    for raw in ("true", "nan"):
        toml = tmp_path / "app.toml"
        toml.write_text(f"[doc_store]\nmax_size_mb = {raw}\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(toml)


def test_python310_fallback_parses_doc_store_section(tmp_path, monkeypatch):
    """Python 3.10 无 tomli 时走内建子集解析器：新段必须同样可读。"""
    toml = tmp_path / "app.toml"
    toml.write_text(
        '[cache]\nenabled = true\nplacement = "home"\n\n[doc_store]\nmax_size_mb = 512\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("mortis_rag_mcp.config.tomllib", None)

    cfg = load_config(toml)

    assert cfg.doc_store.max_size_mb == 512
    assert cfg.cache.placement == "home"


def test_fallback_parser_reads_doc_store_keys():
    data = _fallback_toml('[doc_store]\nmax_size_mb = 512\n')
    assert data["doc_store"]["max_size_mb"] == 512


def test_doc_store_config_default_and_validation():
    assert DocStoreConfig().max_size_mb == 2048
    assert DocStoreConfig(1024).max_size_bytes == 1024 * 1024 * 1024
    for bad in (True, 0, -1, "2048"):
        with pytest.raises(ValueError):
            DocStoreConfig(max_size_mb=bad)


# ---------------------------------------------- 与索引/监听接缝的真实行为

def test_home_placement_writes_nothing_into_vault_and_creates_no_docstore(tmp_path):
    """默认 home：索引不向库内写任何东西，且只读路径不创建文档库（§11.2/§12.6）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    indexer = MarkdownIndexer(vault, _config(cache_dir))

    indexer.sync()

    assert [p.name for p in vault.iterdir()] == ["a.md"]
    assert not (cache_dir / "default" / "doc_store").exists()


def test_indexer_document_store_seam_is_lazy_writable_and_closable(tmp_path):
    """C92 接缝端到端：惰性打开、写模式补门禁、close 后仍能读到已提交事实。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    indexer = MarkdownIndexer(vault, _config(cache_dir))
    indexer.sync()

    # 只读访问：不建库、不分配 generation（读路径零副作用）
    assert indexer._doc_store is None
    read_only = indexer.document_store()
    assert read_only.generation_id == ""
    assert not (cache_dir / "default" / "doc_store").exists()

    # 写模式：同一个实例补齐写门禁并分配 generation
    writable = indexer.document_store(write=True)
    assert writable is read_only
    assert writable.generation_id
    staged = writable.stage_revision(
        source="a.pdf",
        source_sha256="sha",
        render_sha256="render",
        parser_fingerprint="fp",
        markdown="# parsed",
    )
    writable.commit_revision(staged.revision_id)
    assert writable.get_active("a.pdf") is not None

    # close 后重开：committed 事实仍在（「重启」语义），句柄可释放
    indexer.close_document_store()
    assert indexer._doc_store is None
    reopened = indexer.document_store()
    assert reopened.get_active("a.pdf") is not None
    indexer.close_document_store()


def test_vault_placement_cache_subtree_is_excluded_from_scan(tmp_path):
    """显式 vault：库内缓存子树必须被 ignore 规则豁免，索引不得吃自己的缓存。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    cache_subdir = vault / ".mcp_cache"
    cache_subdir.mkdir()
    (cache_subdir / "cached.md").write_text("# cached\nshould not be indexed", encoding="utf-8")

    indexer = MarkdownIndexer(vault, _config(tmp_path / "unused", placement="vault"))
    matcher = indexer._ignore_matcher()
    assert matcher.is_ignored(".mcp_cache/cached.md")[0] is True

    indexer.sync()
    assert {chunk.source for chunk in indexer.all_chunks()} == {"a.md"}

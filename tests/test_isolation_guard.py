"""测试宿主隔离守卫（v0.8.1 C54）。

这里锁的是「测试不再碰宿主真实状态」的机制本身，而不是某个业务行为：
① 会话配置 pin 是真实存在的文件，且 resolve_config_path() 指向它；
② 无 [cache] 段的 app.toml 走 MORTIS_RAG_CACHE_DIR env 覆盖，落进会话隔离区（C54b 的验收口径）；
③ env 覆盖短路在 resolve_default_cache_dir() 的改名逻辑之前（T24：不得触发 os.rename）；
④ 子进程（stdio 用例以 {**os.environ} 传 env）继承同一个缓存根覆盖；
⑤ conftest 的 pytest_sessionfinish 守卫仍生效：绝不写宿主 STATUS.md/status.json。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import conftest
from mortis_rag_mcp.config import load_config, resolve_config_path, resolve_default_cache_dir


def test_session_config_pin_is_a_real_file():
    """① 假 pin（env 指向不存在的文件）会静默回落宿主配置——这里必须指向真实文件。"""
    pinned = Path(os.environ["MORTIS_RAG_CONFIG"])
    assert pinned.is_file(), "会话配置 pin 必须指向真实文件，否则 resolve_config_path 会静默回落宿主配置"
    assert "[cache]" in pinned.read_text(encoding="utf-8")
    assert resolve_config_path() == pinned
    assert Path(os.environ["MORTIS_RAG_CACHE_DIR"]).is_absolute()


def test_config_without_cache_section_lands_in_env_override(tmp_path):
    """② 没写 [cache] 段的配置走 env 覆盖，不再落真实 ~/.mortis_rag_mcp_cache。"""
    cfg_file = tmp_path / "app.toml"
    cfg_file.write_text('mode = "static"\n', encoding="utf-8")

    cfg = load_config(str(cfg_file))

    assert cfg.cache.enabled is True
    assert cfg.cache.dir == os.environ["MORTIS_RAG_CACHE_DIR"]


def test_env_override_short_circuits_before_directory_rename(tmp_path, monkeypatch):
    """③ env 覆盖早于改名逻辑：不得搬动宿主的 ~/.vault_mcp_cache（T24）。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old_cache = tmp_path / ".vault_mcp_cache"
    old_cache.mkdir()
    target = tmp_path / "redirected_cache"
    monkeypatch.setenv("MORTIS_RAG_CACHE_DIR", str(target))

    with patch.object(Path, "rename", side_effect=AssertionError("env 覆盖下不允许发生目录改名")):
        assert resolve_default_cache_dir() == str(target)

    assert old_cache.exists(), "旧缓存目录必须原地不动"
    assert not (tmp_path / ".mortis_rag_mcp_cache").exists()


def test_cache_env_inherited_by_subprocess():
    """④ stdio 用例把 os.environ 整包传给子进程，覆盖必须一起继承下去。"""
    code = "import os;print(os.environ.get('MORTIS_RAG_CACHE_DIR',''))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.stdout.strip() == os.environ["MORTIS_RAG_CACHE_DIR"]


def test_sessionfinish_guard_never_writes_host_status(monkeypatch):
    """⑤ 守卫仍在：NO_STATUS_HOOK 先短路，绝不调用 doctor.record_test_run。"""
    assert os.environ.get("MORTIS_RAG_NO_STATUS_HOOK") == "1"
    from mortis_rag_mcp import doctor

    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("测试运行不得写宿主 STATUS.md")

    monkeypatch.setattr(doctor, "record_test_run", _boom)
    conftest.pytest_sessionfinish(MagicMock(), 0)
    assert called["n"] == 0

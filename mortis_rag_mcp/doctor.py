"""本机环境自检与 STATUS.md 生成器（agent 信任锚）。

设计约束（与仓库规约一致）：
- 零第三方依赖：复用 providers.py，不碰外部网络库，杜绝响应截断。
- 派生数据哲学：单项探测失败只记入报告，绝不抛异常拖垮整体。
- 原子写：tmp + replace（带 PID 和 Thread-ID，并带 Windows 访问冲突重试）。
- 离线容错：单库离线给 Warning，不锁死全局状态。
- 门禁防伪：core_present 严密把关，杜绝空态 all([]) == True 虚假 VALID。
输出（均在 ~/.mortis_rag_mcp/ 下）：
- status.json  机器可读全量报告
- STATUS.md    人类/agent 可读信任锚
退出码：0 = overall ok；1 = 存在失败项。
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

FRESHNESS_DAYS = 7


# ---------- 路径与小工具 ----------

def _status_dir() -> Path:
    from .registry import user_config_dir
    return user_config_dir()


def _status_md_path() -> Path:
    return _status_dir() / "STATUS.md"


def _status_json_path() -> Path:
    return _status_dir() / "status.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _section(ok: bool, detail: str) -> dict:
    return {"ok": ok, "detail": detail, "at": _now_iso()}


def _read_json() -> dict:
    try:
        return json.loads(_status_json_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        # Windows 文件锁冲突重试（应对 Agent/编辑器并发读取 Win32 ERROR_ACCESS_DENIED）
        for attempt in range(4):
            try:
                tmp.replace(path)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _repo_version() -> str:
    try:
        pyproject = _repo_root() / "pyproject.toml"
        if pyproject.is_file():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("version") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    try:
        import importlib.metadata
        return importlib.metadata.version("mortis-rag-mcp")
    except Exception:
        pass
    return "unknown"


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_repo_root(), capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ---------- 探测项 ----------

def check_python() -> dict:
    ok = sys.version_info >= (3, 10)
    return _section(ok, f"{platform.python_version()}（{sys.executable}）" + ("" if ok else "，需要 >= 3.10"))


def check_package() -> dict:
    try:
        import mortis_rag_mcp  # noqa: F401
        return _section(True, f"import 成功，版本 {_repo_version()}，commit {_git_commit()}")
    except Exception as exc:
        return _section(False, f"import 失败：{exc}（检查 PYTHONPATH / venv）")


def check_optional_deps() -> dict:
    found, missing = [], []
    for mod in ("numpy", "sqlite_vec"):
        try:
            m = __import__(mod)
            found.append(f"{mod} {getattr(m, '__version__', '?')}")
        except Exception:
            missing.append(mod)
    detail = "已装：" + "、".join(found) if found else ""
    if missing:
        detail += ("；" if detail else "") + "未装（自动回退，不影响核心）：" + "、".join(missing)
    return _section(True, detail)


def check_config(app_config: str | None) -> tuple[dict, object | None]:
    try:
        from .config import load_config, resolve_config_path, resolve_api_key
        path = resolve_config_path(app_config)
        cfg = load_config(path)
        emb = getattr(cfg, "embedding", None)
        mode = getattr(emb, "mode", "?")
        model = getattr(emb, "model", "?")
        endpoint = str(getattr(emb, "endpoint", "")).lower()
        key = resolve_api_key(getattr(emb, "api_key", "") or "")
        is_local = any(h in endpoint for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"))
        key_configured = bool(key) or mode == "static" or is_local

        # 消除 static 模式下自相矛盾的"缺失"提示
        if key:
            status_text = "已配置"
        elif is_local:
            status_text = "免密/本地"
        elif mode == "static":
            status_text = "免密(内置哈希)"
        else:
            status_text = "缺失"

        rr = getattr(cfg, "reranker", None)
        rr_detail = ""
        if getattr(rr, "enabled", False):
            rr_key = resolve_api_key(getattr(rr, "api_key", "") or "")
            rr_ep = str(getattr(rr, "endpoint", "")).lower()
            rr_local = any(h in rr_ep for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"))
            if not (rr_key or rr_local):
                key_configured = False
                rr_detail = "，reranker api_key 缺失"

        detail = f"{Path(str(path)).name if path else '内置默认'}：embedding={mode}/{model}，api_key {status_text}{rr_detail}"
        return _section(key_configured, detail), cfg
    except Exception as exc:
        return _section(False, f"配置加载失败：{exc}"), None


def check_registry() -> dict:
    try:
        from .registry import VaultRegistry, registry_path
        reg = VaultRegistry(registry_path())
        vaults = reg.load()
        names, missing = [], []
        for v in vaults:
            name = str(getattr(v, "name", getattr(v, "path", "?")))
            names.append(name)
            p = getattr(v, "path", None)
            if p and not Path(str(p)).exists():
                missing.append(f"{name}({p})")
        if not vaults:
            return _section(True, "注册表为空（尚未注册知识库，正常）")
        if missing:
            available_count = len(vaults) - len(missing)
            if available_count > 0:
                return _section(True, f"{available_count}/{len(vaults)} 个库可用；离线库: {', '.join(missing)}")
            return _section(False, f"全部注册库均不可访问: {', '.join(missing)}")
        return _section(True, f"{len(vaults)} 个库全部在线：{'、'.join(names)}")
    except Exception as exc:
        return _section(False, f"注册表加载失败：{exc}")


def check_cache_dir(cfg: object | None) -> dict:
    try:
        cache_cfg = getattr(cfg, "cache", None)
        d = getattr(cache_cfg, "dir", None) or (Path.home() / ".mortis_rag_mcp_cache")
        d = Path(str(d))
        return _section(True, f"{d}（{'存在' if d.exists() else '初次索引时自动创建'}）")
    except Exception as exc:
        return _section(True, f"缓存目录检查跳过：{exc}")


def probe_embedding(cfg: object) -> dict:
    try:
        from .providers import create_embedding_provider
        emb = getattr(cfg, "embedding", None)
        mode = getattr(emb, "mode", "static")
        if mode != "external":
            return _section(True, f"mode={mode}（非 external，跳过在线探测）")
        provider = create_embedding_provider(emb)
        t0 = time.monotonic()
        vecs = provider.embed(["ping"])
        ms = int((time.monotonic() - t0) * 1000)
        if vecs and len(vecs) == 1 and len(vecs[0]) > 0:
            actual_dim = len(vecs[0])
            expected_dim = getattr(emb, "dimension", None)
            if expected_dim and actual_dim != expected_dim:
                return _section(False, f"维度不匹配：模型返回 {actual_dim} 维，配置预期 {expected_dim} 维")
            return _section(True, f"探活成功，dim={actual_dim}，{ms}ms")
        return _section(False, "探活返回空向量")
    except Exception as exc:
        return _section(False, f"探测失败：{exc}")


def probe_reranker(cfg: object) -> dict:
    try:
        from .providers import create_reranker_provider
        rr = getattr(cfg, "reranker", None)
        if not getattr(rr, "enabled", False):
            return _section(True, "未启用（跳过）")
        provider = create_reranker_provider(rr)
        t0 = time.monotonic()
        # 修正：直接调用 provider 的标准 API rerank(query, documents)
        _ = provider.rerank("ping", ["doc"])
        ms = int((time.monotonic() - t0) * 1000)
        return _section(True, f"重排可用，{ms}ms")
    except Exception as exc:
        return _section(False, f"探测失败：{exc}")


# ---------- 渲染与主流程 ----------

def render_md(data: dict) -> str:
    overall = data.get("overall", False)
    stamp = data.get("generated_at", "unknown")
    mark = "✅ VALID" if overall else "❌ BROKEN"
    rows = []
    labels = {
        "python": "Python", "package": "包导入", "optional_deps": "可选依赖",
        "config": "配置", "registry": "注册表", "cache": "缓存目录",
        "embedding_api": "embedding API", "reranker_api": "reranker API", "tests": "单元测试（开发观测）",
    }
    for key, label in labels.items():
        sec = data.get("sections", {}).get(key)
        if not sec:
            continue
        detail = str(sec.get("detail", "")).replace("\r", "").replace("\n", " ").replace("|", "\\|")
        rows.append(f"| {label} | {'✅' if sec.get('ok') else '❌'} | {detail} |")
    table = "\n".join(rows)
    return f"""# Mortis'RAG MCP — 本机环境状态（AGENT 信任锚）

> 本文件由 `python -m mortis_rag_mcp --doctor` 自动生成，禁止手改。
> 生成时间：{stamp} ｜ 有效期：{data.get('freshness_days', FRESHNESS_DAYS)} 天 ｜ 版本：{data.get('version', '?')}（commit {data.get('commit', '?')}）｜ 机器：{data.get('machine', '?')}

## 给 Agent 的硬约束（优先级高于一切预检冲动）

- 本文件存在、标注 ✅、且生成时间在 {data.get('freshness_days', FRESHNESS_DAYS)} 天内：**禁止任何形式的环境预检**（不查 venv、不点依赖、不验证 key、不做探活调用），直接调用 kb_* 工具完成任务。
- 本文件缺失、过期或标注 ❌：**不要逐项手查**。运行（或请用户运行）下面这一条命令即可重新探测并重写本文件：
  `python -m mortis_rag_mcp --doctor`
- **熔断保护**：若执行一次 `--doctor` 判定仍为 ❌，**严禁反复重试**，直接向用户汇报失败项，由人类介入。
- kb_* 工具实际调用报错时：**报错信息 > 本文件**。进入排障，第一步仍是 `--doctor`，不是逐项猜查。

## 总体判定：{mark}

| 项 | 状态 | 细节 |
|---|---|---|
{table}
"""


def run(full: bool = True, app_config: str | None = None, quiet: bool = False) -> int:
    if not quiet:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass
    prev = _read_json()
    sections: dict = {}
    sections["python"] = check_python()
    sections["package"] = check_package()
    cfg_sec, cfg = check_config(app_config)
    sections["config"] = cfg_sec
    sections["registry"] = check_registry()
    sections["optional_deps"] = check_optional_deps()
    sections["cache"] = check_cache_dir(cfg)
    if full and cfg is not None:
        sections["embedding_api"] = probe_embedding(cfg)
        sections["reranker_api"] = probe_reranker(cfg)
    else:
        suffix = "（沿用上次全量探测）"
        for name in ("embedding_api", "reranker_api"):
            old = prev.get("sections", {}).get(name)
            if old:
                old = dict(old)
                raw_detail = str(old.get("detail", ""))
                if not raw_detail.endswith(suffix):
                    old["detail"] = raw_detail + suffix
                sections[name] = old
    merged = {**prev.get("sections", {}), **sections}
    # 门禁加固：核心项至少 4 项（python, package, config, registry）全部在场且 ok
    core_keys = ["python", "package", "config", "registry", "embedding_api", "reranker_api"]
    core_present = [k for k in core_keys if k in merged]
    overall = (len(core_present) >= 4) and all(merged[k].get("ok") for k in core_present)
    data = {
        "generated_at": _now_iso(),
        "freshness_days": FRESHNESS_DAYS,
        "overall": overall,
        "version": _repo_version(),
        "commit": _git_commit(),
        "machine": platform.node(),
        "sections": merged,
    }
    _atomic_write(_status_json_path(), json.dumps(data, ensure_ascii=False, indent=2))
    _atomic_write(_status_md_path(), render_md(data))
    if not quiet:
        print(render_md(data))
        print(f"overall={'VALID' if overall else 'BROKEN'}，已写入 {_status_md_path()}")
    return 0 if overall else 1


def record_test_run(passed: int, failed: int, skipped: int, total_collected: int = 0) -> None:
    """供 tests/conftest.py 调用：仅记录成绩，绝不污染 overall 运行时判定，杜绝空跑伪阳性。"""
    try:
        data = _read_json()
        sections = data.get("sections", {})
        is_full_run = (total_collected >= 150) or (total_collected > 0 and (passed + failed + skipped) == total_collected and total_collected >= 100)
        detail_suffix = "" if is_full_run else "（局部测试）"
        sections["tests"] = _section(failed == 0, f"{passed} passed, {failed} failed, {skipped} skipped（commit {_git_commit()}）{detail_suffix}")
        data["sections"] = sections
        data.setdefault("generated_at", _now_iso())
        data.setdefault("freshness_days", FRESHNESS_DAYS)
        data.setdefault("version", _repo_version())
        data.setdefault("commit", _git_commit())
        data.setdefault("machine", platform.node())
        # 门禁约束：只有核心项真实存在且过半时才核算 overall，防止空文件被测试跑出伪 VALID
        core_keys = ["python", "package", "config", "registry", "embedding_api", "reranker_api"]
        core_present = [k for k in core_keys if k in sections]
        data["overall"] = (len(core_present) >= 4) and all(sections[k].get("ok") for k in core_present)
        _atomic_write(_status_json_path(), json.dumps(data, ensure_ascii=False, indent=2))
        _atomic_write(_status_md_path(), render_md(data))
    except Exception:
        pass

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
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

FRESHNESS_DAYS = 7

# 沿用标注的统一形状：必须以「（沿用…本次未重新…）」结尾，且不含嵌套括号。
# 只认这一种形状，避免把探活项自身合法含括号的 detail 切坏（见 _strip_carryover_note）。
_CARRYOVER_NOTE_RE = re.compile(r"（沿用[^（）]*本次未重新[^（）]*）\s*$")


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


def _section_age_minutes(at: object) -> int | None:
    """解析 section 的 at 时间戳，返回距今的整分钟数；解析失败返回 None。

    供 full=False 沿用旧探测结果时生成肉眼可读的陈旧度标注（对抗审查 F5）。
    """
    if not isinstance(at, str) or not at:
        return None
    try:
        then = datetime.fromisoformat(at)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.astimezone()
    delta = datetime.now(then.tzinfo) - then
    return max(0, int(delta.total_seconds() // 60))


def _section(ok: bool, detail: str) -> dict:
    return {"ok": ok, "detail": detail, "at": _now_iso()}


def _strip_carryover_note(detail: str) -> str:
    """剥掉上一次 full=False 追加的「沿用」标注，保证重复刷新不叠加。

    刻意不用 `rsplit("（", 1)[0]`：探活项自身的 detail 就合法地含全角括号
    （`mode=static（非 external，跳过在线探测）`、`未启用（跳过）`），按最后一个
    「（」切会把原始信息一并切掉，刷新后的 detail 变成一句自相矛盾的话。
    这里只认完整的「（沿用…本次未重新…）」尾部形状。
    """
    return _CARRYOVER_NOTE_RE.sub("", detail)


def _carry_probe_section(section: Mapping[str, Any]) -> dict:
    """沿用旧的全量探活结果，并把「陈旧」写进 detail（对抗审查 F5）。

    full=False 不重新探活外部 API，若沿用旧 ok=True 而实际已宕机，信任锚会在最长
    FRESHNESS_DAYS 新鲜度窗口内显示假健康；把上次探测的距今时长直接写进 detail，
    消费方无需比对时间戳即可识破。
    """
    carried = dict(section)
    age_min = _section_age_minutes(carried.get("at"))
    age_note = (
        f"（沿用 {age_min} 分钟前的全量探测结果，本次未重新探活）"
        if age_min is not None
        else "（沿用上次全量探测结果，本次未重新探活）"
    )
    carried["detail"] = _strip_carryover_note(str(carried.get("detail", ""))) + age_note
    return carried


def _carry_tests_section(section: Mapping[str, Any]) -> dict:
    """沿用旧的测试成绩，并同样标注陈旧度。

    测试成绩只在 pytest 里刷新，`--doctor` 与启动期轻量刷新都不会重跑；若原样并入
    merged，`--doctor` 刚把 generated_at 刷成"现在"，旧成绩单就会被读成"刚刚测过"。
    """
    carried = dict(section)
    age_min = _section_age_minutes(carried.get("at"))
    age_note = (
        f"（沿用 {age_min} 分钟前的测试成绩，本次未重新跑测试）"
        if age_min is not None
        else "（沿用上次测试成绩，本次未重新跑测试）"
    )
    carried["detail"] = _strip_carryover_note(str(carried.get("detail", ""))) + age_note
    return carried


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
    """探测可选加速依赖的安装情况（只探存在性，不真正 import）。

    刻意不用 `__import__`：本函数在服务端启动的后台刷新线程里跑，与 stdio 握手
    同期；numpy 的**首次导入**是数百毫秒级 CPU，会与握手抢 GIL，而这里只是想
    在报告里写一行版本号 —— 代价明显不成比例。改用 `find_spec` 探存在性
    （对顶层模块不触发导入）+ 发行档案读版本号。

    退化面：极少数只以裸目录存在、没有 dist-info/metadata 的包会显示 `unknown`
    而非真实 `__version__`；这类包在本项目语境下不存在（numpy / sqlite-vec 都以
    发行包形式安装）。
    """
    import importlib.metadata as _md
    import importlib.util as _ilu

    found, missing = [], []
    for mod in ("numpy", "sqlite_vec"):
        try:
            spec = _ilu.find_spec(mod)
        except Exception:
            spec = None
        if spec is None:
            missing.append(mod)
            continue
        try:
            ver = _md.version(mod)
        except Exception:
            ver = "unknown"
        found.append(f"{mod} {ver}")
    detail = "已装：" + "、".join(found) if found else ""
    if missing:
        detail += ("；" if detail else "") + "未装（自动回退，不影响核心）：" + "、".join(missing)
    return _section(True, detail)


def _is_local_endpoint(endpoint: str) -> bool:
    """判定 endpoint 是否指向本机 —— 必须 fail-closed。

    信任锚语义：判为「本机」即允许免 API key，且 STATUS.md 会据此标 ✅。
    因此这里只认两类明确形态：

    1. 主机名白名单（`localhost` / `host.docker.internal`，后者是容器内访问宿主的
       既定写法，显式保留）；
    2. 能严格解析为 IP 字面量、且确属环回（含 `::ffff:127.0.0.1` 这类 v4-mapped
       v6）或全零地址（`0.0.0.0` / `::`，连接时由内核映射回本机）。

    绝不做前缀/子串模糊匹配：此前 `host.startswith("127.")` 会把
    `127.0.0.1.attacker.com`、`127.example.com` 这类**远端域名**判成本机，
    于是远端端点漏配 key 也会被信任锚标成健康，agent 跳过预检直到真实调用才撞
    401 —— 这正是信任锚最不能犯的错（说谎）。

    代价是 `127.1` / 十进制 `2130706433` 等花式 IP 写法不再被认作本机，会要求
    配置 key —— 方向是 fail-closed，可接受。
    """
    if not endpoint:
        return False
    try:
        from urllib.parse import urlsplit
        ep = endpoint if "://" in endpoint else f"http://{endpoint}"
        host = (urlsplit(ep).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost", "host.docker.internal"):
        return True
    try:
        from ipaddress import ip_address
        addr = ip_address(host)
    except ValueError:
        # 域名（含 127.0.0.1.attacker.com / localhost. 尾点 / punycode 等）到此即拒。
        return False
    if addr.is_loopback:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None and mapped.is_loopback:
        return True
    return addr.is_unspecified


def check_config(app_config: str | None) -> tuple[dict, object | None]:
    try:
        from .config import load_config, resolve_config_path, resolve_api_key
        path = resolve_config_path(app_config)
        cfg = load_config(path)
        emb = getattr(cfg, "embedding", None)
        mode = getattr(emb, "mode", "?")
        model = getattr(emb, "model", "?")
        endpoint = str(getattr(emb, "endpoint", "")).strip()
        key = resolve_api_key(getattr(emb, "api_key", "") or "")
        is_local = _is_local_endpoint(endpoint)
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

        missing_fields = []
        if mode == "external":
            if not endpoint:
                key_configured = False
                missing_fields.append("endpoint缺失")
            if not model:
                key_configured = False
                missing_fields.append("model缺失")

        rr = getattr(cfg, "reranker", None)
        rr_detail = ""
        if getattr(rr, "enabled", False):
            rr_key = resolve_api_key(getattr(rr, "api_key", "") or "")
            rr_ep = str(getattr(rr, "endpoint", "")).strip()
            rr_model = str(getattr(rr, "model", "")).strip()
            rr_local = _is_local_endpoint(rr_ep)
            if not rr_ep:
                key_configured = False
                rr_detail += "，reranker endpoint 缺失"
            if not rr_model:
                key_configured = False
                rr_detail += "，reranker model 缺失"
            if not (rr_key or rr_local):
                key_configured = False
                rr_detail += "，reranker api_key 缺失"

        missing_str = f"（{'/'.join(missing_fields)}）" if missing_fields else ""
        detail = f"{Path(str(path)).name if path else '内置默认'}：embedding={mode}/{model}，api_key {status_text}{missing_str}{rr_detail}"
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
    from .registry import _process_file_lock
    with _process_file_lock(_status_dir() / "status.lock"):
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
            # 对齐对抗审查 F5：沿用旧探测结果时必须让"陈旧"肉眼可见。
            for name in ("embedding_api", "reranker_api"):
                old = prev.get("sections", {}).get(name)
                if old:
                    sections[name] = _carry_probe_section(old)
        # tests 与探活无关，full=True / full=False 都不会重新采集，因此两个分支都要
        # 补陈旧度标注：否则 --doctor 刚刷新的 generated_at 会把旧成绩单一起"续期"。
        prev_sections = prev.get("sections", {})
        if "tests" not in sections and prev_sections.get("tests"):
            sections["tests"] = _carry_tests_section(prev_sections["tests"])
        merged = {**prev.get("sections", {}), **sections}

        # 门禁加固：
        # 1. 核心项 python, package, config, registry 全部在场且 ok
        # 2. 若 embedding 模式为 external，embedding_api 必须在场且 ok，杜绝未探测外部网络即放行
        # 3. 若 reranker 启用，reranker_api 必须在场且 ok
        core_keys = ["python", "package", "config", "registry"]
        emb_mode = getattr(getattr(cfg, "embedding", None), "mode", "static") if cfg else "static"
        if emb_mode == "external":
            core_keys.append("embedding_api")
        if cfg and getattr(getattr(cfg, "reranker", None), "enabled", False):
            core_keys.append("reranker_api")

        prev_gen = prev.get("generated_at")
        is_fresh = True
        if full or not prev_gen:
            gen_at = _now_iso()
        else:
            gen_at = prev_gen
            try:
                gen_dt = datetime.fromisoformat(prev_gen)
                age_days = (datetime.now(timezone.utc).astimezone() - gen_dt).total_seconds() / 86400.0
                if age_days > FRESHNESS_DAYS:
                    is_fresh = False
            except Exception:
                is_fresh = True

        all_core_ok = all(k in merged and merged[k].get("ok") for k in core_keys)
        overall = all_core_ok and is_fresh
        data = {
            "generated_at": gen_at,
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
        from .registry import _process_file_lock
        with _process_file_lock(_status_dir() / "status.lock"):
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
            # 门禁约束：只有核心项真实存在且有效，且原有 data 中已有 overall 时才核算，防止空文件被测试跑出伪 VALID
            core_keys = ["python", "package", "config", "registry"]
            if "embedding_api" in sections:
                core_keys.append("embedding_api")
            if "reranker_api" in sections:
                core_keys.append("reranker_api")
            core_present = [k for k in core_keys if k in sections]
            if len(core_present) >= 4 and all(sections[k].get("ok") for k in core_present):
                data["overall"] = bool(data.get("overall", False))
            else:
                data["overall"] = False
            _atomic_write(_status_json_path(), json.dumps(data, ensure_ascii=False, indent=2))
            _atomic_write(_status_md_path(), render_md(data))
    except Exception:
        pass


from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ImportError:  # Python 3.10 without the optional tomli backport.
    tomllib = None


# 规范 API key 环境变量：config 里 api_key 为空时回退读取（${ENV} 插值仍优先）。
API_KEY_ENV_VAR = "VAULT_MCP_API_KEY"


@dataclass(slots=True)
class EmbeddingConfig:
    mode: str = "static"
    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 30.0
    dimension: int = 384
    # Send the "dimensions" field in the request body. MRL-capable models
    # (Qwen3-Embedding series) accept it; fixed-dimension models (bge-m3) reject
    # it with HTTP 400, so set this to false for those.
    send_dimensions: bool = True
    # Extra attempts after the first failure (0 keeps the old fail-fast behavior).
    max_retries: int = 3
    # Max texts per embedding HTTP request. <=0 disables batching and sends
    # every chunk of a file in one request (old behavior).
    batch_size: int = 32
    # Base seconds for the exponential backoff between retries.
    retry_backoff: float = 1.0


@dataclass(slots=True)
class RerankerConfig:
    enabled: bool = False
    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 30.0


@dataclass(slots=True)
class VectorConfig:
    # 向量存储后端："memory"（默认，numpy 暴力扫描 + 现有 .bin 缓存）或
    # "sqlite_vec"（可选依赖 sqlite-vec，未安装时自动回退 memory）。
    backend: str = "memory"


DEFAULT_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".vault_mcp_cache")


@dataclass(slots=True)
class CacheConfig:
    # Programmatically constructed configs (e.g. unit tests) default to disabled;
    # load_config() enables it by default unless the TOML explicitly disables it.
    dir: str = DEFAULT_CACHE_DIR
    enabled: bool = False
    embedding_max_workers: int = 6
    # "home": cache files under `dir` (default ~/.vault_mcp_cache).
    # "vault": cache files under a hidden `.mcp_cache/` subfolder of each vault,
    # which keeps every sub-library's vectors next to its own notes.
    placement: str = "home"
    subdir: str = ".mcp_cache"
    # Cache namespace: a stable subfolder under the cache root so different
    # projects / agents never collide with each other's entries. Keep "default"
    # for personal single-tenant use; change it only when sharing a machine.
    namespace: str = "default"
    # Optional explicit cache identity. When set, the cache file name is derived
    # from this value instead of the vault path, making the cache immune to path
    # spelling differences (case, symlinks, trailing separators) across agents.
    id: str = ""
    # Optional cleanup: delete stale cache files older than this many days.
    # 0 disables the sweep.
    max_age_days: int = 0


DEFAULT_EXCLUDE_PATTERNS = [".obsidian"]
DEFAULT_EXCLUDE_TAGS = ["no-rag", "private", "draft", "私密", "豁免"]
DEFAULT_EXCLUDE_FRONTMATTER_KEYS = ["rag_exclude", "rag_ignore", "no_rag"]


@dataclass(slots=True)
class AppConfig:
    # 空串 = 未配置默认库（0.3.0 起 vault 选择完全由用户级注册表接管，
    # 该字段仅作为 legacy [vault].path 的读取出口供自动迁移使用）。
    vault_path: str = ""
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    vector: VectorConfig = field(default_factory=VectorConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    # 混合检索开关：true（默认）用 FTS5 BM25 + 向量余弦 + bigram 词法三路 RRF
    # 融合；false 完整还原旧的「词法软信号 + 余弦」行为。
    use_hybrid: bool = True
    chunk_size: int = 1200
    chunk_overlap: int = 0
    # 图片 alt/图注注入（opt-in，默认关）：开启后在每张图片所在行后插入一行
    # `[图片: alt 图注 (文件名)]`，让图片的 alt/图注变得可检索。注意这会改变
    # chunk content → chunk.id，等价于全库重新 embedding——所以默认必须关闭。
    inject_image_captions: bool = False
    # Hybrid search tuning: how many candidates each RRF route (FTS5 BM25 /
    # vector cosine / bigram lexical) contributes to the fused pool, and the
    # cap on how many chunks a single rerank API call may carry.
    rrf_per_route: int = 40
    rerank_cap: int = 60
    # 单次 kb_search 允许返回的最大条数。客户端（或被提示注入的 LLM）传来的
    # top_k / limit 一律夹到这个范围内，避免 10**9 这类值让 sqlite-vec 去建
    # 千万级 KNN 堆，或让单次响应变成几 MB。
    max_top_k: int = 200
    debounce_seconds: float = 0.5
    # 文件监听方式："auto"（默认）= Windows 原生 ReadDirectoryChangesW 可用就用，
    # 否则退回轮询；"native" = 优先原生，启动失败退回轮询；"poll" = 始终轮询
    #（0.4.1 及以前的行为）。
    watch_method: str = "auto"
    # 原生监听生效期间的低频兜底：每隔这么多秒做一次全量 sha256 对账，覆盖
    # 原生事件可能丢失的极端情况。<=0 关闭兜底同步（只保留事件驱动）。
    watch_fallback_interval: float = 30.0
    exclude_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS))
    exclude_tags: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_TAGS))
    exclude_frontmatter_keys: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_FRONTMATTER_KEYS))
    ignore_file: str = ".vaultignore"

    def __post_init__(self) -> None:
        self.vault_path = os.fspath(self.vault_path)
        if self.embedding.mode not in {"static", "external"}:
            raise ValueError("embedding.mode must be 'static' or 'external'")
        if self.embedding.dimension < 1:
            raise ValueError("embedding.dimension must be positive")
        # 重试次数必须封顶：每次尝试都吃满一个 timeout，而调用链一直握着
        # _sync_lock，max_retries=1000 会把整个服务挂死数小时。
        if not 0 <= self.embedding.max_retries <= 10:
            raise ValueError("embedding.max_retries must be in [0, 10]")
        if self.embedding.batch_size < 0:
            raise ValueError("embedding.batch_size must be >= 0")
        if self.embedding.retry_backoff <= 0:
            raise ValueError("embedding.retry_backoff must be positive")
        if not 0 < self.embedding.timeout <= 300:
            raise ValueError("embedding.timeout must be in (0, 300]")
        if self.cache.embedding_max_workers < 1:
            raise ValueError("cache.embedding_max_workers must be positive")
        if self.cache.placement not in {"home", "vault"}:
            raise ValueError("cache.placement must be 'home' or 'vault'")
        if not self.cache.subdir:
            raise ValueError("cache.subdir must not be empty")
        if not self.cache.namespace:
            raise ValueError("cache.namespace must not be empty")
        if self.cache.max_age_days < 0:
            raise ValueError("cache.max_age_days must be >= 0")
        if self.vector.backend not in {"memory", "sqlite_vec"}:
            raise ValueError("vector.backend must be 'memory' or 'sqlite_vec'")
        if self.chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if self.chunk_overlap < 0 or self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be in [0, chunk_size)")
        if self.rrf_per_route < 1:
            raise ValueError("rrf_per_route must be >= 1")
        if self.rerank_cap < 1:
            raise ValueError("rerank_cap must be >= 1")
        if not 1 <= self.max_top_k <= 5000:
            raise ValueError("max_top_k must be in [1, 5000]")
        if self.watch_method not in {"auto", "native", "poll"}:
            raise ValueError("watch_method must be 'auto', 'native' or 'poll'")
        if self.watch_fallback_interval < 0:
            raise ValueError("watch_fallback_interval must be >= 0")


def _env(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([^}]+)\}", lambda m: os.getenv(m.group(1), ""), value)
    return value


def _fallback_toml(text: str) -> dict[str, Any]:
    """Small TOML subset for Python 3.10 when tomli is not installed."""
    import ast

    result: dict[str, Any] = {}
    section: dict[str, Any] = result
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # [[array-of-table]]: append a fresh dict to the named list.
        if line.startswith("[[") and line.endswith("]]"):
            name = line[2:-2].strip()
            lst = result.setdefault(name, [])
            if not isinstance(lst, list):
                raise ValueError(f"TOML conflict: [[{name}]] over a non-list key")
            section = {}
            lst.append(section)
            continue
        if line.startswith("[") and line.endswith("]"):
            section = result
            for part in line[1:-1].strip().split("."):
                section = section.setdefault(part, {})
            continue
        if "=" not in line:
            continue
        key, raw_value = (part.strip() for part in line.split("=", 1))
        raw_value = raw_value.split(" #", 1)[0].strip()
        if raw_value.lower() in {"true", "false"}:
            value: Any = raw_value.lower() == "true"
        elif raw_value.startswith("[") and raw_value.endswith("]"):
            value = [item.strip().strip('"\'') for item in raw_value[1:-1].split(",") if item.strip()]
        else:
            try:
                value = ast.literal_eval(raw_value)
            except (SyntaxError, ValueError):
                value = raw_value.strip('"\'')
        section[key] = value
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if tomllib is not None:
        return tomllib.loads(data.decode("utf-8"))
    return _fallback_toml(data.decode("utf-8"))


def read_toml_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Public TOML entry point (registry.py and config share the same parser)."""
    return _read_toml(Path(path))


def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Configuration resolution chain: --app-config > VAULT_MCP_CONFIG env
    > ~/.vault_mcp/config.toml > None (built-in defaults).

    This keeps vault-mcp portable: no path is ever baked into the source tree,
    and every user/device anchors its own settings under the home directory.
    """
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    env_path = os.getenv("VAULT_MCP_CONFIG", "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            return candidate
    home_candidate = Path.home() / ".vault_mcp" / "config.toml"
    return home_candidate if home_candidate.is_file() else None


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, Mapping) else {}


def _numeric(
    section: dict[str, Any],
    flat: dict[str, Any],
    key: str,
    kind: type,
    default: Any,
    minimum: float | None = None,
    maximum: float | None = None,
) -> Any:
    """读取数值型配置键（分组优先，其次扁平），类型/范围错误给出可读报错。

    此前一律裸写 int()/float()：TOML 里把 max_retries 写成 "3 次" 会抛一段原始
    traceback，把 rrf_per_route 写成 40.5 会被静默截断成 40。
    """
    raw = section.get(key, flat.get(key, default))
    # bool 是 int 的子类：int(True) == 1 会让 `timeout = true` 悄悄变成 1 秒。
    if isinstance(raw, bool):
        raise ValueError(f"config key '{key}' must be a {kind.__name__}, got a boolean")
    try:
        value = kind(raw)
    except (TypeError, ValueError):
        raise ValueError(f"config key '{key}' must be a {kind.__name__}, got {raw!r}") from None
    if kind is int and isinstance(raw, float) and raw != int(raw):
        raise ValueError(f"config key '{key}' must be an integer, got {raw!r}")
    # NaN 与任何值的比较都是 False，min/max 校验会全部放行，必须显式挡掉。
    if kind is float and not math.isfinite(value):
        raise ValueError(f"config key '{key}' must be a finite number, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"config key '{key}' must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"config key '{key}' must be <= {maximum}, got {value}")
    return value


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load app.toml while accepting both flat and grouped configuration keys."""
    data = _read_toml(Path(path)) if path is not None else {}
    vault = _section(data, "vault")
    embedding = {**data, **_section(data, "embedding")}
    reranker = {**data, **_section(data, "reranker")}
    vector = {**data, **_section(data, "vector")}
    cache = {**data, **_section(data, "cache")}
    index = _section(data, "index")

    vault_path = _env(data.get("vault_path", vault.get("path", "")))
    emb = EmbeddingConfig(
        mode=str(embedding.get("mode", "static")).lower(),
        endpoint=str(_env(embedding.get("endpoint", ""))),
        model=str(_env(embedding.get("model", ""))),
        api_key=str(_env(embedding.get("api_key", "")) or os.getenv(API_KEY_ENV_VAR, "")),
        timeout=_numeric(embedding, data, "timeout", float, 30.0, 0.0, 300.0),
        dimension=_numeric(embedding, data, "dimension", int, 384, 1),
        send_dimensions=bool(embedding.get("send_dimensions", True)),
        max_retries=_numeric(embedding, data, "max_retries", int, 3, 0, 10),
        batch_size=_numeric(embedding, data, "batch_size", int, 32, 0),
        retry_backoff=_numeric(embedding, data, "retry_backoff", float, 1.0, 0.0, 60.0),
    )
    rer = RerankerConfig(
        enabled=bool(reranker.get("enabled", False)),
        endpoint=str(_env(reranker.get("endpoint", ""))),
        model=str(_env(reranker.get("model", ""))),
        api_key=str(_env(reranker.get("api_key", "")) or os.getenv(API_KEY_ENV_VAR, "")),
        timeout=_numeric(reranker, data, "timeout", float, 30.0, 0.0, 300.0),
    )
    cch = CacheConfig(
        dir=str(_env(cache.get("dir", DEFAULT_CACHE_DIR))),
        enabled=bool(cache.get("enabled", True)),
        embedding_max_workers=_numeric(cache, data, "embedding_max_workers", int, 6, 1, 32),
        placement=str(cache.get("placement", "home")).lower(),
        subdir=str(cache.get("subdir", ".mcp_cache")),
        namespace=str(cache.get("namespace", "default")).lower(),
        id=str(_env(cache.get("id", ""))),
        max_age_days=_numeric(cache, data, "max_age_days", int, 0, 0),
    )
    raw_exclude_patterns = index.get("exclude_patterns", data.get("exclude_patterns", DEFAULT_EXCLUDE_PATTERNS))
    if isinstance(raw_exclude_patterns, str):
        exclude_patterns = [p.strip() for p in raw_exclude_patterns.split(",") if p.strip()]
    else:
        exclude_patterns = list(raw_exclude_patterns)

    raw_exclude_tags = index.get("exclude_tags", data.get("exclude_tags", DEFAULT_EXCLUDE_TAGS))
    if isinstance(raw_exclude_tags, str):
        exclude_tags = [t.strip() for t in raw_exclude_tags.split(",") if t.strip()]
    else:
        exclude_tags = list(raw_exclude_tags)

    raw_exclude_fm = index.get("exclude_frontmatter_keys", data.get("exclude_frontmatter_keys", DEFAULT_EXCLUDE_FRONTMATTER_KEYS))
    if isinstance(raw_exclude_fm, str):
        exclude_fm = [k.strip() for k in raw_exclude_fm.split(",") if k.strip()]
    else:
        exclude_fm = list(raw_exclude_fm)

    ignore_file = str(index.get("ignore_file", data.get("ignore_file", ".vaultignore")))

    return AppConfig(
        vault_path=str(vault_path),
        embedding=emb,
        reranker=rer,
        vector=VectorConfig(backend=str(vector.get("backend", "memory")).lower()),
        cache=cch,
        use_hybrid=bool(index.get("use_hybrid", data.get("use_hybrid", True))),
        chunk_size=_numeric(index, data, "chunk_size", int, 1200, 1),
        chunk_overlap=_numeric(index, data, "chunk_overlap", int, 0, 0),
        inject_image_captions=bool(index.get("inject_image_captions", data.get("inject_image_captions", False))),
        rrf_per_route=_numeric(index, data, "rrf_per_route", int, 40, 1),
        rerank_cap=_numeric(index, data, "rerank_cap", int, 60, 1),
        max_top_k=_numeric(index, data, "max_top_k", int, 200, 1, 5000),
        debounce_seconds=_numeric(index, data, "debounce_seconds", float, 0.5, 0.0, 60.0),
        watch_method=str(index.get("watch_method", data.get("watch_method", "auto"))).strip().lower(),
        watch_fallback_interval=_numeric(index, data, "watch_fallback_interval", float, 30.0, 0.0, 3600.0),
        exclude_patterns=exclude_patterns,
        exclude_tags=exclude_tags,
        exclude_frontmatter_keys=exclude_fm,
        ignore_file=ignore_file,
    )

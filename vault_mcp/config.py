from __future__ import annotations

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
    # Hybrid search tuning: how many candidates each RRF route (FTS5 BM25 /
    # vector cosine / bigram lexical) contributes to the fused pool, and the
    # cap on how many chunks a single rerank API call may carry.
    rrf_per_route: int = 40
    rerank_cap: int = 60
    debounce_seconds: float = 0.5
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
        if self.embedding.max_retries < 0:
            raise ValueError("embedding.max_retries must be >= 0")
        if self.embedding.batch_size < 0:
            raise ValueError("embedding.batch_size must be >= 0")
        if self.embedding.retry_backoff <= 0:
            raise ValueError("embedding.retry_backoff must be positive")
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
        timeout=float(embedding.get("timeout", 30.0)),
        dimension=int(embedding.get("dimension", 384)),
        send_dimensions=bool(embedding.get("send_dimensions", True)),
        max_retries=int(embedding.get("max_retries", 3)),
        batch_size=int(embedding.get("batch_size", 32)),
        retry_backoff=float(embedding.get("retry_backoff", 1.0)),
    )
    rer = RerankerConfig(
        enabled=bool(reranker.get("enabled", False)),
        endpoint=str(_env(reranker.get("endpoint", ""))),
        model=str(_env(reranker.get("model", ""))),
        api_key=str(_env(reranker.get("api_key", "")) or os.getenv(API_KEY_ENV_VAR, "")),
        timeout=float(reranker.get("timeout", 30.0)),
    )
    cch = CacheConfig(
        dir=str(_env(cache.get("dir", DEFAULT_CACHE_DIR))),
        enabled=bool(cache.get("enabled", True)),
        embedding_max_workers=int(cache.get("embedding_max_workers", 6)),
        placement=str(cache.get("placement", "home")).lower(),
        subdir=str(cache.get("subdir", ".mcp_cache")),
        namespace=str(cache.get("namespace", "default")).lower(),
        id=str(_env(cache.get("id", ""))),
        max_age_days=int(cache.get("max_age_days", 0)),
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
        chunk_size=int(index.get("chunk_size", data.get("chunk_size", 1200))),
        chunk_overlap=int(index.get("chunk_overlap", data.get("chunk_overlap", 0))),
        rrf_per_route=int(index.get("rrf_per_route", data.get("rrf_per_route", 40))),
        rerank_cap=int(index.get("rerank_cap", data.get("rerank_cap", 60))),
        debounce_seconds=float(index.get("debounce_seconds", data.get("debounce_seconds", 0.5))),
        exclude_patterns=exclude_patterns,
        exclude_tags=exclude_tags,
        exclude_frontmatter_keys=exclude_fm,
        ignore_file=ignore_file,
    )

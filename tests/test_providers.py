from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig, RerankerConfig, load_config
from mortis_rag_mcp import providers
from mortis_rag_mcp.providers import ExternalEmbeddingProvider, ExternalRerankerProvider, ProviderError, StaticEmbeddingProvider


def test_load_config_supports_static_embedding_and_optional_reranker(tmp_path):
    path = tmp_path / "app.toml"
    path.write_text(
        """
        [vault]
        path = "C:/知识库/Obsidian Vault"
        [embedding]
        mode = "static"
        dimension = 3
        [reranker]
        enabled = false
        """,
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.vault_path == tmp_path / "C:/知识库/Obsidian Vault" if False else "C:/知识库/Obsidian Vault"
    assert config.embedding.mode == "static"
    assert config.embedding.dimension == 3
    assert config.reranker.enabled is False


def test_external_embedding_request_has_expected_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        # 条数必须与输入一致：provider 会校验，少返回会抛 ProviderError。
        return _JsonResponse(
            {"data": [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3, 0.4]}]}
        )

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(
        endpoint="https://embedding.test/v1/embeddings",
        model="embed-model",
        api_key="secret",
        timeout=7,
    )

    assert provider.embed(["你好", "world"]) == [[0.1, 0.2], [0.3, 0.4]]
    request = captured["request"]
    assert isinstance(request, Request)
    assert request.full_url == "https://embedding.test/v1/embeddings"
    assert request.get_header("Authorization") == "Bearer secret"
    assert json.loads(request.data.decode("utf-8")) == {
        "model": "embed-model",
        "input": ["你好", "world"],
    }
    assert captured["timeout"] == 7


def test_external_reranker_request_has_expected_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return _JsonResponse({"results": [{"index": 1, "relevance_score": 0.8}]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalRerankerProvider(
        endpoint="https://rerank.test/v1/rerank",
        model="rerank-model",
        api_key="secret",
    )

    assert provider.rerank("问题", ["a", "b"]) == [
        {"index": 1, "relevance_score": 0.8}
    ]
    request = captured["request"]
    assert json.loads(request.data.decode("utf-8")) == {
        "model": "rerank-model",
        "query": "问题",
        "documents": ["a", "b"],
    }


def test_reranker_failure_degrades_to_original_order(monkeypatch):
    sleeps = []

    def fail(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fail)
    # 重试会退避等待，这里只记录不真睡，保持测试秒级完成。
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalRerankerProvider(endpoint="https://rerank.test")

    assert provider.rerank_or_none("q", ["a", "b"]) is None
    assert len(sleeps) == provider.max_retries


def test_static_embedding_does_not_call_llm(monkeypatch):
    monkeypatch.setattr(
        "mortis_rag_mcp.providers.urlopen",
        lambda *args, **kwargs: pytest.fail("static mode called an external model"),
    )
    provider = StaticEmbeddingProvider(dimension=4)

    vectors = provider.embed(["same", "same", "不同"])

    assert len(vectors) == 3
    assert all(len(vector) == 4 for vector in vectors)
    assert vectors[0] == vectors[1]


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


ENDPOINT = "https://embedding.test/v1/embeddings"


def _http_error(code: int, headers: dict | None = None) -> HTTPError:
    return HTTPError(ENDPOINT, code, "error", headers or {}, None)


def test_embedding_retries_after_429_then_succeeds(monkeypatch):
    calls: list[Request] = []
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise _http_error(429)
        return _JsonResponse({"data": [{"embedding": [0.3, 0.4]}]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model")

    assert provider.embed(["hello"]) == [[0.3, 0.4]]
    assert len(calls) == 2
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 1.0 * 1.5


def test_embedding_retries_after_500_then_succeeds(monkeypatch):
    calls: list[Request] = []
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise _http_error(500)
        return _JsonResponse({"data": [{"embedding": [1.0]}]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model")

    assert provider.embed(["hello"]) == [[1.0]]
    assert len(calls) == 2
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 1.0 * 1.5


def test_embedding_does_not_retry_on_404(monkeypatch):
    calls: list[Request] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise _http_error(404)

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model")

    with pytest.raises(ProviderError):
        provider.embed(["hello"])
    assert len(calls) == 1


def test_embedding_gives_up_after_max_retries(monkeypatch):
    calls: list[Request] = []
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise _http_error(429)

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model", max_retries=2)

    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])
    assert len(calls) == 3
    assert "after 3 attempts" in str(excinfo.value)
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 1.5
    assert 2.0 <= sleeps[1] <= 3.0


def test_embedding_backoff_grows_exponentially(monkeypatch):
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        raise _http_error(500)

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(
        endpoint=ENDPOINT, model="embed-model", max_retries=3, retry_backoff=1.0
    )

    with pytest.raises(ProviderError):
        provider.embed(["hello"])
    assert len(sleeps) == 3
    # 退避带向上抖动，只断言量级与单调性。
    assert 1.0 <= sleeps[0] <= 1.5
    assert 2.0 <= sleeps[1] <= 3.0
    assert 4.0 <= sleeps[2] <= 6.0
    assert sleeps[0] < sleeps[1] < sleeps[2]


def test_embedding_honors_retry_after_header(monkeypatch):
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        raise _http_error(429, {"Retry-After": "5"})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model", max_retries=2)

    with pytest.raises(ProviderError):
        provider.embed(["hello"])
    assert len(sleeps) == 2
    assert all(5.0 <= seconds <= 7.5 for seconds in sleeps)


def test_embedding_ignores_non_numeric_retry_after_header(monkeypatch):
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        raise _http_error(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model", max_retries=2)

    with pytest.raises(ProviderError):
        provider.embed(["hello"])
    # HTTP-date 形式不解析，退回指数退避。
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 1.5
    assert 2.0 <= sleeps[1] <= 3.0


def test_embedding_splits_requests_by_batch_size(monkeypatch):
    batches: list[list[str]] = []

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        batches.append(payload["input"])
        return _JsonResponse({"data": [{"embedding": [float(len(text))]} for text in payload["input"]]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model", batch_size=2)

    vectors = provider.embed(["a", "bb", "ccc", "dddd", "eeeee"])

    assert batches == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
    assert vectors == [[1.0], [2.0], [3.0], [4.0], [5.0]]


def test_embedding_batch_size_zero_keeps_single_request(monkeypatch):
    batches: list[list[str]] = []

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        batches.append(payload["input"])
        return _JsonResponse({"data": [{"embedding": [0.0]} for _ in payload["input"]]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model", batch_size=0)

    assert len(provider.embed(["a", "b", "c"])) == 3
    assert batches == [["a", "b", "c"]]


def test_embedding_batch_rejects_short_response(monkeypatch):
    def fake_urlopen(request, timeout):
        return _JsonResponse({"data": [{"embedding": [0.1]}]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model", batch_size=2)

    with pytest.raises(ProviderError, match="returned 1 vectors for 2 inputs"):
        provider.embed(["a", "bb", "ccc"])


def test_load_config_reads_embedding_retry_settings(tmp_path):
    path = tmp_path / "app.toml"
    path.write_text(
        """
        [embedding]
        mode = "external"
        max_retries = 5
        batch_size = 16
        retry_backoff = 0.5
        """,
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.embedding.max_retries == 5
    assert config.embedding.batch_size == 16
    assert config.embedding.retry_backoff == 0.5


def test_embedding_retry_defaults():
    embedding = EmbeddingConfig(mode="external")

    assert embedding.max_retries == 3
    assert embedding.batch_size == 32
    assert embedding.retry_backoff == 1.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_retries": -1},
        {"batch_size": -1},
        {"retry_backoff": 0},
    ],
)
def test_embedding_retry_settings_reject_invalid_values(overrides):
    with pytest.raises(ValueError):
        AppConfig(embedding=EmbeddingConfig(mode="external", **overrides))


# --------------------------------------------------------------------------
# B0 止血：退避上限与响应校验（单批路径此前完全裸奔）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["inf", "Infinity", "1e999", "nan"])
def test_retry_after_non_finite_values_are_ignored(monkeypatch, raw):
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        raise _http_error(429, {"Retry-After": raw})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m", max_retries=1)

    with pytest.raises(ProviderError):
        provider.embed(["hello"])
    # 关键：绝不能出现 inf/nan —— time.sleep(inf) 会把线程永久挂死。
    assert len(sleeps) == 1
    assert sleeps[0] == sleeps[0]  # 非 nan
    assert sleeps[0] <= providers._MAX_BACKOFF


def test_retry_after_is_capped_by_max_backoff(monkeypatch):
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        raise _http_error(429, {"Retry-After": "86400"})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m", max_retries=2)

    with pytest.raises(ProviderError):
        provider.embed(["hello"])
    # 服务端让等一天，本地只等到上限：持有锁的线程不能睡死。
    assert all(seconds <= providers._MAX_BACKOFF for seconds in sleeps)


def test_backoff_jitters_to_avoid_thundering_herd(monkeypatch):
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        raise _http_error(429, {"Retry-After": "10"})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    monkeypatch.setattr("mortis_rag_mcp.providers._sleep", sleeps.append)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m", max_retries=4)

    with pytest.raises(ProviderError):
        provider.embed(["hello"])
    # 同一个 Retry-After 下多次等待不应完全相同，否则 6 个 worker 同时醒来。
    assert len(set(sleeps)) > 1
    assert all(10.0 <= seconds <= 15.0 for seconds in sleeps)


def test_single_batch_short_response_is_rejected(monkeypatch):
    """默认 batch_size=32，单文件多数走这条路径，此前完全没有条数校验。"""

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        # 上游少返回一个向量：回填时 zip() 会静默截断并让后续 chunk 错位。
        return _JsonResponse({"data": [{"embedding": [1.0]} for _ in payload["input"][:-1]]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m")

    with pytest.raises(ProviderError, match="vectors for"):
        provider.embed(["a", "b", "c"])


def test_multi_batch_short_response_is_rejected(monkeypatch):
    """batch_size=2 + 4 条输入才会真正进切片循环，验证第二条路径也有校验。"""
    calls: list[int] = []

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append(len(payload["input"]))
        return _JsonResponse({"data": [{"embedding": [1.0]}]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m", batch_size=2)

    with pytest.raises(ProviderError, match="vectors for"):
        provider.embed(["a", "b", "c", "d"])
    # 第一批就被拒，不应继续发后续批次。
    assert calls == [2]


def test_embedding_reorders_response_by_index(monkeypatch):
    def fake_urlopen(request, timeout):
        # 乱序返回但带正确的 index：必须按 index 还原，而不是按返回顺序。
        return _JsonResponse(
            {
                "data": [
                    {"index": 2, "embedding": [3.0]},
                    {"index": 0, "embedding": [1.0]},
                    {"index": 1, "embedding": [2.0]},
                ]
            }
        )

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m")

    assert provider.embed(["a", "b", "c"]) == [[1.0], [2.0], [3.0]]


def test_embedding_rejects_duplicate_response_index(monkeypatch):
    def fake_urlopen(request, timeout):
        return _JsonResponse(
            {
                "data": [
                    {"index": 0, "embedding": [1.0]},
                    {"index": 0, "embedding": [2.0]},
                ]
            }
        )

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m")

    with pytest.raises(ProviderError, match="indexes"):
        provider.embed(["a", "b"])


# --------------------------------------------------------------------------
# 配置解析的边界：NaN / bool / 非整数浮点（_numeric 引入的校验）
# --------------------------------------------------------------------------


def _write_toml(tmp_path: Path, body: str) -> Path:
    toml = tmp_path / "app.toml"
    content = '[vault]\npath = "%s"\n%s\n' % ((tmp_path / "vault").as_posix(), body)
    toml.write_text(content, encoding="utf-8")
    return toml


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[embedding]\nmode = "static"\ntimeout = nan', "finite"),
        ('[embedding]\nmode = "static"\ntimeout = true', "boolean"),
        ('[index]\nrrf_per_route = 40.5', "integer"),
        ('[embedding]\nmode = "static"\nmax_retries = "3 次"', "must be a int"),
        ('[embedding]\nmode = "static"\ntimeout = 9999', "must be <="),
    ],
)
def test_config_numeric_rejects_nonsense_values(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        load_config(_write_toml(tmp_path, body))


def test_config_numeric_accepts_legitimate_values(tmp_path):
    toml = _write_toml(
        tmp_path,
        '[embedding]\nmode = "static"\ntimeout = 12.5\nmax_retries = 0\n'
        "[index]\nrrf_per_route = 40\nmax_top_k = 500",
    )

    config = load_config(toml)
    assert config.embedding.timeout == 12.5
    assert config.embedding.max_retries == 0
    assert config.rrf_per_route == 40
    assert config.max_top_k == 500

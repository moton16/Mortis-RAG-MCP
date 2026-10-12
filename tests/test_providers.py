from __future__ import annotations

from io import BytesIO
import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig, RerankerConfig, load_config
from mortis_rag_mcp import providers
from mortis_rag_mcp.providers import ExternalEmbeddingProvider, ExternalRerankerProvider, ProviderError, StaticEmbeddingProvider


@pytest.fixture(autouse=True)
def durable_test_journal(tmp_path, monkeypatch):
    """C100/R2: transport tests use a real temporary journal, never bypass the guard.

    Historical automatic POST retry assertions below are explicitly superseded:
    an HTTP/network failure is unknown, even when max_retries is configured.
    Pure backoff arithmetic remains independently tested (not a send policy).
    """
    from mortis_rag_mcp.doc_store import ControlStore, resolve_storage_layout
    from mortis_rag_mcp.paid_requests import PaidRequestJournal
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "journal-cache")
    cfg.cache.placement = "home"
    control = ControlStore(resolve_storage_layout(cfg, tmp_path / "vault"))
    original = providers._JsonHttpProvider.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.configure_paid_requests(PaidRequestJournal(control, "transport-test"), lambda _: True, "fixture")
    monkeypatch.setattr(providers._JsonHttpProvider, "__init__", init)
    yield
    control.close()


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

    vectors = provider.embed(["你好", "world"])
    assert vectors[0] == pytest.approx([1 / 5**0.5, 2 / 5**0.5])
    assert vectors[1] == pytest.approx([0.6, 0.8])  # Existing C98 L2-float32 contract.
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
    calls: list[Request] = []

    def fail(request, timeout=None):
        calls.append(request)
        raise OSError("offline")

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fail)
    provider = ExternalRerankerProvider(endpoint="https://rerank.test")

    assert provider.rerank_or_none("q", ["a", "b"]) is None
    # v0.9.2：一次物理 POST，零自动重发；网络失败按 transport_unknown 分类。
    assert len(calls) == 1
    assert provider.audit_write_errors == 0


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


def _http_error(
    code: int, headers: dict | None = None, body: bytes = b""
) -> HTTPError:
    return HTTPError(ENDPOINT, code, "error", headers or {}, BytesIO(body))


def test_paid_embedding_error_includes_http_body_without_retry(monkeypatch):
    def fail(request, timeout):
        raise _http_error(
            400,
            body=b'{"error":{"message":"input was misclassified as image"}}',
        )

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fail)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model")

    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])

    message = str(excinfo.value)
    assert "SUBMISSION_UNKNOWN" in message
    assert "HTTP 400" in message
    assert "input was misclassified as image" in message


def test_local_embedding_error_includes_bounded_http_body(monkeypatch):
    detail = "invalid text input " + ("x" * 400)

    def fail(request, timeout):
        raise _http_error(
            422,
            body=json.dumps({"error": {"message": detail}}).encode("utf-8"),
        )

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fail)
    provider = ExternalEmbeddingProvider(
        endpoint="http://127.0.0.1:8000/v1/embeddings",
        model="embed-model",
    )
    provider.request_journal = None
    provider.paid_guard = None

    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])

    message = str(excinfo.value)
    assert "LOCAL_REQUEST_FAILED" in message
    assert "HTTP 422" in message
    assert "invalid text input" in message
    assert detail not in message


def test_embedding_429_is_unknown_without_automatic_retry(monkeypatch):
    calls: list[Request] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise _http_error(429, {"Retry-After": "1"})
        return _JsonResponse({"data": [{"embedding": [0.3, 0.4]}]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model")

    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])
    # 收到响应 ≠ 可以重发：429 必须是 http_error 且远端影响未知。
    assert len(calls) == 1
    assert excinfo.value.outcome == "http_error"
    assert excinfo.value.response_received is True
    assert excinfo.value.remote_effect_unknown is True
    assert excinfo.value.http_status == 429


def test_embedding_500_is_unknown_without_automatic_retry(monkeypatch):
    calls: list[Request] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise _http_error(500)
        return _JsonResponse({"data": [{"embedding": [1.0]}]})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model")

    with pytest.raises(ProviderError, match="SUBMISSION_UNKNOWN"):
        provider.embed(["hello"])
    assert len(calls) == 1


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


def test_embedding_max_retries_does_not_authorize_unknown_resend(monkeypatch):
    calls: list[Request] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise _http_error(429)

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="embed-model", max_retries=2)

    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])
    # max_retries 只保留解析兼容：它绝不能变成「再发一次」的授权。
    assert len(calls) == 1
    assert "SUBMISSION_UNKNOWN" in str(excinfo.value)


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
    assert vectors == [[1.0]] * 5  # One-dimensional nonzero outputs normalize to 1.


def test_embedding_batch_size_zero_keeps_single_request(monkeypatch):
    batches: list[list[str]] = []

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        batches.append(payload["input"])
        return _JsonResponse({"data": [{"embedding": [1.0]} for _ in payload["input"]]})

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
# v0.9.2：故障分类必须区分「收到响应 / 产物可用 / 允许自动重算」三件事
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["inf", "Infinity", "1e999", "nan"])
def test_retry_after_is_diagnostic_only(monkeypatch, raw):
    """Retry-After 只作诊断：它既不授权重发，也不改变分类。"""
    calls: list[Request] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise _http_error(429, {"Retry-After": raw})

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m")

    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])
    assert len(calls) == 1
    assert excinfo.value.outcome == "http_error"
    assert providers._retry_after_seconds({"Retry-After": raw}) is None


def test_rejected_4xx_are_not_transport_unknown(monkeypatch):
    calls: list[Request] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise _http_error(401, body=b'{"error":{"message":"bad key"}}')

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m")

    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])
    assert len(calls) == 1
    assert excinfo.value.outcome == "rejected"
    assert excinfo.value.response_received is True
    # 明确拒绝 ≠ 远端影响未知：它可以释放占用、由 quarantine 控制后续轮次。
    assert excinfo.value.remote_effect_unknown is False


def test_broken_json_response_is_response_invalid(monkeypatch):
    calls: list[Request] = []
    broken = _JsonResponse({"data": []})
    broken.read = lambda: b"{not json"

    def broken_response(request, timeout):
        calls.append(request)
        return broken

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", broken_response)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m")
    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])
    assert len(calls) == 1
    assert excinfo.value.outcome == "response_invalid"
    assert excinfo.value.response_received is True


def test_generic_urlerror_is_not_claimed_as_not_sent(monkeypatch):
    """generic URLError 不许伪装成 not_sent（那等于宣称供应商未收费）。"""
    from urllib.error import URLError

    calls: list[Request] = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise URLError("connection reset by peer")

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m")

    with pytest.raises(ProviderError) as excinfo:
        provider.embed(["hello"])
    assert len(calls) == 1
    assert excinfo.value.outcome == "transport_unknown"
    assert excinfo.value.remote_effect_unknown is True


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
                    {"index": 2, "embedding": [0.6, 0.8]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 1, "embedding": [0.0, 1.0]},
                ]
            }
        )

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m")

    vectors = provider.embed(["a", "b", "c"])
    assert vectors[:2] == [[1.0, 0.0], [0.0, 1.0]]
    assert vectors[2] == pytest.approx([0.6, 0.8])


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


def test_local_free_endpoint_bypasses_paid_guard(monkeypatch):
    captured = {}
    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _JsonResponse({"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(
        endpoint="http://127.0.0.1:8080/v1/embeddings",
        model="embeddinggemma2",
        dimension=2,
    )
    provider.request_journal = None
    provider.paid_guard = None
    vecs = provider.embed(["test local bypass"])
    assert vecs == [[1.0, 0.0]]
    assert captured["url"] == "http://127.0.0.1:8080/v1/embeddings"

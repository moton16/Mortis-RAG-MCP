"""Offline evaluation measures supplied rankings, never claims native validation."""
import importlib
import math

import pytest


def evaluator():
    return importlib.import_module("scripts.eval_quality")


def case(**updates):
    result = {"id": "q1", "category": "text", "query": "alpha",
              "relevance": {"a": 2, "b": 1}, "ranking": ["a", "b"],
              "baseline": ["b", "a"]}
    result.update(updates)
    return result


def test_known_recall_ndcg_and_fixture_not_final_pass():
    report = evaluator().evaluate({"scope": "fixture", "queries": [case()]})
    assert report["recall_at_10"] == 1
    assert report["ndcg_at_10"] == 1
    assert report["status"] == "fixture_measured"
    assert report["real_api_requests"] == 0
    assert report["delta_ndcg"] > 0


@pytest.mark.parametrize("queries", [[], [case(), case()],
    [case(ranking=["a", "a"])], [case(relevance={"a": math.nan})],
    [case(relevance={})]])
def test_invalid_measurements_rejected(queries):
    with pytest.raises(ValueError):
        evaluator().evaluate({"scope": "fixture", "queries": queries})


def test_quality_gate_requires_representative_evidence():
    report = evaluator().evaluate({"scope": "quality", "queries": [case()]})
    assert report["status"] == "incomplete"
    assert "corpus_evidence" in report["missing"]


def test_bootstrap_seed_reproducible():
    data = {"scope": "fixture", "queries": [case(), case(id="q2", ranking=["b", "a"])]}
    assert evaluator().evaluate(data, seed=17) == evaluator().evaluate(data, seed=17)


def test_eval_search_offline_config_ignores_host_and_rejects_external(tmp_path):
    module = importlib.import_module("scripts.eval_search")
    assert module.offline_config(None).embedding.mode == "static"
    path = tmp_path / "external.toml"
    path.write_text('[embedding]\nmode="external"\nendpoint="https://example.test/embed"\n',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="offline"):
        module.offline_config(str(path))

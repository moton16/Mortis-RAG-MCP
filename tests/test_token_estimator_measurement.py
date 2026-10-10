import importlib.util
from pathlib import Path


def test_measurement_reports_threshold_not_calibration():
    path = Path(__file__).resolve().parents[1] / "scripts" / "measure_token_estimator.py"
    spec = importlib.util.spec_from_file_location("offline_measurement", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.measure([{"id": "fixture", "category": "unit", "text": "abc"}],
                            lambda text: 100, max_mape=.01)
    assert report["threshold_pass"] is False
    assert report["profile_calibrated"] is False
    assert report["samples"][0]["actual"] == 100
    assert len(report["samples"][0]["input_sha256"]) == 64

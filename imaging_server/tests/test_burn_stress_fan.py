import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
BURN_PATH = ROOT / "bench-client" / "vstl_burn_stress.py"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


burn = _load_module("vstl_burn_stress_fan", BURN_PATH)


def test_fan_summary_reports_ok_with_rpm_samples():
    summary = burn._summarize_fan([2200, 3100, 2800], True)

    assert summary["fan_status"] == "OK"
    assert summary["fan_min_rpm"] == 2200
    assert summary["fan_max_rpm"] == 3100
    assert summary["fan_avg_rpm"] == 2700


def test_fan_summary_distinguishes_no_sensor_and_not_spinning():
    assert burn._summarize_fan([], False)["fan_status"] == "NO SENSOR"
    stopped = burn._summarize_fan([], True)
    assert stopped["fan_status"] == "NOT SPINNING"
    assert stopped["fan_max_rpm"] == "0"

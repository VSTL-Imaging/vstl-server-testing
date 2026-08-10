import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAPTURE_PATH = ROOT / "bench-client" / "vstl_image_capture.py"
sys.path.insert(0, str(ROOT / "bench-client"))
spec = importlib.util.spec_from_file_location("vstl_image_capture_progress", CAPTURE_PATH)
capture = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(capture)


def test_partclone_program_terminated_line_is_not_treated_as_interruption():
    state = {
        "operation": "restoring",
        "phase": "restoring nvme0n1p1",
        "partition_eta_sec": 12,
        "partition_eta_text": "0:00:12",
        "write_speed": "80 MB/s",
    }

    capture._update_capture_state_from_line("Program terminated.", state)

    assert "terminated" not in state
    assert state["phase"] == "restoring nvme0n1p1"
    assert state["last_line"] == "Partition tool completed"
    assert state["partition_eta_sec"] is None
    assert state["partition_eta_text"] == "--"
    assert state["write_speed"] == "80 MB/s"

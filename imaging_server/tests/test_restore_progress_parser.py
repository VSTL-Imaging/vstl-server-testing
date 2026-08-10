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
        "current_partition": "nvme0n1p1",
        "parts": ["nvme0n1p1", "nvme0n1p2", "nvme0n1p3", "nvme0n1p4"],
        "parts_total": 4,
        "partition_percent": 100.0,
        "partition_eta_sec": 12,
        "partition_eta_text": "0:00:12",
        "write_speed": "80 MB/s",
    }

    capture._update_capture_state_from_line("Program terminated.", state)

    assert "terminated" not in state
    assert state["phase"] == "finished nvme0n1p1"
    assert state["last_line"] == "Finished nvme0n1p1; preparing next partition"
    assert state["parts_done"] == 1
    assert state["parts_left"] == 3
    assert state["partition_eta_sec"] is None
    assert state["partition_eta_text"] == "--"
    assert state["write_speed"] == "80 MB/s"


def test_restore_runner_sends_newline_after_partclone_completion():
    restore_path = ROOT / "bench-client" / "vstl_image_restore.py"
    source = restore_path.read_text(encoding="utf-8")

    assert "stdin=subprocess.PIPE" in source
    assert "sent newline after partclone completion" in source

import importlib.util
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAPTURE_PATH = ROOT / "bench-client" / "vstl_image_capture.py"
RESTORE_PATH = ROOT / "bench-client" / "vstl_image_restore.py"
sys.path.insert(0, str(ROOT / "bench-client"))
spec = importlib.util.spec_from_file_location("vstl_image_capture_progress", CAPTURE_PATH)
capture = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(capture)
restore_spec = importlib.util.spec_from_file_location("vstl_image_restore_progress", RESTORE_PATH)
restore = importlib.util.module_from_spec(restore_spec)
assert restore_spec.loader is not None
restore_spec.loader.exec_module(restore)


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


def test_restore_runner_can_continue_after_confirmed_partclone_completion():
    state = {
        "current_partition": "nvme0n1p1",
        "parts": ["nvme0n1p1", "nvme0n1p2"],
        "completed_partitions": ["nvme0n1p1"],
        "last_line": "Finished nvme0n1p1; preparing next partition",
    }

    assert restore._should_continue_after_partclone("Program terminated.", state)


def test_restore_runner_does_not_continue_after_unknown_partition_completion():
    state = {
        "current_partition": "Info-img-id",
        "parts": ["nvme0n1p1", "nvme0n1p2"],
        "completed_partitions": [],
        "last_line": "Partition tool completed",
    }

    assert not restore._should_continue_after_partclone("Program terminated.", state)


def test_restore_does_not_count_metadata_file_as_partition():
    state = {
        "operation": "restoring",
        "phase": "restoring",
        "current_partition": "Info-img-id",
        "parts": ["nvme0n1p1", "nvme0n1p2", "nvme0n1p3", "nvme0n1p4"],
        "parts_total": 4,
        "partition_percent": 100.0,
    }

    capture._update_capture_state_from_line("Program terminated.", state)

    assert state.get("parts_done") in (None, 0)
    assert state.get("completed_partitions") in (None, [])
    assert state["last_line"] == "Partition tool completed"


def test_restore_progress_does_not_infer_current_part_from_metadata(tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "parts").write_text("nvme0n1p1 nvme0n1p2\n", encoding="utf-8")
    (image_dir / "Info-img-id.txt").write_text("metadata\n", encoding="utf-8")
    state = {
        "operation": "restoring",
        "image_dir": str(image_dir),
        "completed_partitions": [],
    }
    events = []

    capture._emit_capture_progress(events.append, state, time.monotonic(), str(image_dir), {})

    assert events
    assert events[-1].get("current_partition") in (None, "")
    assert events[-1]["parts_total"] == 2


def test_restore_specific_partclone_line_sets_current_partition():
    state = {
        "operation": "restoring",
        "parts": ["nvme0n1p1", "nvme0n1p2"],
        "parts_total": 2,
    }

    capture._update_capture_state_from_line(
        "Starting to restore image (-) to device (/dev/nvme0n1p2)",
        state,
    )

    assert state["current_partition"] == "nvme0n1p2"
    assert state["partition_index"] == 2
    assert state["partition_percent"] == 0.0


def test_restore_ptcl_read_batch_line_sets_current_partition():
    state = {
        "operation": "restoring",
        "parts": ["nvme0n1p1", "nvme0n1p2", "nvme0n1p3", "nvme0n1p4"],
        "parts_total": 4,
    }

    capture._update_capture_state_from_line(
        "ptcl-read-batch /home/partimag/example/sys-ptcl-img.gz /dev/nvme0n1p1",
        state,
    )

    assert state["current_partition"] == "nvme0n1p1"
    assert state["partition_index"] == 1
    assert state["partition_percent"] == 0.0

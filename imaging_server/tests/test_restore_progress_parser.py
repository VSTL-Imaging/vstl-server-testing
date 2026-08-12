import importlib.util
import json
import os
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


def _write_restore_metadata(path: Path, **overrides):
    path.mkdir()
    meta = {
        "image_name": f"{path.name}.img",
        "image_subdir": path.name,
        "model": "Latitude 5440",
        "part_number": "0C00",
        "cpu": "13th Gen Intel Core i5-1335U",
        "os_name": "Windows 11 Pro",
        "os_version": "25H2",
        "os_build": "26200",
        "os_token": "Win_11_Pro_25H2",
    }
    meta.update(overrides)
    (path / restore._CAPTURE_META_FILE).write_text(
        json.dumps(meta), encoding="utf-8",
    )
    return meta


def test_restore_picker_source_collapses_same_os_to_latest_capture(tmp_path, monkeypatch):
    older = tmp_path / "older-backup"
    newer = tmp_path / "newer-backup"
    _write_restore_metadata(
        older,
        image_name="OLDER_LATITUDE_5440_Win_11_Pro_25H2.img",
        captured_at="2026-07-11T11:27:08+00:00",
    )
    _write_restore_metadata(
        newer,
        image_name="NEWER_LATITUDE_5440_Win_11_Pro_25H2.img",
        captured_at="2026-08-12T06:43:22+00:00",
    )
    monkeypatch.setattr(restore, "_NFS_MOUNT_POINT", str(tmp_path))
    monkeypatch.setattr(restore, "mount_nfs", lambda *args: (True, "mounted"))

    result = restore.find_local_golden_copies(
        "server", "/images/dev", "rw", "Latitude 5440", "0C00",
        "13th Gen Intel Core i5-1335U",
    )

    assert result["status"] == "found"
    assert [copy["image_name"] for copy in result["copies"]] == [
        "NEWER_LATITUDE_5440_Win_11_Pro_25H2.img",
    ]
    assert result["golden_copy"]["image_name"] == "NEWER_LATITUDE_5440_Win_11_Pro_25H2.img"
    assert result["golden_copy"]["captured_at"] == "2026-08-12T06:43:22+00:00"


def test_restore_latest_copy_falls_back_to_directory_mtime(tmp_path, monkeypatch):
    older = tmp_path / "older-no-meta-time"
    newer = tmp_path / "newer-no-meta-time"
    _write_restore_metadata(older, image_name="OLDER_NO_TIME.img", captured_at="")
    _write_restore_metadata(newer, image_name="NEWER_NO_TIME.img", captured_at="")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))
    monkeypatch.setattr(restore, "_NFS_MOUNT_POINT", str(tmp_path))
    monkeypatch.setattr(restore, "mount_nfs", lambda *args: (True, "mounted"))

    result = restore.find_local_golden_copies(
        "server", "/images/dev", "rw", "Latitude 5440", "0C00",
        "13th Gen Intel Core i5-1335U",
    )

    assert [copy["image_name"] for copy in result["copies"]] == ["NEWER_NO_TIME.img"]
    assert result["golden_copy"]["image_name"] == "NEWER_NO_TIME.img"


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
        "ptcl-read-batch /home/partimag/example/sys-ptcl-img.gz /dev/nvme0n1p1 --quiet",
        state,
    )

    assert state["current_partition"] == "nvme0n1p1"
    assert state["partition_index"] == 1
    assert state["partition_percent"] == 0.0


def test_restore_progress_infers_first_partition_when_start_line_is_missing():
    state = {
        "operation": "restoring",
        "parts": ["nvme0n1p1", "nvme0n1p2", "nvme0n1p3", "nvme0n1p4"],
        "parts_total": 4,
    }

    capture._update_capture_state_from_line(
        "Current Block: 8027694, Total Block: 124762111, Complete: 7.00%",
        state,
    )

    assert state["current_partition"] == "nvme0n1p1"
    assert state["partition_index"] == 1
    assert state["partition_percent"] == 7.0


def test_restore_progress_infers_next_partition_after_previous_completed():
    state = {
        "operation": "restoring",
        "current_partition": "nvme0n1p1",
        "parts": ["nvme0n1p1", "nvme0n1p2", "nvme0n1p3", "nvme0n1p4"],
        "parts_total": 4,
        "completed_partitions": ["nvme0n1p1"],
        "partition_percent": 100.0,
    }

    capture._update_capture_state_from_line(
        "Current Block: 10, Total Block: 1000, Complete: 1.00%",
        state,
    )

    assert state["current_partition"] == "nvme0n1p2"
    assert state["partition_index"] == 2
    assert state["partition_percent"] == 1.0


def test_restore_program_terminated_marks_inferred_partition_complete():
    state = {
        "operation": "restoring",
        "parts": ["nvme0n1p1", "nvme0n1p2"],
        "parts_total": 2,
        "partition_percent": 100.0,
    }

    capture._update_capture_state_from_line("Program terminated.", state)

    assert state["current_partition"] == "nvme0n1p1"
    assert state["completed_partitions"] == ["nvme0n1p1"]
    assert state["parts_left"] == 1

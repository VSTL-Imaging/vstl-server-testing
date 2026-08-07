import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESTORE_PATH = ROOT / "bench-client" / "vstl_image_restore.py"
sys.path.insert(0, str(ROOT / "bench-client"))
spec = importlib.util.spec_from_file_location("vstl_image_restore_post_restore", RESTORE_PATH)
ir = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(ir)


def test_post_restore_firmware_script_uses_windows_update_driver_firmware_scan():
    script = ir._post_restore_firmware_powershell()
    assert "Microsoft.Update.Session" in script
    assert "pnputil /scan-devices" in script
    assert "firmware|bios|uefi|system firmware|driver" in script
    assert "shutdown.exe /r" in script


def test_restore_uses_clonezilla_proportional_partition_table():
    source = RESTORE_PATH.read_text(encoding="utf-8")
    command = source[source.index('cmd = ['):source.index('env = dict(os.environ)')]
    assert '"-k1", "-r"' in command


def test_restore_verification_fails_on_large_trailing_unallocated_space(monkeypatch):
    calls = []

    def fake_run(argv, timeout=30):
        calls.append(argv)
        if argv[:2] == ["lsblk", "-no"]:
            return 0, "sda 512G disk\nsda1 200M part vfat SYSTEM\nsda3 237G part ntfs Windows\nsda4 780M part ntfs Recovery", ""
        if argv[:2] == ["sfdisk", "-J"]:
            return 0, json.dumps({
                "partitiontable": {
                    "label": "gpt",
                    "lastlba": 1000215182,
                    "partitions": [
                        {"node": "/dev/sda1", "start": 2048, "size": 409600},
                        {"node": "/dev/sda2", "start": 411648, "size": 32768},
                        {"node": "/dev/sda3", "start": 444416, "size": 497025024},
                        {"node": "/dev/sda4", "start": 497469440, "size": 1597440},
                    ],
                },
            }), ""
        if argv[:2] == ["blockdev", "--getss"]:
            return 0, "512\n", ""
        return 0, "", ""

    monkeypatch.setattr(ir, "_run", fake_run)
    monkeypatch.setattr(ir, "validate_partition_layout", lambda device: {"ok": True, "issues": []})

    verified, evidence = ir._verify_restore("/dev/sda")

    assert verified is False
    assert "unallocated space" in evidence
    assert ["sgdisk", "-e", "/dev/sda"] in calls


def test_successful_restore_fails_when_partition_layout_is_not_verified(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    monkeypatch.setattr(ir, "mount_nfs", lambda *args, **kwargs: (True, "mounted"))
    monkeypatch.setattr(ir, "_NFS_MOUNT_POINT", str(tmp_path))
    monkeypatch.setattr(ir.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(ir, "_ocs_restoredisk", lambda *args, **kwargs: (True, "restore ok"))
    monkeypatch.setattr(ir, "_verify_restore", lambda device: (False, "too much unallocated space"))

    result = ir.run_restore("image", "/dev/sda", "10.255.0.75", "/images/dev")

    assert result["ok"] is False
    assert result["result"] == "FAIL"
    assert "partition layout verification failed" in result["error_message"]
    assert result["firmware_update_trigger"]["installed"] is False


def test_install_post_restore_firmware_trigger_writes_setupcomplete_and_startup(tmp_path, monkeypatch):
    root = tmp_path / "win"
    (root / "Windows" / "Setup" / "Scripts").mkdir(parents=True)
    monkeypatch.setattr(ir, "_WINDOWS_RW_MOUNT_POINT", str(root))
    monkeypatch.setattr(ir, "validate_partition_layout", lambda device: {"os_partition": "/dev/sda3", "issues": []})

    commands = []

    def fake_run(argv, timeout=30):
        commands.append(argv)
        if argv and argv[0] == "mount":
            (root / "Windows").mkdir(exist_ok=True)
        return 0, "", ""

    monkeypatch.setattr(ir, "_run", fake_run)

    result = ir.install_post_restore_firmware_trigger("/dev/sda")

    assert result["ok"] is True
    assert result["installed"] is True
    script = root / "ProgramData" / "VSTL" / "PostRestoreFirmware" / "TriggerFirmwareDriverUpdate.ps1"
    launcher = root / "ProgramData" / "VSTL" / "PostRestoreFirmware" / "RunPostRestoreFirmwareUpdate.cmd"
    setupcomplete = root / "Windows" / "Setup" / "Scripts" / "SetupComplete.cmd"
    startup = (
        root / "ProgramData" / "Microsoft" / "Windows" / "Start Menu"
        / "Programs" / "Startup" / "VSTL-PostRestoreFirmware.cmd"
    )
    assert script.exists()
    assert launcher.exists()
    assert setupcomplete.exists()
    assert startup.exists()
    assert "VSTL_POST_RESTORE_FIRMWARE_TRIGGER" in setupcomplete.read_text(encoding="utf-8")
    assert "TriggerFirmwareDriverUpdate.ps1" in startup.read_text(encoding="utf-8")
    assert ["umount", str(root)] in commands


def test_successful_restore_reports_firmware_trigger(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    monkeypatch.setattr(ir, "mount_nfs", lambda *args, **kwargs: (True, "mounted"))
    monkeypatch.setattr(ir, "_NFS_MOUNT_POINT", str(tmp_path))
    monkeypatch.setattr(ir.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(ir, "_ocs_restoredisk", lambda *args, **kwargs: (True, "restore ok"))
    monkeypatch.setattr(ir, "_verify_restore", lambda device: (True, "lsblk ok"))
    monkeypatch.setattr(
        ir,
        "install_post_restore_firmware_trigger",
        lambda device: {"ok": True, "installed": True, "evidence": "trigger installed"},
    )

    result = ir.run_restore("image", "/dev/sda", "10.255.0.75", "/images/dev")

    assert result["ok"] is True
    assert result["firmware_update_trigger"]["installed"] is True
    assert "trigger installed" in result["evidence"]

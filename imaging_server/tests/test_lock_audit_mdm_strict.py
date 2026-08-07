import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_AUDIT_PATH = ROOT / "bench-client" / "vstl_lock_audit.py"

spec = importlib.util.spec_from_file_location("vstl_lock_audit", LOCK_AUDIT_PATH)
la = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(la)


def _touch_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def test_default_windows_task_folders_do_not_count_as_mdm(tmp_path):
    """Clean Windows images pre-create these folders; they are not enrollment."""
    _touch_dir(tmp_path / "Windows/System32/Tasks/Microsoft/Windows/Management/Provisioning")
    _touch_dir(tmp_path / "Windows/System32/Tasks/Microsoft/Windows/Workplace Join")
    _touch_dir(tmp_path / "Windows/ServiceProfiles/LocalService/AppData/Roaming/Microsoft/Crypto/PCPKSP")

    assert la.detect_intune(str(tmp_path))["present"] is False
    assert la.detect_azure_ad(str(tmp_path))["present"] is False


def test_enterprise_mgmt_parent_alone_is_not_intune(tmp_path):
    _touch_dir(tmp_path / "Windows/System32/Tasks/Microsoft/Windows/EnterpriseMgmt")

    result = la.detect_intune(str(tmp_path))

    assert result["present"] is False
    assert result["status"] == "NOT_ENROLLED"


def test_enterprise_mgmt_guid_child_counts_as_intune(tmp_path):
    guid = "01234567-89ab-cdef-0123-456789abcdef"
    _touch_dir(tmp_path / f"Windows/System32/Tasks/Microsoft/Windows/EnterpriseMgmt/{guid}")

    result = la.detect_intune(str(tmp_path))

    assert result["present"] is True
    assert result["status"] == "ENROLLED"
    assert guid in result["evidence"]


def test_agent_and_cloud_cache_paths_require_content(tmp_path):
    intune = tmp_path / "ProgramData/Microsoft/IntuneManagementExtension"
    azure = tmp_path / "Windows/ServiceProfiles/NetworkService/AppData/Roaming/Microsoft/CloudAPCache/AzureAd"
    _touch_dir(intune)
    _touch_dir(azure)

    assert la.detect_intune(str(tmp_path))["present"] is False
    assert la.detect_azure_ad(str(tmp_path))["present"] is False

    (intune / "AgentExecutor.log").write_text("installed")
    (azure / "cache.dat").write_text("joined")

    assert la.detect_intune(str(tmp_path))["present"] is True
    assert la.detect_azure_ad(str(tmp_path))["present"] is True


def test_bitlocker_is_not_part_of_active_bench_lock_policy():
    assert "bitlocker" not in la.LOCK_KEYS_ORDER
    assert "bitlocker" not in la.LOCK_LABELS

    result = la.detect_bitlocker([{"device": "/dev/sda4", "type": "bitlocker"}])

    assert result["present"] is False
    assert result["status"] == "NOT_CHECKED_BY_POLICY"

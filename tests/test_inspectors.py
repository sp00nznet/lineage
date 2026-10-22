"""os_caption's Windows 11 correction -- ProductName lies on Windows 11."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lineage.inspectors import _win11_caption  # noqa: E402
from lineage.relevance import Version  # noqa: E402


def test_productname_says_10_on_windows_11():
    # Observed on dpi, build 26200: ProductName "Windows 10 Pro".
    assert _win11_caption("Windows 10 Pro", Version("10.0.26200.7462")) == "Windows 11 Pro"
    assert _win11_caption("Windows 10 Enterprise LTSC 2024", Version("10.0.26100.1")) == "Windows 11 Enterprise LTSC 2024"
    assert _win11_caption("Windows 10 Pro", Version("10.0.19045.5737")) == "Windows 10 Pro"
    assert _win11_caption("Windows Server 2022 Datacenter", Version("10.0.20348.1")) == "Windows Server 2022 Datacenter"
    assert _win11_caption("Windows Server 2025 Datacenter", Version("10.0.26100.1")) == "Windows Server 2025 Datacenter"

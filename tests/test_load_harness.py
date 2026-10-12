"""The load harness must use a private, runtime-selected scratch directory."""
from pathlib import Path


def test_load_harness_does_not_use_a_shared_hardcoded_temporary_path():
    source = (Path(__file__).resolve().parents[1] / "tools/plugin-load-test.sh").read_text()
    assert "TMP=/tmp/plugin-load-test" not in source
    assert "mktemp -d" in source
    assert "TMPDIR" in source
    assert "trap" in source

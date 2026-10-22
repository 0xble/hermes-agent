"""Gateway cases run in canonical Python shards with messaging SDKs installed."""

from pathlib import Path

from scripts.ci import portable as ci


_REPO = Path(__file__).resolve().parents[2]


def test_gateway_files_have_messaging_dependencies_and_required_shard_coverage():
    gateway_files = {
        path.relative_to(_REPO).as_posix()
        for path in (_REPO / "tests" / "gateway").rglob("test_*.py")
    }
    assert gateway_files, "the gateway test directory must be present"
    assert "messaging" in ci.EXTRAS, "canonical Python setup must install messaging SDKs"
    selected = set(ci.shard_files(_REPO, 1)[0])
    missing = gateway_files - selected
    assert not missing, f"gateway files missing from required Python shards: {sorted(missing)}"

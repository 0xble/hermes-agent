"""The signed shared-agent document toolchain needs one compatible runtime."""

from pathlib import Path
import tomllib

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]


def test_shared_document_versions_match_manifest_and_lock():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    documents = {
        requirement.name: requirement
        for requirement in map(
            Requirement, metadata["project"]["optional-dependencies"]["documents"]
        )
    }
    # These versions are part of the existing sealed-release verifier and the
    # shipped HTML-to-PDF script, not merely a packaging preference.
    expected = {"lxml": "6.1.3", "pandas": "3.0.6", "playwright": "1.60.0"}
    for name, version in expected.items():
        assert str(documents[name].specifier) == f"=={version}"
        assert any(
            row["name"] == name and row["version"] == version
            for row in lock["package"]
        )

    # A different pin in another extra or the test group makes uv's universal
    # resolution fail, even when that extra is not installed on the shared host.
    selectors = list(metadata["project"]["optional-dependencies"].values())
    selectors.extend(metadata["dependency-groups"].values())
    for requirements in selectors:
        for requirement in requirements:
            if not isinstance(requirement, str):
                continue
            parsed = Requirement(requirement)
            if parsed.name == "playwright":
                assert str(parsed.specifier) == "==1.60.0"
    assert {
        row["version"] for row in lock["package"] if row["name"] == "playwright"
    } == {"1.60.0"}

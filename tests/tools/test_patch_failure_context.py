"""Failure diagnostics for targeted and V4A patches.

Every failed path must be actionable and must leave the target bytes unchanged.
"""

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


def _ops(tmp_path):
    return ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))


def test_no_match_shows_bounded_closest_region_and_does_not_write(tmp_path):
    path = tmp_path / "target.txt"
    original = "header\nneedle-ish line\nfooter\n"
    path.write_text(original)

    result = _ops(tmp_path).patch_replace(
        str(path), "needle completely absent", "replacement")

    assert not result.success
    assert result.error is not None
    assert "Closest region in the file" in result.error
    assert "file was not modified" in result.error
    excerpt = result.error.split("Closest region in the file", 1)[1]
    assert len(excerpt) <= 1200 + 200  # label/instructions sit outside the excerpt cap
    assert len(excerpt.splitlines()) <= 8
    assert "needle-ish line" in excerpt
    assert path.read_text() == original


def test_ambiguous_match_reports_capped_line_numbers_and_does_not_write(tmp_path):
    path = tmp_path / "target.txt"
    original = "anchor\n" * 9
    path.write_text(original)

    result = _ops(tmp_path).patch_replace(str(path), "anchor", "changed")

    assert not result.success
    assert result.error is not None
    assert "Found 9 matches" in result.error
    assert all(f"L{line}:" in result.error for line in range(1, 6))
    assert "L6:" not in result.error
    assert path.read_text() == original


def test_validation_reason_is_first_and_does_not_write(tmp_path):
    path = tmp_path / "target.txt"
    original = "present\n"
    path.write_text(original)
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@ missing @@\n"
        "-missing\n"
        "+replacement\n"
        "*** End Patch"
    )

    result = _ops(tmp_path).patch_v4a(patch)

    assert not result.success
    assert result.error is not None
    first_line = result.error.splitlines()[0]
    assert "hunk 1" in first_line and "not found" in first_line
    assert "Patch validation failed (no files were modified)" in result.error
    assert path.read_text() == original


def test_validation_multiple_reasons_keeps_each_reason_and_does_not_write(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first_original, second_original = "one\n", "two\n"
    first.write_text(first_original)
    second.write_text(second_original)
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {first}\n"
        "@@ missing @@\n"
        "-missing\n"
        "+replacement\n"
        f"*** Update File: {second}\n"
        "@@ also-missing @@\n"
        "-missing\n"
        "+replacement\n"
        "*** End Patch"
    )

    result = _ops(tmp_path).patch_v4a(patch)

    assert not result.success
    assert result.error is not None
    assert result.error.splitlines()[0].startswith(str(first))
    assert str(second) in result.error
    assert "no files were modified" in result.error
    assert first.read_text() == first_original
    assert second.read_text() == second_original

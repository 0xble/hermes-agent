"""Portable CI keeps the selected side environment and one reader-safe predecessor."""
import json
import os


def test_side_environment_gc_keeps_selected_and_previous(tmp_path):
    from pm.operations import collect_environment_generations

    root = tmp_path / "installs" / "0123456789abcdef" / "test-environment"
    root.mkdir(parents=True)
    generations = []
    for index in range(4):
        generation = root / f"gen-{index:032x}"
        generation.mkdir()
        (generation / "venv").mkdir()
        os.utime(generation, (index, index))
        generations.append(generation)
    (root / "active.json").write_text(
        json.dumps({"generation": generations[-1].name}), encoding="utf-8"
    )

    removed = collect_environment_generations(root, keep_previous=1)

    assert {path.name for path in removed} == {generations[0].name, generations[1].name}
    assert {path.name for path in root.glob("gen-*")} == {generations[2].name, generations[3].name}
    assert generations[3].is_dir()

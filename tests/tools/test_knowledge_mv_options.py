"""GNU mv option values are not destructive source operands; no commands execute."""
import pytest

from agent.delegation_context import delegated_child_context
from tools.knowledge_boundary import command_denial_reason
from tools.knowledge_command_paths import literal_paths


@pytest.mark.parametrize("command,sources", [
    ("mv -t/destination /source", ["/source"]),
    ("mv -ft/destination /source /other", ["/source", "/other"]),
    ("mv -ft /destination /source", ["/source"]),
    ("mv -T /source /destination", ["/source"]),
    ("mv -- -t/destination /source", ["-t/destination"]),
    ("mv -fS.t /source /destination", ["/source"]),
    ("mv -fS .t /source /destination", ["/source"]),
    ("mv -t/destination -- /source", ["/source"]),
])
def test_mv_consuming_options_preserve_source_destination_boundary(command, sources):
    assert literal_paths(command)[1] == sources


@pytest.mark.parametrize("option", ["-t", "-ft", "--target-directory="])
def test_mv_target_ancestor_is_not_a_destructive_source(tmp_path, monkeypatch, option):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    command = f"mv {option}{home} ordinary.txt"
    with delegated_child_context(read_only_knowledge=True):
        assert command_denial_reason(command, cwd=str(tmp_path)) is None

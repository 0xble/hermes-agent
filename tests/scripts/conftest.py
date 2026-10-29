"""Script/installer tests own a machine home in addition to HERMES_HOME."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_installer_machine_home(isolated_machine_home):
    """Use the shared opt-in boundary automatically for installer/script tests."""

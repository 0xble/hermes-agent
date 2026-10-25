"""Profile-scoped visibility policy for native goal notices."""
from hermes_cli.config import load_config_readonly


def goal_notices_enabled() -> bool:
    """Read on use so multiplexed profiles never share the launch profile's policy."""
    return (load_config_readonly().get("goals") or {}).get("notices", True) is not False

"""Exit diagnostics captured before the process bypasses asyncio finalization."""
from __future__ import annotations

import traceback


def make_gateway_process_exit_hook(exit_diag):
    def before_exit(code, success, error):
        if error is None:
            exit_diag("asyncio.run.returned", success=success)
            exit_diag("gateway.exit_clean" if code == 0 else "gateway.exit_nonzero")
        elif isinstance(error, SystemExit):
            exit_diag("asyncio.run.SystemExit", code=error.code,
                      traceback="".join(traceback.format_exception(error)))
        elif isinstance(error, KeyboardInterrupt):
            exit_diag("asyncio.run.KeyboardInterrupt", traceback="".join(traceback.format_exception(error)))
            print("\nGateway stopped.")
        else:
            exit_diag("asyncio.run.exception", exc_type=type(error).__name__, exc_repr=repr(error),
                      traceback="".join(traceback.format_exception(error)))
    return before_exit

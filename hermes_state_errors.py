"""Compatibility exports for legacy fork state error imports."""
from persistence.sqlite._state.errors import *  # noqa: F401,F403
from persistence.core.errors import GatewayOwnerRestartError
from storage_errors import is_malformed_db_error, is_fts_scoped_corruption_error


def is_fts_query_syntax_error(exc):
    """Recognize only SQLite's FTS5 parser diagnostics."""
    import sqlite3
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF != sqlite3.SQLITE_ERROR:
        return False
    text = str(exc).lower()
    return text == "unterminated string" or text.startswith("fts5: syntax error near ")

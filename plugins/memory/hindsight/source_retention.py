"""Automatic source-candidate discovery for the Hindsight provider.

This module is deliberately provider-local. Hermes already passes the full
OpenAI-style message list to providers that opt into ``sync_turn(...,
messages=...)``. We use that existing boundary to discover source material
without adding model tools, global hooks, or a second daemon.

The extractor is conservative about secrets and ephemeral artifacts, but it
favors retaining substantive non-sensitive source material automatically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

_MIN_TEXT_CHARS = 512
_TEXT_SOURCE_TOOLS = {
    "web_extract": "webpage",
    "web_extract_content": "webpage",
    "youtube_transcript": "transcript",
    "youtube_content": "transcript",
    "transcript": "transcript",
    "transcribe": "transcript",
    "speech_to_text": "transcript",
    "audio_transcribe": "transcript",
    "read_file": "file_extraction",
}
_FILE_SOURCE_TYPES = {"input_file", "file", "file_attachment", "document"}
_SECRET_PATH_RE = re.compile(
    r"(?:^|[/\\])(?:\.env(?:\.|$)|\.ssh(?:[/\\]|$)|credentials?(?:\.|$)|"
    r"secrets?(?:[/\\]|\.|$)|private[_-]?keys?(?:[/\\]|\.|$)|tokens?(?:[/\\]|\.|$)|"
    r"passwords?(?:[/\\]|\.|$))",
    re.IGNORECASE,
)
_SECRET_CONTENT_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S{12,})",
    re.IGNORECASE,
)
_EPHEMERAL_PATH_RE = re.compile(
    r"(?:[/\\](?:\.git|node_modules|__pycache__|\.venv|venv|dist|build|cache|caches|logs?)(?:[/\\]|$)|"
    r"(?:^|[/\\])(?:tmp|temp)(?:[/\\]|$))",
    re.IGNORECASE,
)
_ARTIFACT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".html", ".htm", ".csv", ".json", ".yaml", ".yml",
    ".tex", ".rtf", ".pdf", ".docx", ".odt",
}
_EXCLUDED_ARTIFACT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".c", ".cpp",
    ".sh", ".zsh", ".fish", ".toml", ".lock", ".db", ".sqlite", ".log",
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8", errors="replace"))


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except Exception:
            return str(value)
    return str(value)


def _json_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return value.strip()
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
    except Exception:
        return value.strip()


def _path_value(args: dict[str, Any]) -> str:
    for key in ("file_path", "path", "filename", "file", "local_path"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _url_value(args: dict[str, Any]) -> str:
    for key in ("url", "source_url", "video_url", "webpage_url"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_url(value)
    return ""


def _is_secret_path(path: str) -> bool:
    return bool(_SECRET_PATH_RE.search(path.replace(os.sep, "/")))


def _is_ephemeral_path(path: str) -> bool:
    return bool(_EPHEMERAL_PATH_RE.search(path.replace(os.sep, "/")))


def _safe_file(path: str) -> Path | None:
    if not path or _is_secret_path(path):
        return None
    try:
        original = Path(path).expanduser()
        if original.is_symlink():
            return None
        candidate = original.resolve()
        if not candidate.is_file():
            return None
        return candidate
    except (OSError, RuntimeError):
        return None


def _looks_like_pasted_source(content: str) -> bool:
    """Recognize long pasted evidence without treating every prompt as a source."""
    normalized = content.strip()
    if len(normalized) < 2000:
        return False
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if len(lines) < 6:
        return False
    markers = (
        "source:", "transcript:", "begin transcript", "quote:",
        "http://", "https://", "according to", "references", "## ",
    )
    lowered = normalized.lower()
    return any(marker in lowered for marker in markers)


def _is_secret_content(content: str) -> bool:
    return bool(_SECRET_CONTENT_RE.search(content))


def _is_durable_artifact_path(path: str) -> bool:
    normalized = path.replace(os.sep, "/")
    if _is_secret_path(normalized) or _is_ephemeral_path(normalized):
        return False
    suffix = Path(path).suffix.lower()
    if suffix in _EXCLUDED_ARTIFACT_EXTENSIONS:
        return False
    if suffix not in _ARTIFACT_EXTENSIONS:
        return False
    return any(part in normalized for part in ("/Vault/", "/Workspaces/", "/Documents/", "/Reports/"))


def _tool_result_map(messages: Iterable[dict[str, Any]]) -> dict[str, str]:
    results: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "").strip()
        if call_id:
            results[call_id] = _as_text(message.get("content"))
    return results


def _assistant_calls(messages: Iterable[dict[str, Any]]) -> Iterable[tuple[str, dict[str, Any], str]]:
    results = _tool_result_map(messages)
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = str(function.get("name") or "").strip().lower()
            if not name:
                continue
            call_id = str(call.get("id") or "").strip()
            yield name, _json_args(function.get("arguments")), results.get(call_id, "")


@dataclass(frozen=True)
class SourceCandidate:
    """A source eligible for automatic Hindsight retention."""

    source_type: str
    source_id: str
    context: str
    content: str = ""
    file_path: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    content_hash: str = ""

    @property
    def source_shape(self) -> str:
        return "file" if self.file_path else "complete_extraction"

    @property
    def automatic_key(self) -> tuple[str, str]:
        return self.source_id, self.content_hash


def _candidate_text(
    *,
    source_type: str,
    source_id: str,
    content: str,
    origin: str,
    context: str,
    session_id: str,
    tags: tuple[str, ...] = (),
) -> SourceCandidate | None:
    content = content.strip()
    if len(content) < _MIN_TEXT_CHARS or _is_secret_content(content):
        return None
    if _is_secret_path(origin) or _is_ephemeral_path(origin):
        return None
    digest = _sha256_text(content)
    metadata = {
        "source_type": source_type,
        "source_id": source_id,
        "source_origin": origin,
        "source_shape": "complete_extraction",
        "content_hash": digest,
        "session_id": session_id,
        "retained_automatically": "true",
    }
    return SourceCandidate(
        source_type=source_type,
        source_id=source_id,
        context=context,
        content=content,
        metadata=metadata,
        tags=tags,
        content_hash=digest,
    )


def _candidate_file(
    *,
    source_type: str,
    source_id: str,
    path: str,
    context: str,
    session_id: str,
) -> SourceCandidate | None:
    safe = _safe_file(path)
    if safe is None:
        return None
    try:
        digest = _sha256_bytes(safe.read_bytes())
    except OSError:
        return None
    metadata = {
        "source_type": source_type,
        "source_id": source_id,
        "source_origin": str(safe),
        "source_shape": "original_file",
        "content_hash": digest,
        "session_id": session_id,
        "retained_automatically": "true",
    }
    return SourceCandidate(
        source_type=source_type,
        source_id=source_id,
        context=context,
        file_path=str(safe),
        metadata=metadata,
        tags=(f"source:{source_type}",),
        content_hash=digest,
    )


def discover_source_candidates(
    messages: Iterable[dict[str, Any]] | None,
    *,
    session_id: str = "",
    retain_attachments: bool = False,
    retain_file_extractions: bool = False,
) -> list[SourceCandidate]:
    """Discover substantive, non-sensitive sources from completed messages.

    This intentionally handles explicit source-bearing boundaries rather than
    treating every tool result as durable knowledge.
    """
    if not messages:
        return []
    messages = list(messages)
    # ``MemoryManager.sync_all`` supplies the session message list. Restrict
    # discovery to the latest user-led turn so an old webpage/file is not
    # re-submitted on every later turn or after a long session append.
    last_user_index = next(
        (index for index in range(len(messages) - 1, -1, -1)
         if isinstance(messages[index], dict) and messages[index].get("role") == "user"),
        0,
    )
    messages = messages[last_user_index:]
    candidates: list[SourceCandidate] = []

    # Raw file bytes cross a stronger trust boundary than text already present
    # in the session. Do not even open or hash attachments unless the provider
    # has an explicit, default-off opt-in.
    if retain_attachments:
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            parts = content if isinstance(content, list) else []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                ptype = str(part.get("type") or "").strip().lower()
                if ptype not in _FILE_SOURCE_TYPES:
                    continue
                path = _path_value(part)
                if not path:
                    continue
                source_id = str(part.get("file_id") or part.get("id") or f"file-{_sha256_text(str(Path(path).expanduser().resolve()))[:32]}")
                candidate = _candidate_file(
                    source_type="user_file",
                    source_id=f"attachment:{source_id}",
                    path=path,
                    context=(
                        "File supplied by the user in chat and retained under the explicit "
                        "raw-attachment opt-in. Preserve the source and distinguish its "
                        "claims from Hermes analysis."
                    ),
                    session_id=session_id,
                )
                if candidate:
                    candidates.append(candidate)

    # Long, explicitly source-like pasted text is separate evidence rather than
    # merely another conversational request. Short prose remains ordinary
    # session retention and is not duplicated here.
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            pasted = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and str(part.get("type") or "") == "text"
            )
        else:
            pasted = _as_text(content)
        if not _looks_like_pasted_source(pasted) or _is_secret_content(pasted):
            continue
        digest = _sha256_text(pasted.strip())
        candidate = _candidate_text(
            source_type="pasted_source",
            source_id=f"pasted-{digest[:32]}",
            content=pasted,
            origin="user message",
            context=(
                "Long source-like text pasted by Brian in chat and used in the completed turn. "
                "Treat it as source evidence, not as Brian's decision or Hermes analysis."
            ),
            session_id=session_id,
            tags=("source:pasted",),
        )
        if candidate:
            candidates.append(candidate)

    # Tool-produced source text and final durable text artifacts.
    for name, args, result in _assistant_calls(messages):
        if name in _TEXT_SOURCE_TOOLS:
            source_type = _TEXT_SOURCE_TOOLS[name]
            if source_type == "file_extraction" and not retain_file_extractions:
                continue
            origin = _url_value(args) or _path_value(args) or name
            if source_type == "webpage":
                source_id = f"webpage-{_sha256_text(origin)[:32]}"
                context = (
                    "Webpage content retrieved and substantively used in the completed turn. "
                    "External source that may become stale; preserve retrieval provenance."
                )
            else:
                source_id = f"{source_type}-{_sha256_text(origin or name)[:32]}"
                context = (
                    f"Complete {source_type} extraction used in the completed turn. "
                    "Preserve attribution and uncertainty; distinguish source evidence from Hermes analysis."
                )
            candidate = _candidate_text(
                source_type=source_type,
                source_id=source_id,
                content=result,
                origin=origin,
                context=context,
                session_id=session_id,
                tags=(f"source:{source_type}",),
            )
            if candidate:
                candidates.append(candidate)

        if name in {"write_file", "file_write"}:
            path = _path_value(args)
            content = _as_text(args.get("content"))
            if _is_durable_artifact_path(path):
                candidate = _candidate_text(
                    source_type="artifact",
                    source_id=f"artifact-{_sha256_text(str(Path(path).expanduser().resolve()))[:32]}",
                    content=content,
                    origin=path,
                    context=(
                        "Durable artifact created by Hermes and used or delivered in the completed turn. "
                        "Treat the artifact as derived output and preserve its canonical path."
                    ),
                    session_id=session_id,
                    tags=("source:artifact",),
                )
                if candidate:
                    candidates.append(candidate)

    unique: list[SourceCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate.automatic_key in seen:
            continue
        seen.add(candidate.automatic_key)
        unique.append(candidate)
    return unique


__all__ = ["SourceCandidate", "discover_source_candidates"]

"""No-store reads use a capability handshake, private routes and typed SDK responses.

A public SDK read is not a fallback: servers may persist its query, result or
error even when the Hermes provider has disabled retain operations.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp


class PrivateReadError(RuntimeError):
    """A deliberately content-free failure at the private transport boundary."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PrivateReadResponse:
    value: Any
    query_sha256: str
    bank_sha256: str
    session_sha256: str
    epoch: int
    result_nonempty: bool

    @property
    def results(self):
        return self.value.results

    @property
    def text(self):
        return self.value.text

    def tool_result(self, text: str) -> str:
        return json.dumps(
            {
                "result": text,
                "_hermes_private_read": {
                    "no_store": True,
                    "session_sha256": self.session_sha256,
                    "epoch": self.epoch,
                    "result_nonempty": self.result_nonempty,
                    "bank_sha256": self.bank_sha256,
                    "query_sha256": self.query_sha256,
                    "result_text_sha256": _sha256(text),
                },
            },
            ensure_ascii=False,
        )


class PrivateReadClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout: float,
        session_id: str,
        epoch: int,
    ):
        parsed = urlsplit(base_url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PrivateReadError(
                "Private Hindsight reads require an existing API endpoint."
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._session_sha256 = _sha256(session_id)
        self._epoch = epoch

    async def arecall(self, *, bank_id: str, **kwargs) -> PrivateReadResponse:
        from hindsight_client_api.models.recall_request import RecallRequest
        from hindsight_client_api.models.recall_response import RecallResponse

        return await self._read(
            "recall", bank_id, kwargs, RecallRequest, RecallResponse
        )

    async def areflect(self, *, bank_id: str, **kwargs) -> PrivateReadResponse:
        from hindsight_client_api.models.reflect_request import ReflectRequest
        from hindsight_client_api.models.reflect_response import ReflectResponse

        return await self._read(
            "reflect", bank_id, kwargs, ReflectRequest, ReflectResponse
        )

    async def _read(
        self, operation: str, bank_id: str, kwargs: dict, request_model, response_model
    ):
        if (
            not isinstance(bank_id, str)
            or not bank_id
            or bank_id in {".", ".."}
            or "/" in bank_id
            or "\\" in bank_id
        ):
            raise PrivateReadError("Private Hindsight bank route is invalid.")
        headers = {"Cache-Control": "no-store"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        try:
            # Do not reuse a public client's transport, hooks, cache or embedded-daemon bootstrap.
            async with aiohttp.ClientSession(
                headers=headers,
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as client:
                async with client.get(
                    self._base_url + "/version", allow_redirects=False
                ) as response:
                    if response.status != 200:
                        raise PrivateReadError(
                            "Private Hindsight capability is unavailable."
                        )
                    capability = await response.json()
                    if capability.get("features", {}).get("private_reads") is not True:
                        raise PrivateReadError(
                            "Hindsight does not support no-store reads."
                        )
                body = request_model(**kwargs).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                url = (
                    self._base_url
                    + "/v1/default/banks/"
                    + quote(bank_id, safe="")
                    + "/private/"
                    + operation
                )
                async with client.post(
                    url, json=body, allow_redirects=False
                ) as response:
                    if (
                        response.headers.get("X-Hindsight-Private-Read") != "1"
                        or response.status != 200
                    ):
                        raise PrivateReadError(
                            "Private Hindsight read was not acknowledged."
                        )
                    value = response_model.model_validate(await response.json())
            nonempty = (
                any(
                    isinstance(row.text, str) and bool(row.text.strip())
                    for row in (value.results or [])
                )
                if operation == "recall"
                else isinstance(value.text, str) and bool(value.text.strip())
            )
            return PrivateReadResponse(
                value,
                _sha256(kwargs["query"]),
                _sha256(bank_id),
                self._session_sha256,
                self._epoch,
                nonempty,
            )
        except PrivateReadError:
            raise
        except Exception:
            # Validation, HTTP and provider errors may contain private request/response bytes.
            raise PrivateReadError("Private Hindsight read failed.") from None

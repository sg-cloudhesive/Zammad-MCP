"""Optional middleware that appends agent-portal customer context to ticket tools."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import mcp.types as mt
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

from .oauth_settings import env_flag_enabled

logger = logging.getLogger(__name__)

_CONTEXT_PREFIX = "Customer context (agent portal):\n"
_TRUNCATION_SUFFIX = "\n[context truncated]"
_NOTE_UNAVAILABLE = "Customer context is not available for this ticket."
_NOTE_COULD_NOT_LOAD = "Customer context could not be loaded."
_NOTE_NO_TOKEN = "Customer context requires a signed-in user."

ENV_URL = "TICKET_CONTEXT_URL"
ENV_TOOLS = "TICKET_CONTEXT_TOOLS"
ENV_TIMEOUT = "TICKET_CONTEXT_TIMEOUT_SECONDS"
ENV_MAX_CHARS = "TICKET_CONTEXT_MAX_CHARS"

_DEFAULT_TOOLS = ("zammad_get_ticket",)
_HTTP_STATUS_MIN = 200
_HTTP_STATUS_MAX = 300
_MAX_TIMEOUT_SECONDS = 60.0
_MIN_CONTEXT_CHARS = 1000


@dataclass(frozen=True)
class TicketContextSettings:
    """Configuration for optional ticket-context enrichment."""

    url: str | None
    tools: tuple[str, ...]
    timeout_seconds: float
    max_chars: int
    insecure: bool

    @property
    def enabled(self) -> bool:
        """Return whether ticket-context enrichment is active."""
        return self.url is not None

    def log_host(self) -> str:
        """Return the context URL hostname for logging (never the path or query)."""
        if self.url is None:
            return ""
        return urlparse(self.url).hostname or ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TicketContextSettings:
        """Build settings from environment variables."""
        env = os.environ if environ is None else environ
        raw_url = env.get(ENV_URL, "").strip()
        url = _parse_url(raw_url) if raw_url else None
        tools = _parse_tools(env.get(ENV_TOOLS, ""))
        timeout_seconds = _parse_timeout(env.get(ENV_TIMEOUT, ""))
        max_chars = _parse_max_chars(env.get(ENV_MAX_CHARS, ""))
        insecure = env_flag_enabled("ZAMMAD_INSECURE", env)
        return cls(
            url=url,
            tools=tools,
            timeout_seconds=timeout_seconds,
            max_chars=max_chars,
            insecure=insecure,
        )


class TicketContextMiddleware(Middleware):
    """Append agent-portal customer context after successful configured tool calls."""

    def __init__(
        self,
        settings: TicketContextSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Bind middleware to validated settings.

        Args:
            settings: Ticket-context configuration.
            http_client: Optional HTTP client for tests (MockTransport).
        """
        self._settings = settings
        self._http_client = http_client

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Run the tool, then optionally append customer context."""
        result = await call_next(context)
        ticket_id = _parse_ticket_id(context.message.arguments or {})
        if context.message.name not in self._settings.tools or _result_is_error(result) or ticket_id is None:
            return result
        token = _token_from_access(get_access_token())
        if not token:
            return _append_note(result, [_NOTE_NO_TOKEN], self._settings.max_chars)
        try:
            payload = await self._fetch_context(ticket_id, token)
        except _ContextFetchError as exc:
            logger.warning(
                "ticket context fetch failed for tool=%s ticket_id=%s detail=%s",
                context.message.name,
                ticket_id,
                exc.log_detail,
            )
            note = _NOTE_UNAVAILABLE if exc.unavailable else _NOTE_COULD_NOT_LOAD
            return _append_note(result, [note], self._settings.max_chars)
        text = _CONTEXT_PREFIX + json.dumps(payload, indent=2)
        return _append_text_block(result, text, max_chars=self._settings.max_chars)

    async def _fetch_context(self, ticket_id: int, token: str) -> dict[str, Any]:
        """POST to the agent portal and return the JSON object body."""
        headers = {"Authorization": f"Bearer {token}"}
        body = {"ticket_id": ticket_id}
        verify = not self._settings.insecure
        timeout = self._settings.timeout_seconds
        url = self._settings.url
        if url is None:
            raise _ContextFetchError(log_detail="disabled", unavailable=False)
        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=timeout,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    verify=verify,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise _ContextFetchError(log_detail=type(exc).__name__, unavailable=False) from exc
        except httpx.HTTPError as exc:
            raise _ContextFetchError(log_detail=type(exc).__name__, unavailable=False) from exc
        if response.status_code in {401, 403, 404}:
            raise _ContextFetchError(
                log_detail=f"http_{response.status_code}",
                unavailable=True,
            )
        if response.status_code < _HTTP_STATUS_MIN or response.status_code >= _HTTP_STATUS_MAX:
            raise _ContextFetchError(
                log_detail=f"http_{response.status_code}",
                unavailable=False,
            )
        try:
            payload = response.json()
        except json.JSONDecodeError:
            raise _ContextFetchError(log_detail="json_decode", unavailable=False) from None
        if not isinstance(payload, dict):
            raise _ContextFetchError(log_detail="unexpected_json", unavailable=False)
        return payload


@dataclass(frozen=True)
class _ContextFetchError(Exception):
    """Internal signal for fail-open context fetch handling."""

    log_detail: str
    unavailable: bool


def _parse_url(raw: str) -> str:
    """Validate TICKET_CONTEXT_URL."""
    parsed = urlparse(raw.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError(f"{ENV_URL} must be an http:// or https:// URL with a host, got {raw.strip()!r}")
    return raw.strip()


def _parse_tools(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated tool list."""
    if not raw.strip():
        return _DEFAULT_TOOLS
    tools = tuple(part.strip() for part in raw.split(",") if part.strip())
    return tools if tools else _DEFAULT_TOOLS


def _parse_timeout(raw: str) -> float:
    """Parse TICKET_CONTEXT_TIMEOUT_SECONDS."""
    value = 8.0 if not raw.strip() else float(raw.strip())
    if value <= 0 or value > _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"{ENV_TIMEOUT} must be greater than 0 and at most 60, got {value}")
    return value


def _parse_max_chars(raw: str) -> int:
    """Parse TICKET_CONTEXT_MAX_CHARS."""
    value = 60_000 if not raw.strip() else int(raw.strip())
    if value < _MIN_CONTEXT_CHARS:
        raise ValueError(f"{ENV_MAX_CHARS} must be at least 1000, got {value}")
    return value


def _parse_ticket_id(arguments: dict[str, Any]) -> int | None:
    """Read ticket_id from tool arguments when it is int-like."""
    if "ticket_id" not in arguments:
        return None
    raw = arguments["ticket_id"]
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _token_from_access(access: Any) -> str | None:
    """Extract a bearer token from the FastMCP access token object."""
    if access is None:
        return None
    token = getattr(access, "token", None)
    if isinstance(token, str) and token:
        return token
    return None


def _result_is_error(result: ToolResult) -> bool:
    """Return whether a tool result represents an MCP tool error."""
    meta = result.meta
    if not meta:
        return False
    return meta.get("isError") is True or meta.get("error") is True


def _append_note(result: ToolResult, notes: list[str], max_chars: int) -> ToolResult:
    """Append a fail-open customer-context note block."""
    text = _CONTEXT_PREFIX + json.dumps({"notes": notes}, indent=2)
    return _append_text_block(result, text, max_chars=max_chars)


def _append_text_block(result: ToolResult, text: str, *, max_chars: int) -> ToolResult:
    """Append one text block, preserving structured content and meta."""
    if len(text) > max_chars:
        keep = max(0, max_chars - len(_TRUNCATION_SUFFIX))
        text = text[:keep] + _TRUNCATION_SUFFIX
    block = mt.TextContent(type="text", text=text)
    return result.model_copy(update={"content": [*result.content, block]})

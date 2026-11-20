"""Tests for optional ticket-context middleware."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import mcp.types as mt
import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult

from mcp_zammad.server import ZammadMCPServer
from mcp_zammad.ticket_context import TicketContextMiddleware, TicketContextSettings

_SECRET_TOKEN = "secret-token-must-not-log"


def _enabled_env(**overrides: str) -> dict[str, str]:
    env = {
        "TICKET_CONTEXT_URL": "https://agent.example.com/api/agent/tickets/context",
        "TICKET_CONTEXT_TOOLS": "zammad_get_ticket",
        "TICKET_CONTEXT_TIMEOUT_SECONDS": "8",
        "TICKET_CONTEXT_MAX_CHARS": "60000",
    }
    env.update(overrides)
    return env


def _settings(**overrides: str) -> TicketContextSettings:
    return TicketContextSettings.from_env(_enabled_env(**overrides))


def _base_result() -> ToolResult:
    return ToolResult(
        content=[mt.TextContent(type="text", text='{"id": 99, "title": "Help"}')],
        structured_content={"id": 99, "title": "Help"},
    )


def _context(name: str, arguments: dict[str, Any] | None = None) -> MiddlewareContext[mt.CallToolRequestParams]:
    return MiddlewareContext(
        message=mt.CallToolRequestParams(name=name, arguments=arguments or {}),
        method="tools/call",
    )


async def _run_middleware(
    middleware: TicketContextMiddleware,
    *,
    tool: str = "zammad_get_ticket",
    arguments: dict[str, Any] | None = None,
    result: ToolResult | None = None,
) -> ToolResult:
    base = _base_result() if result is None else result

    async def call_next(_ctx: MiddlewareContext[mt.CallToolRequestParams]) -> ToolResult:
        return base

    return await middleware.on_call_tool(_context(tool, arguments), call_next)


def test_settings_disabled_when_url_unset() -> None:
    settings = TicketContextSettings.from_env({})
    assert settings.enabled is False
    assert settings.tools == ("zammad_get_ticket",)


def test_settings_parse_valid_url_and_tools() -> None:
    settings = _settings(
        TICKET_CONTEXT_TOOLS="zammad_get_ticket, zammad_search_tickets ",
        TICKET_CONTEXT_TIMEOUT_SECONDS="12.5",
        TICKET_CONTEXT_MAX_CHARS="5000",
    )
    assert settings.enabled is True
    assert settings.tools == ("zammad_get_ticket", "zammad_search_tickets")
    assert settings.timeout_seconds == 12.5
    assert settings.max_chars == 5000
    assert settings.log_host() == "agent.example.com"


def test_settings_rejects_invalid_url() -> None:
    with pytest.raises(ValueError, match="TICKET_CONTEXT_URL"):
        TicketContextSettings.from_env({"TICKET_CONTEXT_URL": "ftp://bad"})


def test_settings_rejects_bad_timeout() -> None:
    env = _enabled_env(TICKET_CONTEXT_TIMEOUT_SECONDS="0")
    with pytest.raises(ValueError, match="TICKET_CONTEXT_TIMEOUT_SECONDS"):
        TicketContextSettings.from_env(env)


def test_settings_rejects_small_max_chars() -> None:
    env = _enabled_env(TICKET_CONTEXT_MAX_CHARS="50")
    with pytest.raises(ValueError, match="TICKET_CONTEXT_MAX_CHARS"):
        TicketContextSettings.from_env(env)


@pytest.mark.asyncio
async def test_only_configured_tools_are_augmented() -> None:
    settings = _settings(TICKET_CONTEXT_TOOLS="zammad_get_ticket")
    middleware = TicketContextMiddleware(settings)
    with patch("mcp_zammad.ticket_context.get_access_token", return_value=None):
        unchanged = await _run_middleware(middleware, tool="zammad_search_tickets", arguments={"ticket_id": 1})
    assert len(unchanged.content) == 1


@pytest.mark.asyncio
async def test_error_results_unchanged() -> None:
    settings = _settings()
    middleware = TicketContextMiddleware(settings)
    error_result = ToolResult(
        content=[mt.TextContent(type="text", text="boom")],
        meta={"isError": True},
    )
    with patch("mcp_zammad.ticket_context.get_access_token", return_value=SimpleNamespace(token=_SECRET_TOKEN)):
        out = await _run_middleware(middleware, arguments={"ticket_id": 1}, result=error_result)
    assert out is error_result
    assert len(out.content) == 1


@pytest.mark.asyncio
async def test_success_appends_one_block_and_sends_bearer_token(caplog: pytest.LogCaptureFixture) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"customer": "Acme", "ticket_id": 42})

    transport = httpx.MockTransport(handler)
    settings = _settings()
    async with httpx.AsyncClient(transport=transport) as client:
        middleware = TicketContextMiddleware(settings, http_client=client)
        with (
            patch(
                "mcp_zammad.ticket_context.get_access_token",
                return_value=SimpleNamespace(token=_SECRET_TOKEN),
            ),
            caplog.at_level(logging.WARNING),
        ):
            out = await _run_middleware(middleware, arguments={"ticket_id": 42})

    assert len(out.content) == 2
    assert out.content[0].text == _base_result().content[0].text
    assert "Acme" in out.content[1].text
    assert captured[0].headers["Authorization"] == f"Bearer {_SECRET_TOKEN}"
    body = json.loads(captured[0].content.decode())
    assert body == {"ticket_id": 42}
    assert _SECRET_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_missing_token_appends_note_without_http() -> None:
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call to {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        middleware = TicketContextMiddleware(settings, http_client=client)
        with patch("mcp_zammad.ticket_context.get_access_token", return_value=None):
            out = await _run_middleware(middleware, arguments={"ticket_id": 7})

    assert len(out.content) == 2
    assert "signed-in user" in out.content[1].text


@pytest.mark.asyncio
async def test_http_500_fails_open() -> None:
    settings = _settings()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    middleware = TicketContextMiddleware(
        settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with patch(
        "mcp_zammad.ticket_context.get_access_token",
        return_value=SimpleNamespace(token=_SECRET_TOKEN),
    ):
        out = await _run_middleware(middleware, arguments={"ticket_id": 1})

    assert len(out.content) == 2
    assert out.content[0].text == _base_result().content[0].text
    assert "could not be loaded" in out.content[1].text


@pytest.mark.asyncio
async def test_http_403_not_available_note() -> None:
    settings = _settings()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    middleware = TicketContextMiddleware(
        settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with patch(
        "mcp_zammad.ticket_context.get_access_token",
        return_value=SimpleNamespace(token=_SECRET_TOKEN),
    ):
        out = await _run_middleware(middleware, arguments={"ticket_id": 1})

    assert "not available for this ticket" in out.content[1].text


@pytest.mark.asyncio
async def test_timeout_fails_open() -> None:
    settings = _settings(TICKET_CONTEXT_TIMEOUT_SECONDS="0.01")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    middleware = TicketContextMiddleware(
        settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with patch(
        "mcp_zammad.ticket_context.get_access_token",
        return_value=SimpleNamespace(token=_SECRET_TOKEN),
    ):
        out = await _run_middleware(middleware, arguments={"ticket_id": 1})

    assert "could not be loaded" in out.content[1].text


@pytest.mark.asyncio
async def test_non_json_fails_open() -> None:
    settings = _settings()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    middleware = TicketContextMiddleware(
        settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with patch(
        "mcp_zammad.ticket_context.get_access_token",
        return_value=SimpleNamespace(token=_SECRET_TOKEN),
    ):
        out = await _run_middleware(middleware, arguments={"ticket_id": 1})

    assert "could not be loaded" in out.content[1].text


@pytest.mark.asyncio
async def test_truncation_suffix() -> None:
    settings = _settings(TICKET_CONTEXT_MAX_CHARS="1000")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"detail": "x" * 5000})

    middleware = TicketContextMiddleware(
        settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with patch(
        "mcp_zammad.ticket_context.get_access_token",
        return_value=SimpleNamespace(token=_SECRET_TOKEN),
    ):
        out = await _run_middleware(middleware, arguments={"ticket_id": 1})

    block = out.content[1].text
    assert len(block) <= 1000
    assert block.endswith("[context truncated]")


def test_server_registers_middleware_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TICKET_CONTEXT_URL", raising=False)
    off = ZammadMCPServer()
    assert not any(isinstance(m, TicketContextMiddleware) for m in off.mcp.middleware)

    monkeypatch.setenv("TICKET_CONTEXT_URL", "https://agent.example.com/api/context")
    on = ZammadMCPServer()
    assert any(isinstance(m, TicketContextMiddleware) for m in on.mcp.middleware)

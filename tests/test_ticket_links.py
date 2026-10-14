"""Tests for ticket linking: ZammadClient.link_tickets/get_ticket_links and the MCP tools."""

from collections.abc import Generator
from unittest.mock import Mock, patch

import pytest
import requests
from pydantic import ValidationError

from mcp_zammad.client import ZammadClient
from mcp_zammad.models import GetTicketLinksParams, LinkTicketsParams, LinkTicketsResult, TicketLinksResult
from mcp_zammad.server import ZammadMCPServer

URL = "https://test.zammad.com/api/v1"


@pytest.fixture
def api() -> Generator[Mock, None, None]:
    """Mock zammad_py.ZammadAPI and return the instance the client will use."""
    with patch("mcp_zammad.client.ZammadAPI") as mock_api:
        instance = Mock()
        mock_api.return_value = instance
        yield instance


@pytest.fixture
def transport(api: Mock) -> Mock:
    """Raw session double, captured before the client wraps it in ResilientSession."""
    return api.session


def _response(payload: dict | None = None, ok: bool = True, text: str = "") -> Mock:
    response = Mock()
    response.ok = ok
    response.status_code = 200 if ok else 422
    response.text = text
    response.json.return_value = payload or {}
    return response


# ---------- client: link_tickets ----------


def test_link_tickets_posts_source_number_and_target_id(api: Mock, transport: Mock) -> None:
    api.ticket.find.return_value = {"id": 10, "number": "79271214"}
    transport.request.return_value = _response({})
    client = ZammadClient(url=URL, http_token="test-token")

    result = client.link_tickets(10, 20, "child")

    api.ticket.find.assert_called_once_with(10)
    method, url = transport.request.call_args.args[:2]
    assert (method, url) == ("POST", f"{URL}/links/add")
    assert transport.request.call_args.kwargs["json"] == {
        "link_type": "child",
        "link_object_source": "Ticket",
        "link_object_source_number": "79271214",
        "link_object_target": "Ticket",
        "link_object_target_value": 20,
    }
    assert result == {
        "linked": True,
        "source_ticket_id": 10,
        "source_ticket_number": "79271214",
        "target_ticket_id": 20,
        "link_type": "child",
    }


def test_link_tickets_rejects_self_link(api: Mock, transport: Mock) -> None:
    client = ZammadClient(url=URL, http_token="test-token")

    with pytest.raises(ValueError, match="different tickets"):
        client.link_tickets(10, 10)

    transport.request.assert_not_called()


def test_link_tickets_raises_with_zammad_error_text(api: Mock, transport: Mock) -> None:
    api.ticket.find.return_value = {"id": 10, "number": "100"}
    transport.request.return_value = _response(ok=False, text='{"error":"Not authorized"}')
    client = ZammadClient(url=URL, http_token="test-token")

    with pytest.raises(requests.HTTPError, match="Not authorized"):
        client.link_tickets(10, 20)


# ---------- client: get_ticket_links ----------


def test_get_ticket_links_maps_assets_and_skips_non_tickets(api: Mock, transport: Mock) -> None:
    transport.request.return_value = _response(
        {
            "links": [
                {"link_type": "child", "link_object": "Ticket", "link_object_value": 20},
                {"link_type": "normal", "link_object": "KnowledgeBase::Answer::Translation", "link_object_value": 5},
                {"link_type": "normal", "link_object": "Ticket", "link_object_value": 30},
            ],
            "assets": {"Ticket": {"20": {"id": 20, "number": 20002, "title": "[Normal] CR: Expand disk"}}},
        }
    )
    client = ZammadClient(url=URL, http_token="test-token")

    links = client.get_ticket_links(10)

    method, url = transport.request.call_args.args[:2]
    assert (method, url) == ("GET", f"{URL}/links")
    assert transport.request.call_args.kwargs["params"] == {"link_object": "Ticket", "link_object_value": 10}
    assert links == [
        {"link_type": "child", "ticket_id": 20, "number": "20002", "title": "[Normal] CR: Expand disk"},
        {"link_type": "normal", "ticket_id": 30, "number": None, "title": None},
    ]


def test_get_ticket_links_empty(api: Mock, transport: Mock) -> None:
    transport.request.return_value = _response({"links": [], "assets": {}})
    client = ZammadClient(url=URL, http_token="test-token")

    assert client.get_ticket_links(10) == []


# ---------- models ----------


def test_link_params_default_type_and_self_link_validation() -> None:
    assert LinkTicketsParams(source_ticket_id=1, target_ticket_id=2).link_type == "normal"
    with pytest.raises(ValidationError, match="different tickets"):
        LinkTicketsParams(source_ticket_id=1, target_ticket_id=1)
    with pytest.raises(ValidationError):
        LinkTicketsParams(source_ticket_id=1, target_ticket_id=2, link_type="sibling")


# ---------- server tools ----------


@pytest.fixture
def link_tools(decorator_capturer):
    """Build a server with a mocked client and return (client, tools dict)."""
    server_inst = ZammadMCPServer()
    server_inst.client = Mock()
    tools, capture_tool = decorator_capturer(server_inst.mcp.tool)
    server_inst.mcp.tool = capture_tool
    server_inst.get_client = lambda: server_inst.client
    server_inst._setup_tools()
    return server_inst.client, tools


def test_link_tool_returns_result(link_tools) -> None:
    client, tools = link_tools
    client.link_tickets.return_value = {
        "linked": True,
        "source_ticket_id": 10,
        "source_ticket_number": "100",
        "target_ticket_id": 20,
        "link_type": "child",
    }

    result = tools["zammad_link_tickets"](
        **LinkTicketsParams(source_ticket_id=10, target_ticket_id=20, link_type="child").model_dump()
    )

    assert isinstance(result, LinkTicketsResult)
    assert result.source_ticket_number == "100"
    client.link_tickets.assert_called_once_with(source_ticket_id=10, target_ticket_id=20, link_type="child")


def test_get_links_tool_returns_result(link_tools) -> None:
    client, tools = link_tools
    client.get_ticket_links.return_value = [{"link_type": "child", "ticket_id": 20, "number": "20002", "title": "CR"}]

    result = tools["zammad_get_ticket_links"](**GetTicketLinksParams(ticket_id=10).model_dump())

    assert isinstance(result, TicketLinksResult)
    assert result.links[0].ticket_id == 20
    client.get_ticket_links.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_link_tools_annotations() -> None:
    tools = {tool.name: tool for tool in await ZammadMCPServer().mcp.list_tools()}

    link = tools["zammad_link_tickets"]
    assert link.annotations.title == "Link Tickets"
    assert link.annotations.readOnlyHint is False
    assert link.annotations.destructiveHint is False

    get_links = tools["zammad_get_ticket_links"]
    assert get_links.annotations.title == "Get Ticket Links"
    assert get_links.annotations.readOnlyHint is True

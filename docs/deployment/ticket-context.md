# Ticket context enrichment (optional)

When `TICKET_CONTEXT_URL` is set, successful calls to configured MCP tools (default:
`zammad_get_ticket`) return the same Zammad payload as today **plus** one extra text
content block with customer context from an external HTTP endpoint (for example the
CloudHesive agent portal). The MCP URL, tool names, and primary tool output are
unchanged. When the variable is unset, the server behaves like upstream Zammad-MCP
with no ticket-context middleware registered.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `TICKET_CONTEXT_URL` | *(unset)* | When unset or blank, the feature is off. Otherwise must be `http://` or `https://` with a host. |
| `TICKET_CONTEXT_TOOLS` | `zammad_get_ticket` | Comma-separated tool names to enrich. |
| `TICKET_CONTEXT_TIMEOUT_SECONDS` | `8` | HTTP timeout (seconds); must be `> 0` and `≤ 60`. |
| `TICKET_CONTEXT_MAX_CHARS` | `60000` | Max length of the appended text block; must be `≥ 1000`. Truncation adds `[context truncated]`. |

TLS follows the same conventions as the rest of this server: `ZAMMAD_INSECURE` disables
certificate verification for the context HTTP client; otherwise the default trust store
and `SSL_CERT_FILE` apply.

## Context endpoint contract

The MCP server issues:

```http
POST {TICKET_CONTEXT_URL}
Authorization: Bearer <caller's Zammad OAuth access token>
Content-Type: application/json

{"ticket_id": <int>}
```

Expected responses:

- **200** — JSON object (any shape). It is appended verbatim (pretty-printed) after the
  prefix `Customer context (agent portal):`.
- **401**, **403**, **404** — Fail open: the tool result is unchanged except for an extra
  text block with `{"notes": ["Customer context is not available for this ticket."]}`.
- **Other errors**, timeouts, non-JSON bodies — Fail open with
  `{"notes": ["Customer context could not be loaded."]}`.

If there is no bearer token (user not signed in under OAuth), no HTTP call is made; a
note block explains that context requires a signed-in user.

Failures are logged at WARNING with tool name, ticket id, and HTTP status or exception
type only (never the token, URL query, or response body).

## Keeping this fork current

All ticket-context logic lives in `mcp_zammad/ticket_context.py` with tests in
`tests/test_ticket_context.py`. Upstream integration is a small hook in
`mcp_zammad/server.py` immediately after audit middleware registration (import +
`TicketContextSettings.from_env()` + conditional `add_middleware`).

When refreshing from [basher83/Zammad-MCP](https://github.com/basher83/Zammad-MCP):

```bash
git fetch upstream
git merge upstream/main
```

That merge is expected to stay conflict-free as long as upstream edits do not overlap
the same hook lines in `server.py`.
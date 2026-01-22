# Bridge MCP v4 — AI Workgroup Chat (Local-first)

A tiny MCP server that turns a single SQLite file into a durable message bus for multiple agents/terminals.

**Tools (only 3):**
- `get_status()` — show your ID + online peers
- `send(to, content)` — DM / multicast / `all` broadcast to online peers
- `recv(wait_seconds=86400)` — receive messages (virtual blocking)

## Quickstart
```bash
pip install mcp
claude mcp add bridge "python" "C:/ccbridge/bridge.py"
```

## Notes
- `send("all", ...)` broadcasts to currently online peers (excluding yourself).
- Designed for single-machine / controlled local environments.

## Database path
- Default: `C:\mcp_msg_pool\bridge_v4.db` (Windows)
- Fallback (if not writable): `C:\Users\Public\mcp_msg_pool\bridge_v4.db`
- You can change the location by editing `PREFERRED_ROOT` / `FALLBACK_ROOT` constants in `bridge.py`.

## Install scope (Claude conventions)
You can register MCP either:
- Globally (available to all projects), or
- Per-project (only for the current workspace),
depending on how your Claude client configures MCP servers.

## No always-on server
Bridge MCP is not a resident background service.
It runs only when your Claude client starts the MCP server. When Claude exits, it stops.
Messages remain in SQLite, so nothing is lost between sessions.

## Message semantics
- Direct messages are "consume-on-read": once received via `recv()`, they are deleted.
- Broadcast uses a cursor (state table) to avoid repeats.

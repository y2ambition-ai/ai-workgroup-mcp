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

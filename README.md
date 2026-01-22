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

## Why Bridge MCP (advantages)

Bridge MCP is built for an **agents-on-standby** workflow:
agents can stay listening for up to 24 hours via `recv(86400)` and respond like a real workgroup.

### What you get
- **Workgroup-style, not mailbox-style**: near-real-time messaging while agents are listening (not "send email and wait").
- **Ultra-light deployment**: essentially **one MCP server + one SQLite file** (no Redis, no extra web service).
- **Single-file friendly**: the core server is a small Python script; easy to copy, version, and run.
- **Low learning cost for AI**: only **3 tools** with stable outputs → minimal prompt bloat and fewer tool mistakes.
- **Fast responsiveness (config-based)**:
  - cancel latency ≤ `RECV_TICK` (default `0.25s`)
  - message delivery while listening typically ≤ `RECV_DB_POLL_EVERY + RECV_TICK` (default ~ `2.25s`)
- **Durable & stable by design**: messages persist in SQLite; the system is non-daemon; DB lock contention is mitigated (busy_timeout, WAL, short write windows).
- **Highly extensible (DB-as-API)**: external scripts/plugins can write to SQLite to inject data/events/broadcasts without changing the MCP tool surface.
- **Adaptive online roster**: built-in heartbeats maintain who's online; `send("all", ...)` targets the current online snapshot.
- **Readable outputs**: timestamps + grouping + batching keep responses compact.

### Runtime model (no always-on server)
Bridge MCP is **not** a resident background service.
It runs only when your Claude client starts the MCP server. When Claude exits, it stops.
Messages remain in SQLite, so nothing is lost between sessions.

### Requirements
- Python 3.x
- `pip install mcp`
- An MCP-capable client (Claude Code / Claude Desktop / etc.)

### Platform notes
Tested primarily on **Windows**.
On macOS/Linux, it should work with minor path adjustments (edit `PREFERRED_ROOT` / `FALLBACK_ROOT` in `bridge.py`).

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

## Design &amp; performance (practical)

### Design logic
- **Local-first, zero infra**: one MCP server + one SQLite file (durable message bus).
- **DB-as-API**: external scripts *can* write into the same SQLite DB to produce messages/broadcasts without adding new MCP tools.
- **Stable 3-tool surface**: fewer tool mistakes, easier prompting and long-term usage.
- **Non-daemon**: this is not a background service. It runs only when your Claude client starts it; when Claude exits, it stops. Messages remain in SQLite.

### Token cost (what "low token" means here)
Tool calls still use tokens for inputs/outputs, but the **protocol surface is tiny (3 tools)** and the outputs are stable/compact.
In practice this reduces "tool-selection overhead" and prompt bloat compared to larger tool suites.

### Latency expectations (default config in `bridge.py`)
These are derived from the default constants:
- **Cancel latency**: &le; `RECV_TICK` (default `0.25s`) — how fast `recv()` reacts to a new command.
- **Message delivery while listening**: typically &le; `RECV_DB_POLL_EVERY + RECV_TICK` (default `2.0s + 0.25s &asymp; 2.25s`).
- **Maintenance**: every `RECV_MAINT_EVERY` (default `10s`) for heartbeat/cleanup.
- **Batching**: long outputs are split by `MAX_BATCH_SIZE` (default `5000` chars).

Real-world latency depends on disk speed, number of agents, and your client's concurrency model.

### Tuning knobs (safe to tweak)
Edit these constants in `bridge.py`:
- `RECV_TICK` (cancel responsiveness)
- `RECV_DB_POLL_EVERY` (receive latency vs DB load)
- `RECV_MAINT_EVERY` (heartbeat/cleanup frequency)
- TTLs: `HEARTBEAT_TTL`, `BROADCAST_TTL`, `DIRECT_MSG_TTL`

### Debugging checklist
- **Confirm DB path**: see "Database path" section above; check the file exists.
- **Logs**: Bridge prints `[BRIDGE] ...` logs to stderr. Run `python bridge.py` manually to see startup/log output when debugging.
- **If you see `DB Busy/Locked`**:
  - close any SQLite viewer holding the file,
  - avoid placing the DB under aggressive sync/scan folders (e.g., cloud sync),
  - reduce simultaneous writers (many agents spamming `send`),
  - keep `busy_timeout` enabled (already set in code).

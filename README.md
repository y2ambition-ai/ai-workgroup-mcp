# Bridge MCP v4 — AI Workgroup Chat (Local-first)

A tiny MCP server that turns a single SQLite file into a durable message bus for multiple agents/terminals.

**Tools (only 3):**
- `get_status()` — show your ID + online peers
- `send(to, content)` — DM / multicast / `all` broadcast to online peers
- `recv(wait_seconds=86400)` — receive messages (virtual blocking)

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
- **Durable &amp; stable by design**: messages persist in SQLite; non-daemon runtime; DB lock contention is mitigated (busy_timeout, WAL, short write windows).
- **Highly extensible (DB-as-API)**: external scripts/plugins can write to SQLite to inject data/events/broadcasts without changing the MCP tool surface.
- **Adaptive online roster**: built-in heartbeats maintain who's online; `send("all", ...)` targets the current online snapshot.
- **Readable outputs**: timestamps + grouping + batching keep responses compact.

*Note:* "Real-time" here is **near-real-time** (poll-based). Actual latency depends on polling config and environment.

### Semantic-adaptive API (concept)
Bridge MCP is the lightweight workgroup bus. For "semantic routing" (sending the right task to the right agent),
build a small external Python producer that reads external data, decides recipients, and writes messages into SQLite (broadcast/DM).

## 3-terminal onboarding (recommended)

1) Install:
```bash
pip install mcp
```

2) Open 3 terminals / 3 agents and register Bridge MCP in each (recommended global install):
```bash
claude mcp add bridge --scope=user "python" "C:/ccbridge/bridge.py"
```

3) In Agent-1 and Agent-2, tell them:
&gt; "Stay in continuous listening mode. If you receive any message, reply immediately, then return to listening."

Then run:
```bash
recv(86400)
```

4) In Agent-3, run a broadcast test:
```bash
send("all", "Test: everyone reply with your ID and a one-line status, then go back to listening.")
recv(30)
```
If nobody replies, call `recv(30)` again (some clients are polling-based).

5) Manager workflow test (recommended):

**Run `get_status()` first and use the actual worker IDs shown there.**

From Agent-3 (as manager), DM each worker:
```bash
send("<WORKER_1_ID>", "Reply with a one-line status to the manager, then keep listening.")
send("<WORKER_2_ID>", "Reply with a one-line status to the manager, then keep listening.")
```
Collect replies via `recv(60)`.

If all checks pass — congrats, your AI team is born.

## Recommended install scope

Because Bridge MCP is extremely lightweight (3 tools, compact outputs), it's typically best to register it globally:
```bash
claude mcp add bridge --scope=user "python" "C:/ccbridge/bridge.py"
```
If you prefer per-project registration, follow your Claude client's MCP configuration conventions.

## No always-on server (non-daemon)

Bridge MCP is not a resident background service.
It runs only when your Claude client starts the MCP server. When Claude exits, it stops.
Messages remain in SQLite, so nothing is lost between sessions.

## Message semantics

Direct messages are "consume-on-read": once received via `recv()`, they are deleted.

Broadcast uses a cursor (state table) to avoid repeats.

## Database path

Default: `C:\mcp_msg_pool\bridge_v4.db` (Windows)

Fallback (if not writable): `C:\Users\Public\mcp_msg_pool\bridge_v4.db`

Change by editing `PREFERRED_ROOT` / `FALLBACK_ROOT` in `bridge.py`.

## Mixed teams (in theory)

In theory, any MCP-capable client can join this chat system.
I primarily tested on Claude Code/Claude Desktop; you can try building a mixed team (Claude + GPT + Gemini, etc.) if your clients support MCP.

## Token cost (practical note)

Tool calls still use input/output tokens, but the tool surface is tiny and outputs are compact.
This feels close to agents writing/reading a local TXT file — but with durable, queryable, long-term context.
Parallel work doesn't magically reduce model tokens, but it can reduce human iteration cost by enabling faster feedback and easier postmortems.

## Scaling note

Bridge MCP keeps the **communication layer lightweight**: adding more agents doesn't inherently "block" messaging.
In practice, scaling is mostly limited by:
- **your model token budget** (more agents = more parallel outputs),
- **your machine resources** (CPU/disk),
- and **the storage backend** (SQLite is great for small/medium local teams).

If you want a much larger team or heavier concurrency, consider upgrading the backend beyond SQLite
(e.g., Postgres/Redis/message queue) and tune polling parameters (`RECV_DB_POLL_EVERY`, `RECV_TICK`).

## Requirements

- Python 3.x
- `pip install mcp`
- An MCP-capable client (Claude Code / Claude Desktop / etc.)

## Platform notes

Tested primarily on Windows.
On macOS/Linux, it should work with minor path adjustments (edit `PREFERRED_ROOT` / `FALLBACK_ROOT`).

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
- **Cancel latency**: &amp;le; `RECV_TICK` (default `0.25s`) — how fast `recv()` reacts to a new command.
- **Message delivery while listening**: typically &amp;le; `RECV_DB_POLL_EVERY + RECV_TICK` (default `2.0s + 0.25s &amp;asymp; 2.25s`).
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

---

**More examples:** [PLAYBOOK.md](PLAYBOOK.md) / [PLAYBOOK.zh-CN.md](PLAYBOOK.zh-CN.md) *(now with Recipe 10: Rapid-fire workflow / 口喷工作流)*

*For team workflows, copy [PROMPT_GLOBAL.md](PROMPT_GLOBAL.md) into your global prompt / CLAUDE.md.*

## Contact / Maintainer note

**Contact:** WeChat `vvvykvvv` | Email `84927052@qq.com`

**Maintainer note:** I'm not a programmer; this repo is built for my personal workflow and most changes are done with AI assistance.  
**Please use GitHub Issues/Discussions for feedback** — I will ask AI to review and summarize periodically.  
Contact info above is for urgent matters only (to reduce spam).

---

*Powered by Bridge MCP (by vvvykvvv)*
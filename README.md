# AI Workgroup Chat (MCP) — Bridge MCP

A **local-first** SQLite message bus for multi-agent collaboration.
**Only 3 tools:** `get_status()` / `send(to, content)` / `recv(wait_seconds)`
Zero extra infra (no Redis / no web server). Works like an "AI workgroup chat".

**Engine:** v9_stable (background heartbeat + PID janitor + lease-based receive)
Technical details: `docs/ENGINE_v9_STABLE.md`

Quick links: `PLAYBOOK.md` | `PLAYBOOK.zh-CN.md` | `PROMPT_GLOBAL.md` | `examples/` | `docs/ENGINE_v9_STABLE.md`

---

## Install

Python 3.x required.

```bash
pip install mcp
claude mcp add bridge --scope=user "python" "C:/ccbridge/bridge.py"
```

Claude Desktop users can also configure MCP via your client config (see `docs/ENGINE_v9_STABLE.md` for the JSON example).

---

## 3-terminal onboarding (recommended)

Open 3 terminals / 3 agents and register Bridge MCP in each.

**In Agent-1 and Agent-2:** tell them "stay listening; reply immediately; then return to listening", then run:

```bash
recv(86400)  # or any duration you like
```

**In Agent-3 (manager):**

1. run `get_status()` and copy the real worker IDs
2. **broadcast test:**
   ```bash
   send("all", "Test: reply with your ID + one-line status, then go back to listening.")
   recv(30)  # if nobody replies, call recv(30) again
   ```
3. **DM test** (use actual IDs from `get_status`):
   ```bash
   send("<WORKER_1_ID>", "Reply with a one-line status to the manager, then keep listening.")
   send("<WORKER_2_ID>", "Reply with a one-line status to the manager, then keep listening.")
   ```
   manager: `recv(60)`

Congrats — your AI team is born.

---

## Why this (advantages)

- **Workgroup-style (near-real-time), not mailbox-style:** agents on standby can respond quickly.
- **Ultra-light:** one MCP server + one SQLite file.
- **Low learning cost:** 3 stable tools; easy to prompt; low tool-selection overhead.
- **Highly extensible (DB-as-API):** external scripts can write messages into SQLite as producers/bots.
- **Adaptive online/offline:** background heartbeat + cleanup maintains a clean roster.

---

## Message semantics (v9_stable)

v9_stable uses a lease queue:

1. messages start `queued`
2. receiver leases them to `inflight` (short lease TTL)
3. on successful `recv`, messages are ACKed (deleted)
4. if a tool call is aborted/crashes before ACK, leases are released/expired and messages return to `queued` (not lost)

---

## Database path (default)

**Windows default:**

```
C:\mcp_msg_pool\bridge_v9_stable.db
```

**Fallback if not writable:**

```
C:\Users\Public\mcp_msg_pool\bridge_v9_stable.db
```

Change via `PREFERRED_ROOT` / `FALLBACK_ROOT` in `bridge.py`.

---

## Contact / maintainer note

**WeChat:** vvvykvvv
**Email:** 84927052@qq.com

I'm not a professional programmer and I don't really use GitHub manually — most updates are done with AI tools.
Please open Issues/Discussions for bugs/ideas; I'll ask AI to read and summarize.

**Attribution (if you build on top):** Powered by Bridge MCP (by vvvykvvv)

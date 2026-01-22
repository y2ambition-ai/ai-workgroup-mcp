# Bridge MCP Playbook — "AI Workgroup" Recipes

This playbook contains practical, repeatable patterns to run a multi-agent "AI workgroup" with Bridge MCP.
All patterns assume you already registered the MCP server globally:

```bash
pip install mcp
claude mcp add bridge --scope=user "python" "C:/ccbridge/bridge.py"
```

## Before you start: 3 simple rules
1. **One manager, many workers** is the default. You talk to the manager; the manager talks to everyone else.
2. **Workers stay on standby**: they run `recv(86400)`, reply fast, and return to listening.
3. **Keep outputs short + structured**: one-line status updates, numbered bullet results, and short follow-ups.

**Note:** Agent IDs are randomly assigned. Always run `get_status()` first and use the actual IDs shown there.

## Recipe 1 — CEO + Workers (classic "delegate & report")
**Goal:** manager assigns tasks; workers reply; manager summarizes.

### Setup
- **Terminal A:** CEO / Manager
- **Terminal B:** Worker-1
- **Terminal C:** Worker-2

### Worker instruction (copy/paste)
Tell Worker-1 and Worker-2:
> "Stay in continuous listening mode. If you receive a message, reply immediately with a concise answer, then return to listening."

Then on each worker run:
```bash
recv(86400)
```

### CEO flow
1) Check IDs:
```bash
get_status()
```

2) Assign tasks (DM or broadcast):
```bash
send("<WORKER_1_ID>", "Task: Research option A. Reply: steps + tradeoffs + recommendation. Then keep listening.")
send("<WORKER_2_ID>", "Task: Research option B. Reply: risks + cost + timeline. Then keep listening.")
```

3) Collect reports:
```bash
recv(120)
```

4) Summarize & decide: CEO writes a final summary and next steps.

**Expected outcome:** workers answer quickly; CEO stays in control.

## Recipe 2 — Departments (persistent identity per folder)
**Goal:** simulate a "company" where each department has long-term identity and memory.

### Pattern
Each agent runs inside its own folder:
- `company/CEO/`
- `company/Research/`
- `company/Engineering/`
- `company/QA/`

Each folder has its own `CLAUDE.md` (or memory file) → persistent identity.

### Minimal workflow
1. CEO assigns tasks via DM/broadcast.
2. Departments report back.
3. QA audits outputs and asks for fixes.

**Tip:** treat folders as "employees", not as tasks.

## Recipe 3 — Help channel (DM for work, all for help)
**Goal:** reduce your attention cost.

### Rule
Workers DM each other to execute.
If stuck, they broadcast once:
```bash
send("all", "Need help on X. Anyone has a plan? Reply with 3 steps.")
```

### Manager behavior
Manager only intervenes when all requests arrive.

## Recipe 4 — Research sprint (parallel answers → best synthesis)
**Goal:** get multiple independent solutions fast.

### CEO prompt
```bash
send("all", "Sprint: propose 3 approaches. Reply with: (1) plan, (2) risks, (3) quick win. Then back to listening.")
recv(90)
```

### Follow-up
CEO picks one approach and asks 1 worker to deep-dive, 1 worker to critique, 1 worker to produce a checklist.

## Recipe 5 — Auditor agent (postmortem & correctness)
**Goal:** keep quality high over long iterations.

### Roles
- Manager (CEO)
- Worker(s)
- Auditor (QA)

### Auditor instructions
Auditor always asks: assumptions, edge cases, failure modes, minimal reproduction, acceptance checks.

### CEO flow
```bash
send("<QA_ID>", "Audit Worker-1 output. Provide: 5 risks + 5 fixes + final verdict.")
recv(120)
```

## Recipe 6 — "AI manages AI" in one message
**Goal:** one broadcast to coordinate many IDs quickly.

### Example command:
```bash
send("all", "<001-003> do research; <004-006> draft; <007-009> standby. Reply only with your ID + 1-line status.")
```

**Note:** It's prompt-driven. Bridge MCP provides the communication layer.

## Recipe 7 — Mixed CLI team (theory)
**Goal:** build a mixed team (Claude + other MCP-capable clients).

### Reality check
This is theory unless you confirm other clients support MCP.
If they do, treat them like another employee: give them a role folder + role prompt + standby loop.

## Recipe 8 — Long tasks (human cost goes down)
Key idea: parallel work doesn't magically reduce model tokens, but reduces your human iteration cost:
- faster first answers
- easier follow-ups
- better postmortems ("why did you do it this way?")

## Recipe 9 — "Coordination game" (fun stress-test)
**Goal:** test multi-agent turn-taking, negotiation, and coordination.

Example: run a cooperative "tile game" simulation (no money/gambling).
- One agent is host/referee
- Others propose moves
- Host enforces rules and maintains state

This is a fun way to test concurrency and message clarity.

## Troubleshooting (common)
- **If nobody replies after broadcast:** call `recv(30)` again (polling near-real-time).
- **If DB Busy/Locked:** close DB viewers, avoid cloud-sync folders, reduce simultaneous writers.
- **If your client can't "interrupt recv":** use short polling: `recv(5)` repeatedly.

---

*Powered by Bridge MCP (by vvvykvvv)*

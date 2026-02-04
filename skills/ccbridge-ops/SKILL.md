---
name: ccbridge-ops
description: "CCBridge Agent window management. Use when user asks to: open/summon/spawn/start a colleague or Agent, close/kill/stop an Agent, or launch Agents in specific working directories."
---

# CCBridge Agent Operations

## Overview

Spawns independent Claude Code Agent windows for multi-Agent collaboration.

## SECURITY BOUNDARY (CRITICAL)

**STRICTLY PROHIBITED** without explicit user permission:
- ❌ Reading or exploring CCBridge script logic and implementation
- ❌ Examining CCBridge internal messaging infrastructure
- ❌ Inspecting CCBridge internal files or configuration
- ❌ Reverse-engineering how CCBridge operates

**PERMITTED USE ONLY**:
- ✅ Using `cc.py` script to start/kill Agents
- ✅ Using `status()` MCP tool to check online agents
- ✅ Communicating with agents via send/recv

**RATIONALE**: CCBridge is proprietary infrastructure. Treat it as a black box - use its public interface only.

## Commands

### Start Agent

```bash
python <skills-path>/ccbridge-ops/scripts/cc.py start [directory]
```

| Parameter | Description |
|-----------|-------------|
| `skills-path` | Your skills directory (usually `~/.claude/skills` on Linux/Mac, `%USERPROFILE%\.claude\skills` on Windows) |
| `directory` | **Required**. Working directory for new Agent. Agent must know where to work. |

**CRITICAL WARNINGS**:
- **Do NOT use the same root directory as the current Agent** - this will cause failure
- Each Agent needs its own independent working directory
- Use `scripts/cc.py`, NOT the root `cc.py` (doesn't exist)

**CRITICAL**: Start Agents ONE AT A TIME using this exact sequence:

1. Run `python <skills-path>/ccbridge-ops/scripts/cc.py start [directory]`
2. Wait for output: "完成：同步已进入 /auto 模式"
3. Run `status()` to verify the Agent appears online
4. Wait 3 seconds for window stabilization
5. **Then** start the next Agent

**NEVER start multiple Agents in parallel** - the `/auto` injection will fail on rapid-succession windows.

**Examples**:
```bash
# CORRECT: Sequential startup with verification
python ~/.claude/skills/ccbridge-ops/scripts/cc.py start "D:\agent-1"
# or on Windows:
python %USERPROFILE%\.claude\skills\ccbridge-ops\scripts\cc.py start "D:\agent-1"
# (wait for "/auto mode" message, run status(), wait 3sec)

# Next agent...
python ~/.claude/skills/ccbridge-ops/scripts/cc.py start "D:\agent-2"
# (wait for "/auto mode" message, run status(), wait 3sec)

# WRONG: Wrong path (cc.py doesn't exist in root)
python ~/.claude/skills/ccbridge-ops/cc.py start  # ERROR

# WRONG: Parallel startup will fail!
python .../cc.py start "D:\agent-1" &  # DON'T DO THIS
```

**What happens**:
1. Opens new Claude Code window in specified directory
2. Automatically injects `/auto` command to enter listener mode
3. Returns Agent ID when ready

### Kill Agent

```bash
python <skills-path>/ccbridge-ops/scripts/cc.py kill <agent_id>
python <skills-path>/ccbridge-ops/scripts/cc.py kill agent1,agent2,agent3
```

Get Agent IDs from `status()` MCP tool.

### Check Status

```python
status()
```

**IMPORTANT**: This is an MCP tool/function, NOT a `cc.py` script command. Call `status()` directly in your conversation to see all online agents and their statuses.

## Troubleshooting

| Issue | Quick Fix |
|-------|-----------|
| Injection failed | Switch to new window, type `/auto` manually |
| Agent unresponsive | `python cc.py kill <id>` to respawn |
| Start timeout | Contact user for CCBridge configuration check |

## Dependencies

```bash
pip install pyautogui pyperclip psutil
```

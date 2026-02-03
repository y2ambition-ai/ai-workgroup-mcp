import os
import sys
import time
import json
import uuid
import random
import shutil
import threading
import atexit
from pathlib import Path
from itertools import groupby
from mcp.server.fastmcp import FastMCP

# =========================================================
# RootBridge - v36 (Janitor Edition)
# Change Log:
# - Refactored Leader election to "Janitor" pattern (Maintenance role).
# - "Business Leader" is now purely a naming convention for humans/LLMs.
# - Fixed Race Condition in rename() using threading.Lock.
# - Fixed Deadlock Alert logic (No broadcast fallback, Human-in-the-loop).
# - Improved deadlock warning message for clarity.
# =========================================================

try:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
except: pass

mcp = FastMCP("RootBridge")

# --- Configuration ---
if sys.platform == "win32":
    # Windows path convention
    POOL_ROOT = Path("C:/mcp_msg_pool")
else:
    POOL_ROOT = Path.home() / ".mcp_msg_pool"

try:
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
except Exception as e:
    print(f"[FATAL] Failed to create pool root: {e}", file=sys.stderr)

# --- Constants ---
HEARTBEAT_TTL = 12.0    # Offline threshold
ZOMBIE_TTL    = 3600.0  # Zombie cleanup threshold
JANITOR_TTL   = 10.0    # Janitor rotation time
DEADLOCK_WARNING_COOLDOWN = 60.0  # Cooldown between deadlock warnings

# --- Identity & State ---
SESSION_ID = None
MY_FOLDER = None
MY_INBOX = None
CURRENT_STATE = "NORMAL"
WAITING_SINCE = None

# Thread lock: Protect MY_FOLDER and SESSION_ID during atomic rename
IDENTITY_LOCK = threading.Lock()

# --- Helpers ---

def _atomic_write(target: Path, content: dict) -> bool:
    """Atomic write to prevent file locking issues."""
    tmp = None
    try:
        tmp = target.parent / f"{target.stem}_{uuid.uuid4().hex}.tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(content, f, ensure_ascii=False)
        os.replace(tmp, target)
        return True
    except Exception:
        if tmp and tmp.exists():
            try: tmp.unlink()
            except: pass
        return False

def _update_state(state: str):
    """Update heartbeat with thread safety."""
    global CURRENT_STATE, WAITING_SINCE
    now = time.time()

    # Update in-memory state
    CURRENT_STATE = state
    if state == "WAITING" and WAITING_SINCE is None:
        WAITING_SINCE = now
    elif state != "WAITING":
        WAITING_SINCE = None

    # Update file state (locked to prevent writes during rename)
    with IDENTITY_LOCK:
        if not MY_FOLDER: return
        meta = {
            "id": SESSION_ID,
            "pid": os.getpid(),
            "ts": now,
            "state": state,
            "waiting_since": WAITING_SINCE,
            "cwd": str(Path.cwd())  # Add current working directory
        }
        _atomic_write(MY_FOLDER / "heartbeat.json", meta)

# --- Identity Management ---

def setup_session(name):
    global SESSION_ID, MY_FOLDER, MY_INBOX
    safe_name = "".join([c for c in name if c.isalnum() or c in ('-', '_')])
    if not safe_name: safe_name = f"agent_{random.randint(100,999)}"

    with IDENTITY_LOCK:
        SESSION_ID = safe_name
        MY_FOLDER = POOL_ROOT / safe_name
        MY_INBOX = MY_FOLDER / "inbox"

        try:
            MY_FOLDER.mkdir(parents=True, exist_ok=True)
            MY_INBOX.mkdir(exist_ok=True)
        except OSError as e:
            print(f"[WARN] Failed to create session folders: {e}", file=sys.stderr)

    _update_state("NORMAL")

def get_id():
    global SESSION_ID
    if SESSION_ID: return SESSION_ID

    while True:
        cid = f"agent_{random.randint(100, 999)}"
        target = POOL_ROOT / cid
        try:
            target.mkdir(exist_ok=False)
            setup_session(cid)
            return SESSION_ID
        except FileExistsError:
            time.sleep(0.1)
            continue

# =========================================================
# Janitor Loop (The System Maintainer)
# =========================================================

def janitor_loop():
    """
    Janitor Loop:
    1. Elect Janitor (maintains system lock janitor.json)
    2. Clean expired dead files (Zombies)
    3. Detect Deadlock and alert Business Managers only
    """
    last_deadlock_warning = 0

    while True:
        try:
            get_id() # Ensure we exist
            _update_state(CURRENT_STATE) # Heartbeat

            # --- 1. Janitor Election ---
            janitor_file = POOL_ROOT / "janitor.json"
            now = time.time()
            is_janitor = False

            # Try to elect or renew
            if janitor_file.exists():
                try:
                    d = json.loads(janitor_file.read_text(encoding='utf-8'))
                    if d['pid'] == os.getpid() or (now - d['ts'] > JANITOR_TTL):
                        is_janitor = True
                except: is_janitor = True
            else:
                is_janitor = True

            if is_janitor:
                # Clock in
                _atomic_write(janitor_file, {"pid": os.getpid(), "ts": now})

                # --- 2. Monitor & Clean ---
                all_waiting = True
                online_active_count = 0
                business_managers = []  # List of "Business Leaders"

                for p in POOL_ROOT.iterdir():
                    if not p.is_dir(): continue
                    # Ignore system files
                    if p.name in ["janitor.json", "leader.json"]: continue

                    try:
                        hb_file = p / "heartbeat.json"
                        if hb_file.exists():
                            hb = json.loads(hb_file.read_text(encoding='utf-8'))

                            # [Clean] Remove Zombies
                            if now - hb['ts'] > ZOMBIE_TTL:
                                shutil.rmtree(p)
                                continue

                            # [Check] Is Alive?
                            if now - hb['ts'] <= HEARTBEAT_TTL:
                                online_active_count += 1

                                # If anyone is working, no deadlock
                                if hb.get('state') != "WAITING":
                                    all_waiting = False

                                # Identify Business Managers (Naming convention)
                                if "leader" in p.name.lower():
                                    business_managers.append(p)
                        else:
                            # Cleanup empty shells
                            shutil.rmtree(p)
                    except (OSError, json.JSONDecodeError, PermissionError):
                        pass

                # --- 3. Deadlock Detection ---
                # Trigger: Everyone waiting AND >= 1 agent online
                if all_waiting and online_active_count >= 1:
                    if now - last_deadlock_warning > DEADLOCK_WARNING_COOLDOWN:

                        msg_text = (
                            "【系统提示】全员 recv() 等待中（无人在执行任务）\n"
                            "→ 可能原因：阶段性工作已完成\n"
                            "→ 建议：检查进度并推进下一阶段/派发新任务"
                        )

                        # [Logic] Only alert if Managers exist. No fallback broadcast.
                        if business_managers:
                            msg_payload = {
                                "from": "SYSTEM_JANITOR",
                                "msg": msg_text,
                                "ts": now
                            }
                            fname = f"{now}_sys_deadlock.json"

                            count = 0
                            for target_dir in business_managers:
                                try:
                                    inbox = target_dir / "inbox"
                                    inbox.mkdir(exist_ok=True)
                                    _atomic_write(inbox / fname, msg_payload)
                                    count += 1
                                except: pass

                            if count > 0:
                                last_deadlock_warning = now

                        else:
                            # Human-in-the-loop fallback: Log to console only.
                            if now - last_deadlock_warning > 10.0:
                                print(f"[JANITOR] Deadlock detected. No Business Manager found. Waiting for human intervention...", file=sys.stderr)
                                last_deadlock_warning = now # Prevent log spamming

        except Exception as e:
            pass

        time.sleep(5.0) # Maintenance cycle

# Start the Janitor thread
threading.Thread(target=janitor_loop, daemon=True).start()

# Cleanup on exit
def _cleanup():
    with IDENTITY_LOCK:
        if MY_FOLDER:
            (MY_FOLDER / "heartbeat.json").unlink(missing_ok=True)
atexit.register(_cleanup)

# --- MCP Tools ---

@mcp.tool()
def status() -> str:
    """
    Get current network topology.
    Returns:
    - 🟢 = Agent Active.
    - ⏳ = Agent Waiting.
    - * = SELF.
    - 👑 = Business Manager (Name contains 'leader').
    - 显示每个 Agent 的工作目录。
    """
    get_id()
    lines = []
    now = time.time()

    for p in POOL_ROOT.iterdir():
        if not p.is_dir(): continue
        if p.name in ["janitor.json", "leader.json"]: continue

        hb = p / "heartbeat.json"
        if hb.exists():
            try:
                d = json.loads(hb.read_text(encoding='utf-8'))
                if now - d['ts'] > HEARTBEAT_TTL: continue

                name = p.name
                state = d.get('state', 'NORMAL')
                cwd = d.get('cwd', 'unknown')  # Get working directory

                is_self = " *" if str(name) == str(SESSION_ID) else ""
                # Visual indicator only - does not affect system logic
                is_manager = " 👑" if "leader" in name.lower() else ""
                icon = "⏳" if state == "WAITING" else "🟢"

                lines.append(f"{icon} {name}{is_self}{is_manager} | {cwd}")
            except: pass

    return "\n".join(lines) if lines else "None"

@mcp.tool()
def rename(new_name: str) -> str:
    """
    Change current Agent ID.
    """
    global SESSION_ID, MY_FOLDER, MY_INBOX

    # 1. Validation
    old = get_id()
    safe = "".join([c for c in new_name if c.isalnum() or c in ('-', '_')])
    if not safe: return "Invalid"

    target = POOL_ROOT / safe

    # 2. Check Target Availability
    if target.exists():
        if safe in ["janitor", "leader"]: # Reserved names check
             # Try to clean if it's a legacy folder
            try: shutil.rmtree(target)
            except: return "Name reserved/taken"
        else:
            hb_file = target / "heartbeat.json"
            if hb_file.exists():
                try:
                    d = json.loads(hb_file.read_text(encoding='utf-8'))
                    if time.time() - d['ts'] < HEARTBEAT_TTL:
                        return "Name taken" # Active agent exists
                    shutil.rmtree(target) # Dead agent, remove it
                except: return "Name taken"
            else:
                try: shutil.rmtree(target)
                except: pass

    # 3. Critical Section (Thread Safe Rename)
    try:
        with IDENTITY_LOCK:
            os.rename(MY_FOLDER, target)
            SESSION_ID = safe
            MY_FOLDER = target
            MY_INBOX = MY_FOLDER / "inbox"

        _update_state("NORMAL") # Immediately update heartbeat at new location
        return "OK"
    except Exception as e:
        print(f"[ERROR] Rename failed: {e}", file=sys.stderr)
        return "Fail"

@mcp.tool()
def send(to: str, msg: str) -> str:
    """
    Send message to target(s).
    to: "agent_id", comma-list, or "all".
    """
    sender = get_id()
    targets = []
    now = time.time()

    if to == "all":
        for p in POOL_ROOT.iterdir():
            if not p.is_dir() or p.name == sender: continue
            if p.name in ["janitor.json", "leader.json"]: continue

            hb = p / "heartbeat.json"
            if hb.exists():
                try:
                    d = json.loads(hb.read_text(encoding='utf-8'))
                    if now - d['ts'] <= HEARTBEAT_TTL:
                        targets.append(p)
                except: pass
    else:
        for r in to.split(","):
            t = POOL_ROOT / r.strip()
            if t.exists(): targets.append(t)

    if not targets: return "No target"

    payload = {"from": sender, "msg": msg, "ts": now}
    # Unique filename for atomic delivery
    fname = f"{now}_{uuid.uuid4().hex}.json"

    success = 0
    for folder in targets:
        try:
            inbox = folder / "inbox"
            inbox.mkdir(exist_ok=True)
            if _atomic_write(inbox / fname, payload):
                success += 1
        except: pass

    return "OK" if success > 0 else "Fail"

@mcp.tool()
def recv(wait: int = 86400) -> str:
    """
    Blocking wait for messages.
    """
    get_id()
    start = time.time()
    _update_state("WAITING")

    try:
        while True:
            # Check for files
            files = sorted(MY_INBOX.glob("*.json")) if MY_INBOX else []
            valid_msgs = []
            files_to_delete = []

            if files:
                for f in files:
                    try:
                        text = f.read_text(encoding='utf-8')
                        data = json.loads(text)
                        valid_msgs.append(data)
                        files_to_delete.append(f)
                    except: pass

                # Small buffer to ensure file locks are released by OS
                if valid_msgs:
                    time.sleep(0.5)

                for f in files_to_delete:
                    try: f.unlink()
                    except: pass

                _update_state("NORMAL")

                # Sort by Sender then Time (Original Logic kept)
                # Note: This groups messages by sender.
                valid_msgs.sort(key=lambda x: (x['from'], x['ts']))

                out = []
                for name, group in groupby(valid_msgs, key=lambda x: x['from']):
                    chunk = list(group)
                    if len(chunk) == 1:
                        ts = time.strftime("%H:%M:%S", time.localtime(chunk[0]['ts']))
                        out.append(f"[{name} {ts}]: {chunk[0]['msg']}")
                    else:
                        out.append(f"[{name} x{len(chunk)}]:")
                        for m in chunk:
                            ts = time.strftime("%H:%M:%S", time.localtime(m['ts']))
                            out.append(f" - [{ts}] {m['msg']}")
                return "\n".join(out)

            if time.time() - start > wait:
                _update_state("NORMAL")
                return "Timeout"

            time.sleep(0.5)

    except Exception as e:
        print(f"[ERROR] Recv error: {e}", file=sys.stderr)
        _update_state("NORMAL")
        return "Error"

if __name__ == "__main__":
    print("[DEBUG] RootBridge v36 (Janitor) Starting...")
    get_id()
    print(f"[DEBUG] Started as {SESSION_ID}")
    mcp.run()

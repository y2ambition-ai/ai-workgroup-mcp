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
# RootBridge - v39 (Token Optimized)
# Change Log:
# - Docstrings: High-density, low-token, precise format descriptions.
# - Logic: ZOMBIE_TTL=300s, DEADLOCK_DELAY=20s.
# - Fixes: All previous Janitor/Recv fixes included.
# =========================================================

try:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
except: pass

mcp = FastMCP("RootBridge")

# --- Configuration ---
if sys.platform == "win32":
    POOL_ROOT = Path("C:/mcp_msg_pool")
else:
    POOL_ROOT = Path.home() / ".mcp_msg_pool"

try:
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
except Exception as e:
    print(f"[FATAL] Failed to create pool root: {e}", file=sys.stderr)

# --- Constants ---
HEARTBEAT_TTL = 12.0    # Offline threshold
ZOMBIE_TTL    = 300.0   # 5 mins cleanup
JANITOR_TTL   = 10.0    # Janitor rotation
DEADLOCK_WARNING_COOLDOWN = 60.0
DEADLOCK_TRIGGER_DELAY = 20.0 

# --- Identity & State ---
SESSION_ID = None
MY_FOLDER = None
MY_INBOX = None
CURRENT_STATE = "NORMAL"
WAITING_SINCE = None
IDENTITY_LOCK = threading.Lock()

# --- Helpers ---

def _atomic_write(target: Path, content: dict) -> bool:
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
    global CURRENT_STATE, WAITING_SINCE
    now = time.time()
    CURRENT_STATE = state
    if state == "WAITING" and WAITING_SINCE is None:
        WAITING_SINCE = now
    elif state != "WAITING":
        WAITING_SINCE = None

    with IDENTITY_LOCK:
        if not MY_FOLDER: return
        meta = {
            "id": SESSION_ID,
            "pid": os.getpid(),
            "ts": now,
            "state": state,
            "waiting_since": WAITING_SINCE,
            "cwd": str(Path.cwd())
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
        except OSError: pass
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
# Janitor Loop
# =========================================================

def janitor_loop():
    last_deadlock_warning = 0
    all_waiting_since = None

    while True:
        try:
            get_id()
            _update_state(CURRENT_STATE)

            # 1. Election
            janitor_file = POOL_ROOT / "janitor.json"
            now = time.time()
            is_janitor = False
            if janitor_file.exists():
                try:
                    d = json.loads(janitor_file.read_text(encoding='utf-8'))
                    if d['pid'] == os.getpid() or (now - d['ts'] > JANITOR_TTL):
                        is_janitor = True
                except: is_janitor = True
            else: is_janitor = True

            if is_janitor:
                _atomic_write(janitor_file, {"pid": os.getpid(), "ts": now})

                # 2. Clean & Monitor
                all_waiting = True
                online_active_count = 0
                business_managers = []

                for p in POOL_ROOT.iterdir():
                    if not p.is_dir() or p.name in ["janitor.json", "leader.json"]: continue
                    try:
                        hb_file = p / "heartbeat.json"
                        if hb_file.exists():
                            hb = json.loads(hb_file.read_text(encoding='utf-8'))
                            # Zombie Clean (5 mins)
                            if now - hb['ts'] > ZOMBIE_TTL:
                                shutil.rmtree(p)
                                continue
                            # Active Check
                            if now - hb['ts'] <= HEARTBEAT_TTL:
                                online_active_count += 1
                                if hb.get('state') != "WAITING": all_waiting = False
                                if "leader" in p.name.lower(): business_managers.append(p)
                        else: shutil.rmtree(p)
                    except: pass

                # 3. Deadlock
                if all_waiting:
                    if all_waiting_since is None: all_waiting_since = now
                else: all_waiting_since = None

                if all_waiting and online_active_count >= 1 and all_waiting_since:
                    if (now - all_waiting_since >= DEADLOCK_TRIGGER_DELAY and
                        now - last_deadlock_warning > DEADLOCK_WARNING_COOLDOWN):
                        
                        msg_text = "【系统提示】全员 recv() 等待中"
                        if business_managers:
                            payload = {"from": "SYSTEM_JANITOR", "msg": msg_text, "ts": now}
                            fname = f"{now}_sys_deadlock.json"
                            for target in business_managers:
                                try:
                                    inbox = target / "inbox"
                                    inbox.mkdir(exist_ok=True)
                                    _atomic_write(inbox / fname, payload)
                                except: pass
                        else:
                            print(f"[JANITOR] Deadlock. No Manager found.", file=sys.stderr)
                        last_deadlock_warning = now

        except: pass
        time.sleep(5.0)

threading.Thread(target=janitor_loop, daemon=True).start()

def _cleanup():
    with IDENTITY_LOCK:
        if MY_FOLDER: (MY_FOLDER / "heartbeat.json").unlink(missing_ok=True)
atexit.register(_cleanup)

# --- MCP Tools (Optimized) ---

@mcp.tool()
def status() -> str:
    """
    List online agents.
    Returns strings like: "🟢 agent_1 | /path/to/cwd"
    Legend: 🟢=Active, ⏳=Waiting, *=Self, 👑=Manager.
    """
    get_id()
    lines = []
    now = time.time()
    for p in POOL_ROOT.iterdir():
        if not p.is_dir() or p.name in ["janitor.json", "leader.json"]: continue
        hb = p / "heartbeat.json"
        if hb.exists():
            try:
                d = json.loads(hb.read_text(encoding='utf-8'))
                if now - d['ts'] > HEARTBEAT_TTL: continue
                name = p.name
                state = d.get('state', 'NORMAL')
                cwd = d.get('cwd', 'unknown')
                is_self = " *" if str(name) == str(SESSION_ID) else ""
                is_mgr = " 👑" if "leader" in name.lower() else ""
                icon = "⏳" if state == "WAITING" else "🟢"
                lines.append(f"{icon} {name}{is_self}{is_mgr} | {cwd}")
            except: pass
    return "\n".join(lines) if lines else "None"

@mcp.tool()
def rename(new_name: str) -> str:
    """
    Rename self.
    Args: new_name (alphanumeric, '-', '_').
    Returns: "OK" or "Fail/Name taken".
    """
    global SESSION_ID, MY_FOLDER, MY_INBOX
    old = get_id()
    safe = "".join([c for c in new_name if c.isalnum() or c in ('-', '_')])
    if not safe: return "Invalid"
    target = POOL_ROOT / safe
    if target.exists():
        if safe in ["janitor", "leader"]: 
            try: shutil.rmtree(target)
            except: return "Name reserved"
        else:
            hb = target / "heartbeat.json"
            if hb.exists():
                try:
                    if time.time() - json.loads(hb.read_text(encoding='utf-8'))['ts'] < HEARTBEAT_TTL:
                        return "Name taken"
                except: pass
            try: shutil.rmtree(target)
            except: pass

    try:
        with IDENTITY_LOCK:
            os.rename(MY_FOLDER, target)
            SESSION_ID = safe
            MY_FOLDER = target
            MY_INBOX = MY_FOLDER / "inbox"
        _update_state("NORMAL")
        return "OK"
    except: return "Fail"

@mcp.tool()
def send(to: str, msg: str) -> str:
    """
    Send message.
    Args: to ("name", "a,b", or "all"), msg (str).
    Returns: "OK" (if >=1 sent) or "Fail".
    """
    sender = get_id()
    targets = []
    now = time.time()
    if to == "all":
        for p in POOL_ROOT.iterdir():
            if not p.is_dir() or p.name == sender or p.name in ["janitor.json", "leader.json"]: continue
            hb = p / "heartbeat.json"
            if hb.exists():
                try:
                    if now - json.loads(hb.read_text(encoding='utf-8'))['ts'] <= HEARTBEAT_TTL:
                        targets.append(p)
                except: pass
    else:
        for r in to.split(","):
            t = POOL_ROOT / r.strip()
            if t.exists(): targets.append(t)

    if not targets: return "No target"
    payload = {"from": sender, "msg": msg, "ts": now}
    fname = f"{now}_{uuid.uuid4().hex}.json"
    success = 0
    for folder in targets:
        try:
            inbox = folder / "inbox"
            inbox.mkdir(exist_ok=True)
            if _atomic_write(inbox / fname, payload): success += 1
        except: pass
    return "OK" if success > 0 else "Fail"

@mcp.tool()
def recv(wait: int = 86400) -> str:
    """
    Blocks until msg received.
    Returns: String "[Sender HH:MM:SS]: Msg" or "Timeout".
    """
    get_id()
    start = time.time()
    _update_state("WAITING")
    try:
        while True:
            files = sorted(MY_INBOX.glob("*.json")) if MY_INBOX else []
            valid_msgs = []
            files_to_delete = []
            if files:
                for f in files:
                    try:
                        valid_msgs.append(json.loads(f.read_text(encoding='utf-8')))
                        files_to_delete.append(f)
                    except: pass
                if valid_msgs: time.sleep(0.5)
                for f in files_to_delete:
                    try: f.unlink()
                    except: pass
                _update_state("NORMAL")
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
                if not (list(MY_INBOX.glob("*.json")) if MY_INBOX else []):
                    _update_state("NORMAL")
                    return "Timeout"
            time.sleep(0.5)
    except:
        _update_state("NORMAL")
        return "Error"

if __name__ == "__main__":
    print("[DEBUG] RootBridge v39 (Optimized) Starting...")
    get_id()
    print(f"[DEBUG] Started as {SESSION_ID}")
    mcp.run()

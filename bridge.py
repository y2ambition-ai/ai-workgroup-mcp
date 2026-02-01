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
# RootBridge - v26 Clean Edition
#
# Changes:
# - Removed SESSION_ID_FILE (badge) mechanism
# - Fixed ID generation race condition with atomic mkdir
#
# Core:
# 1. Send: Writes and immediately verifies existence.
# 2. Recv: Reads -> Sleeps 1.5s -> Deletes. (Fixes race condition)
# 3. Status: Hides offline agents.
# =========================================================

try:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
except: pass

mcp = FastMCP("RootBridge")

if sys.platform == "win32":
    POOL_ROOT = Path("C:/mcp_msg_pool")
else:
    POOL_ROOT = Path.home() / ".mcp_msg_pool"

try:
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
except Exception as e:
    print(f"[FATAL] {e}", file=sys.stderr)

HEARTBEAT_TTL = 60.0    # 离线阈值
ZOMBIE_TTL    = 3600.0  # 清理阈值
LEADER_TTL    = 40.0

# --- Identity ---
SESSION_ID = None
MY_FOLDER = None
MY_INBOX = None
CURRENT_STATE = "NORMAL"

# --- Helpers ---

def _atomic_write(target: Path, content: dict) -> bool:
    """
    Step 1: Write Temp
    Step 2: Rename (Atomic)
    Step 3: Verify Existence (Physics Check)
    """
    try:
        tmp = target.with_suffix(".tmp")
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(content, f, ensure_ascii=False)
        
        os.replace(tmp, target)
        
        # [Constraint] 物理回读校验
        # 即使 Recv 秒删，只要在 rename 后存在过哪怕 1ms，OS 也会确认
        # 配合 Recv 的 1.5s 延迟删除，这里绝对安全
        if target.exists() and target.stat().st_size > 0:
            return True
        return False
    except:
        return False

def _update_state(state: str):
    global CURRENT_STATE
    CURRENT_STATE = state
    if not MY_FOLDER: return
    now = time.time()
    meta = {"id": SESSION_ID, "pid": os.getpid(), "ts": now, "state": state}
    _atomic_write(MY_FOLDER / "heartbeat.json", meta)

# --- Identity Logic ---

def setup_session(name):
    global SESSION_ID, MY_FOLDER, MY_INBOX
    safe_name = "".join([c for c in name if c.isalnum() or c in ('-', '_')])
    if not safe_name: safe_name = f"agent_{random.randint(100,999)}"
    
    SESSION_ID = safe_name
    MY_FOLDER = POOL_ROOT / safe_name
    MY_INBOX = MY_FOLDER / "inbox"
    
    try:
        MY_FOLDER.mkdir(parents=True, exist_ok=True)
        MY_INBOX.mkdir(exist_ok=True)
        _update_state("NORMAL")
    except: pass

def get_id():
    global SESSION_ID
    if SESSION_ID: return SESSION_ID

    while True:
        cid = f"agent_{random.randint(100, 999)}"
        target = POOL_ROOT / cid
        try:
            # 原子创建目录，避免竞态
            target.mkdir(exist_ok=False)
            setup_session(cid)
            return SESSION_ID
        except FileExistsError:
            time.sleep(0.1)
            continue

# --- Leader Loop ---

def leader_loop():
    while True:
        get_id()
        _update_state(CURRENT_STATE)
        
        leader_file = POOL_ROOT / "leader.json"
        now = time.time()
        is_leader = False
        try:
            if leader_file.exists():
                d = json.loads(leader_file.read_text(encoding='utf-8'))
                if d['pid'] == os.getpid() or (now - d['ts'] > LEADER_TTL):
                    is_leader = True
            else:
                is_leader = True
                
            if is_leader:
                _atomic_write(leader_file, {"pid": os.getpid(), "ts": now})
                # 慢清理：1小时
                for p in POOL_ROOT.iterdir():
                    if p.is_dir() and (p / "heartbeat.json").exists():
                        try:
                            hb = json.loads((p / "heartbeat.json").read_text(encoding='utf-8'))
                            if now - hb['ts'] > ZOMBIE_TTL:
                                shutil.rmtree(p)
                        except: pass
        except: pass
        time.sleep(5.0)

threading.Thread(target=leader_loop, daemon=True).start()
atexit.register(lambda: (MY_FOLDER / "heartbeat.json").unlink(missing_ok=True) if MY_FOLDER else None)

# --- MCP Tools ---

@mcp.tool()
def status() -> str:
    """List all ONLINE agents. 🟢=Normal, ⏳=Waiting for messages. * marks self."""
    get_id()
    lines = []
    now = time.time()
    
    for p in POOL_ROOT.iterdir():
        if not p.is_dir(): continue
        hb = p / "heartbeat.json"
        if hb.exists():
            try:
                d = json.loads(hb.read_text(encoding='utf-8'))
                age = now - d['ts']
                
                # [Constraint 1] 彻底隐藏离线者
                if age > HEARTBEAT_TTL: continue
                
                name = p.name
                state = d.get('state', 'NORMAL')
                
                mark = " *" if str(name) == str(SESSION_ID) else ""
                icon = "⏳" if state == "WAITING" else "🟢"
                lines.append(f"{icon} {name}{mark}")
            except: pass
            
    return "\n".join(lines) if lines else "None"

@mcp.tool()
def rename(new_name: str) -> str:
    """Change my ID. Only alphanumeric, '-', '_' allowed."""
    global SESSION_ID, MY_FOLDER, MY_INBOX
    old = get_id()
    safe = "".join([c for c in new_name if c.isalnum() or c in ('-', '_')])
    if not safe: return "Invalid"
    
    target = POOL_ROOT / safe
    if target.exists() and (target / "heartbeat.json").exists():
        try:
            d = json.loads((target / "heartbeat.json").read_text(encoding='utf-8'))
            if time.time() - d['ts'] < HEARTBEAT_TTL: return "Name taken"
        except: pass
        try: shutil.rmtree(target)
        except: pass

    try:
        os.rename(MY_FOLDER, target)
        SESSION_ID = safe
        MY_FOLDER = target
        MY_INBOX = MY_FOLDER / "inbox"
        _update_state("NORMAL")
        return "OK"
    except: return "Fail"

@mcp.tool()
def send(to: str, msg: str) -> str:
    """Send message. to="all" for everyone, or comma-separated like "agent_1,agent_2"."""
    sender = get_id()
    targets = []
    
    if to == "all":
        targets = [p for p in POOL_ROOT.iterdir() if p.is_dir() and p.name != sender]
    else:
        for r in to.split(","):
            t = POOL_ROOT / r.strip()
            if t.exists(): targets.append(t)
    
    if not targets: return "No target"

    payload = {"from": sender, "msg": msg, "ts": time.time()}
    fname = f"{time.time()}_{uuid.uuid4().hex}.json"
    
    success = 0
    for folder in targets:
        inbox = folder / "inbox"
        inbox.mkdir(exist_ok=True)
        # [Constraint 2] 强校验：只有文件物理存在才算成功
        if _atomic_write(inbox / fname, payload):
            success += 1
            
    return "OK" if success > 0 else "Fail"

@mcp.tool()
def recv(wait: int = 86400) -> str:
    """Block wait for messages. Returns "Timeout" if no message within wait seconds."""
    get_id()
    start = time.time()
    _update_state("WAITING")
    
    try:
        while True:
            # 使用 glob 模式匹配 json (按文件名时间排序)
            files = sorted(MY_INBOX.glob("*.json")) if MY_INBOX else []

            if files:
                valid_msgs = []
                files_to_delete = []
                
                # 1. 内存读取 (Reading Phase)
                for f in files:
                    try:
                        text = f.read_text(encoding='utf-8')
                        data = json.loads(text)
                        valid_msgs.append(data)
                        files_to_delete.append(f)
                    except json.JSONDecodeError:
                        # 坏文件立即删，不卡队列
                        try: f.unlink()
                        except: pass
                    except: pass # 文件被锁？跳过下次再读

                if valid_msgs:
                    # [Constraint 3] 延迟删除 (Holding Phase)
                    # 关键！在这里睡 1.5秒。
                    # 此时文件已经被读到内存 valid_msgs 里了，
                    # 但硬盘上的 .json 文件还在。
                    # 发送者的 _atomic_write 检查能不能通过？能！
                    time.sleep(1.5) 

                    # 3. 物理删除 (Deletion Phase)
                    for f in files_to_delete:
                        try: f.unlink()
                        except: pass

                    _update_state("NORMAL")

                    # 4. 极简合并返回 (先按发送者分组，同发送者内按时间排序)
                    valid_msgs.sort(key=lambda x: (x['from'], x['ts']))  # 双重排序
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
            
            time.sleep(2.0)
    except:
        _update_state("NORMAL")
        return "Error"

if __name__ == "__main__":
    # 立即初始化 ID 和文件夹（不等到第一次工具调用）
    print("[DEBUG] Bridge starting, calling get_id()...")
    get_id()
    print(f"[DEBUG] Bridge started, SESSION_ID={SESSION_ID}, MY_FOLDER={MY_FOLDER}")
    mcp.run()
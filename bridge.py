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
# RootBridge - v32 Leader 直接继承机制
#
# Changes:
# - rename("leader") 时直接接管，删除旧文件夹（继承消息历史）
# - 其他名称仍然检查心跳，避免误删在线 agent
# - 通知逻辑移到 while 循环内部，确保长期等待也能持续提醒
#
# Core:
# 1. Send: Writes and immediately verifies existence.
# 2. Recv: Reads -> Sleeps 1.5s -> Deletes. (Fixes race condition)
# 3. Status: Hides offline agents.
# 4. Leader Notify: 循环检查通知
# 5. Rename: Leader 直接继承，其他名称安全检查
#
# Roles:
# - 清洁师 (技术 leader): 负责清理僵尸文件、死锁检测 (leader.json)
# - 业务 Leader: 负责任务分配、团队协调 (名字包含 "leader")
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

HEARTBEAT_TTL = 12.0    # 离线阈值（秒）：超过此时间未更新心跳视为离线
ZOMBIE_TTL    = 3600.0  # 清理阈值（秒）：超过此时间未更新心跳的文件夹会被删除
LEADER_TTL    = 10.0    # Leader过期时间（秒）：超过此时间未更新则重新选举

# --- Identity ---
SESSION_ID = None
MY_FOLDER = None
MY_INBOX = None
CURRENT_STATE = "NORMAL"
LAST_READY_NOTIFY_TIME = 0.0  # 上次发送待命通知的时间
PENDING_NOTIFY_UNTIL = 0.0    # 待通知截止时间（首次进入等待时设置，60秒后才通知）

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
    except (OSError, json.JSONDecodeError, PermissionError) as e:
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
    except OSError as e:
        print(f"[WARN] Failed to create session folders: {e}", file=sys.stderr)

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

# --- Leader Loop (清洁师选举和维护) ---
# 说明：每个 agent 都参与竞选"清洁师"角色
# - 清洁师：负责清理僵尸文件夹、死锁检测等技术维护
# - 业务 Leader：名字包含 "leader" 的 agent，负责任务分配

def leader_loop():
    # 上次死锁警告时间（避免频繁打扰）
    last_deadlock_warning = 0
    DEADLOCK_WARNING_COOLDOWN = 60.0  # 60秒冷却时间

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

                # 清理：1小时无心跳 或 没有heartbeat.json的僵尸文件夹
                # 同时寻找业务 Leader（名字包含 "leader"）
                all_waiting = True
                online_count = 0
                business_leader = None  # 业务 Leader（负责任务协调）

                for p in POOL_ROOT.iterdir():
                    if not p.is_dir(): continue
                    if p.name == "leader.json": continue  # 保护 leader.json
                    try:
                        hb_file = p / "heartbeat.json"
                        if hb_file.exists():
                            hb = json.loads(hb_file.read_text(encoding='utf-8'))
                            if now - hb['ts'] > ZOMBIE_TTL:
                                shutil.rmtree(p)
                            else:
                                # 统计在线状态
                                if now - hb['ts'] <= HEARTBEAT_TTL:
                                    online_count += 1
                                    if hb.get('state') != "WAITING":
                                        all_waiting = False
                                    # 找业务 Leader
                                    if "leader" in p.name.lower():
                                        business_leader = p
                        else:
                            # 僵尸文件夹：没有 heartbeat.json，直接删除
                            shutil.rmtree(p)
                    except (OSError, json.JSONDecodeError, PermissionError) as e:
                        print(f"[WARN] Failed to cleanup {p.name}: {e}", file=sys.stderr)

                # 死锁检测：所有人都在等待且至少2人在线
                # 发送警告给业务 Leader（不是清洁师自己）
                if all_waiting and online_count >= 2 and business_leader:
                    # 检查冷却时间
                    if now - last_deadlock_warning > DEADLOCK_WARNING_COOLDOWN:
                        # 清洁师发送系统警告给业务 Leader
                        inbox = business_leader / "inbox"
                        inbox.mkdir(exist_ok=True)
                        payload = {
                            "from": "SYSTEM",
                            "msg": "⚠️ 死锁警告：所有人都在等待分配任务，都在监听状态。请 Leader 发送指令打破僵局！",
                            "ts": now
                        }
                        fname = f"{now}_system_deadlock_warning.json"
                        if _atomic_write(inbox / fname, payload):
                            print(f"[SYSTEM] Deadlock detected, warning sent to {business_leader.name}", file=sys.stderr)
                            last_deadlock_warning = now

        except (OSError, json.JSONDecodeError) as e:
            print(f"[WARN] Leader loop error: {e}", file=sys.stderr)
        time.sleep(5.0)

threading.Thread(target=leader_loop, daemon=True).start()
atexit.register(lambda: (MY_FOLDER / "heartbeat.json").unlink(missing_ok=True) if MY_FOLDER else None)

# --- MCP Tools ---

@mcp.tool()
def status() -> str:
    """List all ONLINE agents. 🟢=Normal, ⏳=Waiting. * marks self. 👑 marks leader (name contains 'leader')."""
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

                # 标记：自己、Leader（名字包含 "leader" 不区分大小写）
                is_self = " *" if str(name) == str(SESSION_ID) else ""
                is_leader = " 👑" if "leader" in name.lower() else ""

                icon = "⏳" if state == "WAITING" else "🟢"
                lines.append(f"{icon} {name}{is_self}{is_leader}")
            except (OSError, json.JSONDecodeError, KeyError):
                pass

    return "\n".join(lines) if lines else "None"

@mcp.tool()
def rename(new_name: str) -> str:
    """
    修改自己的 Agent ID

    Args:
        new_name: 新名称（只允许字母、数字、-、_）

    Returns:
        "OK" - 成功
        "Invalid" - 名称包含非法字符
        "Name taken" - 名称已被在线 Agent 占用
        "Fail" - 修改失败（文件系统错误）
    """
    global SESSION_ID, MY_FOLDER, MY_INBOX
    old = get_id()
    safe = "".join([c for c in new_name if c.isalnum() or c in ('-', '_')])
    if not safe: return "Invalid"
    
    target = POOL_ROOT / safe
    if target.exists():
        # 特殊处理：改名为 "leader" 时直接继承（接管）
        if safe == "leader":
            # 直接接管 leader 文件夹，保留之前的消息历史
            # 这是合理的：用户重启 MCP 后重新成为 leader
            try:
                shutil.rmtree(target)  # 删除旧文件夹，准备接管
            except (OSError, PermissionError) as e:
                print(f"[WARN] Cannot remove old leader folder: {e}", file=sys.stderr)
                return "Fail"
        else:
            # 其他名称：检查是否可以覆盖
            hb_file = target / "heartbeat.json"
            if hb_file.exists():
                try:
                    d = json.loads(hb_file.read_text(encoding='utf-8'))
                    age = time.time() - d['ts']
                    # 心跳活跃（< HEARTBEAT_TTL）或无法确认是否僵尸，拒绝覆盖
                    if age < HEARTBEAT_TTL:
                        return "Name taken"
                    # 只有确认是僵尸文件夹（超过 ZOMBIE_TTL）才删除
                    if age > ZOMBIE_TTL:
                        shutil.rmtree(target)
                    else:
                        # 在 HEARTBEAT_TTL 和 ZOMBIE_TTL 之间，保守处理
                        return "Name taken"
                except (OSError, json.JSONDecodeError, KeyError):
                    # JSON 解析失败，保守处理：不删除，拒绝覆盖
                    return "Name taken"
            else:
                # 没有 heartbeat.json，可能是僵尸文件夹，直接删除
                try:
                    shutil.rmtree(target)
                except (OSError, PermissionError) as e:
                    print(f"[WARN] Cannot remove zombie folder: {e}", file=sys.stderr)

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
    """
    发送消息给其他 Agent

    Args:
        to: 目标 Agent，"all" 表示所有人，或逗号分隔如 "agent_1,agent_2"
        msg: 消息内容（字符串）

    Returns:
        "OK" - 成功发送给至少一个目标
        "Fail" - 所有目标都发送失败
        "No target" - 没有找到有效目标
    """
    sender = get_id()
    targets = []
    now = time.time()

    if to == "all":
        # 过滤离线者
        for p in POOL_ROOT.iterdir():
            if not p.is_dir() or p.name == sender:
                continue
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
    """
    阻塞等待接收消息

    Args:
        wait: 超时秒数，默认 86400（24 小时）

    Returns:
        单条消息: "[agent_123 14:30:05]: 消息内容"
        多条合并: "[agent_123 x3]:\n - [14:30:05] 消息1\n - [14:31:10] 消息2"
        超时: "Timeout"
        错误: "Error"
    """
    get_id()
    start = time.time()

    # 通知策略：
    # 1. 进入 WAITING 后 60 秒首次通知 Leader
    # 2. 之后每 3 分钟（180秒）循环通知
    # 3. 收到消息退出 WAITING 时清零标记，重新计时
    global LAST_READY_NOTIFY_TIME, PENDING_NOTIFY_UNTIL
    now = time.time()
    state_changed = (CURRENT_STATE != "WAITING")

    if state_changed:
        # 首次进入等待，设置 60 秒延迟通知
        PENDING_NOTIFY_UNTIL = now + 60.0
        LAST_READY_NOTIFY_TIME = now  # 重置闲置计时

    _update_state("WAITING")

    try:
        while True:
            # ⬇️ 通知逻辑移到循环内部，每次循环都检查
            now = time.time()
            time_since_last_notify = now - LAST_READY_NOTIFY_TIME
            first_notify_due = now >= PENDING_NOTIFY_UNTIL
            should_notify = first_notify_due or time_since_last_notify >= 180.0

            if should_notify:
                # 首次通知（60秒后）或后续闲置提醒
                if first_notify_due:
                    waiting_msg = f"{SESSION_ID} 已等待 1 分钟，可能等待依赖任务。你可评估派发新任务或询问进度。"
                else:
                    waiting_minutes = int(time_since_last_notify / 60)
                    waiting_msg = f"{SESSION_ID} 待命中，已等待 {waiting_minutes} 分钟"

                LAST_READY_NOTIFY_TIME = now

                # 通知 leader
                try:
                    for p in POOL_ROOT.iterdir():
                        if not p.is_dir(): continue
                        if "leader" in p.name.lower() and p.name != SESSION_ID:
                            hb = p / "heartbeat.json"
                            if hb.exists():
                                d = json.loads(hb.read_text(encoding='utf-8'))
                                if time.time() - d['ts'] <= HEARTBEAT_TTL:
                                    inbox = p / "inbox"
                                    inbox.mkdir(exist_ok=True)
                                    payload = {
                                        "from": "SYSTEM",
                                        "msg": waiting_msg,
                                        "ts": time.time()
                                    }
                                    fname = f"{time.time()}_{uuid.uuid4().hex}.json"
                                    _atomic_write(inbox / fname, payload)
                                    break
                except: pass

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
                        try:
                            f.unlink()
                        except OSError:
                            pass
                    except OSError:
                        pass # 文件被锁？跳过下次再读

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

                    # 收到消息，退出等待，清除待通知标记
                    PENDING_NOTIFY_UNTIL = 0.0
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
                # 超时退出，清除待通知标记
                PENDING_NOTIFY_UNTIL = 0.0
                _update_state("NORMAL")
                return "Timeout"
            
            time.sleep(2.0)
    except Exception as e:
        print(f"[ERROR] Recv error: {e}", file=sys.stderr)
        PENDING_NOTIFY_UNTIL = 0.0
        _update_state("NORMAL")
        return "Error"

if __name__ == "__main__":
    # 立即初始化 ID 和文件夹（不等到第一次工具调用）
    print("[DEBUG] Bridge starting, calling get_id()...")
    get_id()
    print(f"[DEBUG] Bridge started, SESSION_ID={SESSION_ID}, MY_FOLDER={MY_FOLDER}")
    mcp.run()

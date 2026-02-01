import os, sys, time, io, uuid, sqlite3, random, asyncio, platform, threading, atexit
from pathlib import Path
from contextlib import contextmanager
from collections import defaultdict
from mcp.server.fastmcp import FastMCP

# =========================================================
# RootBridge - Dual DB Edition (High Stability)
#
# Changes:
# - Split into two DB files:
#   1. bridge_state_v11.db -> peers, leader_lock (Fast, frequent, critical)
#   2. bridge_msg_v11.db   -> messages (Heavy, bulk, can wait)
#
# Benefit: Leader cleaning messages won't block agents from heartbeating.
# =========================================================

# --- 1) Basic IO (Windows UTF-8) ---
try:
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

mcp = FastMCP("RootBridge")

# --- 2) Config ---
BRIDGE_VERSION = "v11_dual"
DB_FILE_STATE = f"bridge_state_{BRIDGE_VERSION}.db"
DB_FILE_MSG = f"bridge_msg_{BRIDGE_VERSION}.db"

if sys.platform == "win32":
    PREFERRED_ROOT = Path("C:/mcp_msg_pool")
    FALLBACK_ROOT = Path("C:/Users/Public/mcp_msg_pool")
else:
    PREFERRED_ROOT = Path.home() / ".mcp_msg_pool"
    FALLBACK_ROOT = Path("/tmp/mcp_msg_pool")

# Liveness
HEARTBEAT_TTL = 300          # seconds
HEARTBEAT_INTERVAL = 10.0    # seconds

# Leader lock
LEADER_LEASE_TTL = 45.0
LEADER_RENEW_EVERY = 15.0

# Messages
MSG_TTL = 86400
LEASE_TTL = 30
MAX_BATCH_CHARS = 5000

# Recv loop
RECV_TICK = 0.5
RECV_DB_POLL_EVERY_LEADER = 2.0
RECV_DB_POLL_EVERY_FOLLOWER = 6.0
RECV_FAST_POLL_ONLY_FOR_LEADER = True

# Maintenance
CLEAN_LOCAL_EVERY = 15.0
CLEAN_REMOTE_EVERY = 120.0
CHECKPOINT_EVERY = 600.0

# --- 3) Global state ---
SESSION_NAME = None
SESSION_PID = os.getpid()
SESSION_HOST = platform.node()
LAST_ACTIVE_TS = 0.0

_BG_STARTED = False
_BG_LOCK = threading.Lock()

def _choose_db_root() -> Path:
    for root in (PREFERRED_ROOT, FALLBACK_ROOT):
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".perm_probe"
            probe.touch()
            probe.unlink(missing_ok=True)
            return root
        except Exception:
            continue
    FALLBACK_ROOT.mkdir(parents=True, exist_ok=True)
    return FALLBACK_ROOT

DB_ROOT = _choose_db_root()
PATH_STATE = DB_ROOT / DB_FILE_STATE
PATH_MSG = DB_ROOT / DB_FILE_MSG

@contextmanager
def get_db(which: str = "state"):
    """
    which: 'state' (peers/lock) or 'msg' (messages)
    """
    target_path = PATH_STATE if which == "state" else PATH_MSG
    conn = None
    try:
        conn = sqlite3.connect(str(target_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def _db_retry(fn, retries: int = 7, base_delay: float = 0.03, max_delay: float = 0.35):
    for i in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            s = str(e).lower()
            if ("locked" in s) or ("busy" in s):
                time.sleep(min(max_delay, base_delay * (2 ** i)) + random.random() * base_delay)
                continue
            raise
    return fn()

def _table_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == col for r in rows)
    except Exception:
        return False

def _is_pid_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0: return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, pid)
            if not h: return ctypes.get_last_error() == 5
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return ok and code.value == 259
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True

# ---------------- Schema (Split) ----------------

def init_db():
    # 1) Initialize STATE DB (Peers, Locks)
    def _init_state():
        with get_db("state") as conn:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS peers (
                        id TEXT PRIMARY KEY,
                        pid INTEGER,
                        hostname TEXT,
                        last_seen REAL,
                        cwd TEXT,
                        recv_state TEXT,
                        recv_started REAL,
                        recv_deadline REAL,
                        recv_wait_seconds INTEGER,
                        recv_last_touch REAL,
                        mode TEXT,
                        mode_since REAL,
                        active_last_touch REAL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS leader_lock (
                        k TEXT PRIMARY KEY,
                        owner_id TEXT,
                        host TEXT,
                        pid INTEGER,
                        lease_until REAL,
                        updated_at REAL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen);")
    
    # 2) Initialize MSG DB (Messages)
    def _init_msg():
        with get_db("msg") as conn:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        msg_id TEXT PRIMARY KEY,
                        ts REAL,
                        ts_str TEXT,
                        from_user TEXT,
                        to_user TEXT,
                        content TEXT,
                        state TEXT DEFAULT 'queued',
                        lease_owner TEXT,
                        lease_until REAL,
                        attempt INTEGER DEFAULT 0,
                        delivered_at REAL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_to_user_state_ts ON messages(to_user, state, ts);")

    try:
        _db_retry(_init_state)
        _db_retry(_init_msg)
    except Exception:
        pass

init_db()

# ---------------- Peers / ID (STATE DB) ----------------

def _update_heartbeat(name: str, pid: int):
    now = time.time()
    cwd = os.getcwd()
    host = SESSION_HOST

    def do():
        with get_db("state") as conn:
            with conn:
                cur = conn.execute(
                    "UPDATE peers SET pid=?, hostname=?, last_seen=?, cwd=?, active_last_touch=COALESCE(?, active_last_touch), mode=COALESCE(mode, 'working'), mode_since=COALESCE(mode_since, ?) WHERE id=?",
                    (pid, host, now, cwd, (LAST_ACTIVE_TS if LAST_ACTIVE_TS > 0 else None), now, name),
                )
                if cur.rowcount == 0:
                    conn.execute(
                        "INSERT OR IGNORE INTO peers (id, pid, hostname, last_seen, cwd, mode, mode_since, active_last_touch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (name, pid, host, now, cwd, "working", now, (LAST_ACTIVE_TS if LAST_ACTIVE_TS > 0 else now)),
                    )
    try:
        _db_retry(do)
    except Exception:
        pass

def _claim_id_atomic(pid: int) -> str:
    now = time.time()
    cutoff = now - HEARTBEAT_TTL
    cwd = os.getcwd()
    host = SESSION_HOST

    for _ in range(5000):
        cid = f"{random.randint(1, 999):03d}"
        def attempt():
            with get_db("state") as conn:
                with conn:
                    try:
                        conn.execute(
                            "INSERT INTO peers (id, pid, hostname, last_seen, cwd, mode, mode_since, active_last_touch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (cid, pid, host, now, cwd, "working", now, now),
                        )
                        return True
                    except sqlite3.IntegrityError:
                        row = conn.execute("SELECT last_seen FROM peers WHERE id=?", (cid,)).fetchone()
                        if row and row["last_seen"] < cutoff:
                            cur = conn.execute(
                                "UPDATE peers SET pid=?, hostname=?, last_seen=?, cwd=? WHERE id=? AND last_seen < ?",
                                (pid, host, now, cwd, cid, cutoff),
                            )
                            return cur.rowcount == 1
                        return False
        if _db_retry(attempt):
            return cid
    raise RuntimeError("ID pool exhausted")

def get_session():
    global SESSION_NAME
    if not SESSION_NAME:
        SESSION_NAME = _claim_id_atomic(SESSION_PID)
        _update_heartbeat(SESSION_NAME, SESSION_PID)
    return SESSION_NAME, SESSION_PID

def _remove_self():
    if not SESSION_NAME: return
    def do():
        with get_db("state") as conn:
            with conn:
                conn.execute("DELETE FROM peers WHERE id=?", (SESSION_NAME,))
    try:
        _db_retry(do)
    except Exception:
        pass

atexit.register(_remove_self)

def _get_active_peers():
    limit = time.time() - HEARTBEAT_TTL
    with get_db("state") as conn:
        rows = conn.execute(
            "SELECT * FROM peers WHERE last_seen > ? ORDER BY id",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

# ---------------- Recv/listen status (STATE DB) ----------------

def _set_recv_waiting(name: str, wait_seconds: int):
    now = time.time()
    deadline = now + float(wait_seconds) if (wait_seconds > 0) else None
    def do():
        with get_db("state") as conn:
            with conn:
                conn.execute(
                    "UPDATE peers SET recv_state='waiting', recv_started=?, recv_deadline=?, recv_wait_seconds=?, recv_last_touch=?, mode='waiting', mode_since=?, active_last_touch=? WHERE id=?",
                    (now, deadline, int(wait_seconds), now, now, now, name),
                )
    try: _db_retry(do)
    except Exception: pass

def _touch_recv_waiting(name: str):
    now = time.time()
    def do():
        with get_db("state") as conn:
            with conn:
                conn.execute("UPDATE peers SET recv_last_touch=?, active_last_touch=COALESCE(?, active_last_touch) WHERE id=? AND recv_state='waiting'", (now, (LAST_ACTIVE_TS if LAST_ACTIVE_TS > 0 else None), name))
    try: _db_retry(do)
    except Exception: pass

def _clear_recv_waiting(name: str):
    def do():
        with get_db("state") as conn:
            with conn:
                conn.execute(
                    "UPDATE peers SET recv_state=NULL, recv_started=NULL, recv_deadline=NULL, recv_wait_seconds=NULL, recv_last_touch=NULL, mode='working', mode_since=?, active_last_touch=COALESCE(?, active_last_touch) WHERE id=?",
                    (time.time(), (LAST_ACTIVE_TS if LAST_ACTIVE_TS > 0 else None), name),
                )
    try: _db_retry(do)
    except Exception: pass

# ---------------- Leader lease (STATE DB) ----------------

def _get_leader_owner() -> str | None:
    def do():
        with get_db("state") as conn:
            row = conn.execute("SELECT owner_id, lease_until FROM leader_lock WHERE k='main'").fetchone()
            if not row: return None
            if (row["lease_until"] is not None) and (float(row["lease_until"]) < time.time()): return None
            return row["owner_id"]
    try: return _db_retry(do)
    except Exception: return None

def _try_acquire_or_renew_leader(my_id: str, my_pid: int) -> bool:
    now = time.time()
    until = now + LEADER_LEASE_TTL
    def do():
        with get_db("state") as conn:
            with conn:
                conn.execute("INSERT OR IGNORE INTO leader_lock(k, owner_id, host, pid, lease_until, updated_at) VALUES ('main', ?, ?, ?, ?, ?)", (my_id, SESSION_HOST, my_pid, until, now))
                cur = conn.execute("UPDATE leader_lock SET owner_id=?, host=?, pid=?, lease_until=?, updated_at=? WHERE k='main' AND (lease_until < ? OR owner_id=?)", (my_id, SESSION_HOST, my_pid, until, now, now, my_id))
                if cur.rowcount == 1: return True
                row = conn.execute("SELECT owner_id, lease_until FROM leader_lock WHERE k='main'").fetchone()
                return (row and row["owner_id"] == my_id and float(row["lease_until"]) >= now)
    try: return bool(_db_retry(do))
    except Exception: return False

# ---------------- Messages (MSG DB) ----------------

def _send_db_batch(from_user: str, recipients: list[str], content: str) -> str:
    ts = time.time()
    ts_str = time.strftime("%H:%M:%S")
    data = []
    first_short = None
    for to_user in recipients:
        mid = uuid.uuid4().hex
        if not first_short: first_short = mid[:8]
        data.append((mid, ts, ts_str, from_user, to_user, content))
    def do():
        with get_db("msg") as conn:
            with conn:
                conn.executemany("INSERT INTO messages (msg_id, ts, ts_str, from_user, to_user, content) VALUES (?, ?, ?, ?, ?, ?)", data)
    _db_retry(do)
    return first_short or "--------"

def _lease_messages(my_name: str, max_chars: int = MAX_BATCH_CHARS):
    now = time.time()
    lease_until = now + LEASE_TTL
    def do():
        with get_db("msg") as conn:
            with conn:
                conn.execute("UPDATE messages SET state='queued', lease_owner=NULL, lease_until=NULL WHERE to_user=? AND state='inflight' AND lease_until IS NOT NULL AND lease_until < ?", (my_name, now))
                rows = conn.execute("SELECT * FROM messages WHERE to_user=? AND state='queued' ORDER BY ts ASC LIMIT 200", (my_name,)).fetchall()
                chosen = []
                chosen_ids = []
                cost = 0
                for r in rows:
                    c = len(r["content"] or "") + 60
                    if chosen and (cost + c > max_chars): break
                    chosen.append(dict(r))
                    chosen_ids.append(r["msg_id"])
                    cost += c
                if chosen_ids:
                    placeholders = ",".join("?" * len(chosen_ids))
                    conn.execute(f"UPDATE messages SET state='inflight', lease_owner=?, lease_until=?, attempt=COALESCE(attempt,0)+1, delivered_at=? WHERE msg_id IN ({placeholders}) AND state='queued'", [my_name, lease_until, now, *chosen_ids])
                remaining = conn.execute("SELECT COUNT(1) AS c FROM messages WHERE to_user=? AND state='queued'", (my_name,)).fetchone()["c"]
                remaining = max(0, int(remaining) - len(chosen_ids))
                return chosen, chosen_ids, remaining
    return _db_retry(do)

def _ack_messages(msg_ids: list[str], my_name: str):
    if not msg_ids: return
    def do():
        with get_db("msg") as conn:
            with conn:
                placeholders = ",".join("?" * len(msg_ids))
                conn.execute(f"DELETE FROM messages WHERE msg_id IN ({placeholders}) AND state='inflight' AND lease_owner=?", [*msg_ids, my_name])
    _db_retry(do)

def _release_leases(msg_ids: list[str], my_name: str):
    if not msg_ids: return
    def do():
        with get_db("msg") as conn:
            with conn:
                placeholders = ",".join("?" * len(msg_ids))
                conn.execute(f"UPDATE messages SET state='queued', lease_owner=NULL, lease_until=NULL WHERE msg_id IN ({placeholders}) AND state='inflight' AND lease_owner=?", [*msg_ids, my_name])
    try: _db_retry(do)
    except Exception: pass

def _format_msgs_grouped(msgs: list[dict], remaining: int) -> str:
    if not msgs: return "No messages."
    grouped = defaultdict(list)
    for m in msgs: grouped[m["from_user"]].append(m)
    senders = sorted(grouped.keys(), key=lambda s: min(mm["ts"] for mm in grouped[s]))
    lines = [f"=== {len(msgs)} messages from {len(grouped)} agent(s) ===\n"]
    for s in senders:
        ms = grouped[s]
        lines.append(f"[{s}] - {len(ms)} message(s)")
        for m in ms: lines.append(f"  {m['ts_str']} {m['content']}")
        lines.append("")
    if remaining > 0: lines.append(f"({remaining} more queued. Call recv() again)")
    return "\n".join(lines)

# ---------------- Leader maintenance (Split) ----------------

def _clean_dead_local_peers():
    # Only touches STATE DB
    my_host = SESSION_HOST
    def do():
        with get_db("state") as conn:
            with conn:
                rows = conn.execute("SELECT id, pid FROM peers WHERE hostname=?", (my_host,)).fetchall()
                dead = []
                for r in rows:
                    pid = r["pid"]
                    pid = int(pid) if pid is not None else 0
                    if SESSION_NAME and r["id"] == SESSION_NAME: continue
                    if pid and not _is_pid_alive(pid): dead.append(r["id"])
                if dead:
                    placeholders = ",".join("?" * len(dead))
                    conn.execute(f"DELETE FROM peers WHERE id IN ({placeholders})", dead)
    try: _db_retry(do)
    except Exception: pass

def _clean_remote_and_prune():
    now = time.time()
    cutoff = now - HEARTBEAT_TTL
    msg_cutoff = now - MSG_TTL

    # 1. Clean STATE DB (Peers)
    def do_state():
        with get_db("state") as conn:
            with conn:
                conn.execute("DELETE FROM peers WHERE last_seen < ?", (cutoff,))
                conn.execute("UPDATE peers SET recv_state=NULL, recv_started=NULL, recv_deadline=NULL, recv_wait_seconds=NULL, recv_last_touch=NULL WHERE recv_state IS NOT NULL AND recv_deadline IS NOT NULL AND recv_deadline < ?", (now,))
    
    # 2. Clean MSG DB (Messages)
    def do_msg():
        with get_db("msg") as conn:
            with conn:
                conn.execute("UPDATE messages SET state='queued', lease_owner=NULL, lease_until=NULL WHERE state='inflight' AND lease_until IS NOT NULL AND lease_until < ?", (now,))
                conn.execute("DELETE FROM messages WHERE ts < ?", (msg_cutoff,))

    try: _db_retry(do_state)
    except Exception: pass
    
    try: _db_retry(do_msg)
    except Exception: pass

def _checkpoint():
    # Optimization: checkpoint BOTH DBs
    for db_type in ["state", "msg"]:
        try:
            with get_db(db_type) as conn:
                # Use PASSIVE to avoid locking writers
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                conn.execute("PRAGMA optimize;")
        except Exception:
            pass

def _maintenance_loop():
    name, pid = get_session()
    time.sleep(random.random() * 3.0)
    last_local = 0.0
    last_remote = 0.0
    last_ckpt = 0.0
    last_leader_renew = 0.0
    is_leader = False

    while True:
        now = time.time()
        try:
            _update_heartbeat(name, pid)
            if (now - last_leader_renew) >= LEADER_RENEW_EVERY or is_leader:
                last_leader_renew = now
                is_leader = _try_acquire_or_renew_leader(name, pid)

            if is_leader:
                if now - last_local >= CLEAN_LOCAL_EVERY:
                    last_local = now
                    _clean_dead_local_peers()
                if now - last_remote >= CLEAN_REMOTE_EVERY:
                    last_remote = now
                    _clean_remote_and_prune()
                if now - last_ckpt >= CHECKPOINT_EVERY:
                    last_ckpt = now
                    _checkpoint()
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVAL + random.random() * 0.2)

def _ensure_background_started():
    global _BG_STARTED
    if _BG_STARTED: return
    with _BG_LOCK:
        if _BG_STARTED: return
        t = threading.Thread(target=_maintenance_loop, daemon=True)
        t.start()
        _BG_STARTED = True

def _mark_active():
    global LAST_ACTIVE_TS
    LAST_ACTIVE_TS = time.time()

# bootstrap
get_session()
_ensure_background_started()

# ---------------- MCP Tools ----------------

@mcp.tool()
def get_status() -> str:
    """List online agents."""
    _mark_active()
    me, _ = get_session()
    peers = _get_active_peers()
    now = time.time()

    def sort_key(p): return (0, p["id"]) if p["id"] == me else (1, p["id"])
    peers_sorted = sorted(peers, key=sort_key)

    lines = []
    for i, p in enumerate(peers_sorted):
        flags = []
        if p["id"] == me: flags.append("THIS")
        
        state_str = ""
        if (p.get("recv_state") == "waiting") and p.get("recv_started"):
            try: elapsed = max(0, int(now - float(p["recv_started"])))
            except Exception: elapsed = 0
            total = p.get("recv_wait_seconds") or 0
            state_str = f"🎧 Waiting ({elapsed}s/{int(total)}s)" if total else f"🎧 Waiting ({elapsed}s)"
        else:
            since = p.get("mode_since") or p.get("active_last_touch") or p.get("recv_last_touch") or None
            w_elapsed = max(0, int(now - float(since))) if since else 0
            state_str = f"❓ Working ({w_elapsed}s)" if w_elapsed >= 1800 else f"🛠 Working ({w_elapsed}s)"

        bracket = " | ".join([*flags, state_str])
        cwd = p.get("cwd") or p.get("host") or "UnknownPath"
        line = f"Agent {p['id']} @ {cwd}  [{bracket}]"
        if i > 0: line = "  " + line
        lines.append(line)
    return "\n".join(lines) if lines else "No active agents."

@mcp.tool()
def send(to: str, content: str) -> str:
    """Send message to 'id' or 'all'."""
    _mark_active()
    name, _ = get_session()
    recipients = [r.strip() for r in (to or "").split(",") if r.strip()]
    peers = _get_active_peers()
    online_ids = [p["id"] for p in peers]

    if any(r.lower() == "all" for r in recipients):
        final = [pid for pid in online_ids if pid != name]
        if not final: return "No other agents online."
    else:
        if name in recipients: return "Error: cannot send to self."
        final = []
        for r in recipients:
            if r not in online_ids: return f"Error: Agent '{r}' offline."
            final.append(r)
    try:
        sid = _send_db_batch(name, final, content)
        return f"Sent (to {len(final)} agent(s), id={sid})"
    except Exception as e: return f"DB Error: {e}"

@mcp.tool()
async def recv(wait_seconds: int = 86400) -> str:
    """Receive messages."""
    _mark_active()
    name, _ = get_session()
    start = time.monotonic()
    my_task_ts = LAST_ACTIVE_TS
    leased_ids: list[str] = []
    waiting_marked = False

    async def try_once():
        nonlocal leased_ids
        msgs, leased_ids, remaining = await asyncio.to_thread(_lease_messages, name, MAX_BATCH_CHARS)
        if not msgs:
            leased_ids = []
            return None
        out = _format_msgs_grouped(msgs, remaining)
        await asyncio.to_thread(_ack_messages, leased_ids, name)
        leased_ids = []
        return out

    try:
        first = await try_once()
        if first: return first
        if wait_seconds <= 0: return "No new messages."

        waiting_marked = True
        await asyncio.to_thread(_set_recv_waiting, name, int(wait_seconds))

        leader = _get_leader_owner()
        is_leader = (leader == name)
        poll_every = RECV_DB_POLL_EVERY_LEADER if (is_leader or not RECV_FAST_POLL_ONLY_FOR_LEADER) else RECV_DB_POLL_EVERY_FOLLOWER
        jitter = (int(name) % 10) * 0.03 if name and name.isdigit() else random.random() * 0.2
        next_db_poll = time.monotonic() + jitter

        while True:
            if LAST_ACTIVE_TS != my_task_ts: return "Cancelled by new command."
            if time.monotonic() - start >= float(wait_seconds): return f"Timeout ({int(wait_seconds)}s)."
            
            now_m = time.monotonic()
            if now_m >= next_db_poll:
                next_db_poll = now_m + poll_every
                await asyncio.to_thread(_touch_recv_waiting, name)
                res = await try_once()
                if res: return res
            await asyncio.sleep(RECV_TICK)

    except (asyncio.CancelledError, KeyboardInterrupt):
        if leased_ids: await asyncio.to_thread(_release_leases, leased_ids, name)
        return "Cancelled."
    except Exception as e:
        if leased_ids: await asyncio.to_thread(_release_leases, leased_ids, name)
        return f"Error: {e}"
    finally:
        if waiting_marked:
            try: await asyncio.to_thread(_clear_recv_waiting, name)
            except Exception: pass

if __name__ == "__main__":
    mcp.run()
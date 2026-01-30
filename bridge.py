import os, sys, time, io, uuid, sqlite3, random, asyncio, platform, threading, atexit
from pathlib import Path
from contextlib import contextmanager
from collections import defaultdict
from mcp.server.fastmcp import FastMCP

# =========================================================
# RootBridge - Stable Leader Lease Edition (Production-ish)
#
# Goals:
# 1) Same tools: get_status / send / recv (behavior-compatible)
# 2) Only ONE leader runs heavy maintenance (PID scan / prune / checkpoint)
# 3) Followers do lightweight heartbeats; optional slower recv polling
# 4) Leader election is simple + stable: DB lease lock with TTL
#
# Notes:
# - sqlite is still the shared "hub"
# - recv still works on any agent; but only leader gets "fast-poll" by default
# =========================================================

# --- 1) Basic IO (Windows UTF-8) ---
try:
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

mcp = FastMCP("RootBridge")

# --- 2) Config ---
BRIDGE_DB_VERSION = "v11_stable"
BRIDGE_DB_FILENAME = f"bridge_{BRIDGE_DB_VERSION}.db"

if sys.platform == "win32":
    PREFERRED_ROOT = Path("C:/mcp_msg_pool")
    FALLBACK_ROOT = Path("C:/Users/Public/mcp_msg_pool")
else:
    PREFERRED_ROOT = Path.home() / ".mcp_msg_pool"
    FALLBACK_ROOT = Path("/tmp/mcp_msg_pool")

# Liveness
HEARTBEAT_TTL = 300          # seconds: peer considered offline after this
HEARTBEAT_INTERVAL = 10.0    # seconds: how often every agent updates its heartbeat

# Leader lock (lease)
LEADER_LEASE_TTL = 45.0      # seconds: leader ownership duration
LEADER_RENEW_EVERY = 15.0    # seconds: leader renew cadence (<= lease ttl)

# Messages
MSG_TTL = 86400              # seconds: delete old messages
LEASE_TTL = 30               # seconds: per-message inflight lease
MAX_BATCH_CHARS = 5000

# recv loop cadence
RECV_TICK = 0.5
RECV_DB_POLL_EVERY_LEADER = 2.0
RECV_DB_POLL_EVERY_FOLLOWER = 6.0   # followers poll less frequently by default
RECV_FAST_POLL_ONLY_FOR_LEADER = True  # fast polling only when you are leader

# Maintenance cadences (leader only)
CLEAN_LOCAL_EVERY = 15.0     # PID scan on same host
CLEAN_REMOTE_EVERY = 120.0   # prune peers/messages
CHECKPOINT_EVERY = 600.0     # checkpoint/optimize

# --- 3) Global state ---
SESSION_NAME = None
SESSION_PID = os.getpid()
SESSION_HOST = platform.node()
LAST_ACTIVE_TS = 0.0

_BG_STARTED = False
_BG_LOCK = threading.Lock()

def log(_msg: str):
    # keep silent by default; add print() if you want diagnostics
    # print(_msg, file=sys.stderr, flush=True)
    pass

def _choose_db_path() -> Path:
    for root in (PREFERRED_ROOT, FALLBACK_ROOT):
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".perm_probe"
            probe.touch()
            probe.unlink(missing_ok=True)
            return root / BRIDGE_DB_FILENAME
        except Exception:
            continue
    FALLBACK_ROOT.mkdir(parents=True, exist_ok=True)
    return FALLBACK_ROOT / BRIDGE_DB_FILENAME

DB_PATH = _choose_db_path()

@contextmanager
def get_db():
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL is crucial for better multi-process concurrency
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
    """
    Expensive check (esp. on Windows). Only leader calls it.
    """
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                # Access denied => process likely exists
                return ctypes.get_last_error() == 5
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            if not ok:
                return True
            STILL_ACTIVE = 259
            return code.value == STILL_ACTIVE
        except Exception:
            # better to keep than incorrectly delete a living peer
            return True

    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True

# ---------------- Schema ----------------

def init_db():
    def _do():
        with get_db() as conn:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS peers (
                        id TEXT PRIMARY KEY,
                        pid INTEGER,
                        hostname TEXT,
                        last_seen REAL,
                        cwd TEXT
                    )
                """)
                # migrations
                if not _table_has_column(conn, "peers", "hostname"):
                    try: conn.execute("ALTER TABLE peers ADD COLUMN hostname TEXT")
                    except Exception: pass
                if not _table_has_column(conn, "peers", "cwd"):
                    try: conn.execute("ALTER TABLE peers ADD COLUMN cwd TEXT")
                    except Exception: pass

                # recv/listen status (optional)
                for col, ddl in [
                    ("recv_state", "ALTER TABLE peers ADD COLUMN recv_state TEXT"),
                    ("recv_started", "ALTER TABLE peers ADD COLUMN recv_started REAL"),
                    ("recv_deadline", "ALTER TABLE peers ADD COLUMN recv_deadline REAL"),
                    ("recv_wait_seconds", "ALTER TABLE peers ADD COLUMN recv_wait_seconds INTEGER"),
                    ("recv_last_touch", "ALTER TABLE peers ADD COLUMN recv_last_touch REAL"),
                ]:
                    if not _table_has_column(conn, "peers", col):
                        try: conn.execute(ddl)
                        except Exception: pass

                # mode status (optional): 'waiting' if inside recv(), otherwise 'working'
                for col, ddl in [
                    ("mode", "ALTER TABLE peers ADD COLUMN mode TEXT"),
                    ("mode_since", "ALTER TABLE peers ADD COLUMN mode_since REAL"),
                    ("active_last_touch", "ALTER TABLE peers ADD COLUMN active_last_touch REAL"),
                ]:
                    if not _table_has_column(conn, "peers", col):
                        try: conn.execute(ddl)
                        except Exception: pass

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
                for col, ddl in [
                    ("state", "ALTER TABLE messages ADD COLUMN state TEXT DEFAULT 'queued'"),
                    ("lease_owner", "ALTER TABLE messages ADD COLUMN lease_owner TEXT"),
                    ("lease_until", "ALTER TABLE messages ADD COLUMN lease_until REAL"),
                    ("attempt", "ALTER TABLE messages ADD COLUMN attempt INTEGER DEFAULT 0"),
                    ("delivered_at", "ALTER TABLE messages ADD COLUMN delivered_at REAL"),
                ]:
                    if not _table_has_column(conn, "messages", col):
                        try: conn.execute(ddl)
                        except Exception: pass

                # leader lock table
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
                conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_to_user_state_ts ON messages(to_user, state, ts);")
    try:
        _db_retry(_do)
    except Exception:
        pass

init_db()

# ---------------- Peers / ID ----------------

def _update_heartbeat(name: str, pid: int):
    now = time.time()
    cwd = os.getcwd()
    host = SESSION_HOST

    def do():
        with get_db() as conn:
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

    # random 3-digit id to keep compatibility with your workflow
    for _ in range(5000):
        cid = f"{random.randint(1, 999):03d}"

        def attempt():
            with get_db() as conn:
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

    raise RuntimeError("ID pool exhausted (001-999)")

def get_session():
    global SESSION_NAME
    if not SESSION_NAME:
        SESSION_NAME = _claim_id_atomic(SESSION_PID)
        _update_heartbeat(SESSION_NAME, SESSION_PID)
    return SESSION_NAME, SESSION_PID

def _remove_self():
    if not SESSION_NAME:
        return
    def do():
        with get_db() as conn:
            with conn:
                conn.execute("DELETE FROM peers WHERE id=?", (SESSION_NAME,))
    try:
        _db_retry(do)
    except Exception:
        pass

atexit.register(_remove_self)

def _get_active_peers():
    limit = time.time() - HEARTBEAT_TTL
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, hostname, cwd, mode, mode_since, active_last_touch, recv_state, recv_started, recv_deadline, recv_wait_seconds, recv_last_touch "
            "FROM peers WHERE last_seen > ? ORDER BY id",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "host": r["hostname"],
            "cwd": r["cwd"],
            "mode": r["mode"] if "mode" in r.keys() else None,
            "mode_since": r["mode_since"] if "mode_since" in r.keys() else None,
            "active_last_touch": r["active_last_touch"] if "active_last_touch" in r.keys() else None,
            "recv_state": r["recv_state"] if "recv_state" in r.keys() else None,
            "recv_started": r["recv_started"] if "recv_started" in r.keys() else None,
            "recv_deadline": r["recv_deadline"] if "recv_deadline" in r.keys() else None,
            "recv_wait_seconds": r["recv_wait_seconds"] if "recv_wait_seconds" in r.keys() else None,
            "recv_last_touch": r["recv_last_touch"] if "recv_last_touch" in r.keys() else None,
        })
    return out


# ---------------- Recv/listen status ----------------

def _set_recv_waiting(name: str, wait_seconds: int):
    """
    Mark this peer as currently waiting inside recv().
    This is purely for observability (team lead can see who is waiting).
    """
    now = time.time()
    deadline = now + float(wait_seconds) if (wait_seconds and wait_seconds > 0) else None

    def do():
        with get_db() as conn:
            with conn:
                conn.execute(
                    "UPDATE peers SET recv_state=?, recv_started=?, recv_deadline=?, recv_wait_seconds=?, recv_last_touch=?, mode='waiting', mode_since=?, active_last_touch=? "
                    "WHERE id=?",
                    ("waiting", now, deadline, int(wait_seconds), now, now, now, name),
                )
    try:
        _db_retry(do)
    except Exception:
        pass

def _touch_recv_waiting(name: str):
    now = time.time()
    def do():
        with get_db() as conn:
            with conn:
                conn.execute("UPDATE peers SET recv_last_touch=?, active_last_touch=COALESCE(?, active_last_touch) WHERE id=? AND recv_state='waiting'", (now, (LAST_ACTIVE_TS if LAST_ACTIVE_TS > 0 else None), name))
    try:
        _db_retry(do)
    except Exception:
        pass

def _clear_recv_waiting(name: str):
    def do():
        with get_db() as conn:
            with conn:
                conn.execute(
                    "UPDATE peers SET recv_state=NULL, recv_started=NULL, recv_deadline=NULL, recv_wait_seconds=NULL, recv_last_touch=NULL, mode='working', mode_since=?, active_last_touch=COALESCE(?, active_last_touch) "
                    "WHERE id=?",
                    (time.time(), (LAST_ACTIVE_TS if LAST_ACTIVE_TS > 0 else None), name),
                )
    try:
        _db_retry(do)
    except Exception:
        pass

# ---------------- Leader lease ----------------

def _get_leader_owner() -> str | None:
    def do():
        with get_db() as conn:
            row = conn.execute(
                "SELECT owner_id, lease_until FROM leader_lock WHERE k='main'"
            ).fetchone()
            if not row:
                return None
            if (row["lease_until"] is not None) and (float(row["lease_until"]) < time.time()):
                return None
            return row["owner_id"]
    try:
        return _db_retry(do)
    except Exception:
        return None

def _try_acquire_or_renew_leader(my_id: str, my_pid: int) -> bool:
    now = time.time()
    until = now + LEADER_LEASE_TTL

    def do():
        with get_db() as conn:
            with conn:
                # seed row
                conn.execute("""
                    INSERT OR IGNORE INTO leader_lock(k, owner_id, host, pid, lease_until, updated_at)
                    VALUES ('main', ?, ?, ?, ?, ?)
                """, (my_id, SESSION_HOST, my_pid, until, now))

                # renew if self OR steal if expired
                cur = conn.execute("""
                    UPDATE leader_lock
                    SET owner_id=?, host=?, pid=?, lease_until=?, updated_at=?
                    WHERE k='main' AND (lease_until < ? OR owner_id=?)
                """, (my_id, SESSION_HOST, my_pid, until, now, now, my_id))

                if cur.rowcount == 1:
                    return True

                row = conn.execute("SELECT owner_id, lease_until FROM leader_lock WHERE k='main'").fetchone()
                if not row:
                    return False
                return (row["owner_id"] == my_id) and (row["lease_until"] is not None) and (float(row["lease_until"]) >= now)

    try:
        return bool(_db_retry(do))
    except Exception:
        return False

# ---------------- Messages ----------------

def _send_db_batch(from_user: str, recipients: list[str], content: str) -> str:
    ts = time.time()
    ts_str = time.strftime("%H:%M:%S")
    data = []
    first_short = None
    for to_user in recipients:
        mid = uuid.uuid4().hex
        if not first_short:
            first_short = mid[:8]
        data.append((mid, ts, ts_str, from_user, to_user, content))

    def do():
        with get_db() as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO messages (msg_id, ts, ts_str, from_user, to_user, content) VALUES (?, ?, ?, ?, ?, ?)",
                    data,
                )
    _db_retry(do)
    return first_short or "--------"

def _estimate_cost(row: sqlite3.Row) -> int:
    return len(row["content"] or "") + 60

def _lease_messages(my_name: str, max_chars: int = MAX_BATCH_CHARS):
    now = time.time()
    lease_until = now + LEASE_TTL

    def do():
        with get_db() as conn:
            with conn:
                # recover my expired inflight leases
                conn.execute("""
                    UPDATE messages
                    SET state='queued', lease_owner=NULL, lease_until=NULL
                    WHERE to_user=? AND state='inflight' AND lease_until IS NOT NULL AND lease_until < ?
                """, (my_name, now))

                # fetch a limited window to reduce cost under backlog
                rows = conn.execute("""
                    SELECT * FROM messages
                    WHERE to_user=? AND state='queued'
                    ORDER BY ts ASC
                    LIMIT 200
                """, (my_name,)).fetchall()

                chosen = []
                chosen_ids = []
                cost = 0
                for r in rows:
                    c = _estimate_cost(r)
                    if chosen and (cost + c > max_chars):
                        break
                    chosen.append(dict(r))
                    chosen_ids.append(r["msg_id"])
                    cost += c

                if chosen_ids:
                    placeholders = ",".join("?" * len(chosen_ids))
                    conn.execute(f"""
                        UPDATE messages
                        SET state='inflight',
                            lease_owner=?,
                            lease_until=?,
                            attempt=COALESCE(attempt,0)+1,
                            delivered_at=?
                        WHERE msg_id IN ({placeholders}) AND state='queued'
                    """, [my_name, lease_until, now, *chosen_ids])

                # remaining count (approx) - cheap:
                remaining = conn.execute("""
                    SELECT COUNT(1) AS c FROM messages WHERE to_user=? AND state='queued'
                """, (my_name,)).fetchone()["c"]
                remaining = max(0, int(remaining) - len(chosen_ids))
                return chosen, chosen_ids, remaining

    return _db_retry(do)

def _ack_messages(msg_ids: list[str], my_name: str):
    if not msg_ids:
        return
    def do():
        with get_db() as conn:
            with conn:
                placeholders = ",".join("?" * len(msg_ids))
                conn.execute(
                    f"DELETE FROM messages WHERE msg_id IN ({placeholders}) AND state='inflight' AND lease_owner=?",
                    [*msg_ids, my_name],
                )
    _db_retry(do)

def _release_leases(msg_ids: list[str], my_name: str):
    if not msg_ids:
        return
    def do():
        with get_db() as conn:
            with conn:
                placeholders = ",".join("?" * len(msg_ids))
                conn.execute(
                    f"UPDATE messages SET state='queued', lease_owner=NULL, lease_until=NULL "
                    f"WHERE msg_id IN ({placeholders}) AND state='inflight' AND lease_owner=?",
                    [*msg_ids, my_name],
                )
    try:
        _db_retry(do)
    except Exception:
        pass

def _format_msgs_grouped(msgs: list[dict], remaining: int) -> str:
    if not msgs:
        return "No messages."
    grouped = defaultdict(list)
    for m in msgs:
        grouped[m["from_user"]].append(m)
    senders = sorted(grouped.keys(), key=lambda s: min(mm["ts"] for mm in grouped[s]))
    lines = [f"=== {len(msgs)} messages from {len(grouped)} agent(s) ===\n"]
    for s in senders:
        ms = grouped[s]
        lines.append(f"[{s}] - {len(ms)} message(s)")
        for m in ms:
            lines.append(f"  {m['ts_str']} {m['content']}")
        lines.append("")
    if remaining > 0:
        lines.append(f"({remaining} more queued. Call recv() again)")
    return "\n".join(lines)

# ---------------- Leader maintenance ----------------

def _clean_dead_local_peers():
    # leader-only, same-host pid scan
    my_host = SESSION_HOST

    def do():
        with get_db() as conn:
            with conn:
                rows = conn.execute("SELECT id, pid FROM peers WHERE hostname=?", (my_host,)).fetchall()
                dead = []
                for r in rows:
                    pid = r["pid"]
                    pid = int(pid) if pid is not None else 0
                    if SESSION_NAME and r["id"] == SESSION_NAME:
                        continue
                    if pid and not _is_pid_alive(pid):
                        dead.append(r["id"])
                if dead:
                    placeholders = ",".join("?" * len(dead))
                    conn.execute(f"DELETE FROM peers WHERE id IN ({placeholders})", dead)
    try:
        _db_retry(do)
    except Exception:
        pass

def _clean_remote_and_prune():
    now = time.time()
    cutoff = now - HEARTBEAT_TTL
    msg_cutoff = now - MSG_TTL

    def do():
        with get_db() as conn:
            with conn:
                conn.execute("DELETE FROM peers WHERE last_seen < ?", (cutoff,))
                # clear stale "waiting" flags (e.g., agent crashed or timed out)
                conn.execute("""
                    UPDATE peers
                    SET recv_state=NULL, recv_started=NULL, recv_deadline=NULL, recv_wait_seconds=NULL, recv_last_touch=NULL
                    WHERE recv_state IS NOT NULL AND recv_deadline IS NOT NULL AND recv_deadline < ?
                """, (now,))
                conn.execute("""
                    UPDATE messages
                    SET state='queued', lease_owner=NULL, lease_until=NULL
                    WHERE state='inflight' AND lease_until IS NOT NULL AND lease_until < ?
                """, (now,))
                conn.execute("DELETE FROM messages WHERE ts < ?", (msg_cutoff,))
    try:
        _db_retry(do)
    except Exception:
        pass

def _checkpoint():
    try:
        with get_db() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("PRAGMA optimize;")
    except Exception:
        pass

def _maintenance_loop():
    """
    Everyone: heartbeat.
    One leader (lease lock): does heavy work.
    """
    name, pid = get_session()

    # stagger start to avoid lock herd
    time.sleep(random.random() * 3.0)

    last_local = 0.0
    last_remote = 0.0
    last_ckpt = 0.0
    last_leader_renew = 0.0
    is_leader = False

    while True:
        now = time.time()

        try:
            # 1) always heartbeat (light)
            _update_heartbeat(name, pid)

            # 2) leader acquire/renew (not too frequent)
            if (now - last_leader_renew) >= LEADER_RENEW_EVERY or is_leader:
                last_leader_renew = now
                is_leader = _try_acquire_or_renew_leader(name, pid)

            # 3) leader-only heavy tasks
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

        # base sleep + tiny jitter to desync
        time.sleep(HEARTBEAT_INTERVAL + random.random() * 0.2)

def _ensure_background_started():
    global _BG_STARTED
    if _BG_STARTED:
        return
    with _BG_LOCK:
        if _BG_STARTED:
            return
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
    """List online agents with role + state timers.

    Output format example:
      Agent 001 @ Server-A  [👑 LEADER | YOU | 🎧 Waiting (12s/86400s)]
        Agent 002 @ Server-B  [🛠 Working (18s)]
        Agent 003 @ Server-C  [🎧 Waiting (500s/3600s)]

    Semantics:
    - 🎧 Waiting: the agent is currently blocked inside recv(wait_seconds=...).
    - 🛠 Working: the agent is online but not currently in recv() waiting state (may be executing tasks or idle).
    - If Working exceeds ~30 minutes, a ❓ marker is shown to help spot "maybe stuck / still working?".
    """
    _mark_active()
    me, _ = get_session()
    peers = _get_active_peers()
    leader = _get_leader_owner()
    now = time.time()

    # Order: leader first, then YOU, then others
    def sort_key(p):
        if leader and p["id"] == leader:
            return (0, p["id"])
        if p["id"] == me:
            return (1, p["id"])
        return (2, p["id"])

    peers_sorted = sorted(peers, key=sort_key)

    lines = []
    for i, p in enumerate(peers_sorted):
        flags = []
        if leader and p["id"] == leader:
            flags.append("👑 LEADER")
        if p["id"] == me:
            flags.append("YOU")

        # state
        state_str = ""
        if (p.get("recv_state") == "waiting") and p.get("recv_started"):
            try:
                elapsed = max(0, int(now - float(p["recv_started"])))
            except Exception:
                elapsed = 0
            total = p.get("recv_wait_seconds") or 0
            if total and int(total) > 0:
                state_str = f"🎧 Waiting ({elapsed}s/{int(total)}s)"
            else:
                state_str = f"🎧 Waiting ({elapsed}s)"
        else:
            # default: working
            since = p.get("mode_since") or p.get("active_last_touch") or p.get("recv_last_touch") or None
            if since:
                try:
                    w_elapsed = max(0, int(now - float(since)))
                except Exception:
                    w_elapsed = 0
            else:
                w_elapsed = 0

            if w_elapsed >= 1800:  # 30 min
                state_str = f"❓ Working ({w_elapsed}s)"
            else:
                state_str = f"🛠 Working ({w_elapsed}s)"

        parts = []
        if flags:
            parts.extend(flags)
        parts.append(state_str)

        bracket = " | ".join(parts)

        host = p.get("host") or "UnknownHost"
        line = f"Agent {p['id']} @ {host}  [{bracket}]"

        # indent everyone except the first line (usually leader)
        if i > 0:
            line = "  " + line
        lines.append(line)

    return "\n".join(lines) if lines else "No active agents."
@mcp.tool()
def send(to: str, content: str) -> str:
    """Send a message. to='id' or 'all' (or comma-separated ids)."""
    _mark_active()
    name, _ = get_session()

    recipients = [r.strip() for r in (to or "").split(",") if r.strip()]
    peers = _get_active_peers()
    online_ids = [p["id"] for p in peers]

    if any(r.lower() == "all" for r in recipients):
        final = [pid for pid in online_ids if pid != name]
        if not final:
            return "No other agents online."
    else:
        if name in recipients:
            return "Error: cannot send to self."
        final = []
        for r in recipients:
            if r not in online_ids:
                return f"Error: Agent '{r}' offline."
            final.append(r)

    try:
        sid = _send_db_batch(name, final, content)
        return f"Sent (to {len(final)} agent(s), id={sid})"
    except Exception as e:
        return f"DB Error: {e}"

@mcp.tool()
async def recv(wait_seconds: int = 86400) -> str:
    """
    Receive messages.
    - Always attempts an immediate fetch.
    - For long waits, leader polls fast; followers poll slower by default (configurable).
    - While waiting, we write recv/listen status into `peers` for observability.
    """
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
        # immediate attempt
        first = await try_once()
        if first:
            return first
        if wait_seconds <= 0:
            return "No new messages."

        # mark "waiting" (only if we actually enter the wait loop)
        waiting_marked = True
        await asyncio.to_thread(_set_recv_waiting, name, int(wait_seconds))

        # polling cadence
        leader = _get_leader_owner()
        is_leader = (leader == name)
        poll_every = RECV_DB_POLL_EVERY_LEADER if (is_leader or not RECV_FAST_POLL_ONLY_FOR_LEADER) else RECV_DB_POLL_EVERY_FOLLOWER

        # stagger to avoid herd when multiple agents call recv at once
        jitter = (int(name) % 10) * 0.03 if name and name.isdigit() else random.random() * 0.2
        next_db_poll = time.monotonic() + jitter

        while True:
            if LAST_ACTIVE_TS != my_task_ts:
                return "Cancelled by new command."
            if time.monotonic() - start >= float(wait_seconds):
                return f"Timeout ({int(wait_seconds)}s)."

            now_m = time.monotonic()
            if now_m >= next_db_poll:
                next_db_poll = now_m + poll_every
                await asyncio.to_thread(_touch_recv_waiting, name)

                res = await try_once()
                if res:
                    return res

            await asyncio.sleep(RECV_TICK)

    except (asyncio.CancelledError, KeyboardInterrupt):
        if leased_ids:
            await asyncio.to_thread(_release_leases, leased_ids, name)
        return "Cancelled."
    except Exception as e:
        if leased_ids:
            await asyncio.to_thread(_release_leases, leased_ids, name)
        return f"Error: {e}"
    finally:
        if waiting_marked:
            try:
                await asyncio.to_thread(_clear_recv_waiting, name)
            except Exception:
                pass

if __name__ == "__main__":
    mcp.run()

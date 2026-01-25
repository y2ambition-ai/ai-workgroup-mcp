import os, sys, time, io, uuid, sqlite3, random, asyncio, platform, threading, atexit, signal
from pathlib import Path
from contextlib import contextmanager
from collections import defaultdict
from mcp.server.fastmcp import FastMCP

# =========================================================
# RootBridge - Stable Edition
# Goals:
# 1) Background heartbeat (auto online)
# 2) Safe local janitor (same host + same cwd + PID check)
# 3) Remote offline via TTL
# 4) Lease-based recv batching (no duplicates; unread won't be deleted)
# 5) Simple MCP tools: get_status / send / recv
# =========================================================

# --- 1) Basic IO (Windows UTF-8) ---
try:
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

mcp = FastMCP("RootBridge")

# --- 2) Config ---
BRIDGE_DB_VERSION = "v9_stable"
BRIDGE_DB_FILENAME = f"bridge_{BRIDGE_DB_VERSION}.db"

if sys.platform == "win32":
    PREFERRED_ROOT = Path("C:/mcp_msg_pool")
    FALLBACK_ROOT = Path("C:/Users/Public/mcp_msg_pool")
else:
    PREFERRED_ROOT = Path.home() / ".mcp_msg_pool"
    FALLBACK_ROOT = Path("/tmp/mcp_msg_pool")

HEARTBEAT_TTL = 300          # seconds: remote peers expire by time
MSG_TTL = 86400              # seconds: message retention
LEASE_TTL = 30               # seconds: inflight lease expiry
MAX_BATCH_CHARS = 5000       # message batch cap (approx)

RECV_TICK = 0.5              # asyncio sleep tick
RECV_DB_POLL_EVERY = 2.0     # poll interval
HEARTBEAT_INTERVAL = 10.0    # background heartbeat interval

# maintenance cadence
CLEAN_LOCAL_EVERY = 10.0     # seconds: pid-based local zombie cleanup
CLEAN_REMOTE_EVERY = 60.0    # seconds: ttl cleanup + message prune + lease recovery
CHECKPOINT_EVERY = 300.0     # seconds: WAL checkpoint (low frequency)

# --- 3) Global State ---
SESSION_NAME = None
SESSION_PID = os.getpid()
SESSION_HOST = platform.node()
SESSION_CWD = os.getcwd()
LAST_ACTIVE_TS = 0.0

_BG_STARTED = False
_BG_LOCK = threading.Lock()

def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [BRIDGE] {msg}", file=sys.stderr)

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
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        yield conn
    finally:
        if conn:
            conn.close()

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
    Reliable PID liveness check.

    Windows:
      - Use OpenProcess + GetExitCodeProcess (STILL_ACTIVE=259)
      - Access denied => treat as alive (avoid false deletes)

    POSIX:
      - os.kill(pid, 0) with errno handling
    """
    if not pid or int(pid) <= 0:
        return False

    # --- Windows ---
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259  # process is running

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)

            OpenProcess = k32.OpenProcess
            OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            OpenProcess.restype = wintypes.HANDLE

            GetExitCodeProcess = k32.GetExitCodeProcess
            GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            GetExitCodeProcess.restype = wintypes.BOOL

            CloseHandle = k32.CloseHandle
            CloseHandle.argtypes = [wintypes.HANDLE]
            CloseHandle.restype = wintypes.BOOL

            h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                err = ctypes.get_last_error()
                # 5 = Access is denied (process may exist but not queryable)
                if err == 5:
                    return True
                return False

            code = wintypes.DWORD()
            ok = GetExitCodeProcess(h, ctypes.byref(code))
            CloseHandle(h)

            if not ok:
                # Can't query exit code => be conservative
                return True

            return code.value == STILL_ACTIVE

        except Exception:
            # last-resort conservative fallback
            return True

    # --- POSIX ---
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        # Some systems raise generic OSError with errno=ESRCH when pid doesn't exist
        try:
            import errno
            if getattr(e, "errno", None) == errno.ESRCH:
                return False
            if getattr(e, "errno", None) == errno.EPERM:
                return True
        except Exception:
            pass
        return True

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
                    try:
                        conn.execute("ALTER TABLE peers ADD COLUMN hostname TEXT")
                    except Exception:
                        pass
                if not _table_has_column(conn, "peers", "cwd"):
                    try:
                        conn.execute("ALTER TABLE peers ADD COLUMN cwd TEXT")
                    except Exception:
                        pass

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
                # migrations
                for col, ddl in [
                    ("state", "ALTER TABLE messages ADD COLUMN state TEXT DEFAULT 'queued'"),
                    ("lease_owner", "ALTER TABLE messages ADD COLUMN lease_owner TEXT"),
                    ("lease_until", "ALTER TABLE messages ADD COLUMN lease_until REAL"),
                    ("attempt", "ALTER TABLE messages ADD COLUMN attempt INTEGER DEFAULT 0"),
                    ("delivered_at", "ALTER TABLE messages ADD COLUMN delivered_at REAL"),
                ]:
                    if not _table_has_column(conn, "messages", col):
                        try:
                            conn.execute(ddl)
                        except Exception:
                            pass

                conn.execute("CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_to_user_ts ON messages(to_user, ts);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_state ON messages(to_user, state, lease_until, ts);")

    try:
        _db_retry(_do)
    except Exception as e:
        log(f"Init Warning: {e}")

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
                    "UPDATE peers SET pid=?, hostname=?, last_seen=?, cwd=? WHERE id=?",
                    (pid, host, now, cwd, name),
                )
                if cur.rowcount == 0:
                    conn.execute(
                        "INSERT OR IGNORE INTO peers (id, pid, hostname, last_seen, cwd) VALUES (?, ?, ?, ?, ?)",
                        (name, pid, host, now, cwd),
                    )
    try:
        _db_retry(do)
    except Exception:
        pass

def _claim_id_atomic(pid: int) -> str:
    """
    Claim a 3-digit ID. If candidate exists but is stale (last_seen < now-HEARTBEAT_TTL),
    steal it safely.
    """
    now = time.time()
    cutoff = now - HEARTBEAT_TTL
    cwd = os.getcwd()
    host = SESSION_HOST

    for _ in range(5000):
        cid = f"{random.randint(1, 999):03d}"

        def attempt():
            with get_db() as conn:
                with conn:
                    try:
                        conn.execute(
                            "INSERT INTO peers (id, pid, hostname, last_seen, cwd) VALUES (?, ?, ?, ?, ?)",
                            (cid, pid, host, now, cwd),
                        )
                        return True
                    except sqlite3.IntegrityError:
                        row = conn.execute(
                            "SELECT last_seen FROM peers WHERE id=?",
                            (cid,),
                        ).fetchone()
                        if row and row["last_seen"] is not None and row["last_seen"] < cutoff:
                            cur = conn.execute(
                                "UPDATE peers SET pid=?, hostname=?, last_seen=?, cwd=? "
                                "WHERE id=? AND last_seen < ?",
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
        # immediate register
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

# signal handlers (best-effort)
try:
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
except Exception:
    pass
try:
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
except Exception:
    pass

def _get_active_peers(my_name: str):
    limit = time.time() - HEARTBEAT_TTL
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, hostname, cwd FROM peers WHERE last_seen > ? ORDER BY id",
            (limit,),
        ).fetchall()
    return [{"id": r["id"], "host": r["hostname"], "cwd": r["cwd"]} for r in rows]

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
    try:
        return len(row["content"] or "") + 60
    except Exception:
        return 120

def _lease_messages(my_name: str, max_chars: int = MAX_BATCH_CHARS):
    now = time.time()
    lease_until = now + LEASE_TTL

    def do():
        with get_db() as conn:
            with conn:
                # recover expired inflight for this user
                conn.execute("""
                    UPDATE messages
                    SET state='queued', lease_owner=NULL, lease_until=NULL
                    WHERE to_user=? AND state='inflight' AND lease_until IS NOT NULL AND lease_until < ?
                """, (my_name, now))

                rows = conn.execute("""
                    SELECT * FROM messages
                    WHERE to_user=? AND state='queued'
                    ORDER BY ts ASC
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
                        WHERE msg_id IN ({placeholders})
                    """, [my_name, lease_until, now, *chosen_ids])

                # remaining queued count (cheap: derive from rows)
                remaining = max(0, len(rows) - len(chosen_ids))
                return chosen, chosen_ids, remaining

    return _db_retry(do)

def _ack_messages(msg_ids: list[str], my_name: str):
    if not msg_ids:
        return

    def do():
        with get_db() as conn:
            with conn:
                placeholders = ",".join("?" * len(msg_ids))
                conn.execute(f"""
                    DELETE FROM messages
                    WHERE msg_id IN ({placeholders})
                      AND state='inflight'
                      AND lease_owner=?
                """, [*msg_ids, my_name])

    _db_retry(do)

def _release_leases(msg_ids: list[str], my_name: str):
    if not msg_ids:
        return

    def do():
        with get_db() as conn:
            with conn:
                placeholders = ",".join("?" * len(msg_ids))
                conn.execute(f"""
                    UPDATE messages
                    SET state='queued', lease_owner=NULL, lease_until=NULL
                    WHERE msg_id IN ({placeholders})
                      AND state='inflight'
                      AND lease_owner=?
                """, [*msg_ids, my_name])

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

    # stable sender order by first ts
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

# ---------------- Background Maintenance ----------------

def _clean_dead_local_peers():
    """
    Local janitor:
    Checks ALL peers on SAME host (any cwd); if PID is dead => delete.
    This cleans up dead agents regardless of which directory/project they ran in.
    """
    my_host = SESSION_HOST

    def do():
        with get_db() as conn:
            with conn:
                rows = conn.execute(
                    "SELECT id, pid FROM peers WHERE hostname=?",  # 同机器，所有目录
                    (my_host,),
                ).fetchall()

                dead = []
                for r in rows:
                    if SESSION_NAME and r["id"] == SESSION_NAME:
                        continue
                    pid = r["pid"]
                    if pid is None:
                        continue
                    if not _is_pid_alive(int(pid)):
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
                # expire peers by time (covers remote crashes, power loss, etc.)
                conn.execute("DELETE FROM peers WHERE last_seen < ?", (cutoff,))

                # recover any expired inflight leases (all users)
                conn.execute("""
                    UPDATE messages
                    SET state='queued', lease_owner=NULL, lease_until=NULL
                    WHERE state='inflight' AND lease_until IS NOT NULL AND lease_until < ?
                """, (now,))

                # prune old messages
                conn.execute("DELETE FROM messages WHERE ts < ?", (msg_cutoff,))

    try:
        _db_retry(do)
    except Exception:
        pass

def _checkpoint():
    def do():
        with get_db() as conn:
            with conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.execute("PRAGMA optimize;")
    try:
        _db_retry(do)
    except Exception:
        pass

def _maintenance_loop():
    name, pid = get_session()

    last_local = 0.0
    last_remote = 0.0
    last_ckpt = 0.0

    # initial sweep
    _clean_dead_local_peers()
    _clean_remote_and_prune()

    while True:
        now = time.time()
        try:
            _update_heartbeat(name, pid)

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

        time.sleep(HEARTBEAT_INTERVAL)

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

# auto-register + background heartbeat at import-time
get_session()
_ensure_background_started()

# ---------------- MCP Tools ----------------

@mcp.tool()
def get_status() -> str:
    """
    Show your ID and currently online agents.
    """
    _mark_active()
    name, _ = get_session()
    peers = _get_active_peers(name)

    lines = [f"YOU: {name} @ {SESSION_HOST}", f"ONLINE: {len(peers)}"]
    for p in peers:
        marker = " - YOU" if p["id"] == name else ""
        lines.append(f"  {p['id']} @ {p['host']} ({p['cwd']}){marker}")
    return "\n".join(lines)

@mcp.tool()
def send(to: str, content: str) -> str:
    """
    Send a message.
    - to="123" or to="123,456" or to="all" (broadcast to all online except yourself)
    """
    _mark_active()
    name, _ = get_session()

    recipients = [r.strip() for r in (to or "").split(",") if r.strip()]
    peers = _get_active_peers(name)
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
async def recv(wait_seconds: int = 3600) -> str:
    """
    Receive messages (lease + batch). Unread messages are not deleted.
    If the client forcefully aborts the tool call and the MCP link closes, restart MCP to reconnect.
    """
    _mark_active()
    name, _ = get_session()
    start = time.monotonic()
    my_task_ts = LAST_ACTIVE_TS
    leased_ids: list[str] = []

    async def try_once():
        nonlocal leased_ids
        msgs, leased_ids, remaining = await asyncio.to_thread(_lease_messages, name, MAX_BATCH_CHARS)
        if not msgs:
            leased_ids = []
            return None
        out = _format_msgs_grouped(msgs, remaining)
        # implicit ack
        await asyncio.to_thread(_ack_messages, leased_ids, name)
        leased_ids = []
        return out

    try:
        first = await try_once()
        if first:
            return first
        if wait_seconds <= 0:
            return "No new messages."

        next_poll = time.monotonic() + RECV_DB_POLL_EVERY
        while True:
            if LAST_ACTIVE_TS != my_task_ts:
                return "Cancelled by new command."
            if time.monotonic() - start >= float(wait_seconds):
                return f"Timeout ({int(wait_seconds)}s)."

            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + RECV_DB_POLL_EVERY
                res = await try_once()
                if res:
                    return res

            await asyncio.sleep(RECV_TICK)

    except asyncio.CancelledError:
        if leased_ids:
            await asyncio.to_thread(_release_leases, leased_ids, name)
        return "Cancelled."
    except KeyboardInterrupt:
        if leased_ids:
            await asyncio.to_thread(_release_leases, leased_ids, name)
        return "Cancelled."
    except Exception as e:
        if leased_ids:
            await asyncio.to_thread(_release_leases, leased_ids, name)
        return f"Error: {e}"

if __name__ == "__main__":
    mcp.run()

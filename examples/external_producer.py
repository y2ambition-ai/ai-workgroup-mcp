#!/usr/bin/env python
"""
Bridge MCP External Producer Example

This script demonstrates how external code can write messages directly
to the Bridge MCP SQLite database (DB-as-API pattern).

WARNING: For local, controlled environments only.
"""

import sqlite3
import time
import uuid
from pathlib import Path

# Database configuration (matches bridge.py)
PREFERRED_ROOT = Path("C:/mcp_msg_pool")
FALLBACK_ROOT = Path("C:/Users/Public/mcp_msg_pool")
DB_FILENAME = "bridge_v4.db"

HEARTBEAT_TTL = 300  # 5 minutes

def get_db_path():
    """Get the database path, trying preferred then fallback."""
    preferred = PREFERRED_ROOT / DB_FILENAME
    if preferred.exists():
        return preferred
    return FALLBACK_ROOT / DB_FILENAME

def get_online_peers(conn):
    """Get list of online peer IDs."""
    limit = time.time() - HEARTBEAT_TTL
    cur = conn.execute("SELECT id FROM peers WHERE last_seen > ? ORDER BY id", (limit,))
    return [row[0] for row in cur.fetchall()]

def broadcast_to_online(content, from_user="BOT"):
    """Broadcast a message to all currently online peers."""
    db_path = get_db_path()
    if not db_path.exists():
        print(f"Error: DB not found at {db_path}")
        return
    
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=5000;")
        
        # Get online peers
        peers = get_online_peers(conn)
        if not peers:
            print("No online peers found.")
            return
        
        # Broadcast to each online peer
        msg_id_base = uuid.uuid4().hex[:8]
        ts = time.time()
        ts_str = time.strftime("%H:%M:%S")
        
        for peer_id in peers:
            msg_id = f"{msg_id_base}_{peer_id}"
            conn.execute("""
                INSERT INTO messages (msg_id, ts, ts_str, from_user, to_user, content, is_broadcast)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (msg_id, ts, ts_str, from_user, peer_id, content, 1))
        
        conn.commit()
        print(f"Broadcasted to {len(peers)} online peer(s): {peers}")
        
    except sqlite3.Error as e:
        print(f"DB Error: {e}")
    finally:
        if conn:
            conn.close()

def dm(to_id, content, from_user="BOT"):
    """Send a direct message to a specific peer ID."""
    db_path = get_db_path()
    if not db_path.exists():
        print(f"Error: DB not found at {db_path}")
        return
    
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=5000;")
        
        msg_id = uuid.uuid4().hex[:8]
        ts = time.time()
        ts_str = time.strftime("%H:%M:%S")
        
        conn.execute("""
            INSERT INTO messages (msg_id, ts, ts_str, from_user, to_user, content, is_broadcast)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (msg_id, ts, ts_str, from_user, to_id, content, 0))
        
        conn.commit()
        print(f"Sent DM to {to_id}")
        
    except sqlite3.Error as e:
        print(f"DB Error: {e}")
    finally:
        if conn:
            conn.close()

# Example usage
if __name__ == "__main__":
    # Example 1: Broadcast to all online agents
    broadcast_to_online("Hello from external BOT! This is a DB-as-API test.")
    
    # Example 2: Send a direct message
    # dm("001", "Direct message from external script.")
    
    # Example 3: Simulated alert
    # broadcast_to_online("ALERT: Build completed successfully.")

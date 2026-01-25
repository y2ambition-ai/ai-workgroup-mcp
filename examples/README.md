# External Producer Example

This directory demonstrates the **"DB-as-API"** pattern:
external scripts can write directly to the Bridge MCP SQLite database
to inject messages, events, or broadcasts without adding new MCP tools.

## Use Case

- Automated bots/producers that push notifications into the AI workgroup
- External systems that need to trigger agent actions
- Scheduled tasks that broadcast alerts

## Files

- `external_producer.py` — Minimal example showing how to write to the DB

## v9_stable Schema Notes

**Database file:** `bridge_v9_stable.db` (updated from v4)

**Schema changes in v9_stable:**
- Messages table uses `state` ('queued'/'inflight') instead of `is_broadcast`
- New columns: `lease_owner`, `lease_until`, `attempt`, `delivered_at`
- Messages start in 'queued' state and are leased during `recv()`

**Example functions:**
- `broadcast_to_online(content)` — Send to all currently online peers
- `dm(to_id, content)` — Direct message to a specific peer
- `multicast(to_ids, content)` — Send to multiple peers (comma-separated)

## Security Note

**This example is intended for local, controlled environments only.**
The database path defaults to `C:\mcp_msg_pool\bridge_v9_stable.db` (Windows).

Do not expose the database file to untrusted networks or users.

## Design Note

**External producers are where semantic routing/auto-dispatch logic lives.**

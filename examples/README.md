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

## Security Note

**This example is intended for local, controlled environments only.**
The database path defaults to `C:\mcp_msg_pool\bridge_v4.db` (Windows).

Do not expose the database file to untrusted networks or users.

## Design Note

**External producers are where semantic routing/auto-dispatch logic lives.**
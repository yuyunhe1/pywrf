"""Small SQLite repository for saved route plans."""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "routes.sqlite3"

def _connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE IF NOT EXISTS routes (route_id TEXT PRIMARY KEY, name TEXT NOT NULL, start_json TEXT NOT NULL, end_json TEXT NOT NULL, points_json TEXT NOT NULL, level TEXT NOT NULL, cycle TEXT, forecast_hour INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    return connection

def _row(row):
    if row is None: return None
    item = dict(row)
    for key in ("start", "end", "points"): item[key] = json.loads(item.pop(f"{key}_json"))
    return item

def list_routes():
    with _connect() as db: return [_row(row) for row in db.execute("SELECT * FROM routes ORDER BY updated_at DESC")]

def get_route(route_id):
    with _connect() as db: return _row(db.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone())

def save_route(payload, route_id=None):
    now = datetime.now(timezone.utc).isoformat()
    route_id = route_id or str(uuid.uuid4())
    with _connect() as db:
        exists = db.execute("SELECT created_at FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route_id and exists is None and payload.get("_update"): return None
        created = exists["created_at"] if exists else now
        db.execute("INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?,?,?,?,?,?)", (route_id, payload["name"], json.dumps(payload["start"]), json.dumps(payload["end"]), json.dumps(payload["points"]), payload["level"], payload.get("cycle"), payload.get("forecast_hour"), created, now))
    return get_route(route_id)

def delete_route(route_id):
    with _connect() as db: return db.execute("DELETE FROM routes WHERE route_id=?", (route_id,)).rowcount > 0

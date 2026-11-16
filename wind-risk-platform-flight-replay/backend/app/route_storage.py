"""Small SQLite repository for saved route plans."""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "routes.sqlite3"

def _connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE IF NOT EXISTS routes (route_id TEXT PRIMARY KEY, name TEXT NOT NULL, start_json TEXT NOT NULL, end_json TEXT NOT NULL, points_json TEXT NOT NULL, level TEXT NOT NULL, cycle TEXT, forecast_hour INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, mission_items_json TEXT)")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(routes)")}
    if "mission_items_json" not in columns:
        connection.execute("ALTER TABLE routes ADD COLUMN mission_items_json TEXT")
    if "v3_json" not in columns:
        connection.execute("ALTER TABLE routes ADD COLUMN v3_json TEXT")
    if "source" not in columns:
        connection.execute("ALTER TABLE routes ADD COLUMN source TEXT NOT NULL DEFAULT 'wrf'")
    for name,kind in [('forecast_minutes','INTEGER'),('valid_time','TEXT')]:
        if name not in columns: connection.execute(f'ALTER TABLE routes ADD COLUMN {name} {kind}')
    return connection

def _row(row):
    if row is None: return None
    item = dict(row)
    for key in ("start", "end", "points"): item[key] = json.loads(item.pop(f"{key}_json"))
    mission_items = item.pop("mission_items_json", None)
    item["mission_items"] = json.loads(mission_items) if mission_items else None
    v3 = item.pop("v3_json", None)
    item["v3"] = json.loads(v3) if v3 else None
    if item.get('forecast_minutes') is None and item.get('forecast_hour') is not None:
        item['forecast_minutes']=round(item['forecast_hour']*60)
    if not item.get('valid_time') and item.get('cycle') and item.get('forecast_minutes') is not None:
        try:
            cycle=datetime.strptime(item['cycle'],'%Y-%m-%d %H:%M UTC')
            item['valid_time']=(cycle+timedelta(minutes=item['forecast_minutes'])).strftime('%Y-%m-%d %H:%M UTC')
        except ValueError:pass  # Older/imported records can use a noncanonical cycle label.
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
        db.execute(
            "INSERT OR REPLACE INTO routes (route_id, name, start_json, end_json, points_json, level, cycle, forecast_hour, created_at, updated_at, mission_items_json, v3_json, source, forecast_minutes, valid_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                route_id,
                payload["name"],
                json.dumps(payload["start"]),
                json.dumps(payload["end"]),
                json.dumps(payload["points"]),
                payload["level"],
                payload.get("cycle"),
                payload.get("forecast_hour"),
                created,
                now,
                json.dumps(payload.get("mission_items")) if payload.get("mission_items") else None,
                json.dumps(payload.get("v3")) if payload.get("v3") else None,
                payload.get("source") or "wrf",
                payload.get('forecast_minutes'),
                payload.get('valid_time'),
            ),
        )
    return get_route(route_id)

def update_route_name(route_id, name):
    payload = get_route(route_id)
    if payload is None:
        return None
    payload["name"] = name
    payload["_update"] = True
    return save_route(payload, route_id)

def delete_route(route_id):
    with _connect() as db: return db.execute("DELETE FROM routes WHERE route_id=?", (route_id,)).rowcount > 0

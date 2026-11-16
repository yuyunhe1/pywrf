"""Identity for a transient route; evaluating a plan never persists a route."""
import hashlib
import json

from .departure_windows import _geometry


def geometry_route(points, level, name="新规划航线", source="wrf"):
    cleaned, _ = _geometry(points)
    key = json.dumps([level, cleaned] if source == "wrf" else [level, cleaned, source], separators=(",", ":"), allow_nan=False)
    identifier = "planned-" + hashlib.sha256(key.encode()).hexdigest()[:24]
    return {"route_id": identifier, "points": cleaned, "level": level, "name": name, "ephemeral": True, "source": source}

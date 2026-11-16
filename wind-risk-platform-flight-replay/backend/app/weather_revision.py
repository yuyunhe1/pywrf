"""Forecast identity shared by selectors and version-addressed display frames."""
import hashlib
import json


def forecast_revision(source):
    from . import wrf_cache_provider as wrf, gfs_provider as gfs
    index = wrf.load_index() if source == 'wrf' else {}
    paths = ([wrf.cache_dir() / row['path'] for row in index.get('files', [])]
             if source == 'wrf' else [row.path for row in gfs.discover_files()])
    revisions = []
    for path in paths:
        if path.is_file():
            stat = path.stat()
            revisions.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns))
    identity = (index.get('wind_time_model'), sorted((r['path'], r.get('sha256')) for r in index.get('files', [])),
                sorted(revisions)) if source == 'wrf' else sorted(revisions)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]

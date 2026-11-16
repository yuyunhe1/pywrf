"""Preserve legacy imports while routing the original CLI to native production."""
MARKER='/root/pyWRF-automation/operations/native_wrf_10min/active.json'
BEGIN='# BEGIN NATIVE_WRF_CLI_DISPATCH\n'
END='# END NATIVE_WRF_CLI_DISPATCH\n'


def guarded_source(original):
    text=original.decode('utf-8')
    assert BEGIN not in text
    point='from __future__ import annotations\n'
    assert text.count(point)==1
    block='''
# BEGIN NATIVE_WRF_CLI_DISPATCH
if __name__ == "__main__":
    import json as _native_json
    import runpy as _native_runpy
    import sys as _native_sys
    from pathlib import Path as _NativePath
    _native_marker = _NativePath("%s")
    if _native_marker.is_file():
        _native_config = _native_json.loads(_native_marker.read_text(encoding="utf-8"))
        _native_directory = _NativePath(_native_config["directory"]).resolve()
        if _native_marker.parent.resolve() not in _native_directory.parents:
            raise RuntimeError("Invalid native producer directory")
        _native_sys.path.insert(0, str(_native_directory))
        _native_runpy.run_path(str(_native_directory / "run_native.py"), run_name="__main__")
        raise SystemExit(0)
# END NATIVE_WRF_CLI_DISPATCH
'''%MARKER
    return text.replace(point,point+block,1).encode('utf-8')

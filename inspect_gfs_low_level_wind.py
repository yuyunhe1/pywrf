import argparse
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def find_wgrib2(path):
    if path:
        return path
    found = shutil.which("wgrib2")
    if not found:
        raise SystemExit("未找到 wgrib2。请安装 wgrib2，或使用 --wgrib2 指定路径。")
    return found


def build_env(args):
    env = os.environ.copy()
    lib_dirs = []
    if args.library_dir:
        lib_dirs.append(args.library_dir)
    if env.get("CONDA_PREFIX"):
        lib_dirs.append(str(Path(env["CONDA_PREFIX"]) / "lib"))
    if args.wgrib2:
        wgrib2_path = Path(args.wgrib2).resolve()
        lib_dirs.append(str(wgrib2_path.parent.parent / "lib"))

    existing = env.get("LD_LIBRARY_PATH")
    merged = []
    for item in lib_dirs:
        if item and item not in merged:
            merged.append(item)
    if existing:
        merged.append(existing)
    if merged:
        env["LD_LIBRARY_PATH"] = ":".join(merged)
    return env


def run_wgrib2_inventory(path, args):
    wgrib2 = find_wgrib2(args.wgrib2)
    result = subprocess.run(
        [wgrib2, str(path)],
        capture_output=True,
        text=True,
        env=build_env(args),
    )
    if result.returncode != 0:
        raise SystemExit(
            f"wgrib2 执行失败: {wgrib2}\n"
            f"file: {path}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.splitlines()


def parse_inventory(lines):
    records = []
    for line in lines:
        parts = line.split(":")
        if len(parts) < 5:
            continue
        var = parts[3].strip()
        level = parts[4].strip()
        forecast = parts[5].strip() if len(parts) > 5 else ""
        records.append({"var": var, "level": level, "forecast": forecast, "line": line})
    return records


def height_value(level):
    match = re.match(r"(\d+(?:\.\d+)?)\s+m\s+above ground$", level)
    if not match:
        return None
    return float(match.group(1))


def inspect_file(path, args):
    lines = run_wgrib2_inventory(path, args)
    records = parse_inventory(lines)
    wind_records = [r for r in records if r["var"] in {"UGRD", "VGRD"}]
    above_ground = [r for r in wind_records if "above ground" in r["level"]]

    by_var = defaultdict(set)
    for record in above_ground:
        by_var[record["var"]].add(record["level"])

    print("=" * 100)
    print(f"File: {path}")
    print(f"Total GRIB messages: {len(records)}")
    print(f"UGRD/VGRD messages: {len(wind_records)}")
    print("")
    print("[UGRD/VGRD above-ground levels]")
    for var in ("UGRD", "VGRD"):
        levels = sorted(by_var[var], key=lambda item: (height_value(item) is None, height_value(item) or 999999, item))
        print(f"{var}: {', '.join(levels) if levels else 'None'}")

    low_levels = sorted(
        {r["level"] for r in above_ground},
        key=lambda item: (height_value(item) is None, height_value(item) or 999999, item),
    )
    print("")
    print("[All unique above-ground wind levels]")
    for level in low_levels:
        print(f"  {level}")

    if args.show_lines:
        print("")
        print("[Matched inventory lines]")
        for record in above_ground:
            print(record["line"])


def main():
    parser = argparse.ArgumentParser(description="检查 GFS GRIB2 中 UGRD/VGRD 的 near-surface/above-ground 高度层。")
    parser.add_argument("grib", nargs="?", default=None, help="原始全球 GRIB2 文件路径")
    parser.add_argument("--input-dir", default="./data/gdex_gfs_0p25_global", help="未指定 grib 时，从该目录找第一个 GRIB2")
    parser.add_argument("--wgrib2", default=None)
    parser.add_argument("--library-dir", default=None, help="额外加入 LD_LIBRARY_PATH 的 lib 目录")
    parser.add_argument("--show-lines", action="store_true", help="打印匹配到的 wgrib2 原始清单行")
    args = parser.parse_args()

    if args.grib:
        path = Path(args.grib)
    else:
        files = sorted(Path(args.input_dir).rglob("*.grib2"))
        if not files:
            raise SystemExit(f"未在 {args.input_dir} 找到 GRIB2 文件。")
        path = files[0]

    if not path.exists():
        raise SystemExit(f"文件不存在: {path}")
    inspect_file(path, args)


if __name__ == "__main__":
    main()

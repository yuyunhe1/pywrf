from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path
import logging
import os
import re
import requests
import shutil
import subprocess
import sys
import time

# For logging purposes
logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)

GDEX_GFS_PATTERN = re.compile(r"gfs\.0p25\.(\d{10})\.f(\d{3})\.grib2$")


def runtime_environment():
    env = os.environ.copy()
    library_dirs = []
    conda_prefix = env.get("CONDA_PREFIX")
    if not conda_prefix and Path(sys.prefix, "conda-meta").is_dir():
        conda_prefix = sys.prefix
        env["CONDA_PREFIX"] = conda_prefix
    for variable in ["CONDA_PREFIX", "NETCDF"]:
        prefix = conda_prefix if variable == "CONDA_PREFIX" else env.get(variable)
        if prefix:
            library_dirs.append(str(Path(prefix, "lib")))
    if env.get("JASPERLIB"):
        library_dirs.append(env["JASPERLIB"])

    existing = env.get("LD_LIBRARY_PATH", "").split(os.pathsep)
    paths = []
    for value in library_dirs + existing:
        if value and value not in paths:
            paths.append(value)
    if paths:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(paths)
    return env


def check_shared_libraries(command, cwd: str, label: str, env):
    ldd = shutil.which("ldd")
    executable = next(
        (Path(cwd, value) for value in command if Path(cwd, value).is_file()),
        None,
    )
    if ldd is None or executable is None:
        return

    result = subprocess.run(
        [ldd, str(executable.resolve())],
        capture_output=True,
        text=True,
        env=env,
    )
    output = "\n".join(part for part in [result.stdout, result.stderr] if part)
    missing = [line.strip() for line in output.splitlines() if "not found" in line]
    if missing:
        conda_prefix = env.get("CONDA_PREFIX", "<not set>")
        sys.exit(
            f"ERROR: {label} cannot start because shared libraries are missing:\n  "
            + "\n  ".join(missing)
            + f"\nCONDA_PREFIX={conda_prefix}"
            + f"\nLD_LIBRARY_PATH={env.get('LD_LIBRARY_PATH', '<not set>')}"
            + "\nActivate pywrf and run: source /root/pyWRF-automation/setup_wrf_env.sh"
            + "\nThen inspect: ls -l \"$CONDA_PREFIX\"/lib/libnetcdf.so*"
        )


def parse_gdex_gfs_file(path):
    match = GDEX_GFS_PATTERN.fullmatch(Path(path).name)
    if not match:
        return None

    cycle_time = datetime.strptime(match.group(1), "%Y%m%d%H")
    forecast_hour = int(match.group(2))
    return cycle_time, forecast_hour, cycle_time + timedelta(hours=forecast_hour)


def select_gdex_gfs_files(gfs_path: str, start_date: datetime, end_date: datetime, interval_seconds: int = 10800, gfs_cycle_time: datetime = None):
    if interval_seconds <= 0:
        sys.exit("ERROR: WPS - interval_seconds must be greater than zero")

    duration_seconds = int((end_date - start_date).total_seconds())
    if duration_seconds < 0 or duration_seconds % interval_seconds != 0:
        sys.exit("ERROR: WPS - simulation time range must align with interval_seconds")

    candidates = defaultdict(list)
    for path in Path(gfs_path).rglob("gfs.0p25.*.f*.grib2"):
        parsed = parse_gdex_gfs_file(path)
        if parsed is None:
            continue
        cycle_time, forecast_hour, valid_time = parsed
        if gfs_cycle_time is not None and cycle_time != gfs_cycle_time:
            continue
        if start_date <= valid_time <= end_date:
            candidates[valid_time].append((forecast_hour, cycle_time, path.resolve()))

    expected_times = [
        start_date + timedelta(seconds=offset)
        for offset in range(0, duration_seconds + 1, interval_seconds)
    ]
    missing_times = [valid_time for valid_time in expected_times if not candidates[valid_time]]
    if missing_times:
        formatted = ", ".join(item.strftime("%Y-%m-%d_%H:%M:%S") for item in missing_times)
        sys.exit(
            f"ERROR: WPS - missing GDEX GFS input for {len(missing_times)} valid time(s): {formatted}"
        )

    selected = []
    for valid_time in expected_times:
        # Prefer the shortest forecast lead; use the latest cycle to break ties.
        forecast_hour, cycle_time, path = sorted(
            candidates[valid_time], key=lambda item: (item[0], -item[1].timestamp())
        )[0]
        selected.append(path)
        logging.info(
            "INFO: WPS - selected %s (cycle=%s, forecast=f%03d, valid=%s)",
            path,
            cycle_time.strftime("%Y-%m-%d_%H"),
            forecast_hour,
            valid_time.strftime("%Y-%m-%d_%H"),
        )

    return selected


def link_grib_files(wps_path: str, gfs_files):
    if len(gfs_files) > 26 ** 3:
        sys.exit("ERROR: WPS - more than 17576 GRIB files cannot be linked")

    root = Path(wps_path)
    for index, source in enumerate(gfs_files):
        suffix = "".join(
            chr(ord("A") + value)
            for value in ((index // 676) % 26, (index // 26) % 26, index % 26)
        )
        Path(root, f"GRIBFILE.{suffix}").symlink_to(source)


def remove_matching_files(path: str, patterns):
    root = Path(path)
    for pattern in patterns:
        for item in root.glob(pattern):
            if item.is_symlink() or item.is_file():
                item.unlink()


def run_checked(command, cwd: str, label: str):
    env = runtime_environment()
    check_shared_libraries(command, cwd, label, env)
    result = subprocess.run(command, cwd=cwd, env=env)
    if result.returncode != 0:
        sys.exit(f"ERROR: {label} failed with exit code {result.returncode}")


WRF_TIMING_PATTERN = re.compile(
    r"Timing for main:\s+time\s+"
    r"(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})\s+on domain\s+1"
)


def latest_wrf_model_time(rsl_error_path):
    path = Path(rsl_error_path)
    if not path.exists():
        return None

    latest = None
    with path.open("r", errors="replace") as file:
        for line in file:
            match = WRF_TIMING_PATTERN.search(line)
            if match:
                latest = datetime.strptime(match.group(1), "%Y-%m-%d_%H:%M:%S")
    return latest


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h{minutes:02d}m"
    return f"{minutes:d}m{seconds:02d}s"


def wrf_progress_text(wrf_path: str, start_date: datetime, end_date: datetime, wall_start: float):
    rsl_error = Path(wrf_path, "rsl.error.0000")
    model_time = latest_wrf_model_time(rsl_error)
    wall_elapsed = time.time() - wall_start

    if model_time is None:
        if rsl_error.exists():
            age = time.time() - rsl_error.stat().st_mtime
            return f"WRF initializing; rsl.error.0000 last updated {format_duration(age)} ago"
        return "WRF initializing; waiting for rsl.error.0000"

    total = max(1.0, (end_date - start_date).total_seconds())
    completed = min(total, max(0.0, (model_time - start_date).total_seconds()))
    fraction = completed / total
    width = 30
    filled = min(width, int(fraction * width))
    bar = "#" * filled + "-" * (width - filled)
    eta = None
    if fraction > 0:
        eta = wall_elapsed * (1.0 - fraction) / fraction
    eta_text = format_duration(eta) if eta is not None else "calculating"
    return (
        f"[{bar}] {fraction * 100:6.2f}% "
        f"model={model_time:%Y-%m-%d_%H:%M:%S} "
        f"elapsed={format_duration(wall_elapsed)} ETA={eta_text}"
    )


def run_wrf_with_progress(command, cwd: str, start_date: datetime, end_date: datetime, poll_seconds: int = 5):
    env = runtime_environment()
    check_shared_libraries(command, cwd, "WRF Model - wrf.exe", env)
    process = subprocess.Popen(command, cwd=cwd, env=env)
    wall_start = time.time()
    previous_length = 0

    while process.poll() is None:
        text = wrf_progress_text(cwd, start_date, end_date, wall_start)
        padding = " " * max(0, previous_length - len(text))
        print(f"\r{text}{padding}", end="", flush=True)
        previous_length = len(text)
        time.sleep(poll_seconds)

    text = wrf_progress_text(cwd, start_date, end_date, wall_start)
    padding = " " * max(0, previous_length - len(text))
    print(f"\r{text}{padding}")
    if process.returncode != 0:
        sys.exit(f"ERROR: WRF Model - wrf.exe failed with exit code {process.returncode}")


def mpi_command(num_proc: int, executable: str):
    command = ["mpirun"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.append("--allow-run-as-root")
    command.extend(["-np", str(num_proc), executable])
    return command


def check_rsl_success(wrf_path: str, success_message: str, executable: str):
    rsl_error = Path(wrf_path, "rsl.error.0000")
    if not rsl_error.exists():
        sys.exit(
            f"ERROR: WRF Model - {executable} did not create rsl.error.0000; "
            "check the MPI error printed above"
        )

    content = rsl_error.read_text(errors="replace")
    if success_message not in content:
        tail = "\n".join(content.splitlines()[-20:])
        sys.exit(
            f"ERROR: WRF Model - {executable} did not finish successfully. "
            f"Last lines of rsl.error.0000:\n{tail}"
        )


def link_metgrid_files(wps_path: str, wrf_path: str):
    metgrid_files = sorted(Path(wps_path).glob("met_em.d0*.nc"))
    if not metgrid_files:
        sys.exit(f"ERROR: WRF Model - no met_em files found in {wps_path}")

    root = Path(wrf_path)
    for source in metgrid_files:
        Path(root, source.name).symlink_to(source.resolve())
    return len(metgrid_files)


def expected_valid_times(start_date: datetime, end_date: datetime, interval_seconds: int):
    duration_seconds = int((end_date - start_date).total_seconds())
    return [
        start_date + timedelta(seconds=offset)
        for offset in range(0, duration_seconds + 1, interval_seconds)
    ]


def validate_met_em_files(wps_path: str, max_dom: int, start_date: datetime, end_date: datetime, interval_seconds: int):
    ncdump = shutil.which("ncdump")
    if ncdump is None:
        logging.warning("WARNING: WPS - ncdump was not found; skipping met_em Times validation")
        return

    missing = []
    invalid = []
    for domain in range(1, max_dom + 1):
        for valid_time in expected_valid_times(start_date, end_date, interval_seconds):
            timestamp = valid_time.strftime("%Y-%m-%d_%H:%M:%S")
            path = Path(wps_path, f"met_em.d{domain:02d}.{timestamp}.nc")
            if not path.exists():
                missing.append(str(path))
                continue

            result = subprocess.run(
                [ncdump, "-v", "Times", str(path)],
                capture_output=True,
                text=True,
                env=runtime_environment(),
            )
            if result.returncode != 0 or f'"{timestamp}"' not in result.stdout:
                invalid.append(str(path))

    if missing or invalid:
        details = []
        if missing:
            details.append("missing files:\n  " + "\n  ".join(missing))
        if invalid:
            details.append(
                "files with an empty or incorrect Times record:\n  "
                + "\n  ".join(invalid)
            )
        sys.exit(
            "ERROR: WPS - met_em validation failed before real.exe. "
            + "\n".join(details)
        )

    logging.info("INFO: WPS - all met_em files and internal Times records are valid")


# Setup download worker
def gfs_download_worker(data):
    if not os.path.exists(data[1]):
        start_time = time.time()
        response = requests.get(data[0])
        with open(data[1], "wb") as f:
            f.write(response.content)
        end_time = time.time()
        logging.info(f"INFO: GFS Downloader - {Path(data[1]).name} has been downloaded in {int(end_time - start_time)} seconds")
    else:
        logging.info(f"INFO: GFS Downlaoder - File {Path(data[1]).name} is already exist, skipped")

# Function to download GFS dataset concurrently
def download_gfs(path: str, n_worker: int, start_date: datetime, forecast_time: int, increment: int, cycle_time: str, left_lon: float, right_lon: float, top_lat: float, bottom_lat: float):
    if forecast_time > 384:
        sys.exit("ERROR: GFS Downloader - Forecast time can't be more than 384")
    
    folder_path = f"{path}/{start_date.strftime('%Y-%m-%d')}"
    base_url = "https://nomads.ncep.noaa.gov/cgi-bin"
    year  = str(start_date.year)
    month = str("%02d" % (start_date.month))
    day   = str("%02d" % (start_date.day))

    if not(os.path.isdir(folder_path)):
        os.makedirs(folder_path)
    logging.info(f"INFO: GFS Downloader - Dataset will be saved in {folder_path}")

    list_url = [f"{base_url}/filter_gfs_0p25.pl?file=gfs.t{cycle_time}z.pgrb2.0p25.f{'%03d' % hour}&all_lev=on&all_var=on&subregion=&leftlon={str(left_lon)}&rightlon={str(right_lon)}&toplat={str(top_lat)}&bottomlat={str(bottom_lat)}&dir=%2Fgfs.{year}{month}{day}%2F{cycle_time}%2Fatmos" for hour in range(0, forecast_time + 1, increment)]
    list_filepath = [f"{folder_path}/gfs_4_{year}{month}{day}_{cycle_time}00_{'%03d' % hour}.grb2" for hour in range(0, forecast_time + 1, increment)]

    with ThreadPoolExecutor(max_workers = n_worker) as executor:
        executor.map(gfs_download_worker, zip(list_url, list_filepath))
    logging.info(f"INFO: GFS Downloader - Dataset with cycle time {cycle_time} has been downloaded")

# Function to execute WPS sequences
def run_wps(wps_path: str, gfs_path: str, namelist_wps_path: str, max_dom: int, start_date: datetime, end_date: datetime, opts = None, gfs_layout: str = "legacy", interval_seconds: int = 10800, gfs_cycle_time: datetime = None, reuse_geogrid: bool = False):
    if gfs_layout == "gdex":
        gfs_files = select_gdex_gfs_files(gfs_path, start_date, end_date, interval_seconds, gfs_cycle_time)
    elif gfs_layout == "legacy":
        gfs_files = sorted(Path(gfs_path, start_date.strftime("%Y-%m-%d")).glob("*"))
        if not gfs_files:
            sys.exit(f"ERROR: WPS - no GFS files found under {gfs_path}")
    else:
        sys.exit(f"ERROR: WPS - unsupported GFS layout: {gfs_layout}")

    wps_params = {
        "max_dom": str(max_dom),
        "start_date": start_date.strftime("%Y-%m-%d_%H:%M:%S"),
        "end_date": end_date.strftime("%Y-%m-%d_%H:%M:%S"),
        "interval_seconds": str(interval_seconds),
    }
    
    if opts:
        wps_params.update(opts)

    for key in ["parent_id", "parent_grid_ratio", "i_parent_start", "j_parent_start", "e_we", "e_sn"]:
        value = wps_params.get(key)
        if value != None and len(value.split(",")) != max_dom:
            sys.exit(f"Error: WPS - length of {key} value mismatched to max_dom parameter")

    with open(namelist_wps_path, "r") as file:
        lines = file.readlines()
        for i, line in enumerate(lines):
            for variable, value in wps_params.items():
                matched = re.search(rf"^\s*{re.escape(variable)}\s*=", line)
                if matched:
                    index_of_equal_sign = line.find("=")

                    if variable in ["wrf_core", "map_proj", "geog_data_path", "out_format", "prefix", "fg_name"]:
                        lines[i] = f"{line[:index_of_equal_sign + 1]} '{value}',\n"
                        continue

                    if variable in ["start_date", "end_date", "geog_data_res"]:
                        formatted = f"'{value}',"
                        lines[i] = f"{line[:index_of_equal_sign + 1]} {formatted * max_dom}\n"
                        continue
                    
                    lines[i] = f"{line[:index_of_equal_sign + 1]} {str(value)},\n"

    with open(namelist_wps_path, "w") as file:
        file.writelines(lines)
    
    logging.info(f"INFO: WPS - Configuration file updated")

    # Delete WPS products and links from previous run.
    cleanup_patterns = ["FILE*", "PFILE*", "met_em*", "GRIBFILE.*"]
    if not reuse_geogrid:
        cleanup_patterns.append("geo_em*")
    remove_matching_files(wps_path, cleanup_patterns)

    # Execute geogrid.exe
    expected_geo = [Path(wps_path, f"geo_em.d{domain:02d}.nc") for domain in range(1, max_dom + 1)]
    if reuse_geogrid and all(path.exists() for path in expected_geo):
        logging.info("INFO: WPS - reusing existing geo_em files")
    else:
        run_checked(["./geogrid.exe"], wps_path, "WPS - geogrid.exe")
        logging.info("INFO: WPS - geogrid.exe completed")

    # Create links only for GFS files required by this simulation period.
    link_grib_files(wps_path, gfs_files)
    logging.info(f"INFO: WPS - linked {len(gfs_files)} GFS file(s)")

    # Create a symlink to GFS Variable Table
    vtable = Path(wps_path, "Vtable")
    if vtable.is_symlink() or vtable.exists():
        vtable.unlink()
    vtable.symlink_to(Path(wps_path, "ungrib/Variable_Tables/Vtable.GFS"))
    logging.info("INFO: WPS - Symlink of Vtable.GFS created")
    
    # Execute ungrib.exe
    run_checked(["./ungrib.exe"], wps_path, "WPS - ungrib.exe")
    logging.info("INFO: WPS - ungrib.exe completed")

    # Execute metgrid.exe
    run_checked(["./metgrid.exe"], wps_path, "WPS - metgrid.exe")
    logging.info("INFO: WPS - metgrid.exe completed")

    validate_met_em_files(wps_path, max_dom, start_date, end_date, interval_seconds)
    logging.info("INFO: WPS - Process completed. met_em files is ready")

# Function to execute WRF model
def run_wrf(wps_path: str, wrf_path: str, wrfout_path: str, namelist_input_path: str, run_days: int, max_dom: int, start_date: datetime, end_date: datetime, num_proc: int, wrfout_saved_domain: int, opts = None, interval_seconds: int = 10800):
    duration_hours = int((end_date - start_date).total_seconds() / 3600)
    run_days, run_hours = divmod(duration_hours, 24)
    wrf_params = {
        "run_days": str(run_days),
        "run_hours": str(run_hours),
        "interval_seconds": str(interval_seconds),
        "start_year": str(start_date.year),
        "start_month": "%02d" % start_date.month,
        "start_day": "%02d" % start_date.day,
        "start_hour": "%02d" % start_date.hour,
        "end_year": str(end_date.year),
        "end_month": "%02d" % end_date.month,
        "end_day": "%02d" % end_date.day,
        "end_hour": "%02d" % end_date.hour,
        "max_dom": str(max_dom)
    }
    if opts:
        wrf_params.update(opts)

    for key in ["e_we", "e_sn", "e_vert", "dx", "dy", "grid_id", "parent_id", "i_parent_start", "j_parent_start", "parent_grid_ratio", "parent_time_step_ratio"]:
        value = wrf_params.get(key)
        if value != None and len(value.split(",")) != max_dom:
            sys.exit(f"Error: WRF Model - length of {key} value mismatched to max_dom parameter")

    if wrfout_saved_domain > max_dom:
        sys.exit(f"Error: WRF Model - Maximum saved WRF output file domain must be equal or lower to max_domain parameter")

    with open(namelist_input_path, "r") as file:
        lines = file.readlines()

        for i, line in enumerate(lines):
            for variable, value in wrf_params.items():
                matched = re.search(rf"^\s*{re.escape(variable)}\s*=", line)
                if matched:
                    index_of_equal_sign = line.find("=")
                    
                    # Change time_control parameter
                    if variable in ["start_year", "start_month", "start_day", "start_hour", "end_year", "end_month", "end_day", "end_hour"]:
                        lines[i] = f"{line[:index_of_equal_sign + 1]} {((value + ', ') * max_dom)}\n"
                        continue

                    lines[i] = f"{line[:index_of_equal_sign + 1]} {value},\n"

    with open(namelist_input_path, "w") as file:
        file.writelines(lines)

    logging.info("INFO: WRF Model - Configuration file updated")
    logging.info(f"INFO: WRF Model - Model will take a simulation from {start_date.strftime('%Y-%m-%d_%H:%M:%S')} to {end_date.strftime('%Y-%m-%d_%H:%M:%S')}")

    # Delete unused files from previous run.
    remove_matching_files(wrf_path, ["met_em*", "wrfout*", "wrfrst*", "rsl.error.*", "rsl.out.*"])

    # Create a new symlink to all metgrid files from WPS folder
    metgrid_count = link_metgrid_files(wps_path, wrf_path)
    logging.info(f"INFO: WRF Model - linked {metgrid_count} met_em file(s)")

    # Execute real.exe
    run_checked(mpi_command(num_proc, "./real.exe"), wrf_path, "WRF Model - real.exe")
    check_rsl_success(wrf_path, "SUCCESS COMPLETE REAL_EM INIT", "real.exe")
    logging.info("INFO: WRF Model - real.exe completed successfully")

    # Execute wrf.exe only after real.exe has completed successfully.
    remove_matching_files(wrf_path, ["rsl.error.*", "rsl.out.*"])
    run_wrf_with_progress(mpi_command(num_proc, "./wrf.exe"), wrf_path, start_date, end_date)
    check_rsl_success(wrf_path, "SUCCESS COMPLETE WRF", "wrf.exe")
    logging.info("INFO: WRF Model - Simulation completed successfully")

    # Move output to assigned location
    wrfout_folder_path = Path(wrfout_path, start_date.strftime("%Y-%m-%d"))
    wrfout_folder_path.mkdir(parents=True, exist_ok=True)
    output_files = sorted(Path(wrf_path).glob(f"wrfout_d0{wrfout_saved_domain}_*"))
    if not output_files:
        sys.exit(f"ERROR: WRF Model - no wrfout_d0{wrfout_saved_domain}_* files were generated")
    for output_file in output_files:
        shutil.move(str(output_file), str(wrfout_folder_path / output_file.name))
    logging.info(f"INFO: WRF Model - Simulation files on domain {wrfout_saved_domain} has been saved to {wrfout_folder_path}")

# Calculate execution time
def calculate_execution_time(start: float, stop: float):
    if stop - start < 60:
        execution_duration = ("%1d" % (stop - start))
        logging.info(f"INFO: Automation - Process completed in {execution_duration} seconds")
        sys.exit(0)
    elif stop - start < 3600:
        execution_duration = ("%1d" % ((stop - start) / 60))
        logging.info(f"INFO: Automation - Process completed in {execution_duration} minutes")
        sys.exit(0)
    else:
        execution_duration = ("%1d" % ((stop - start) / 3600))
        logging.info(f"INFO: Automation - Process complete in {execution_duration} hours")
        sys.exit(0)

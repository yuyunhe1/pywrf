""" 
WRF-ARW Model & GFS Automation System using Python 3
Credit : Muhamad Reza Pahlevi (@elpahlevi) & Agung Baruna Setiawan Noor (@agungbaruna)
If you find any trouble, reach the author via email : mr.levipahlevi@gmail.com 
"""

from datetime import datetime, timedelta
import os
import sys
import time

import utils

# Folder path
base_dir                = "/root/pyWRF-automation"
gfs_dir                 = f"{base_dir}/data/gdex_gfs_0p25_global"  # Existing global GDEX GFS files
wps_dir                 = f"{base_dir}/WPS"                 # Path to compiled WPS folder
wrf_dir                 = f"{base_dir}/WRF/test/em_real"    # Path to compiled WRF em_real folder
wrfout_dir              = f"{base_dir}/wrf_output"          # Path to wrfout folder
namelist_wps_file       = f"{wps_dir}/namelist.wps"         # Path to namelist.wps file
namelist_wrf_file       = f"{wrf_dir}/namelist.input"       # Path to namelist.input file

# WPS and WRF parameters
gfs_interval_seconds    = 10800 # GFS input interval: 3 hours
max_dom                 = 2     # Maximum WPS and WRF domain
num_proc                = int(os.environ.get("WRF_NUM_PROC", "4"))
wrfout_saved_domain     = 2     # which wrfout file will be saved

# Time parameters. All GFS/WPS times are UTC.
start_time              = time.time()
start_date_text         = os.environ.get("WRF_START_DATE")
forecast_hours          = int(os.environ.get("WRF_FORECAST_HOURS", "24"))
gfs_cycle_text          = os.environ.get("GFS_CYCLE_TIME")
check_gfs_only          = os.environ.get("CHECK_GFS_ONLY", "0") == "1"
wps_only                = os.environ.get("WPS_ONLY", "0") == "1"
reuse_geogrid           = os.environ.get("REUSE_GEOGRID", "0") == "1"

if not start_date_text:
    sys.exit(
        "ERROR: Set WRF_START_DATE first, for example: "
        "export WRF_START_DATE=2025-11-02_00:00:00"
    )

try:
    start_date = datetime.strptime(start_date_text, "%Y-%m-%d_%H:%M:%S")
except ValueError:
    sys.exit("ERROR: WRF_START_DATE must use format YYYY-MM-DD_HH:MM:SS")

try:
    gfs_cycle_time = datetime.strptime(gfs_cycle_text, "%Y-%m-%d_%H:%M:%S") if gfs_cycle_text else None
except ValueError:
    sys.exit("ERROR: GFS_CYCLE_TIME must use format YYYY-MM-DD_HH:MM:SS")

if forecast_hours <= 0:
    sys.exit("ERROR: WRF_FORECAST_HOURS must be greater than zero")

end_date = start_date + timedelta(hours=forecast_hours)

# Automation Sequences
if check_gfs_only:
    selected_files = utils.select_gdex_gfs_files(
        gfs_dir,
        start_date,
        end_date,
        gfs_interval_seconds,
        gfs_cycle_time,
    )
    logging_message = f"INFO: GFS check completed; {len(selected_files)} required file(s) are available"
    print(logging_message)
    sys.exit(0)

utils.run_wps(
    wps_dir,
    gfs_dir,
    namelist_wps_file,
    max_dom,
    start_date,
    end_date,
    gfs_layout="gdex",
    interval_seconds=gfs_interval_seconds,
    gfs_cycle_time=gfs_cycle_time,
    reuse_geogrid=reuse_geogrid,
)
if not wps_only:
    utils.run_wrf(
        wps_dir,
        wrf_dir,
        wrfout_dir,
        namelist_wrf_file,
        0,
        max_dom,
        start_date,
        end_date,
        num_proc,
        wrfout_saved_domain,
        interval_seconds=gfs_interval_seconds,
    )
utils.calculate_execution_time(start_time, time.time())

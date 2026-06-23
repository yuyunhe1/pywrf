# WRF 使用说明

本文档说明本项目中 WRF 的作用、输入数据、主要配置文件，以及一次完整运行的命令示例。服务器默认工作目录为：

```bash
/root/pyWRF-automation
```

## 1. WRF 简介

WRF, Weather Research and Forecasting Model, 是常用的中尺度数值天气模式。本项目使用 WRF-ARW 对 GFS 全球预报资料进行区域降尺度，输出更高空间分辨率的风场、位势高度、边界层高度等变量。

当前自动化流程为：

```text
GFS GRIB2 数据
  -> WPS/geogrid.exe 生成静态地理网格
  -> WPS/ungrib.exe 解析 GFS GRIB2
  -> WPS/metgrid.exe 插值到 WRF 网格
  -> WRF/real.exe 生成初始场和边界场
  -> WRF/wrf.exe 正式积分模拟
  -> wrf_output/YYYY-MM-DD/wrfout_d0*.*
```

## 2. WRF 使用的输入信息

WRF 运行需要以下几类输入：

1. 静态地理数据

   路径由 `WPS/namelist.wps` 中的 `geog_data_path` 控制：

   ```fortran
   geog_data_path = '/root/pyWRF-automation/WRF_GEOG/WPS_GEOG_LOW_RES/'
   ```

   这些数据包括地形、土地利用、土壤类型、植被比例等。

2. GFS 初始场和边界场

   当前项目使用 UCAR/GDEX 的 GFS 0.25 度全球 GRIB2 文件，本地目录为：

   ```bash
   /root/pyWRF-automation/data/gdex_gfs_0p25_global/
   ```

   文件命名格式为：

   ```text
   data/gdex_gfs_0p25_global/YYYYMMDD/gfs.0p25.YYYYMMDDHH.fNNN.grib2
   ```

   例如：

   ```text
   data/gdex_gfs_0p25_global/20251102/gfs.0p25.2025110200.f003.grib2
   ```

3. WPS 配置

   文件：

   ```bash
   /root/pyWRF-automation/WPS/namelist.wps
   ```

   主要控制模拟区域、投影、分辨率、嵌套网格、GFS 时间间隔等。

4. WRF 配置

   文件：

   ```bash
   /root/pyWRF-automation/WRF/test/em_real/namelist.input
   ```

   主要控制模拟时长、时间步长、输出间隔、垂直层数、物理方案、动力选项等。

5. 自动化脚本配置

   文件：

   ```bash
   /root/pyWRF-automation/main.py
   ```

   当前关键路径写在这里：

   ```python
   base_dir   = "/root/pyWRF-automation"
   gfs_dir    = f"{base_dir}/data/gdex_gfs_0p25_global"
   wps_dir    = f"{base_dir}/WPS"
   wrf_dir    = f"{base_dir}/WRF/test/em_real"
   wrfout_dir = f"{base_dir}/wrf_output"
   ```

## 3. 如何配置 WRF

### 3.1 配置模拟区域

主要修改：

```bash
/root/pyWRF-automation/WPS/namelist.wps
```

当前小范围配置示例：

```fortran
&share
 max_dom = 2,
 interval_seconds = 10800
/

&geogrid
 parent_id         =   1,   1,
 parent_grid_ratio =   1,   3,
 i_parent_start    =   1,  34,
 j_parent_start    =   1,  34,
 e_we              =  100, 100,
 e_sn              =  100, 100,
 geog_data_res     = 'default','default',
 dx = 9000,
 dy = 9000,
 map_proj = 'lambert',
 ref_lat   =  31.10,
 ref_lon   = 118.60,
 truelat1  =  30.0,
 truelat2  =  60.0,
 stand_lon = 118.60,
 geog_data_path = '/root/pyWRF-automation/WRF_GEOG/WPS_GEOG_LOW_RES/'
/
```

常用字段含义：

| 字段 | 含义 |
|---|---|
| `max_dom` | 网格层数，1 表示单层网格，2 表示 d01 + d02 嵌套 |
| `ref_lat`, `ref_lon` | 模拟区域中心点 |
| `dx`, `dy` | d01 水平分辨率，单位 m |
| `parent_grid_ratio` | 嵌套比例，例如 3 表示 d02 分辨率为 d01 的 1/3 |
| `e_we`, `e_sn` | 每层网格东西向、南北向格点数 |
| `i_parent_start`, `j_parent_start` | 子网格在父网格中的起始位置 |
| `truelat1`, `truelat2`, `stand_lon` | Lambert 投影参数 |

如果改变了区域范围、中心点、分辨率或嵌套网格，第一次运行必须重新生成地理网格：

```bash
export REUSE_GEOGRID=0
```

### 3.2 配置 WRF 动力网格

主要修改：

```bash
/root/pyWRF-automation/WRF/test/em_real/namelist.input
```

其中 `&domains` 必须和 `WPS/namelist.wps` 匹配：

```fortran
&domains
 time_step              = 48,
 max_dom                = 2,
 e_we                   = 100,    100,
 e_sn                   = 100,    100,
 e_vert                 = 48,     48,
 dx                     = 9000,
 dy                     = 9000,
 grid_id                = 1,     2,
 parent_id              = 0,     1,
 i_parent_start         = 1,     34,
 j_parent_start         = 1,     34,
 parent_grid_ratio      = 1,     3,
 parent_time_step_ratio = 1,     3,
/
```

注意：

- `max_dom`、`e_we`、`e_sn`、`parent_grid_ratio` 等必须和 `namelist.wps` 保持一致。
- `time_step` 通常可先按 `d01 dx(km) * 4~6` 秒估算。9 km 可先用 36-54 秒，27 km 可先用 120 秒左右。
- 如果出现 `Time step too large`，降低 `time_step`。

### 3.3 配置自动化脚本的 domain

文件：

```bash
/root/pyWRF-automation/main.py
```

当前默认：

```python
max_dom = 2
wrfout_saved_domain = 2
```

如果只跑单层网格，例如全国 27 km 或安徽单层 3 km，需要改成：

```python
max_dom = 1
wrfout_saved_domain = 1
```

如果继续使用 d01 + d02 嵌套，则保持：

```python
max_dom = 2
wrfout_saved_domain = 2
```

### 3.4 配置模拟时间

本项目通过环境变量配置模拟时间，不建议手动长期修改 `namelist.input` 里的日期，因为 `main.py` 会自动更新。

所有 GFS/WPS/WRF 时间均按 UTC 处理：

```bash
export WRF_START_DATE=2025-11-06_00:00:00
export WRF_FORECAST_HOURS=24
```

含义：

```text
WRF_START_DATE      模拟开始时间，格式 YYYY-MM-DD_HH:MM:SS
WRF_FORECAST_HOURS  模拟时长，单位小时
```

如果要固定使用某一个 GFS 起报时次，例如只用 2025-11-06 00 UTC 的 GFS cycle：

```bash
export GFS_CYCLE_TIME=2025-11-06_00:00:00
```

如果不指定 `GFS_CYCLE_TIME`，脚本会在本地 GFS 文件中选择能覆盖目标有效时刻的文件，并优先选择较短预报时效。

### 3.5 配置 GFS 数据源地址

本地 GFS 输入目录在 `main.py` 中配置：

```python
gfs_dir = f"{base_dir}/data/gdex_gfs_0p25_global"
```

历史 GDEX 下载脚本为：

```bash
/root/pyWRF-automation/download_gfs_hourly_70vars_realtime.py
```

GDEX 默认源地址在脚本中：

```python
GDEX_BASE_URL = "https://singapore.nationalresearchplatform.org:8443/ncar/gdex/d084001"
```

下载历史全球 GFS 示例：

```bash
cd /root/pyWRF-automation

python download_gfs_hourly_70vars_realtime.py download-gdex \
  --start-date 20251101 \
  --end-date 20260228 \
  --forecast-hours 0,3,6,9,12,15,18,21,24 \
  --output-dir ./data/gdex_gfs_0p25_global
```

运行 WRF 前可先检查指定时段的 GFS 是否完整：

```bash
export WRF_START_DATE=2025-11-06_00:00:00
export WRF_FORECAST_HOURS=24
CHECK_GFS_ONLY=1 python main.py
```

## 4. WRF 运行 CMD 示例

### 4.1 单日完整运行

```bash
cd /root/pyWRF-automation
conda activate pywrf
source ./setup_wrf_env.sh

export WRF_START_DATE=2025-11-06_00:00:00
export WRF_FORECAST_HOURS=24
export WRF_NUM_PROC=4
export REUSE_GEOGRID=0
unset GFS_CYCLE_TIME

python main.py
```

第一次使用新区域时用：

```bash
export REUSE_GEOGRID=0
```

同一区域后续重复运行可用：

```bash
export REUSE_GEOGRID=1
```

### 4.2 只运行 WPS

用于检查 `geo_em`、`FILE:*` 和 `met_em` 是否正常：

```bash
cd /root/pyWRF-automation
conda activate pywrf
source ./setup_wrf_env.sh

export WRF_START_DATE=2025-11-06_00:00:00
export WRF_FORECAST_HOURS=24
export REUSE_GEOGRID=0

WPS_ONLY=1 python main.py
```

成功后应生成：

```bash
/root/pyWRF-automation/WPS/met_em.d01.*
/root/pyWRF-automation/WPS/met_em.d02.*
```

如果 `max_dom = 1`，则只会有 `met_em.d01.*`。

### 4.3 监控批量运行进度

批量运行脚本：

```bash
cd /root/pyWRF-automation
conda activate pywrf
source ./setup_wrf_env.sh

python run_wrf_validation_batch.py
```

监控进度：

```bash
python monitor_wrf_progress.py --mode batch
```

状态文件：

```bash
/root/pyWRF-automation/data/wrf_batch_plan.csv
/root/pyWRF-automation/data/wrf_batch_status.csv
```

## 5. 输出结果位置

WRF 原始输出：

```bash
/root/pyWRF-automation/wrf_output/YYYY-MM-DD/wrfout_d02_*
```

如果 `wrfout_saved_domain = 1`，则输出为：

```bash
/root/pyWRF-automation/wrf_output/YYYY-MM-DD/wrfout_d01_*
```

WPS 中间结果：

```bash
/root/pyWRF-automation/WPS/geo_em.d0*.nc
/root/pyWRF-automation/WPS/met_em.d0*.*.nc
```

WRF 运行日志：

```bash
/root/pyWRF-automation/WRF/test/em_real/rsl.error.0000
/root/pyWRF-automation/WRF/test/em_real/rsl.out.0000
```

验证结果：

```bash
/root/pyWRF-automation/data/wrf_wind_validation_batch/
```

## 6. 常见检查命令

检查动态库：

```bash
ldd WPS/metgrid.exe | grep -E 'netcdf|not found'
ldd WRF/main/wrf.exe | grep -E 'netcdf|not found'
```

检查 GFS 文件是否存在：

```bash
find /root/pyWRF-automation/data/gdex_gfs_0p25_global \
  -name 'gfs.0p25.*.f*.grib2' | head
```

检查 `met_em` 是否有有效时间维：

```bash
ncdump -h /root/pyWRF-automation/WPS/met_em.d01.2025-11-06_00:00:00.nc | head -40
```

查看 WRF 是否成功：

```bash
tail -40 /root/pyWRF-automation/WRF/test/em_real/rsl.error.0000
```

成功标志通常包含：

```text
SUCCESS COMPLETE WRF
```


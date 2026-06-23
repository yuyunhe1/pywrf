# 基于 GFS 的无人机低空航线分层风速可视化与风险预警平台

后端默认读取仓库中已有的 GFS GRIB2，并将指定 AGL 高度层的 U/V 规则网格转换为
leaflet-velocity JSON。设置 `GFS_DATA_MODE=mock` 可切回确定性的 mock 风场。

## 项目结构

```text
wind-risk-platform/
├─ backend/
│  ├─ app/
│  │  ├─ main.py             # FastAPI 接口与 CORS
│  │  ├─ models.py           # 请求模型和阈值校验
│  │  ├─ gfs_provider.py     # 真实 GRIB2 发现、读取和网格规范化
│  │  ├─ data_provider.py    # real/mock 数据源选择
│  │  ├─ wind_provider.py    # 共享网格模型与 mock 提供器
│  │  └─ route_service.py    # 航线采样和风险统计
│  ├─ tests/test_api.py      # API 自动化测试
│  └─ requirements.txt
├─ frontend/
│  ├─ src/
│  │  ├─ components/
│  │  │  ├─ ControlPanel.vue
│  │  │  ├─ WindMap.vue
│  │  │  └─ AnalysisPanel.vue
│  │  ├─ api.js
│  │  ├─ App.vue
│  │  └─ style.css
│  ├─ package.json
│  └─ vite.config.js
└─ README.md
```

## 功能

- Leaflet 地图、自定义 Canvas 规则网格风速热力图和 leaflet-velocity 风向粒子流
- 起报时间、预报时效、高度层切换
- 地图点击查询最近格点的风速、气象风向、U/V 分量
- Leaflet.draw 手绘航线并自动调用后端分析
- 航线最大/平均风速、高风险比例、综合风险等级和分段着色
- ECharts 航线距离—风速曲线
- UTC 与北京时间并列展示

所有后端经纬度和航线点均使用 `[lon, lat]`；Leaflet 内部显示使用 `[lat, lon]`。
高度层统一使用 `m AGL`，风速和 U/V 分量统一使用 `m/s`。粒子沿风吹向运动，
文字风向为气象风向（风从哪里来）。

## 启动后端

```powershell
cd wind-risk-platform\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

接口文档地址：<http://127.0.0.1:8000/docs>

运行后端测试：

```powershell
cd wind-risk-platform\backend
.\.venv\Scripts\python.exe -m pytest -q
```

## 启动前端

需要 Node.js 18 或更高版本。

```powershell
cd wind-risk-platform\frontend
npm install
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>。Vite 会把 `/api` 代理到
`http://127.0.0.1:8000`。

页面左侧“数据源”可在 `GFS 原始数据` 与 `WRF 降尺度` 之间切换。默认进入页面时
显示 GFS 原始风场；选择 WRF 后，后端会读取 `wrf_cache` 缓存或通过 SFTP 按需同步。
地图初始视野位于 WRF/WPS 配置中心附近：`31.10°N, 118.60°E`。

## API

- `GET /api/times`
- `GET /api/wind?cycle=...&forecast_hour=3&level=100m%20AGL&bbox=minLon,minLat,maxLon,maxLat`
- `GET /api/heatmap?cycle=...&forecast_hour=3&level=100m%20AGL`
- `GET /api/point?lon=118.66&lat=31.12&cycle=...&forecast_hour=3&level=100m%20AGL`
- `POST /api/route/analyze`

`/api/wind` 返回的 `velocity` 字段是 leaflet-velocity 所需的 U/V 规则格点数组。
纬度按北到南排列，数据按行展开。

`/api/heatmap` 返回 `wind_speed.header` 与按行展开的 `wind_speed.data`。后端使用
`sqrt(u² + v²)` 计算风速，前端用半透明 Canvas 规则网格图层着色，并将
leaflet-velocity 粒子流叠加在热力图之上。

## GFS 数据目录

默认递归扫描以下目录：

```text
data/gfs_hourly_windcheck
data/gdex_gfs_0p25_windcheck
data/gdex_gfs_0p25_global
```

可使用系统路径分隔符配置多个其他目录：

```powershell
$env:GFS_DATA_DIRS="D:\gfs\realtime;D:\gfs\archive"
$env:GFS_MAX_GRID_POINTS="0"
python -m uvicorn app.main:app --reload --port 8000
```

下载新文件后，请求 `GET /api/times?refresh=true` 可刷新文件索引和风场缓存。真实读取依赖
`xarray/cfgrib/eccodes`。网格输出保证经度递增且范围为 `-180~180`、纬度从北向南、
 U/V 形状为 `(ny, nx)`。默认 `GFS_MAX_GRID_POINTS=0`，保持源数据分辨率；设置为正整数时
 才会对过大网格自动等步长降采样。

全球地图试验建议只下载低空 U/V，避免同时下载大量等压面变量：

```powershell
python download_gfs_hourly_70vars_realtime.py realtime `
  --output-dir .\data\gfs_hourly_windcheck `
  --global-region --wind-map-only `
  --start-fhour 1 --end-fhour 1 --cycle-count 1
```

完整全球 `0.25°` 网格为 `1440 x 721`，约 104 万格点。当前默认不降采样；同一
cycle/forecast hour 同时存在 point 与 global 文件时，后端优先使用 global 文件。

前端默认请求中国区域 bbox：`73,18,135,54`。底层可以继续使用全球 GRIB2，但
`/api/wind` 和 `/api/heatmap` 只会向浏览器返回中国区域格点。

地图打开时以安徽省合肥市中心为初始视野，显示约 `10 km x 10 km`，即约
`100 km²` 的周边范围。

时间选择以北京时间有效时刻为主，例如 `2026-06-16 15:00 北京时间`。后端会自动
映射到对应的 GFS 起报时间和预报时效，右侧面板保留 UTC 参考信息。

高度层支持低空固定 AGL 层和高层插值层。`10/20/30/40/50/80/100m AGL` 直接读取
GFS 固定高度层；`200/300/500/800/1000/1500/2000/3000m AGL` 使用等压面 U/V/HGT
与地形高度换算 AGL 后做垂直插值。高层插值要求 GRIB2 文件包含等压面 `UGRD/VGRD/HGT`
和 surface HGT，不能只使用 `--wind-map-only` 的轻量文件。

## WRF 实时降尺度缓存

服务器侧可使用仓库根目录的脚本自动完成：

```bash
cd /root/pyWRF-automation
conda activate pywrf
source ./setup_wrf_env.sh

python run_realtime_wrf_platform_cache.py \
  --base-dir /root/pyWRF-automation \
  --gfs-dir /root/pyWRF-automation/data/gdex_gfs_0p25_global \
  --cache-dir /root/pyWRF-automation/data/wrf_platform_cache \
  --forecast-hours 24 \
  --num-proc 4
```

脚本会按当前 UTC 时间寻找最近 GFS 起报点，先探测 `f001`。如果最近起报点尚未发布，
会自动尝试前一个 6 小时起报点。由于当前 WPS/WRF 流程使用 3 小时 GFS 强迫，脚本会为
WRF 下载 `f000/f003/.../f024`；WRF 完成后导出前端使用的 `f001-f024` 小时风场缓存。
实时下载使用 NOMADS hourly 0.25° 接口
`filter_gfs_0p25_1hr.pl`，默认使用 `all_var=on&all_lev=on` 下载所有变量和层次。
如需临时减小文件体积，可通过 `--gfs-vars APCP,HGT,PRMSL,SPFH,TMP,UGRD,VGRD`
指定变量子集。

缓存目录结构类似：

```text
/root/pyWRF-automation/data/wrf_platform_cache/
├─ index.json
└─ 2026062000/
   ├─ wrf_d02_2026062000_f001.npz
   ├─ wrf_d02_2026062000_f002.npz
   └─ ...
```

`.npz` 内包含规则经纬度网格 `lons/lats`、高度层 `levels_m`、以及每个高度层的 `u/v`。
导出时已将 WRF d02 曲线网格近邻重采样为规则 lon/lat 网格，便于 Leaflet 和
leaflet-velocity 读取。

本地平台后端读取服务器缓存时，可以不把整个后端切到 WRF 模式；前端选择 `WRF 降尺度`
时会自动在接口参数中传入 `source=wrf`。只需配置缓存目录和远端 SFTP 信息：

```powershell
cd wind-risk-platform\backend

$env:WRF_CACHE_DIR="D:\GNSS\PyWRF-Automation\pyWRF-automation\data\wrf_platform_cache"
$env:WRF_CACHE_REMOTE_HOST="10.129.59.14"
$env:WRF_CACHE_REMOTE_PORT="22"
$env:WRF_CACHE_REMOTE_USER="root"
$env:WRF_CACHE_REMOTE_PASSWORD="<服务器密码>"
$env:WRF_CACHE_REMOTE_DIR="/root/pyWRF-automation/data/wrf_platform_cache"

.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

`GET /api/times?refresh=true` 会通过 SFTP 刷新远端 `index.json`；请求某个时刻风场时，
后端会按需下载对应 `.npz` 到本地 `WRF_CACHE_DIR` 后读取。接口仍然沿用
`/api/wind`、`/api/heatmap`、`/api/point` 和 `/api/route/analyze`，前端无需改变操作方式。

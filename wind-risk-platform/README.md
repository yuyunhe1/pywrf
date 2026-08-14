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
- 地图点击查询最近格点的风速、气象风向、U/V 分量；点击位置位于已加载风场网格内时，
  前端优先从本地网格直接取值，减少重复后端请求
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
$env:WRF_CACHE_REMOTE_PASSWORD="<服务器密码>"
.\start_backend.bat
```

`start_backend.bat` 会在启动 FastAPI 前自动设置 GFS 实时下载相关环境变量，包括启动自动检查最新起报点、
下载目录、日志目录、`f001` 到 `f012`、最新 1 个起报点等配置。

如果需要手动启动，也可以执行：

```powershell
cd wind-risk-platform\backend
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

### 临时公网展示与国内底图访问

通过 Cloudflare Quick Tunnel 临时展示平台时，只需公开前端 `5173`。前端的 `/api` 请求仍由
Vite 转发至本机后端 `8000`：

```powershell
cd D:\yyh\pywrf
.\cloudflared\cloudflared-windows-amd64.exe tunnel `
  --url http://127.0.0.1:5173 `
  --http-host-header localhost:5173 `
  --protocol http2 `
  --no-autoupdate
```

地图底图不再由访客浏览器直接访问境外 OpenStreetMap 瓦片地址，而是请求同源接口
`/api/map-tiles/{z}/{x}/{y}.png`。后端通过已配置的 HTTP/HTTPS 代理获取 WGS84 兼容底图并缓存到
`data/map_tile_cache/`；因此国内访客无需 VPN，也不会出现高德 GCJ-02 与 GFS/WRF WGS84 数据错位。
修改后需要重新启动后端。默认代理及缓存目录由 `backend/start_backend.bat` 设置：

```text
GFS_PROXY=http://127.0.0.1:10081
MAP_TILE_CACHE_DIR=data/map_tile_cache
```

首次浏览某一区域时后端需要下载瓦片，加载会略慢；之后相同区域直接使用本地缓存。演示期间请保持
代理、后端、前端和 Cloudflare Tunnel 四者运行。停止 Tunnel 时在其终端按 `Ctrl+C`。

运行前端数据处理测试和生产构建：

```powershell
cd wind-risk-platform\frontend
npm test
npm run build
```

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

### 自动下载实时 GFS

后端已集成仓库根目录的 `download_gfs_hourly_70vars_realtime.py`。后端启动时会自动检查最新
GFS 起报点，若数据已发布，会在后台下载 `f001` 到 `f012`；下载完成后会自动刷新后端文件索引，
前端也会轮询下载状态并重新读取最新时刻。

`GET /api/times` 会展示已发现的历史和实时 GFS 时刻；如果在 `source=gfs` 下没有发现当前或未来
GFS 风场，也会自动在后台启动实时下载。请求某个缺失的 GFS 风场时，后端同样会触发下载并返回
`503`，提示稍后刷新。

可手动启动或查看状态：

```powershell
curl -X POST "http://127.0.0.1:8000/api/gfs/download"
curl "http://127.0.0.1:8000/api/gfs/download-status"
```

`backend\start_backend.bat` 已内置以下常用配置；如需临时覆盖，也可以在运行脚本前手动设置同名环境变量：

```powershell
$env:GFS_AUTO_DOWNLOAD="1"
$env:GFS_REALTIME_DOWNLOAD_DIR="D:\yyh\pywrf\data\gfs_hourly_windcheck"
$env:GFS_REALTIME_GLOBAL_REGION="1"
$env:GFS_REALTIME_START_FHOUR="1"
$env:GFS_REALTIME_END_FHOUR="12"
$env:GFS_REALTIME_CYCLE_COUNT="1"
$env:GFS_REALTIME_WIND_MAP_ONLY="0"
```

默认下载全球区域、最新一个起报点的 `f001` 到 `f012`。若只做低空地图展示并希望减小
文件体积，可设置 `GFS_REALTIME_WIND_MAP_ONLY=1`；若需要高层 AGL 插值，则保持为 `0`。
下载日志默认写入 `data/gfs_download_logs/`。如需禁用自动下载：

```powershell
$env:GFS_AUTO_DOWNLOAD="0"
```

下载过程会同时输出到后端终端和日志。如果出现 `WinError 10061`、`Connection refused`
等连接拒绝错误，终端会显示醒目的 `GFS 网络提示`，并在下载状态中标记失败，不会再仅在
`data/gfs_download_logs/` 中留下错误。

在无法直连 NOAA/NOMADS 的网络环境中，可先为当前 PowerShell 会话设置本机代理，再启动后端：

```powershell
$env:GFS_PROXY="http://127.0.0.1:10081"
.\start_backend.bat
```

启动脚本当前默认使用本机 `10081` 端口；如端口发生变化，请把 `10081` 换成代理软件提供的
HTTP 或 Mixed 端口。即使下载地址是 HTTPS，常见本地
代理的地址仍应写成 `http://127.0.0.1:端口`，不要误写成 `https://`。`start_backend.bat`
会把 `GFS_PROXY` 同时传给 `HTTP_PROXY` 和 `HTTPS_PROXY`；该设置只在当前终端及其启动的
后端进程中生效，关闭终端后不会永久保存。

如果不希望本地使用代理，也可以将下载任务部署在能够稳定访问 NOAA 的服务器，下载后再同步
GRIB2 文件到 `GFS_REALTIME_DOWNLOAD_DIR`。NOAA 还通过 AWS Open Data 公布 GFS 数据，且读取
公开桶不要求 AWS 账号；不过该来源提供的是完整 GRIB2 对象，若要维持当前按变量/层次的小文件
下载方式，需要另行实现 `.idx` 索引解析和 HTTP Range 分段下载，不能直接替换当前 NOMADS URL。

全球地图试验建议只下载低空 U/V，避免同时下载大量等压面变量：

```powershell
python download_gfs_hourly_70vars_realtime.py realtime `
  --output-dir .\data\gfs_hourly_windcheck `
  --global-region --wind-map-only `
  --start-fhour 1 --end-fhour 1 --cycle-count 1
```
下载全球各高空层u,v风，位势高度(hgt)，气压
python download_gfs_hourly_70vars_realtime.py realtime  --output-dir .\data\gfs_hourly_windcheck  --global-region  --start-fhour 1 --end-fhour 12 --cycle-count 2

完整全球 `0.25°` 网格为 `1440 x 721`，约 104 万格点。当前默认不降采样；同一
cycle/forecast hour 同时存在 point 与 global 文件时，后端优先使用 global 文件。

前端默认请求中国区域 bbox：`73,18,135,54`。底层可以继续使用全球 GRIB2，但
`/api/wind` 和 `/api/heatmap` 只会向浏览器返回中国区域格点。

地图打开时以安徽省合肥市中心为初始视野，显示约 `10 km x 10 km`，即约
`100 km²` 的周边范围。

时间选择以北京时间有效时刻为主，例如 `2026-06-16 15:00 北京时间`。前端会展示
已发现的历史和实时 GFS 时刻，便于回放测试；后端会自动映射到对应的 GFS 起报时间和
预报时效，右侧面板保留 UTC 参考信息。

高度层支持低空固定 AGL 层和高层插值层。`10/20/30/40/50/80/100m AGL` 直接读取
GFS 固定高度层；`200/300/500/800/1000/1500/2000/3000m AGL` 使用等压面 U/V/HGT
与地形高度换算 AGL 后做垂直插值。高层插值要求 GRIB2 文件包含等压面 `UGRD/VGRD/HGT`
和 surface HGT，不能只使用 `--wind-map-only` 的轻量文件。

### 航点高度与可选多高度层规划

平台默认按前端当前选择的单一高度层进行航线规划，这样地图热力图、粒子流和推荐航线使用的是同一高度层，
便于调试和论文展示。若需要实验性启用多高度层搜索，可在启动后端前设置：

```powershell
$env:ROUTE_PLAN_MULTI_ALTITUDE="1"
```

启用后，后端会优先读取环境变量 `ROUTE_PLAN_AGL_LEVELS`，例如：

```powershell
$env:ROUTE_PLAN_AGL_LEVELS="80,100,200,300,500"
```

如果未设置，则围绕当前前端选择的高度层自动选择若干 AGL 候选层。规划状态为
`(lon, lat, AGL level)`，边代价仍使用统一的 wind-aware cost，并增加高度平滑约束：

- `ROUTE_PLAN_MIN_AGL_M`：最小离地高度，默认 `60`
- `ROUTE_PLAN_MAX_ADJACENT_MSL_CHANGE_M`：相邻航点最大海拔高度变化，默认 `100`
- `ROUTE_PLAN_MAX_CLIMB_GRADIENT`：最大爬升/下降坡度，默认 `0.12`
- `ROUTE_PLAN_CRUISE_SPEED_MPS`：默认巡航速度，默认 `10`
- `ROUTE_PLAN_MULTI_ALTITUDE=1` 启用多高度层搜索；默认不启用

GFS 读取 surface `HGT/orog/gh` 作为地形高程；WRF `.npz` 缓存会尝试读取
`hgt_surface / terrain / terrain_height / elevation / HGT`。航点高度按：

```text
altitude_amsl_m = terrain_height_m + altitude_agl_m
```

导出航线时会同时下载同名的 JSON 和 `.waypoints` 文件。JSON 任务对象包含
`mission_name / coordinate_system / altitude_mode / waypoints / mission_items`；每个地图航点至少包含
`lon, lat, altitude_agl_m, terrain_height_m, altitude_amsl_m, heading_deg, speed_mps`，其中兼容字段
`ele` 表示海拔高度 AMSL。

`.waypoints` 使用任务规划软件通用的 `QGC WPL 110` 文本格式。首行固定为 `QGC WPL 110`，
之后每行按 Tab 分隔以下 12 列：

```text
index current frame command param1 param2 param3 param4 latitude longitude altitude autocontinue
```

平台生成的任务包含 Home（命令 16）、Takeoff（命令 22）、中间航点（命令 16）和 Land（命令 21）。
“历史航线列表”中的导入按钮同时接受 `.json` 和 `.waypoints`。导入 QGC 文件时，平台会保留包括
`DO_JUMP` 在内的完整任务项；地图和风场分析只使用带有效经纬度的任务项。再次保存或导出时，完整
`mission_items` 会写回 JSON 和航点文件，不会因地图显示过滤而丢失任务命令。

### 风切变风险与硬约束

路径规划将风险数据分为节点属性和边属性，A*、LPA*、WA-LPA* 共用相同语义：

- 平均风速、降水和地形安全离地高度属于节点/网格属性；超过各自硬阈值时，只将该节点设为不可通行。
- 水平风切变属于同一高度层相邻节点之间的边属性，定义为边两端的风矢量差：
  `horizontal_shear = sqrt((u2-u1)^2 + (v2-v1)^2)`，单位统一为 `m/s`。
  超过 `hard_delta_wind_vector_ms` 时只禁止该边，不会封锁边两端节点。
  风向变化角仅用于诊断，不作为独立硬约束。
- 垂直风切变当前停用，不参与节点可飞判断、路径搜索或高风险比例，也不会为此加载相邻高度层。
- WRF 水平切变通常代表约 3 km 模式网格尺度；同一风场网格内的航段会得到相同 `u/v`，因此切变为零。
  该结果不能解析建筑、山谷尾流或百米尺度阵风，也不能通过加密航线采样点提高真实空间分辨率。
  当前定义没有按距离归一化，因此阈值具有网格尺度依赖性，3 km 与 1 km 网格的结果不宜直接横向比较。

水平硬约束默认采用 `5.4 m/s`。左侧“阈值设置”中的“最大水平风切变”
会随航线分析及规划请求发送到后端，因此修改后重新规划即可生效；地图突变高危区也使用同一阈值。
实验性默认阈值同时位于 `backend/config/wind_shear.json`。原有高风险比例由风速节点采样与实际经过的
水平风切变边共同统计。这些阈值用于项目实验与验证，可根据观测结果调整，不代表无人机国家强制标准。

规划成功后，接口在顶层和 `wind_shear` 中返回 `max_horizontal_wind_shear`、
`max_horizontal_wind_shear_segment` 及完整的 `horizontal_wind_shear_profile`。沿程里程直接按最终规划节点
的真实航段逐段累计，不使用风速图表的加密采样点。右侧 ECharts 柱状图只隐藏不大于
`0.01 m/s` 的柱子，未改变、平滑或重新排列后端 profile，因此非零事件仍位于真实累计里程位置。
航段距离仅用于累计里程、柱子位置和 Tooltip，不参与风切变数值或硬约束计算。

若水平风切变边完全阻断搜索区域，平台会自动进行一次降级规划：仅关闭水平风切变边约束，继续保留风速、
距离、顺逆风、侧风、地形和降雨等原有约束。地图显示该参考航线，右侧风切变分析会明确高风险警告。
回退参考线不是满足风切变约束的最终航线，因此接口将最终航线 profile 置空，右侧不绘制柱状图。
响应中包含：

```json
{
  "active": true,
  "reason": "wind_shear_blocked",
  "message": "水平风切变边约束已完全阻断起终点；地图显示的是忽略水平风切变的参考航线。"
}
```

### 避风优先搜索范围

避风优先会使用比路程优先更大的搜索走廊。默认 padding 为：

```text
max(1.5°, min(6.0°, max(lon_span, lat_span) * 1.25))
```

可通过环境变量调整：

```powershell
$env:ROUTE_PLAN_WIND_MIN_PADDING_DEG="1.5"
$env:ROUTE_PLAN_WIND_MAX_PADDING_DEG="6.0"
$env:ROUTE_PLAN_WIND_PADDING_FACTOR="1.25"
```

避风优先还会默认执行一次“终点到起点”的反向候选搜索，再将该几何路线倒回为起点到终点，
用正向风场采样重新评分，对比最大风速、平均风速、高风险比例和距离后选择更优候选。若希望关闭：

```powershell
$env:ROUTE_PLAN_REVERSE_COMPARE="0"
```

### OMPL ST-RRT* 复现

已提供一个不依赖 OMPL C++ 编译的 Python 复现脚本，用于理解和验证 OMPL `STRRTstar`
的核心思想：

- 状态为 `(x, y, t)`；
- 运动必须沿时间正方向；
- `space_distance / delta_t <= v_max`；
- 使用起点树和目标树的双向扩展；
- 目标点时间按最短可达时间和逐步扩大的 time bound 采样；
- 以到达目标时间最小为优化目标；
- 示例中包含一个随时间移动的圆形动态障碍。

运行内置动态障碍 demo：

```powershell
.\backend\.venv\Scripts\python.exe backend\scripts\run_strrt_star_reproduction.py --demo
```

从仓库根目录运行：

```powershell
.\wind-risk-platform\backend\.venv\Scripts\python.exe `
  wind-risk-platform\backend\scripts\run_strrt_star_reproduction.py `
  --demo `
  --output data\strrt_star_demo.json
```

自定义起终点：

```powershell
.\wind-risk-platform\backend\.venv\Scripts\python.exe `
  wind-risk-platform\backend\scripts\run_strrt_star_reproduction.py `
  --start 0.0,0.0 `
  --goal 1.0,1.0 `
  --v-max 0.6 `
  --max-time 5
```

该脚本是研究和教学用途的轻量复现，方便后续把 ST-RRT* 思想迁移到无人机跨时间航迹规划。

此外已提供直接链接 OMPL 2.0.1 原生 `ompl::geometric::STRRTstar` 的 C++ 复现。当前本地构建目录为
`OMPL/build-msvc`，安装目录为 `OMPL/install`。从仓库根目录执行：

```powershell
pwsh -File wind-risk-platform\backend\scripts\build_strrt_star_native.ps1
```

脚本会自动配置并编译 `backend/native/strrt_star_demo`，运行二维空间时间规划，并将结果写入：

```text
data/strrt_star_native_result.json
```

可调整求解时长、最大速度、时空上限与随机种子：

```powershell
pwsh -File wind-risk-platform\backend\scripts\build_strrt_star_native.ps1 `
  -SolveTime 3.0 `
  -VMax 0.75 `
  -MaxTime 8.0 `
  -Seed 7 `
  -Output data\strrt_star_native_result.json
```

原生示例使用 `(x, y, t)` 状态、移动圆形障碍、正向时间约束和最大速度约束，输出到达时间、
空间路径长度、探索状态数、最大航段速度及完整时空路径，可作为后续接入实际风场与无人机约束的基线。

## WRF 实时降尺度缓存

前端切换到 WRF 数据源时会优先立即读取本地 `data/wrf_platform_cache`。若已配置远程服务器，
`index.json` 会在后台刷新，SSH 暂时不可用或未配置密码/密钥不会阻止已有本地 WRF 数据显示。
默认同一后端进程最多每 300 秒触发一次后台索引刷新，可通过
`WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS` 调整。时间列表默认只公布已经下载到本地的 `.npz`，避免用户
选中远程存在但本地缺失、且当前 SSH 无法下载的时效；如需恢复按需远程下载，可设置
`WRF_CACHE_EXPOSE_REMOTE_FILES=1`。

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

如需常驻运行，每隔 2 小时探查一次最新 GFS 起报点，发现新的可用 cycle 后自动下载 GFS、
执行 WRF 降尺度并导出 `.npz`：

```bash
python run_realtime_wrf_platform_cache.py \
  --base-dir /root/pyWRF-automation \
  --gfs-dir /root/pyWRF-automation/data/gdex_gfs_0p25_global \
  --cache-dir /root/pyWRF-automation/data/wrf_platform_cache \
  --forecast-hours 24 \
  --num-proc 4 \
  --watch \
  --interval-hours 2
```

脚本会按当前 UTC 时间寻找最近 GFS 起报点，先探测 `f001`。如果最近起报点尚未发布，
会自动尝试前一个 6 小时起报点。由于当前 WPS/WRF 流程使用 3 小时 GFS 强迫，脚本会为
WRF 下载 `f000/f003/.../f024`；WRF 完成后导出前端使用的 `f001-f024` 小时风场缓存。
实时下载使用 NOMADS hourly 0.25° 接口 `filter_gfs_0p25_1hr.pl`。默认下载 WRF
驱动和平台缓存扩展变量所需的变量子集：
`APCP,CAPE,CIN,GUST,HGT,HPBL,PRATE,PRES,PRMSL,RH,SPFH,TMP,UGRD,VGRD,VVEL`。
如需临时调整，可通过 `--gfs-vars` 指定变量子集。

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
新版缓存还会尽可能导出扩展变量：`gust_surface`、`pblh`、`cape`、`cin`、`vvel`、
`rh`、`spfh`、`tmp`、`hgt`、`hgt_surface`、`apcp`、`prate`。其中部分变量依赖 WRF
输出本身是否包含对应诊断量；不存在时会跳过，不影响基础风场缓存。
导出时已将 WRF d02 曲线网格近邻重采样为规则 lon/lat 网格，便于 Leaflet 和
leaflet-velocity 读取。

本地平台后端读取服务器缓存时，可以不把整个后端切到 WRF 模式；前端选择 `WRF 降尺度`
时会自动在接口参数中传入 `source=wrf`。

`backend\start_backend.bat` 已内置本地 WRF cache 目录和远端 SFTP 信息：

```powershell
cd wind-risk-platform\backend
.\start_backend.bat
```

启动时后端会自动刷新远端 `index.json`，并把服务器上最新一个 WRF cycle 的 `.npz`
下载到本地 `./data/wrf_platform_cache/<cycle>/`。后续请求某个尚未同步的时刻时，也会按需下载。

推荐使用 SSH key。若必须临时使用密码，可在运行 `start_backend.bat` 前手动设置：

```powershell
$env:WRF_CACHE_REMOTE_PASSWORD="<服务器密码>"
.\start_backend.bat
```

`GET /api/times?refresh=true` 会通过 SFTP 刷新远端 `index.json`；接口仍然沿用
`/api/wind`、`/api/heatmap`、`/api/point` 和 `/api/route/analyze`，前端无需改变操作方式。

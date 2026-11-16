# 御风智航 · 低空通航决策平台

本仓库 `main` 分支维护新版平台 `wind-risk-platform-flight-replay`，包括前后端、航线规划、固定航线通航窗口、推荐起飞时间及原生 10 分钟 WRF 数据链路。

## 代码范围

- [`wind-risk-platform-flight-replay/`](wind-risk-platform-flight-replay/)：新版平台源码、配置、测试和使用说明。
- [`download_gfs_hourly_70vars_realtime.py`](download_gfs_hourly_70vars_realtime.py) 和 [`gfs_s3_fallback.py`](gfs_s3_fallback.py)：后端启动所需的共享 GFS 下载脚本与 NOAA S3 备用下载逻辑。
- [`setup_wrf_env.sh`](setup_wrf_env.sh)：服务器 WRF/WPS 环境变量配置，供生产数据链路使用。
- [`LICENSE`](LICENSE)：许可证。

WRF 生产入口及其冻结源码位于 [`backend/server_wrf_10min/`](wind-risk-platform-flight-replay/backend/server_wrf_10min/)。气象数据、用户航线数据库、依赖环境、缓存和构建产物不纳入版本控制。

## 使用与测试

安装依赖、启动前后端、配置数据源及运行测试，请参阅[新版平台使用说明](wind-risk-platform-flight-replay/README.md)。

后端启动入口：`wind-risk-platform-flight-replay/backend/start_backend.bat`。
前端启动入口：`wind-risk-platform-flight-replay/frontend/start_frontend.bat`。

航点导入导出测试样例位于 `wind-risk-platform-flight-replay/backend/tests/fixtures/`。
服务器运行 WRF 前需具备已安装的 WRF/WPS、地理数据及对应计算环境。

旧版平台和独立研究脚本已从当前分支移除，可通过 Git 历史查阅。仓库根目录采用保留范围规则，避免本机其他研究文件被误提交。

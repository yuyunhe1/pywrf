# 跨时刻避风规划修复验收

验证日期：2026-11-04；仅修改 `wind-risk-platform-flight-replay`。旧平台和原始 V3 算法文件不修改。

## 行为变化

四种算法先完成避风搜索，再发布通过未来全程验收的首解。A*、LPA*、WA-LPA* 恢复原空间
搜索内核作为候选生成器，用起飞、中途和到达时刻风场生成走廊，并根据候选自身 ETA 修正。
保留精确到达时间的后续时空搜索，用已有可行解的代价上界和可采纳下界剪枝；不同有效到达时刻
不会仅因为“更晚到达”而被合并或删除。

V3 优先搜索原生 MSL 避风走廊，提前排除风切变和高度无法确认的网格，再逐段求解实际时空连接。
新增离线转弯/直飞候选保留上一段实际机头航向，固定空速、0.9 转率余量和独立验收继续生效。
长路线按原生水平网格、垂直层和 UTC 小时边界细分相同控制，修复中间节点数值累计误差。
控制值、连续空间曲线和原中间节点门限均不放宽；原独立 C 文件未改动。

候选、后续替换和后台任务都按同一套旧版策略权重计算距离积分风场代价。
直线只可在搜索后、代价合适且通过完整检查时显示。未找到可行结果时只显示起终点和原因，
不把直连参考线作为可保存的规划航线。

规划任务仍默认 600 秒，可取消，超时保留已经验证的最佳结果。未来窗口的计算不阻塞首解。
统一蓝色粗线、曲线红色定位、固定路线重算和同源风场回放继续保留。

## 用户案例与实测

起点 `(117.17468, 30.91165)`，终点 `(118.13049, 30.39657)`，
2026-11-04 **17:00 北京时间**起飞，避风优先，WRF 起报批次 `2026-11-04 00:00 UTC`。
直连约 107.89 km，100 米 AGL、10 m/s 地速下约飞行 3 小时，途中触发 7.9 m/s 硬上限。

| 算法 | 高度/速度模型 | 首条可行解 | 首解距离 | 后续结果 |
| --- | --- | ---: | ---: | --- |
| A* | 100 m AGL、地速 10 m/s | 0.53 s | 135.17 km | 138.29 km；积分代价由 5257 降至 1845 |
| LPA* | 100 m AGL、地速 10 m/s | 0.57 s | 136.79 km | 131.83 km；积分代价由 5298 降至 2028 |
| WA-LPA* | 100 m AGL、地速 10 m/s | 1.64 s | 136.79 km | 约 4.31 s 发布第二次改进，最终 131.83 km |
| V3 | 300 m MSL、空速 10 m/s | 86.87 s | 152.13 km | 此项测试在首解后停止工作进程，不宣称最终最优 |

前三项计时为已绑定 WRF 数组后到完整首解发布；V3 包含数值子进程冷启动与原生文件加载。
高度与时间模型不同，因此本表不用于比较四种算法的路线质量。
V3 同坐标曾重复测得约 85.06 s，最新源代码复测约 86.87 s；长程仍明显慢于三种旧算法，
但已在默认 600 秒预算内找到可行绕行结果。

V3 长路线最大风速约 5.55 m/s，连续风速、水平风切变、地形、转率与时间覆盖验收通过。
原独立 C 的中间节点和控制检查通过，平台 1 米终点策略通过，终点误差约 **0.60 毫米**。
原微小终点位置门限未通过，回执如实保留 `upstream_valid=false` 和 `POSITION_RESIDUAL`，
不得表述为原严格终点验收通过。

另测约 0.953 km 的 V3 短航线，首解 **2.47 s**；浏览器约 **3.03 s**。
手动 90°/90° 起终点航向约 3.55 s 发布，两个航向均匹配。
这些是上述固定数据与设备上的实测值，不保证任意点对都存在可行解或具有相同耗时。

## 回归验证

- API 与旧算法：**200 passed，1 skipped**；API venv 不运行 SciPy 数值模块，该模块另用固定环境验证。
- 数值与物理：**18 passed**；包括跨小时、原生查询、转弯/直飞航向连续性、控制细分前后路径一致性、
  缺数据、MSL 地形、1 米容差、转率和“两端安全、中间危险”的连续窗口反例。
- 前端：**62 passed**；构建通过。
- 真实浏览器：**34 项通过，JavaScript 异常为零**。其中 WA-LPA* 用户案例 9 项、V3 保存/回放/窗口 18 项、
  新 152 km V3 路线保存材料重载 7 项。测试屏蔽外部底图瓦片请求，未将 OSM 连通性计入验收。
- WA-LPA* 浏览器首解约 3.31 s，显示了 3 个有效版本；预报覆盖外清除路线并禁用保存。
- V3 长路线重载：几何约 **0.125 s** 出现，完整重新计时剖面约 **43.95 s**，样本 9260 个；
  在原 17:00 起飞时刻仍判为可飞，停止窗口任务后保留航线。
- 真实超时测试：专用 **8 秒**预算下约 2.61 s 发布首解、8.08 s 超时，保留可行结果并自动启动窗口任务。
  产品默认预算仍为 600 秒。

包括“起飞时安全、经过时危险”“终点起飞时危险但到达时安全”“未达硬上限但绕行更优”、
均匀场允许直线、资源上限不返回假参考线、旧响应隔离、保存重载和图表红色定位。

原件与 vendor 副本的 144 个 Python、4 个配置文件须逐项通过 SHA256 核对。
平台新增候选、积分策略评分及窗口能力单独验证，不沿用原 V3 的完整执行任务成功标签。
没有接入执行仿真、偏差恢复、在线接管、偏差注入或 4 秒交接专项。

## 接口与复现

沿用 `POST /api/route/plan/jobs` 及其查询/取消接口。结果新增 `objective_cost`、
`objective_model=DISTANCE_INTEGRATED_WIND_V1`、`search_phase` 和 `planning_diagnostics`。
`route_status` 区分 `feasible`、`data_insufficient`、`resource_limit` 与 `no_feasible_route`；
无路线时 `points=[]`，不生成可飞剖面或路线材料引用。`departure_windows` 可暂为 `null`，
通过原有独立窗口接口继续评估。
V3 材料、环境或连接错误继续通过原有任务失败状态及具体原因返回；任务超时单独标记，
有可行结果时保留结果，不把进一步搜索受限解释为路线不可行。

在新版 backend 目录：

```powershell
$apiPython = 'D:\yyh\pywrf\wind-risk-platform\backend\.venv\Scripts\python.exe'
& $apiPython scripts\verify_cross_time_avoidance.py --full --budget 600
& $apiPython scripts\verify_cross_time_avoidance.py --v3 --budget 600
& $apiPython scripts\verify_cross_time_avoidance.py --v3 --short --budget 90
& $apiPython -m pytest tests --basetemp D:\yyh\pywrf\data\cross_time_avoidance_tests\api-reproduce --disable-warnings -q
& 'D:\GoodTools\anaconda3\python.exe' -m pytest tests\test_v3_physics.py --basetemp D:\yyh\pywrf\data\cross_time_avoidance_tests\physics-reproduce -q
```

浏览器验收使用隔离的 `v3_smoke_server.py`（8002）、Vite（5175）和 Chrome 调试端口（9226），
设置 `V3_TEST_WORK_DIR=D:\yyh\pywrf\data\cross_time_avoidance_tests\browser`。
分别运行 `verify_avoidance_browser.py`、`verify_v3_browser.py` 和 `verify_avoidance_browser.py --v3-long`。
隔离服务只模拟无关 GFS；V3 原生数据、规划、窗口和曲线重算均真实执行，不修改用户航线数据库。

原始报告位于 [cross_time_avoidance_tests](../../data/cross_time_avoidance_tests)，主要文件为
`classic-user-case-full-report.json`、`v3-user-case-first-report.json`、`v3-short-first-report.json`、
`manual-headings.json`、`timeout-report.json` 与 browser 目录的三个报告。
最终代码与原件核对见 [integration-audit.json](../../data/cross_time_avoidance_tests/integration-audit.json)。

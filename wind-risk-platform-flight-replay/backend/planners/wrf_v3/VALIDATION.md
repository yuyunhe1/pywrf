# V3 平台集成验收记录

验证日期：2026-11-04。范围为新版平台的航线规划、首解发布、固定连续路径重算和未来通航窗口。
不包含执行仿真、偏差恢复、在线接管、偏差注入或 4 秒交接策略；不沿用原 V3 完整任务成功标签。

本文件保留初版集成验收与性能基线。四种算法先搜索避风再发布首解的最新修复及用户案例，
见 [跨时刻避风验收](../../CROSS_TIME_AVOIDANCE_VALIDATION.md)。

## 来源与验收策略

交付主版共 144 个 Python 文件及 4 个配置文件，复制在 `vendor/`，逐文件来源和 SHA256 见
`UPSTREAM_MANIFEST.json`。原件 `D:/yyh/wind_aware_flight/` 与副本的 148 项哈希均通过核对。
平台适配位于相邻 `adapter/`，工作进程独立运行，并检查未加载执行流水线模块。
最终逐项核对与适配代码哈希见 [integration-audit.json](../../../../data/v3_integration_tests/integration-audit.json)。

按用户最新要求，终点位置容差为 **1 米**，默认不约束起终点机头航向。高级选项可分别约束
真北顺时针航向。原独立物理重积分仍执行，非终点切换节点、航向及控制约束保留。
结果明确记录 `PASSED_PLATFORM_ENDPOINT_1M`、原验收是否通过及其失败项。
以下三个性能案例的原微小位置残差门限未通过，平台 1 米策略通过；不能称为原严格验收通过。
连续曲线还须独立检查区域、地形、风场覆盖及平台风险硬约束，合格才发布。

数值环境固定为 Python 3.13.5 / NumPy 2.1.3 / SciPy 1.15.3，解释器
`D:/GoodTools/anaconda3/python.exe`。API 使用已有的独立 venv，不在其中运行数值求解。

## 真实 WRF 首解耗时

使用原生 d02 数据，起报 `2026-11-03T18:00:00Z`，起飞 `2026-11-04T02:00:00Z`，
固定空速 10 m/s、MSL 300 m、自由起终点航向。
计时从冷启动子进程到通过完整平台验收并发布首解，包含原生数据加载、独立重积分、
连续约束检查及首条曲线材料生成，未以初值求解结束作为首解时间。

| 航线 | 距离 | 首条可行解 | 连续曲线终点误差 | 飞行时长 |
| --- | ---: | ---: | ---: | ---: |
| 短航线 | 0.953 km | 2.20 s | 0.00103 m | 79.90 s |
| 中航线 | 9.532 km | 9.61 s | 0.00047 m | 775.56 s |
| 长航线 | 87.594 km | 23.49 s | 0.00073 m | 6596.55 s |

长航线跨越相邻小时，仍使用随空间、MSL 高度及实际飞行时间变化的原生风场。
数据版本为 `v3_bc456a5bc263d8e4c8de73ae70d741533bb9d74bacbf4db1f99f756d4b1a`。
来源目录包含多个起报批次；当前默认版本只绑定同一原生来源的 25 个连续小时文件。
短航线浏览器端实测首解约 2.86 s。以上为指定案例的实测值，不是任意点对的耗时或可行性保证。

性能原始结果及完整首解：

- [planning-performance.json](../../../../data/v3_integration_tests/planning-performance.json)
- [short_0_95km-plan.json](../../../../data/v3_integration_tests/short_0_95km-plan.json)
- [medium_9_5km-plan.json](../../../../data/v3_integration_tests/medium_9_5km-plan.json)
- [long_87km-plan.json](../../../../data/v3_integration_tests/long_87km-plan.json)

## 约束、接口与界面验证

| 验证 | 结果与范围 |
| --- | --- |
| API 与旧算法回归 | 190 passed，1 skipped；API venv 中跳过 SciPy 物理模块，该模块在数值环境另行运行 |
| 数值与物理验证 | 16 passed；原生查询一致性、恒风解析、跨小时、航向、地形、缺变量/时次、转率、1 米策略、极短控制段和窗口未确认区间 |
| 前端既有验证 | 60 passed；任务状态、取消、旧响应隔离、窗口协调及曲线联动等 |
| 前端构建 | 通过；第三方 VueUse 注释位置警告不影响构建 |
| 真实浏览器验收 | 20 项通过，JavaScript 异常为零；底图瓦片请求在此项测试中屏蔽，未验收外部 OSM 连通性 |
| 统一蓝色航线显示 | 6 项浏览器检查通过；混合风险、数据不足、等待剖面、单时刻/旧算法显示、红色定位与图层开关；统一 `#00f3ff`、4 px 实线 |
| 手动起终点航向 | 90° / 90° 请求约 3.96 s 发布，通过所选航向约束 |
| MSL 地形冲突与越界 | MSL 20 m / 区域外请求约 1.58 s 返回 `BELOW_TERRAIN` / `OUT_OF_ROI` |
| 正常任务结束 | 实测约 14.58 s 完成，保留最终航线并自动启动窗口任务 |
| 规划超时保留 | 专用 8 s 测试预算下约 2.11 s 发布首解、8.09 s 超时，保留结果并启动窗口任务；产品默认仍为 600 s |
| 长航线窗口部分保留 | 专用 60 s 测试预算下约 59.68 s 超时，10 次进度更新，保留已确认窗口；产品默认仍为 600 s |

浏览器验证覆盖：首解优化期间显示、主动停止优化、同一原生 MSL 底图、异步连续窗口、点击窗口与
手动修改起飞时间后保持空间曲线并改变地速/ETA/补偿航向、曲线真实鼠标点击及红色地图定位、
轨迹时间定位、单时刻切换、保存重载、预报覆盖外保留几何但不伪造 ETA，以及长航线显示。
已保存长航线几何约 0.084 s 显示，完整重新计时剖面约 25.38 s，样本 6555 个。
地图现统一使用旧版蓝色粗实线，风险判断、曲线与红色定位仍保留。

窗口对整个起飞区间传播保守边界，边界合并后按 5 分钟向内取整。专门验证了“两端安全而中间危险”
不会被误报为连续可飞窗口，也验证跨小时遇险和转率超限排除对应起飞区间。
长航线在上述 60 s 测试预算内确认 `02:00–11:00 UTC`，其余部分不作为可飞窗口发布。
该长窗口原始报告生成于“未扫描未来范围”展示修正之前；报告中的未确认数量为旧值，
当前代码已将未扫描的未来范围纳入未确认列表，新增数值回归覆盖此项。

详细证据：

- [runtime-report.json](../../../../data/v3_integration_tests/runtime-report.json)
- [timeout-report.json](../../../../data/v3_integration_tests/timeout-report.json)
- [browser-report.json](../../../../data/v3_integration_tests/browser/browser-report.json)
- [浏览器截图](../../../../data/v3_integration_tests/browser/v3-ui.png)
- [统一蓝色显示验证](../../../../data/v3_integration_tests/browser/route-style-report.json)
- [统一蓝色显示截图](../../../../data/v3_integration_tests/browser/uniform-blue-route.png)

## 复现

在新版 `backend` 目录运行：

```powershell
& 'D:\yyh\pywrf\wind-risk-platform\backend\.venv\Scripts\python.exe' -m pytest tests --disable-warnings -q
& 'D:\GoodTools\anaconda3\python.exe' -m pytest tests\test_v3_physics.py --disable-warnings -q
& 'D:\yyh\pywrf\wind-risk-platform\backend\.venv\Scripts\python.exe' scripts\verify_v3_planning.py
& 'D:\yyh\pywrf\wind-risk-platform\backend\.venv\Scripts\python.exe' scripts\verify_v3_runtime.py
& 'D:\yyh\pywrf\wind-risk-platform\backend\.venv\Scripts\python.exe' scripts\verify_v3_timeout.py
```

浏览器验收须先独立启动 `scripts/v3_smoke_server.py`（8002）、指向 8002 的 Vite（5175），
以及带 9226 调试端口的 Chrome；再运行 `scripts/verify_v3_browser.py --long`。
`verify_v3_runtime.py` 的 HTTP 完成检查也需要先启动该隔离服务。
脚本使用隔离数据库与导出目录，不修改用户已保存航线。长航线用例需要前述性能脚本生成的材料。
该隔离服务仅模拟无关的 GFS，V3 原生 WRF、规划、剖面和窗口均真实运行。

在新版 `frontend` 目录运行 `npm test` 与 `npm run build`。

## 结论的边界

通过验收表示指定模型、数据版本和约束下的数值检查通过，不表示整体航时全局最优。
越过地形、时间、空间、高度覆盖或物理约束时，应返回明确不可行/数据不足状态。
窗口预算耗尽时只保留已确认部分，未确认区间不能解释为安全或不安全。
新引入的自由航向候选、1 米终点策略、平台评分和窗口算法都属于本次独立集成验证范围。

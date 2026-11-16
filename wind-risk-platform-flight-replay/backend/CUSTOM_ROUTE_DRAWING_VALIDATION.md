# 自定义绘制固定航线验收

日期：2026-11-05。所有功能修改位于新版 `wind-risk-platform-flight-replay`。

## 实现

- “历史航线 → 更多”弹窗的导入按钮左侧增加同样式的“自定义绘制航线”。
- 独立草稿控制器处理点击、编号、拖动、撤销、清空、取消及重复提交保护。
  绘制期间停止规划和回放、清空原航线展示，并禁用普通选点、风场查询及 Leaflet Draw。
- 底部工具栏显示名称、航点数、当前来源/AGL 高度/地速及保存操作；默认名称采用北京时间。
- GFS/WRF 和高度预览切换保留草稿，绘制期间缩放不自动切换到平均高度。
- 保存前校验坐标、至少两个不同位置和正长度；保留全部航点及顺序，包括拖动后重合的点。
  保存期间锁定编辑及左侧设置，失败保留草稿；成功后提示已保存并自动加载固定航线。
- 沿用 `POST /api/routes`，显式提交 `source` 和 `level`，不增加数据库字段或接口。
  保存不调用搜索，不闭合路径，不触发浏览器下载；原服务仍生成可供手动导出的文件。
- 沿用固定路线评估、预报版本绑定和异步响应隔离；速度、阈值及预报变化只重算固定路径。
  暂无窗口或缺少预报不阻止保存。绿色窗口仍为静态展示。
- 历史弹窗按 `route_id` 选用记录；无 ID 的旧导出仅在名称唯一时兼容，避免同名误选。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 前端 `npm test` | 88 passed，含新增 14 项草稿/保存/同名记录/过期回调检查 |
| 前端 `npm run build` | 通过 |
| 后端窗口、回放、GFS 与时空规划回归 | 64 passed |
| Chrome 实际页面验收 | 59 项通过，0 JavaScript 异常 |
| 正常新版服务 | 5174 前端与 8001 后端 HTTP 200，前端已提供新组件 |

浏览器在独立 5175/8002 服务和独立 SQLite/导出目录下运行。天气使用明确的小时夹具：
100m 为均匀安全风，200m 在第 1 小时出现风峰后恢复安全，300m 始终超限，500m 缺少预报。
保存、导出、连续区间窗口算法、剖面和回放均调用实际实现；本次不将夹具验证称为新增真实 WRF 仿真。
地图瓦片请求被屏蔽，未将瓦片网络连通性列入验收。

浏览器覆盖了真实点击新增、标记拖动、地图拖动/缩放、编号、按钮状态、撤销/清空/取消、
切换来源和高度、网络保存失败重试、在途提交锁定、精确几何持久化、当前 25m/s 地速、
GFS 默认 50m/s、当前禁止但未来可飞、无窗口、数据不足、同名记录 ID 定位、整页刷新重载、
阈值和地速变化时路径不变、曲线红色定位及粒子回放 30 分钟刷新规则。
网络记录确认绘制期间不分析或保存，整个自定义流程不发起航线搜索，也不触发浏览器下载。
固定路线加载后的单时刻分析仍按原流程执行。

后端回归命令（使用已有 API 环境）：

```powershell
cd D:\yyh\pywrf\wind-risk-platform-flight-replay\backend
& D:\yyh\pywrf\wind-risk-platform\backend\.venv\Scripts\python.exe -m pytest tests/test_departure_windows.py tests/test_flight_replay.py tests/test_gfs_temporal.py tests/test_temporal_routing.py --basetemp D:\yyh\pywrf\data\custom_route_tests\pytest-fixed-route -q
```

独立浏览器复测：启动 `scripts/custom_routes_smoke_server.py`，设置 `BACKEND_PORT=8002`、
`FRONTEND_PORT=5175` 启动 Vite，再使用独立 Chrome 配置目录开放调试端口 9227，运行
`scripts/verify_custom_route_browser.py`。脚本会清理该夹具服务的测试航线，检测到非夹具服务则退出。

证据位于工作区 `data/custom_route_tests/`：`browser-report.json`、`drawing-toolbar.png`、
`saved-wrf-windows.png`、`saved-missing-data.png`、`production-readiness.json`。
测试记录未写入正常平台数据库。

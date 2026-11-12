# GeoPilot 系统原型 v1 最终验收

验收日期为 2026-11-12。交付包含本机 Web GIS 工作台、版本化数据资产和方法目录、方案生成与独立确认、真实 QGIS/Python 执行、取消与重试、输入/过程/成果检查、成果下载与地图查询、模型辅助与手工入口，以及源码、样本、测试和使用文档。[启动说明](../../README.md) · [工作台操作](../user-guide/workbench.md) · [源码与证据指纹清单](../../artifacts/reports/final/delivery-manifest.json)

## 验收范围和结果

| 范围 | 实际结果 |
|---|---|
| P0–P11 开发计划 | 122 项原子任务全部签收，详见各阶段证据。 [任务看板](../../plans/06_PROGRESS/TASK_BOARD.md) · [P11 阶段记录](../acceptance/P11.md) |
| V2.0 通用需求 | 40 条逐项映射，36 PASS、4 PARTIAL、0 NOT_IMPLEMENTED。 [需求矩阵](../traceability/requirements-matrix.md) |
| T01–T16 | 16 PASS、0 FAIL、0 NOT_RUN。本轮固定用例组合共 122 项参数化和辅助断言通过。 [固定用例复跑](../acceptance/P11-02.md) |
| 后端 | 全量套件 1,595 项通过。随后新增的端到端测试单独实跑 1 项通过，合计覆盖当前 1,596 个后端用例，0 失败、0 错误、0 跳过。 [全量 JUnit](../../artifacts/test-results/p11-complete-backend-junit.xml) · [新增端到端 JUnit](../../artifacts/test-results/p11-05-junit.xml) |
| 前端 | 14 个测试文件、66 项通过，0 失败、0 跳过。TypeScript 校验及 Vite 生产构建通过。栅格点击修复前曾出现 1 项失败，已保留原始证据并用真实浏览器复验。 [接口审计](../acceptance/interface-audit.md) · [修后 JUnit](../../artifacts/test-results/p11-04-frontend-junit.xml) |
| 净目录启动 | 新 Python venv、新 npm 依赖及空 SQLite 启动通过，专项后端 42 项和交付版本前端 66 项通过。空库实际执行七步道路样例，重开三个下载成果。 [净目录报告](../acceptance/P11-07.md) |

## 真实计算与可追溯性

- 道路统计 v2 使用局部投影 EPSG:32650，500 m 样本得到西区 3、东区 2、未归属 1。同一确认版在两个独立目录复算，CSV 字节一致，缓冲边界 Hausdorff 距离及面积差均为零。属性筛选 v1 经同一方案、执行、检查与成果链得到唯一的 `building-west`。两种方法均经 Chromium 实际操作验证。 [双方法复现](../../artifacts/reproduction/p11-05/README.md)
- 工作台目录的七种可执行方法均复跑，包括道路缓冲、属性过滤、空间关系、图斑分区、土地利用变化、DEM、遥感变化。经审核登记的脚本扩展示例使用同一通用执行链。 [逐方法输入、参考结果、容差和失败用例](../../artifacts/reports/methods/regression.md)
- 生命周期演练验证失败后停止派发、活动取消后子进程退出、重启恢复、重试创建独立运行目录，以及检查中断后保留已登记成果并重查。审核核对 8 个实际运行和 25 份正式成果的资产、方案、工具和运行版本，以及 HTTP、SQLite 与文件指纹。 [生命周期](../acceptance/lifecycle.md) · [跨层接口](../acceptance/interface-audit.md)
- 两期遥感示例以原生 TIFF、JP2、XML 和伴随掩膜打包，完成八步并验证原像元、NoData 和类别。MapLibre 使用独立 PNG 预览，点击按成果指纹重开原始栅格。 [原生产品工作台证据](../../artifacts/smoke/p8-native-workbench-verified/browser.json) · [浏览器像元](../../artifacts/smoke/p8-10/browser.7TgLX6/browser.json)
- 本机 Ollama 建议已通过真实 HTTP 和正式运行。完整发送请求经过 T15 核对，超时回退可从手工入口完成同一方法。PostGIS 为可选快照入口，已在真实 PostgreSQL/PostGIS 验证两个版本停库后复算。 [模型入口](../acceptance/P9.md) · [可选数据源与脚本](../acceptance/P10.md)

## 性能观察

小型固定样本的第一步开始至末步结束间隔为道路七步 16.23 秒、DEM 七步 6.51 秒、NDVI 八步 8.30 秒。上述时长来自本机 QGIS/Python 执行记录，范围截至末步结束，检查和浏览器耗时另计。尚未进行大数据量或并发压力测试。Vite 的主 JavaScript 产物约 1.37 MB，MapLibre worker 约 520 kB，构建保留大包体提示。 [净目录道路步骤](../../artifacts/smoke/p11-07/live-road/run-records/) · [审计样本](../../artifacts/smoke/p11-04/run.or1ZGlPX/verification.json) · [构建日志](../../artifacts/smoke/p11-07/npm-build-final.log)

## 已知限制

- 需求矩阵中的四项部分实现涉及任务语义和范围参与资产候选筛选、角色快照汇总缺字段与不交叠原因、通用工具的栅格 NoData 前置声明、工具级规则自动进入方案。 [需求矩阵](../traceability/requirements-matrix.md)
- 椭球缓冲独立工具通过 T04，初次验收时正式道路 v2 接入局部投影，v1 保留为目录结构示例。后续 v3 已完成双模式工作台验收。 [矢量阶段](../acceptance/P6.md) · [后续验收](../acceptance/road-geodesic-workflow.md)
- 栅格地图预览以单张最长边 1024 像元的图像显示。跨反经线且无法单图表达的范围返回明确状态。遥感定标依赖五个配置中的精确版本，未知版本在确认前阻断。 [栅格预览](../acceptance/P7.md) · [遥感配置与操作](../user-guide/workbench.md)
- 本机部署按单用户和回环地址验证，Ollama 与 PostGIS 需单独配置。PostGIS 运行读取已登记的本地版本，强制终止导出可能留下未登记文件。 [启动说明](../../README.md) · [可选数据源](../acceptance/P10.md)
- 后端仍有 Starlette TestClient 引用 AnyIO 弃用别名的提示。另一条 Pyogrio 提醒来自刻意创建缺 CRS 的测试。 [全量 JUnit](../../artifacts/test-results/p11-complete-backend-junit.xml)

## 交付复验

从原型目录执行，QGIS 变量可指向本机实际 CLI。

```bash
export GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
python -m pytest -q -p no:cacheprovider
python scripts/validate_traceability.py
(cd frontend && npm test -- --run && npm run build)
python artifacts/reports/final/delivery_manifest.py
```

本项新增本报告、P11 阶段签收、可验证交付清单和标准库脚本，更新任务看板、验收日志与最终清单。源工作区缺少 Git 元数据，使用清单的确定性 SHA-256 记录该版代码、配置、测试和关键证据。清单文件路径均相对于原型目录。[交付清单](../../artifacts/reports/final/delivery-manifest.json)

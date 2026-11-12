# GeoPilot 通用需求补齐验收

2026-11-12，依据 V2.0 §3.2、§3.4、§7.2 和 §7.5 复验 CTX-001、CTX-003、TOOL-003、TOOL-004。工作台浏览器与工具规则验证使用本机 Uvicorn、Vite 和 QGIS 3.44.15。栅格门禁另以隔离 SQLite 和实际 Python GIS 工作进程复验。[设计原文](../../../docs/source/GeoPilot_产品需求规格与系统详细设计报告_V2.0.md) · [公共契约](../../plans/01_ARCHITECTURE/02_PUBLIC_CONTRACTS.md)

## CTX-001 按任务筛选候选资产（PASS）

用真实 GeoPackage 录入范围、点和线。原范围为带孔 UTM 面，符合条件的点以 EPSG:4326 登记，仍正确入选。同类型和同字段的远处点、范围孔内点以及缺必需字段或几何类型错误的资产从候选排除。切换至第二个范围后，原远处点入选，原范围内的点排除。宽泛向量角色改选线对象时只保留合适的线候选。隔离目录中的双分支方法切换预期成果后，候选角色分别沿正确的产出步骤回溯。Chromium 也实际切换了成果和对象几何，固定方法仍保留其必需的输入角色。[实际 HTTP](../../artifacts/smoke/traceability-follow-up/context-http.json) · [浏览器与持久化](../../artifacts/smoke/traceability-follow-up/context-browser.json)

## CTX-003 保存并显示适用性原因（PASS）

分别显式选择范围外、孔内、缺字段、缺 CRS 的资产。响应保留原资产版本。前三种有明确阻断原因，缺 CRS 为待核验。真实 Chromium 改换范围后保留原选择，并显示不交叠。改为不存在的字段后显示该字段名。修正后，正常草稿入口保存第二个范围、精确资产版本和可用状态，任务仍未开始运行。只改预期成果并重新生成时，工作台复用原草稿版本，刷新后仍恢复新的筛选条件。旧格式 SQLite 上下文经同一后端恢复成功。[浏览器记录](../../artifacts/smoke/traceability-follow-up/context-browser.json) · [字段提示截图](../../artifacts/smoke/traceability-follow-up/context-field-diagnostic.png)

## TOOL-003 执行前检查栅格空值（PASS）

用真实双波段 257×257 GeoTIFF 和临时注册的工具契约，按整数参数指定第 2 波段。最后窗口第 256 行、第 256 列的 NoData 在 HTTP 调度的派发前被定位。运行和检查状态为 `FAILED / NOT_CHECKED`，步骤退出码为空，日志无子进程命令，未生成正式成果。另一幅第 2 波段含 66,049 个有效零值的 GeoTIFF 通过同一门禁，真实 Python 工具完成并登记成果。低层 Python 调用也不能跳过门禁。生产 NDVI 对反射率和质量掩膜检查各自的标签，保留部分无效和全零质量掩膜的原语义。[调度实测](../../artifacts/smoke/traceability-follow-up/raster-scheduler.2997ijrc/verification.json) · [低层阻断](../../artifacts/smoke/tool-003/python-adapter-blocked/step/result.json) · [NDVI 实际步骤](../../artifacts/smoke/tool-003/ndvi-allowed/step/result.json) · [50 项专项 JUnit](../../artifacts/test-results/tool-003-final-junit.xml) · [366 项关联 JUnit](../../artifacts/test-results/tool-003-regression-junit.xml)

## TOOL-004 登记后运行工具成果规则（PASS）

临时双分支方法的两步分别实际调用 QGIS 属性筛选，方法级规则为空。确认快照固定两个步骤各自的 `output_exists@1`。执行成功并登记两个独立 GeoPackage 后，报告为 `SUCCEEDED / PASSED`，分别记录两条带步骤和工具版本的结果。现有道路和属性工作台样例也通过，保留原下载和检查状态。工具规则从实际保存的请求、结果及相应文件取证。专项还实测了中间成果 CRS 错误时执行成功、检查失败，及中断后同一运行按原规则版本完成重查。中间文件指纹变动时拒绝重查。[双分支 HTTP 运行](../../artifacts/smoke/traceability-follow-up/tool-rules-http.json) · [既有双方法 Chromium 复算](../../artifacts/smoke/traceability-follow-up/road-workbench.3gqAmFUI/browser.json) · [同一运行重查证据](../../artifacts/smoke/tool-004/interrupted-recheck/evidence.json) · [8 项工具专项](../../artifacts/test-results/tool-004-final-junit.xml) · [116 项关联回归](../../artifacts/test-results/tool-004-regression-junit.xml)

## 复跑入口

启动隔离工作台后，以浏览器验证脚本和实际 HTTP 核对候选、页面与工具规则。浏览器脚本需要本机 Playwright 模块。双分支方法定义只加入验收目录使用的配置副本。数据库中的旧上下文和资产版本保存在同目录。

```bash
GEOPILOT_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs \
  GEOPILOT_WORKBENCH_URL=http://127.0.0.1:5191 \
  node artifacts/smoke/traceability-follow-up/verify-context.mjs
python artifacts/smoke/traceability-follow-up/verify-tool-rules.py http://127.0.0.1:5191
PYTHONPATH=src python artifacts/smoke/traceability-follow-up/verify-raster-gate.py
```

后端专项组合复核了候选快照、旧接口、输入检查和 T15 数据边界，共 108 项通过。最终上下文、版本和模型摘要联合复验 81 项通过。前端 67 项及 TypeScript/Vite 生产构建通过。[组合 JUnit](../../artifacts/test-results/traceability-targeted-junit.xml) · [前端 JUnit](../../artifacts/test-results/traceability-frontend-junit.xml)

## 回归中发现并修复的问题

- 两个既有模型流程的草稿与确认对象一致性断言发现工具规则只在确认时出现。草稿和确认现使用同一规则版本，原断言复跑通过。[修复前全量 JUnit](../../artifacts/test-results/traceability-before-plan-preview-junit.xml) · [修复后回归](../../artifacts/test-results/tool-004-regression-junit.xml)
- 只改候选条件且复用当前方案版本时，原实现提前返回并保留了旧上下文。在同一 SQLite 事务刷新当前任务快照后，真实浏览器重新生成并刷新均恢复新条件。[修复前浏览器日志](../../artifacts/smoke/traceability-follow-up/context-revision-before.log) · [修复后浏览器记录](../../artifacts/smoke/traceability-follow-up/context-browser.json)
- 并行回归中，Linux 正回收进程时读取 `/proc/<pid>/stat` 返回 `ESRCH`。原测试探针只处理 `ENOENT`。补齐两种退出情况的判定，保留原真实子进程、退出码、日志和成果断言。[原失败 JUnit](../../artifacts/test-results/traceability-execution-proc-race-junit.xml)

## 最终复验与签收

主控核对实际收集的 1,646 个后端用例标识，与四组回归和一项稍后新增的同版确认测试的 JUnit 逐一相等，无重复。后端 1,646 项和前端 67 项全部通过，均为零失败、错误和跳过。保留既有 Starlette 弃用提醒、缺 CRS 测试数据的 GDAL 写入提示，以及 Vite 包体提示。 [覆盖核对](../../artifacts/smoke/traceability-follow-up/coverage.json) · [收集清单](../../artifacts/smoke/traceability-follow-up/current-collection.txt) · [空间、端到端与集成](../../artifacts/test-results/traceability-full-spatial-junit.xml) · [方法、遥感与接口](../../artifacts/test-results/traceability-full-methods-junit.xml) · [执行、工具契约与检查](../../artifacts/test-results/traceability-full-execution-junit.xml) · [应用与回归](../../artifacts/test-results/traceability-full-application-junit.xml) · [同版确认补测](../../artifacts/test-results/traceability-full-context-final-junit.xml)

补齐内容已同步到公共契约和工作台说明。分析方案和方法定义的 JSON Schema 由当前模型重生并通过 Draft 2020-12 校验。四项逐一签收，需求矩阵为 40 PASS、0 PARTIAL、0 NOT_IMPLEMENTED。 [方案 Schema](../../artifacts/schemas/analysis-plan.schema.json) · [方法 Schema](../../artifacts/schemas/method-definition.schema.json) · [工作台说明](../user-guide/workbench.md) · [需求矩阵](../traceability/requirements-matrix.md)

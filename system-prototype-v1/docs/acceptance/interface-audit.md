# P11-04 跨层接口一致性审计

2026-11-12，Agent-A 执行，主控签收。按工作台实际 API 调用、FastAPI OpenAPI 3.1、当前 Pydantic 类型、SQLite 和原文件复核精确引用。主工作台 `127.0.0.1:5173` 经 Vite 代理到 `127.0.0.1:8000`，两处的 87 个 GET 返回相同的状态和内容。活跃服务以现有工作区运行，另通过现有 GET 路由读取两份已保存的真实模型样本数据库。[前端调用](../../frontend/src/api/workbench.ts) · [公共契约](../../plans/01_ARCHITECTURE/02_PUBLIC_CONTRACTS.md) · [结构化复核](../../artifacts/smoke/p11-04/run.or1ZGlPX/verification.json)

## 跨层实测

80 份 JSON 响应通过实际 OpenAPI 和本机 Pydantic 校验，另外校验已记录的本机模型建议。25 个确认、受理、上下文及模型请求形状与工作台的字段一致。确认和受理发送 `plan_version`，读取方案用 `version`，资产角色携带 `asset_id`、`asset_version`。查询另一运行的报告编号在两个端口均返回 HTTP 404 和 `CHECK_REPORT_NOT_FOUND`。[实际输出](../../artifacts/smoke/p11-04/run.or1ZGlPX/verification.log)

只读复核 3 份 SQLite、8 个运行和 25 个成果。各运行的来源、任务列表、完整确认方案、`request.json`、步骤结果、双状态和报告指针均对应同一记录。原输入与暂存输入的内容指纹一致。逐件核对成果的 `artifact_id`、`run_id`、`plan_id`、`plan_version`、输出角色、图层和指纹，HTTP 下载的 SHA-256 与注册文件字节一致。另以成果元数据读器重开栅格，带统计和共同有效像元数与 SQLite 相符。[结构化复核](../../artifacts/smoke/p11-04/run.or1ZGlPX/verification.json)

| 实际样本 | 版本和双状态 | 核对结果 |
|---|---|---|
| 原生 Landsat / Sentinel‑2 两期 NDVI | 方案 1，方法 `remote-sensing-index-change@1`，`SUCCEEDED / PASSED` | Chromium 刷新恢复 `d3e75535ae284e9d9f55368adf1b2d59`，所见 10 个成果和当前报告编号逐项等于 live HTTP 与 SQLite。 |
| 道路通过与规则阻断 | 同一确认方案 1，方法 `road-buffer-statistics@2`，分别 `SUCCEEDED / PASSED`、`SUCCEEDED / FAILED` | 两次实际计算各有 3 个独立成果编号。阻断运行 `ce21f10cca954467842be96de2fcb074` 的浏览器同时显示运行成功与检查未通过，正式统计 CSV 仍可读。 |
| 两个 DEM 运行 | 各自确认方案 1，方法 `dem-terrain-analysis@1`，`SUCCEEDED / PASSED` | 原坡度、带孔分析区坡度和分类结果的成果 ID 与其各自的来源记录一致。7 次 Chromium 原始像元请求重放后，对应原格及掩膜逐点一致，含坡度 `45`、孔内 NoData 和分类 `4`。 |
| 同一方案的两个版本 | `ee9b6d8a29244c738994eb17a5f3f3e8` 的版本 1 为草稿，版本 2 为确认 | `84bd1f101dcd49c7ae1120c8fba43027` 的 HTTP 来源、方案快照、SQLite 和成果指向版本 2，运行及检查均通过。按版本 1 查询仍返回对应草稿。 |
| Ollama 建议和超时手工回退 | 各自确认方案 1，`attribute-filter-export@1`，均 `SUCCEEDED / PASSED` | 已保存的真实 Chromium 建议和回退记录与两个原数据库、精确运行编号和各自 GeoPackage 对应。原建议按当前 API 响应结构验证，回退记录为 `MODEL_TIMEOUT`。 |

浏览器证据为 [NDVI 工作台恢复](../../artifacts/smoke/p8-native-workbench-verified/browser.json)、[道路双状态](../../artifacts/smoke/p5-10/run.HGGm18/browser.json)、[DEM 原值与掩膜](../../artifacts/smoke/p7-12/run.vLKUuq/browser.json)、[Ollama 建议](../../artifacts/smoke/p9-08/model-browser.json)和[超时手工回退](../../artifacts/smoke/p9-08/manual-fallback.json)。

## 问题及关闭证据

- **地图可见栅格点击竞态，已关闭。** 修复前全量 66 项中有 1 项失败。用例 `uses the actual topmost visible raster when no raster has been located` 实际请求了 `registered-raster`，预期为 `registered-threshold`。主控在 `RasterPreviewLayer.tsx` 内将 MapLibre 可见性与点击索引更新放在同一次绘制处理。修后全量 66 项通过，同一用例在并行负载下单独复跑 5 次均通过。[失败 JUnit](../../artifacts/test-results/p11-04-frontend-before-raster-fix-junit.xml) · [修后 JUnit](../../artifacts/test-results/p11-04-frontend-junit.xml) · [修复文件](../../frontend/src/features/map/RasterPreviewLayer.tsx)
- **真实浏览器复验。** Chromium 141 在 live NDVI 运行上读取 5 个原像元或掩膜结果，包含原指数、差值、NoData 和类别。另延迟类别层 PNG，在两层可见且未定位时点击命中类别层。隐藏类别层后，在同一屏幕位置点击命中差值层。两次真实请求的成果 ID 均与页面一致，返回状态分别为 `nodata` 和 `outside`。本轮重放全部 14 个像元查询，响应与原始 GeoTIFF 一致。[五点 Chromium 证据](../../artifacts/smoke/p8-10/browser.7TgLX6/browser.json) · [异步重叠层证据](../../artifacts/smoke/p8-10/browser.7TgLX6/topmost-raster.json)
- **审计取证脚本更正。** 首轮将资产元数据直接与成果对象比较，成果还有 `band_statistics` 和 `common_valid_pixel_count`。随后按正式成果读器完整比较。第二轮将已保存 DEM 的 query 对象当作 URL，后按实际字段还原请求。Chromium 数值脚本首次选用独立 Conda GDAL Python 时缺 `rasterio`，使用项目 Python 后通过。[首轮审计日志](../../artifacts/smoke/p11-04/run.or1ZGlPX/verification-first.log) · [第二轮审计日志](../../artifacts/smoke/p11-04/run.or1ZGlPX/verification-second.log) · [最终核对](../../artifacts/smoke/p11-04/run.or1ZGlPX/verification.log)

## 实际回归命令

后端在原型目录执行，实际 `2 passed in 45.62s`，零失败、零错误、零跳过。[集成 JUnit](../../artifacts/test-results/p11-04-integration-junit.xml)

```bash
GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process" PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest tests/integration -q -p no:cacheprovider --junitxml=artifacts/test-results/p11-04-integration-junit.xml
```

在 `frontend/` 执行 Vitest 和 TypeScript/Vite 构建，14 个文件、66 项通过，构建用时 610 毫秒。Vite 保留大于 500 kB 的包体提示。[前端 JUnit](../../artifacts/test-results/p11-04-frontend-junit.xml)

```bash
npm run test -- --run --reporter=default --reporter=junit --outputFile.junit=../artifacts/test-results/p11-04-frontend-junit.xml
npm run build
```

独立接口核对使用项目 Python、SQLite `mode=ro`、本机两个 HTTP 端口、实际 OpenAPI 和原始文件。模型样本的证据来自已保存运行。审计新增本报告及 [本轮核对目录](../../artifacts/smoke/p11-04/run.or1ZGlPX/)，界面修复证据见上述问题清单。覆盖范围为所列工作台样本、原像元查询和已有集成回归。

# 道路缓冲统计 v3 双模式验收

2026-11-12。按 V2.0 §5、§7.5、§10.1 和附录 A.1，将已通过 T04 的 WGS84 椭球测地线工具接入正式道路缓冲统计。v3 在同一表单选择测地线或局部投影，两种模式共用锁定七步、正式成果登记和规则报告。[产品设计](../../../docs/source/GeoPilot_产品需求规格与系统详细设计报告_V2.0.md) · [方法配置](../../config/methods/road-buffer-statistics.v3.yaml) · [T04 原工具验收](P6-09.md)

## 计算与取数口径

1. 测地线分支先从原资产准备 WGS84 分析区、目标和分区。对于投影面，源边按最多 100 m 细分后变换，保留内孔与来源标识。
2. 候选范围包含原分析区及其边界的保守椭球距离包络。道路最短测地弧按最多 5 m 取站点，外扩额外纳入 2.51 m 取数余量。选中道路保留原完整几何与属性，随后准备为 WGS84。
3. 正式缓冲调用 `python-wgs84-geodesic-buffer@1`，使用确认的米距离。实际测地弧侧边取站点的最大间隔为 25 m，圆端角间隔不超过 1°。正式缓冲距离与取数余量分开记录。
4. 完整目标先判断与缓冲的空间关系，再裁回 WGS84 分析区，最后按原始对象 ID 去重及分区计数。成果为完整缓冲面、裁回目标和分区 CSV。

对北纬 65° 的独立 T04 探针，目标至真弧的距离区间为 999.7300–999.7500 m，距坐标直弦约 1370.8193 m。1000 m 候选范围纳入真弧而排除远路。实际直弦与候选范围不相交。[高纬取数复验](../../artifacts/smoke/road-workflow/scope-high-latitude-reference.json)

## 工作台与 GIS 验收

Chromium 经真实 Vite 代理和本机 FastAPI 启动任务。逐个登记四个 GeoPackage 图层，在同一 v3 方法下选择距离方式，生成草稿、确认、执行、查看规则和步骤日志，在地图定位后下载三份原始成果。切到测地线时，局部投影 CRS 控件及其草稿字段均省略。曾在 v3 接入前创建的 v2 确认快照在新服务重新打开，按原步骤在独立运行目录完成复算。

局部投影使用 EPSG:32650 原黄金样本，500 m 仍纳入距原范围 300 m 的道路、排除 650 m 的道路。两版本结果均为西区 3、东区 2、未归属 1。测地线使用赤道参考样本，实际 `intersects` 得到相同的三组数量，`within` 排除西圆端的精确 500 m 边界点，得到西区 2、东区 2、未归属 1。赤道参考还核对了真实 GeoPackage 缓冲的西圆端距离、选中目标 ID、完整候选道路、范围外目标裁回和原文件指纹。[浏览器实测](../../artifacts/smoke/road-workflow/workbench.4t9svZFH/browser.json) · [独立赤道样本构造](../../tests/fixtures/geodesic/road_workflow.py)


| 工作台场景 | 运行 ID | 西区 / 东区 / 未归属 |
|---|---|---|
| 局部投影 v2 | `38800a00456b44f7b94ef1989e12f035` | 3 / 2 / 1 |
| 局部投影 v3 | `4a135d2dce0e4f2d9624f89389007173` | 3 / 2 / 1 |
| v2 原确认快照重放 | `78d200d0dc66469db23e0f1f35aaacfe` | 3 / 2 / 1 |
| 测地线 v3，`intersects` | `f2c7afedb31c481e9847099e5e0f4c51` | 3 / 2 / 1 |
| 测地线 v3，`within` | `0326add3a9754651958afa60d20ecff2` | 2 / 2 / 1 |

五次运行均为 `SUCCEEDED / PASSED`，共重新打开 15 个浏览器下载文件。局部投影两版本缓冲的 Hausdorff 距离为 0 m，统计 CSV 字节一致。独立赤道弧长换算在两次测地线下载件得到西圆端 500.000000000013 m，容差为 0.05 m。脚本核对了实际工具编号、版本、过程日志和输入 SHA-256。 [文件重开结果](../../artifacts/smoke/road-workflow/workbench.4t9svZFH/download-verification.json) · [测地线工作台](../../artifacts/smoke/road-workflow/workbench.4t9svZFH/geodesic-v3.png) · [局部投影工作台](../../artifacts/smoke/road-workflow/workbench.4t9svZFH/local-projected-v3.png)


## 使用范围

v3 保持既有点目标、质心分区计数及并列未归属规则。道路、目标和分区使用 `source_feature_id` 保存来源标识。测地线距离为 1–2000 m，原数据可使用已知的二维地理或投影 CRS。分析区、目标和分区转换后的局部范围及缓冲结果须符合 T04 的连续 WGS84 坐标域约束，包括 ±75° 纬度、最多 6° 经度跨度和避开反经线。参与正式缓冲的道路须满足 T04 的单段和单要素限额。取数范围保守覆盖影响带，靠近外扩边界的道路可以进入候选集合。v3 双模式通过手工工作台验收。[工作台操作](../user-guide/workbench.md) · [公共契约](../../plans/01_ARCHITECTURE/02_PUBLIC_CONTRACTS.md)

## 自动回归与主控签收

- 方案与 HTTP 专项 9 项通过，核对两模式工具、混合 CRS、米距离、原 v2、篡改工具拒绝及无效选项诊断。方法、编译、版本、模型和上下文兼容 164 项通过。 [专项](../../artifacts/test-results/road-distance-modes-junit.xml) · [兼容](../../artifacts/test-results/road-distance-compat-junit.xml)
- GIS 专项含原 T04 共 41 项通过，契约、前置和真实 QGIS 关联 110 项通过。投影面加密在独立 UTM 反投影样本的检查点误差为 0.000390 m。 [GIS 专项](../../artifacts/test-results/geodesic-road-scope-final-junit.xml) · [关联回归](../../artifacts/test-results/geodesic-road-regression-junit.xml) · [实际过程](../../artifacts/smoke/geodesic-road-scope/)
- 正式 HTTP 专项 5 项通过，覆盖 6 次七步运行。不同源 CRS、仅远路导致的空成果与零计数、同确认版复算均通过。成果检查重新构建原范围的候选包络、验证实际两段测量调用和缓冲对准备道路的覆盖，复制证据中的 499 m、错位缓冲和伪造外扩均触发失败。既有规则、T04 与工具级检查回归 48 项通过。 [HTTP 专项](../../artifacts/test-results/geodesic-road-workflow-junit.xml) · [原始运行与独立核验](../../artifacts/smoke/geodesic-road-workflow/run.FvhOOd/evidence.json) · [兼容回归](../../artifacts/test-results/geodesic-road-check-regression-junit.xml)
- 按当前收集清单核对 1,670 个后端用例标识。全量分组加最终道路专项复跑覆盖全部用例，最终零失败、零错误、零跳过。前端 68 项和生产构建通过，保留既有 Vite 包体提示。 [覆盖核对](../../artifacts/smoke/road-workflow/coverage.json) · [前端 JUnit](../../artifacts/test-results/road-workflow-frontend-junit.xml)

初次平面对照样本横跨赤道，北半球 UTM 带被已有适用范围门禁拒绝。改用覆盖样本纬带的 EPSG:3395 后，QGIS 对圆端离散的边界点按实际输出几何判定，独立测距仍验证 500 m 及边界内外 ±1 m。分组回归保留过早采用解析触点的失败记录，最终专项已复跑通过。主工作台局部投影仍使用原 EPSG:32650 黄金样本。 [原分组记录](../../artifacts/test-results/road-workflow-full-spatial-final-junit.xml) · [修正后专项](../../artifacts/test-results/geodesic-road-workflow-junit.xml)

主控签收道路 v3 椭球测地线和局部投影两种模式、T04 实际接入、v2 旧确认重放。复验可复用 [Chromium 验证脚本](../../artifacts/smoke/road-workflow/verify-workbench.mjs) 和 [下载重开脚本](../../artifacts/smoke/road-workflow/verify-downloads.py)。

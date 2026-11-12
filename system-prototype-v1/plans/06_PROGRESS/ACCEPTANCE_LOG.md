# 验收日志

按时间倒序追加主控验收记录。只记录实际执行的命令、输出位置和验收结论。

## 2026-11-12 · 道路缓冲统计 v3 · ACCEPTED

WGS84 椭球测地线接入正式七步，两种模式从真实 Chromium 创建、确认、运行和下载通过。独立 500 m 边界与混合 CRS 样本通过，局部投影 v2 原确认快照重放通过，v2/v3 统计 CSV 字节相同。1,670 后端用例与 68 前端用例通过。 [双模式验收](../../docs/acceptance/road-geodesic-workflow.md)

## 2026-11-12 · 通用需求补齐 · ACCEPTED

CTX-001、CTX-003、TOOL-003、TOOL-004 各自有真实 HTTP、Chromium 或 GIS 子进程证据，全部签收。主控对照收集清单确认后端 1,646 项全通过，前端 67 项和生产构建通过。矩阵现为 40 PASS、0 PARTIAL、0 NOT_IMPLEMENTED。 [验收记录](../../docs/acceptance/traceability-follow-up.md) · [追踪矩阵](../../docs/traceability/requirements-matrix.md)

## 2026-11-12 · P11-08、P11-EXIT · ACCEPTED

P11 八项和 P0–P11 共 122 项全部签收，最终清单 12 项核对完成。后端全量 1,595 项及新增端到端 1 项通过，当前收集 1,596 项。前端 66 项和生产构建通过，T01–T16 记录 16 PASS、0 FAIL、0 NOT_RUN。交付脚本验证 417 份源文件和 31 份证据。40 条需求 36 PASS、4 PARTIAL，正式道路测地线未接入与可选依赖已披露。[阶段签收](../../docs/acceptance/P11.md) · [最终验收](../../docs/release/FINAL_ACCEPTANCE.md) · [交付清单](../../artifacts/reports/final/delivery-manifest.json)。

## 2026-11-12 · P11-04、P11-05 · ACCEPTED

- 8 个运行、25 个正式成果、87 次双端口读取与跨层证据一致。栅格点击竞态修复后，前端 66 项及真实 Chromium 双层复验通过。[跨层接口](../../docs/acceptance/interface-audit.md)
- HTTP 道路两次和属性一次均通过，独立工作目录保留完整快照。Chromium 从空数据库验证双方法和正式成果查看。[真实复算](../../docs/acceptance/P11-05.md)

主控签收，放行 P11-08。

## 2026-11-12 · P11-07 · ACCEPTED

全新 venv、前端依赖和 SQLite 验证启动，净目录 42 项后端和 66 项前端通过，实际道路运行及三个下载成果核对通过。前端已同步栅格竞态修复并重新构建。[净目录验证](../../docs/acceptance/P11-07.md)。主控签收。

## 2026-11-12 · P11-01 · ACCEPTED

40 个编号逐项匹配 V2.0，36 PASS、4 PARTIAL、0 NOT_IMPLEMENTED；脚本、链接与部分能力说明已核对。[追踪矩阵](../../docs/traceability/requirements-matrix.md)。主控签收。

## 2026-11-12 · P11-03、P11-06 · ACCEPTED

- P11-03 方法回归和前置条件共 120 项通过，七种目录方法及真实 Python 扩展样例已复算。[方法报告](../../docs/acceptance/P11-03.md)
- P11-06 运行生命周期 62 项通过，复核 20 份 SQLite、22 个运行及四个消失的实际 PID，检查重跑保留已登记成果。[生命周期](../../docs/acceptance/lifecycle.md)

主控签收。

## 2026-11-12 · P11-02 · ACCEPTED

T01–T16 全部通过；真实 GIS、HTTP 和持久化组合共 122 项通过，0 跳过。[固定用例复跑](../../docs/acceptance/P11-02.md)。主控签收。

## 2026-11-12 · P8-10 与 P8 阶段 · ACCEPTED

- 正式两期方法、原生 TIFF/JP2 打包、十件成果和真 Chromium 原像元通过，未知版本在执行前阻断。确认版本在配置出现歧义后仍能使用保存快照复算。阶段联合 234、前端 66，T10–T12 PASS。P8 10/10，累计 114 项验收，放行 P11。[阶段验收](../../docs/acceptance/P8.md) · [正式方法](../../docs/acceptance/P8-10.md)

## 2026-11-12 · P8-09 · ACCEPTED

- 真实差值分类的边界与掩膜通过。等积网格仿射面积和原 UTM 像元转等积面积均独立核对，未重采样。专项 16、联合 280、主控联合 240 项通过。[验收](../../docs/acceptance/P8-09.md)

## 2026-11-12 · P8-08 · ACCEPTED

- 正式差值固定第二期减第一期。真实错位栅格实测七个共同有效、五个 NoData，正负零和极值匹配手算。专项 16、联合 232、主控联合 228 项通过。[验收](../../docs/acceptance/P8-08.md)

## 2026-11-12 · P8-07 · ACCEPTED

- 真实同格、错位和跨 UTM 的逐像元对齐通过，bilinear 先清洗越界值，AOI 孔和两期共同有效掩膜一致。真实 Landsat 30 m 与 Sentinel-2 10 m 链贯通，专项 18、联合 216、主控联合 197 项通过。[验收](../../docs/acceptance/P8-07.md)

## 2026-11-12 · P8-06 · ACCEPTED

- 实际定标和质量掩膜贯通原网格 NDVI，严格零分母、NoData 与范围异常各有证据。真实 Sentinel-2 XML 和合成原 DN 串真实 GIS，专项 14、联合 191、主控联合 194 项通过。[验收](../../docs/acceptance/P8-06.md)

## 2026-11-12 · P8-05 · ACCEPTED

- Landsat 位掩膜和 Sentinel SCL 按正式产品配置生成同格 UInt8 掩膜。合法 QA 0 保留，已声明无效位、填充值、未知类别和原掩膜参与计算。专项 9、联合 147、主控联合 177 项通过。[验收](../../docs/acceptance/P8-05.md)

## 2026-11-12 · P8-04 · ACCEPTED

- 正式产品字段驱动双带反射率，Landsat 逐带乘加、Sentinel 逐带偏移后量化、无效 DN 与合法反射率 0 均有真实 TIFF 和手算证据。专项 11，联合 166，主控联合 177 项通过。[验收](../../docs/acceptance/P8-04.md)

## 2026-11-12 · P8-03 · ACCEPTED

- 登记和暂存后重开真实原 DN 栅格，按原生元数据及精确波段描述定位 Landsat 和 Sentinel-2。原始 Sentinel-2 PB05.11 XML 实证保留，独立核对 B04/B08 与 SCL 分辨率，B8A 误选被拒绝。最终主控联合 157 项通过。[验收](../../docs/acceptance/P8-03.md)

## 2026-11-12 · P8-02 · ACCEPTED

- 精确三键唯一查找与版本回取通过，零命中及多命中各有结构化错误。交叉审查复现内存实例绕开字段校验，已补回归并在构造和查询入口复核。专项 18，联合 92 项通过。[验收](../../docs/acceptance/P8-02.md)

## 2026-11-12 · P8-01 · ACCEPTED

- 五份版本化配置保留精确产品选择键、逐波段元数据位置、质量规则与官方资料。独立 Schema 验证通过，主控联合 128 项通过。[验收](../../docs/acceptance/P8-01.md)

## 2026-11-12 · P7-12 与 P7 阶段 · ACCEPTED

- 真 Chromium 以精确成果引用读取原 TIFF 像元，核对两种显示配色、NoData、行列、来源和迟到响应。独立 GDAL 对比 20 条 HTTP 请求，后端联合 55 项，前端 66 项。P7 12/12、T09 PASS，放行 P8。[原像元验收](../../docs/acceptance/P7-12.md) · [阶段报告](../../docs/acceptance/P7.md)

## 2026-11-12 · P7-11 · ACCEPTED

- 实际两份正式栅格各生成两份独立保存的 PNG 与预览记录。真实 MapLibre 通过换色、显隐、定位和晚响应防护，原 TIFF 指纹、字节和修改时间一致。后端专项 16、主控联合 44、前端 60 项通过。[验收](../../docs/acceptance/P7-11.md) · [浏览器证据](../../artifacts/smoke/p7-11/run.fP4CLC/browser.json)

## 2026-11-11 · P7-10 · ACCEPTED

- 正式七步坡度与坡向、栅格统计和逐像元检查通过。取消检查后重查保留原成果与步骤。主控最终联合 165 项，前端 52 项。真实工作台确认和执行得到 9 个分类像元及 900.720432197735 m²，刷新恢复同一报告。[验收](../../docs/acceptance/P7-10.md) · [联合 JUnit](../../artifacts/test-results/p7-10-joint-junit.xml) · [工作台记录](../../artifacts/smoke/p7-10/terrain-browser.json)

## 2026-11-11 · P7-09 · ACCEPTED

- 实际分类栅格的原像元边界转换到已选等积 CRS，按类别汇总有效像元。真实 UTM 和等积网格通过，无效目标预检阻断。主控联合 29 项通过。[验收](../../docs/acceptance/P7-09.md) · [主控 JUnit](../../artifacts/test-results/p7-09-root-junit.xml)

## 2026-11-11 · P7-08 · ACCEPTED

- 阈值分类以实际有效像元和明确开闭边界计算，支持同类别不交叠区间，保留原网格。主控重跑 32 项通过。[验收](../../docs/acceptance/P7-08.md) · [主控 JUnit](../../artifacts/test-results/p7-08-root-junit.xml)

## 2026-11-11 · P7-07 与 T09 · ACCEPTED

- 实际五步 DEM 计算裁回 3×3，九个 45° 与整图独立 GDAL 参考逐像元一致。先裁再算的八个边缘为 NoData。最终专项 7、联合 123 项通过，含十进制网格的边界回归，T09 PASS。[独立比对](../../artifacts/smoke/p7-07/t09/gdal-comparison.json) · [专项 JUnit](../../artifacts/test-results/p7-07-junit.xml)

## 2026-11-11 · P9-09 与 P9 阶段 · ACCEPTED

- 模型建议与手工的确认方案、真实 QGIS 结果等价，超时后的原草稿也实际通过。专项 2、联合 206 项。主控验收 P9 全部 9 项，T15 PASS。[阶段报告](../../docs/acceptance/P9.md)

## 2026-11-11 · P6 工作台联调 · PASS

- 真实浏览器完成土地利用七步方案，规则通过，矩阵逐项一致且总面积 8500 m²，三个变化图斑在地图定位。[联调记录](../../docs/acceptance/P6.md#工作台联调)

## 2026-11-11 · P7-05 · ACCEPTED

- 真 DEM Horn 坡度支持度与百分比，实际源尺度和单位比例。独立 GDAL 全网格一致，专项 11、联合 114 项通过。[坡度验收](../../docs/acceptance/P7-05.md)

## 2026-11-11 · P7-06、P9-07 与 T15 · ACCEPTED

- 真 DEM 坡向与独立 GDAL 逐像元一致，坡向与单位 13 项通过。[坡向验收](../../docs/acceptance/P7-06.md)
- 模型客户端验证完整请求、有限重试与失败类别。专项 48、联合 240 项，真实本机模型生成候选，完整 HTTP 请求捕获与拒绝零连接通过。T15 PASS。[客户端验收](../../docs/acceptance/P9-07.md)
- 实际模型的浏览器建议由用户修改后，走既有方案与 QGIS 运行，正式成果命中参考点。[浏览器证据](../../artifacts/smoke/p9-08/model-browser.json)

## 2026-11-11 · P9-08 · ACCEPTED

- 候选预览复用同一手工表单。实际浏览器模型超时后原草稿与字段保留，同一版本确认并真实 QGIS 成功。HTTP 3、前端 52 项通过。[手工入口](../../docs/acceptance/P9-08.md)

## 2026-11-11 · P7-04 · ACCEPTED

- 两个真实 Python 步骤交接外扩记录和准备 DEM，复合 CRS、仿射、单位、NoData 与原掩膜保留。8 项专项、99 项关联通过。[准备验收](../../docs/acceptance/P7-04.md)

## 2026-11-11 · P9-03 · ACCEPTED

- 检查完整序列化候选请求，拒绝未知结构、路径、原始样本、坐标、非有限值及转义泄漏。专项 50、联合 152 项通过。T15 仍需合并实际适配调用证据。[请求边界](../../docs/acceptance/P9-03.md)

## 2026-11-11 · P9-05 与 P9-06 · ACCEPTED

- 结构化候选解析专项 52 项、联合 137 项通过，文字说明失败保留有效候选。[解析验收](../../docs/acceptance/P9-05.md)
- 当前本机版本、注册工具和参数校验联合 8 项。真实道路 v2 草稿按本机固定顺序展开，不产生正式记录。[候选校验](../../docs/acceptance/P9-06.md)

## 2026-11-11 · P7-02 与 P7-03 · ACCEPTED

- 邻域外扩按实际像元边界，缺源覆盖明确失败。专项 22、关联 68 项。[外扩](../../docs/acceptance/P7-02.md)
- DEM 单位比值通过实际 GeoTIFF、复合 CRS、真 Python 子进程及请求和日志核对。专项 9、关联 80 项。[单位](../../docs/acceptance/P7-03.md)

## 2026-11-11 · P9-04 · ACCEPTED

- 真实 QGIS 和 Python 注册表验证仅有效候选及精确步骤和选项。联合 11 项通过；追加本地方法后候选同步变化。[候选请求](../../docs/acceptance/P9-04.md)

## 2026-11-11 · P7-01 与 P9-01 · ACCEPTED

- 栅格窗口、掩膜和分块发布在真实 GeoTIFF/COG 验证，25 项专项、60 项关联通过。[栅格窗口](../../docs/acceptance/P7-01.md)
- 有限 UTF-8 JSON、统一 provider 接口与安全错误通过。专项 34、关联 163 项通过；固定返回未联网。[适配接口](../../docs/acceptance/P9-01.md)

## 2026-11-11 · P9-02 · ACCEPTED

- 8 项专项核对真实 GPKG 和 TIFF 的精确版本摘要、自由文本本机引用、禁止字段和恶意元数据。放行最终请求扫描和候选构建。[摘要验收](../../docs/acceptance/P9-02.md)

## 2026-11-11 · P10-07 与 P10 阶段 · ACCEPTED

- 真实脚本同编号失败隔离和实际 PostGIS 停库后两版复现通过，完整运行 2 项无跳过。缺 Docker 的独立试验准确区分 NOT_RUN。主控验收 P10 全部 7 项。[阶段记录](../../docs/acceptance/P10.md)

## 2026-11-11 · P5-11 与 P5 阶段 · ACCEPTED

- Chromium 两次刷新真实道路运行，恢复精确来源、地图版本、三个成果及当前报告。只观察到用户发起的一次 POST；跨任务链接拒绝。[恢复验收](../../docs/acceptance/P5-11.md)
- 主控验收 P5 全部 12 项。前端完整 46、最终 HTTP 16、单位修复关联 18 项通过，真实属性和道路从 UI 独立确认并运行。[阶段报告](../../docs/acceptance/P5.md)

## 2026-11-11 · P5-12 · ACCEPTED

- 展开实际错误、访问日志和按原确认版本重跑。真实 Python 成果重查保留文件和原执行，局部地图失败可重试。最终前端全量 46 项通过。[错误体验验收](../../docs/acceptance/P5-12.md)

## 2026-11-11 · P10-04 · ACCEPTED

- 受审脚本手动收录校验通过。专项 12、关联 152 项通过。真实脚本用同一指纹在试验目录和隔离正式方案运行，实际空间输出与独立参考一致。[收录验收](../../docs/acceptance/P10-04.md)

## 2026-11-11 · P10-03 · ACCEPTED

- 已发布脚本运行独立浏览。6 项 API 专项，主控装配后 46 项前端测试、生产构建通过；浏览器实比脚本日志与成果字节，并预览 WGS84 坐标及保留失败记录。[脚本验收](../../docs/acceptance/P10-03.md)

## 2026-11-11 · P5-10 · ACCEPTED

- 规则总状态与执行独立。主控用实际工作台核对失败规则、报告历史和当前状态；完整前端 41 项通过。[检查验收](../../docs/acceptance/P5-10.md) · [工作台证据](../../artifacts/smoke/p5-workbench/check-details-browser.json)

## 2026-11-11 · P5-06 · ACCEPTED

- 草稿、确认与运行独立，浏览器确认前后核对真实运行计数，属性 v2 与道路方法 v2 的精确提交均实际成功。前端联合 10 项。距离单位误判修复后与真实 QGIS 联合回归 18 项通过。[验收报告](../../docs/acceptance/P5-06.md)

## 2026-11-11 · P5-05 · ACCEPTED

- 主控重跑表单及方案预览 8 项。浏览器核对实际道路 7 步、属性筛选 1 步及缺项方案。问题链接聚焦精确输入字段，参数顺序和原快照一致。[验收报告](../../docs/acceptance/P5-05.md) · [联合 JUnit](../../artifacts/test-results/p5-05-root-junit.xml)

## 2026-11-11 · P5-08 · ACCEPTED

- 地图专项 4 项通过。真实 Chromium 绘制本地点、线、面，局部预览 422 可单独重试，原统计下载和日志独立可访问。
- 主控通过实际工作台点选成果并核对原 ID，隐藏后两次点击成果定位均重新显示。[地图验收](../../docs/acceptance/P5-08.md) · [工作台联动](../../artifacts/smoke/p5-workbench/map-artifact-browser.json)

## 2026-11-11 · P5-04 与 P10-06 · ACCEPTED

- P5-04 精确方法版本驱动角色与关键选项。真实浏览器核对道路 v2、属性筛选 v1 的请求和服务端角色判定，检查土地利用默认和非法 JSON。专项 5 项；主控全量前端重跑 29 项通过。[方法表单](../../docs/acceptance/P5-04.md)
- P10-06 PostGIS 同一逻辑资产追加独立快照。真实源变更和停库后两个固定方案分别完成计算。8 项专项、124 项关联通过，并发和数据库提交异常回滚覆盖。[PostGIS 版本](../../docs/acceptance/P10-06.md)

## 2026-11-11 · P5-09 · ACCEPTED

- 主控验收正式成果、精确来源、下载与诊断列表。2 项前端专项通过；浏览器核对真实 QGIS GeoPackage 下载字节及道路统计 CSV 的列和全部三行。
- 道路运行成功且规则检查失败，正式成果可读性由后端独立提供。[验收报告](../../docs/acceptance/P5-09.md)

## 2026-11-11 · P5-07 与 P10-02 · ACCEPTED

- 主控在真实 Chromium 验证 QGIS 日志、终态停止轮询和页面取消真实运行。[运行面板](../../docs/acceptance/P5-07.md)
- A 从独立快照启动真实脚本，重开单一要素、退出和两路日志。实际超时确认主进程与后代已停止，正式运行隔离。专项 15 项、关联 76 项通过。[脚本进程](../../docs/acceptance/P10-02.md)

## 2026-11-11 · P5-03 与 P10-05 · ACCEPTED

- F 交付真实目录，六个确切方法版本均在 Chromium 核对。独立无 QGIS 实例显示准确不可用原因，Python 方法仍可选择。专项 4 项、关联 11 项和构建通过。[方法目录](../../docs/acceptance/P5-03.md)
- B 交付实际 PostGIS 导出。隔离容器内两条命中落盘至正式 GeoPackage，关闭数据库后按指纹重开，空间筛选精确命中 ID 1。专项 5 项、关联 99 项通过。[PostGIS 导入](../../docs/acceptance/P10-05.md)

## 2026-11-11 · P5-02 · ACCEPTED

- 主控在真实 Chromium 通过本机 API 枚举、登记和选择实际 GeoPackage，资产版本、ID、坐标系和要素数一致。组件 3 项、HTTP 8 项与生产构建通过。[资产面板](../../docs/acceptance/P5-02.md)

## 2026-11-11 · P5-01 与 P10-01 · ACCEPTED

- F 交付 React 工作台壳，4 个组件测试、实际桌面与窄屏 Chromium、TypeScript/Vite 构建均通过。主控检视桌面截图并打开本机预览。真实 GIS 工作台 API 联合 8 项通过。[工作台](../../docs/acceptance/P5-01.md) · [接口 JUnit](../../artifacts/test-results/p5-workbench-api-junit.xml)
- A 交付独立脚本版本和快照，真实暂存 GeoPackage 在源文件移除后仍可读。六并发草稿、同编号发布、准备失败回滚验证通过。专项 24 项、关联 86 项通过。[脚本快照](../../docs/acceptance/P10-01.md)

## 2026-11-11 · P4 阶段 · ACCEPTED

- 16/16 原子任务验收。T16、实际失败和取消、执行与检查中断的重启、正式成果及独立重查通过。主控签收，放行 P5 和 P10 已满足依赖的任务。[阶段验收](../../docs/acceptance/P4.md)
- 最新完整后端回归 900 项通过，使用实际 QGIS 环境，无失败、错误或跳过。[全量 JUnit](../../artifacts/test-results/p4-p6-stage-regression-junit.xml)

## 2026-11-11 · P4-16 · ACCEPTED

- A/F 与主控完成真实检查中断、取消、重查、历史和恢复。重查沿原版本和成果，文件/版本变化拒绝。联合 44 项、真实崩溃窗口 3 项通过。[重查验收](../../docs/acceptance/P4-16.md)

## 2026-11-11 · P6-11 与 P6 阶段 · ACCEPTED

- B 交付正式土地利用七步。三种归属各核对 8500 平方米，关闭合并 5500 平方米，全过滤零面积和空 GeoPackage。实际来源篡改与同总量矩阵偏差都被定位。专项 7 项、关联 53 项、主控土地利用联合 16 项通过。[正式土地利用](../../docs/acceptance/P6-11.md)
- P6 11/11。T04、T13、T14 和土地利用样例均通过，固定用例联合复跑 41 项。主控签收算法与后台交付，页面联调仍随 P5。[阶段验收](../../docs/acceptance/P6.md)

## 2026-11-11 · P4-15 · ACCEPTED

- F 交付 SQLite v4 原子报告、指针与历史查询。A 与主控通过真实 QGIS、HTTP 和 SQLite 验证成功执行与检查阻断、警告、中断。三个真实进程均退出 0，T16 PASS。持久化相关 44 项、主控 HTTP 与旧检查联合 16 项通过。[报告验收](../../docs/acceptance/P4-15.md)

## 2026-11-11 · P4-14 · ACCEPTED

- A 完成真实 GeoPackage、CSV 与 GeoTIFF 成果规则，核对图层、指纹、几何、统计、栅格网格、掩膜和独立边缘/内部数值。专项 14 项、主控与恢复/HTTP 联合 25 项通过。[成果规则](../../docs/acceptance/P4-14.md)

## 2026-11-11 · P6-10 · ACCEPTED

- B 完成真实土地利用前四步。手算四个转移组合合计 10,000 平方米，异 CRS 成果重开为 9999.999988536016 平方米。缺码阻断真实映射启动，50 平方米重复与等量缺口分别拒绝。专项 9 项、关联 252 项通过。[分类与叠加](../../docs/acceptance/P6-10.md)

## 2026-11-11 · P6-08 · ACCEPTED

- F 交付真实三步面分区统计。三种归属共 6 个唯一对象、2200 平方米，物理未覆盖均为 300 平方米。主控修复空目标 CRS 前置，三真实后台步骤成功并产出零汇总。专项和主控复跑均 8 项，关联 88 项通过。T14 PASS。[去重与统计](../../docs/acceptance/P6-08.md)

## 2026-11-11 · P4 后台 HTTP 联调

- 本机 CLI 实际启动并通过回环 HTTP 读取 OpenAPI。HTTP 受理后后台独立领取真实 Python GIS。强制终止旧服务进程的测试中，旧子进程持锁时新服务拒绝恢复，子进程停止后同一编号恢复为 `FAILED / NOT_CHECKED`。后台、取消、超时及真实 QGIS 联合 26 项通过。[JUnit](../../artifacts/test-results/p4-runtime-junit.xml)
- 独立审查复现方法确认漏装已有前提。修复后通过本机应用确认内置属性筛选方法，实际后台 QGIS 生成 1 个要素。HTTP 专项 5 项通过。[JUnit](../../artifacts/test-results/p4-local-app-junit.xml)
- P0 至当前已交付范围累计回归 831 项通过，0 失败、错误或跳过。执行时排除仍在开发的 P4-14 和 P6-10 专项文件。[JUnit](../../artifacts/test-results/p4-p6-verified-regression-junit.xml)
- F 复现服务崩溃时运行步骤永久停留在 `RUNNING` 和未提交成果清单残留。主控恢复实际步骤及日志，清理 SQLite 未提交的清单；两种真实崩溃重跑通过，与 HTTP、调度及成果规则合计 25 项通过。[JUnit](../../artifacts/test-results/p4-recovery-artifact-checks-junit.xml)

## 2026-11-11 · P4-11 与 P4-13 · ACCEPTED

- A 交付 SQLite v3 和正式成果登记。主控接入运行事务及 HTTP；两步实际输出缺失或损坏时零正式记录。专项 14 项、关联 32 项、主控集成 75 项、HTTP 及故障 22 项通过。[成果登记](../../docs/acceptance/P4-11.md)
- B 交付过程检查。真实 Python、QGIS 及变更证据逐项定位到步骤/字段；主控复跑专项 11 项通过。[过程检查](../../docs/acceptance/P4-13.md)

## 2026-11-11 · P4-10 · ACCEPTED

- 同一确认版本和新确认版本均从空目录与第一步重新计算，旧运行快照、日志及数据库记录不变。关联 30 项通过。P4 HTTP 确认和受理联调 24 项通过。[重试验收](../../docs/acceptance/P4-10.md)

## 2026-11-11 · P4-09 · ACCEPTED

- 主控实测启动失败、非零、超时、无效清单、缺文件与损坏文件。失败定位到第二步，第三步未启动。旧产物和可读/损坏的部分成果从运行证据读取，无正式成果。故障与取消联合 9 项通过。[失败停止](../../docs/acceptance/P4-09.md)

## 2026-11-11 · P4-07 与 P6-07 · ACCEPTED

- 主控核对排队取消、步间取消、真实 GIS 活动进程退出及后续待执行。取消意图从 SQLite 轮询，超时错误仍独立；真实调度与应用联合 46 项通过。[取消验收](../../docs/acceptance/P4-07.md)
- F 用真实等积 GPKG 复核完整面与裁后不同胜者、近似并列顺序无关和无重叠等。专项 9 项、相关 45 项、主控与测地线联合 34 项通过。[最大重叠验收](../../docs/acceptance/P6-07.md)

## 2026-11-11 · P6-04 与 P6-09 · ACCEPTED

- F 交付真实两步关系方法。点、线、面全部用双谓词复核，原 ID 与几何保留，独立裁剪样本可重开。专项 8 项、相关 142 项通过。主控与面积拆分联合复跑 19 项通过。T13 PASS。[完整关系与裁剪](../../docs/acceptance/P6-04.md)
- B 交付 WGS84 线缓冲，专用契约通过统一入口和输入门禁。16 个固定探针通过，解析边界符合 0.05 米容差。专项 25 项、相关 35 项通过，主控联合复跑 34 项通过。T04 PASS。[测地线验证](../../docs/acceptance/P6-09.md)

## 2026-11-11 · P4-08 与 P6-05 · ACCEPTED

- 主控修复实际 GIS 主进程退出后静默后代存活。分组终止并保留 stdout、stderr 和退出码，实际 Python 与 QGIS 回归 42 项通过。[超时验收](../../docs/acceptance/P4-08.md)
- A 交付真实等积面拆分。主控复核跨区 24/18 平方米及斜共边、极小窄缝、实际重叠行为，专项 11 项通过，与质心联合复跑 24 项通过。[面积拆分验收](../../docs/acceptance/P6-05.md)

## 2026-11-11 · P4-06 · ACCEPTED

- HTTP 查询在真实调度前后返回 SQLite 已保存状态。规则检查失败与计算成功可同时返回，404 区分缺失记录，500 区分存储故障。专项 2 项、关联 42 项通过。[验收](../../docs/acceptance/P4-06.md)

## 2026-11-11 · P4-05 · ACCEPTED

- 主控用真实 GeoPackage 核对路径、指定图层、几何类型和文件可读性。真实生产步骤结束后删去或损坏成果，两种情况下消费者均未启动，后续步骤保持待执行。专项 9 项通过。[验收](../../docs/acceptance/P4-05.md)

## 2026-11-11 · P4-04 · ACCEPTED

- 主控接通后台受理快照与真实 Python GIS 有序子进程，持久化实际步骤。成功链及第二步不可用的失败链实测 2 项通过，联合真实回归 133 项通过。[执行验收](../../docs/acceptance/P4-04.md)

## 2026-11-11 · P6-03 与 P6-06 · ACCEPTED

- F 复用原对象选择路径，实际点线面的 within 精确命中 1、3、4 个，手算边界差集一致。专项 5 项通过。[内部关系](../../docs/acceptance/P6-03.md)
- A 交付裁前完整面质心归属，使用真实 GPKG 样本核对区界、孔洞、凹面与重叠，13 项通过。主控两任务合跑 18 项通过。[质心](../../docs/acceptance/P6-06.md)

## 2026-11-11 · P4-12 与 P6-02 · ACCEPTED

- B 实现有类型输入规则与可选前置钩子。真实自交样本阻断 QGIS，合法样本输出实际缓冲。专项及主控复跑均 18 项通过，相关回归 65 项通过。[输入检查](../../docs/acceptance/P4-12.md)
- F 以实际点线面复核相交的精确 ID 及原几何，专项 17 项、相关回归 44 项通过。T13 继续等待 within 与完整流程。[相交选择](../../docs/acceptance/P6-02.md)

## 2026-11-11 · P4-03 · ACCEPTED

- 主控实测 SQLite 双线程领取、异常保留、两类启动恢复与停止信号，4 项通过，关联 22 项通过。放行 P4-04。[验收](../../docs/acceptance/P4-03.md)

## 2026-11-11 · P4-01 · ACCEPTED

- A 交付运行双状态与有序步骤仓库、v1 至 v2 迁移。两份真实 P3 库副本保留历史记录、执行/检查组合和确认版本外键。专项模型与存储 22 项通过，主控连同受理复跑 27 项通过。[验收](../../docs/acceptance/P4-01.md)

## 2026-11-11 · P6-01 · ACCEPTED

- F 交付点、线、面 GeoPackage 与独立手算预期，专项 12 项、黄金回归 21 项通过。主控联合复跑 30 项通过，核对构造器与重开精确几何。T13 产品验收仍按后续任务推进。[样本验收](../../docs/acceptance/P6-01.md)

## 2026-11-11 · P4-02 · ACCEPTED

- 主控核对真实确认快照、GeoPackage 指纹、同一活动运行受理、双线程竞争与插入失败清理。专项 5 项通过，输入暂存与工作目录关联测试共 27 项通过。[运行受理证据](../../docs/acceptance/P4-02.md)

## 2026-11-11 · P3 阶段 · ACCEPTED

- 10/10 原子任务验收，累计 37/122。真实七步复算、第二方法复用、检查中断与固定 T07/T08 验收通过。累计 626 项测试通过，0 失败、错误或跳过。主控签收。[阶段报告](../../docs/acceptance/P3.md)

## 2026-11-11 · P3-10 · ACCEPTED

- F 实测编辑生成新草稿、旧运行仍引用原确认快照、并发相同提交复用、旧基线不同提交拒绝及事务失败回滚。专项 9 项、关联 77 项通过。显式单位表达随选项继承。主控核对实现与实际报告并签收。[验收](../../docs/acceptance/P3-10.md)

## 2026-11-11 · P3-09、T07/T08 与检查中断

- P3-09 ACCEPTED。A 实测 12 项通过，确认前重验方法、确切工具、真实资产及输入前提，原子保存确认快照。确认无运行记录，空规则拒绝。[验收](../../docs/acceptance/P3-09.md)
- T07/T08 PASS。主控通过精确确认版本两次真实执行七步，核对 300 m 与 650 m 道路的取数、六个裁回点和分区计数。
- B 实现检查中断 `SUCCEEDED / NOT_CHECKED` 和报告写入失败的 SQLite 回退，专项 5 项通过。主控实跑属性筛选并验证正常规则未通过与异常中断的独立状态。[实跑证据](../../docs/acceptance/P3-road-workflow.md)

## 2026-11-11 · P3-08 · ACCEPTED

- A 交付有序计划编译与实际文件交接。两步真实 QGIS 均退出 0，显式距离转换为 500 m，命名缓冲成果交接，八点集合和独立几何参考一致。专项 30 项、关联 119 项通过，主控独立复跑 30 项通过并签收。[证据](../../docs/acceptance/P3-08.md)

## 2026-11-11 · P3-07 · ACCEPTED

- A 使用真实 GeoPackage、SQLite 和目录配置生成有序草稿，保持选定资产版本与明确参数，定位缺项、条件和无效前序引用。专项 13 项、关联 117 项通过，主控与真实上下文联跑 20 项通过。签收并放行 P3-08。[证据](../../docs/acceptance/P3-07.md)

## 2026-11-11 · P3-05 · ACCEPTED

- F 在实际道路方法上核对七步顺序，拒绝新增、缺失和重排，保留依赖错误。专项 8 项、联合 73 项通过，主控独立复跑 8 项通过。[证据](../../docs/acceptance/P3-05.md)

## 2026-11-11 · P3-03 · ACCEPTED

- B 核对工具确切版本、端口、参数、成果和规则注册。实用 QGIS 3.44.15 目录验证属性筛选配置，实际道路结构示例的未绑定字段逐项定位。专项 22 项，关联回归 157 项，主控复跑 22 项通过并签收。[验收证据](../../docs/acceptance/P3-03.md)

## 2026-11-11 · P3-04 · ACCEPTED

- F 交付通用于方法与方案的依赖校验，覆盖缺失角色、命名输出、重复键、自引用、未来引用和循环，保留声明顺序。专项 9 项，关联回归 65 项通过，主控复跑 9 项通过并签收，放行 P3-05。[验收证据](../../docs/acceptance/P3-04.md)

## 2026-11-11 · P3-02 · ACCEPTED

- B 接入本机方法目录和版本查找。实读两份样例，V2.0 道路定义逐项一致。重复文件、重复键、坏标签和无效字段均有真实文件复现。专项 16 项，主控方法联合复跑 48 项通过。签收并放行 P3-03、P3-04、P3-07。[验收证据](../../docs/acceptance/P3-02.md)

## 2026-11-11 · P3-06 与任务上下文前置 · ACCEPTED

- A 与 F 并行完成方案模型和独立测试，40 项通过。主控核对实际 V2.0 草稿、确认快照和 Schema；模型关联回归 212 项通过。[方案验收](../../docs/acceptance/P3-06.md)
- 任务上下文使用真实资产版本、图层和测量前提，7 项通过。P3-07 的接口前置满足，仍需 P3-02 目录验收。[上下文证据](../../docs/acceptance/P3-07-context.md)

## 2026-11-11 · P3-01 · ACCEPTED

- A 实现通用方法模型与输入 JSON Schema，直接解析 V2.0 四份示例，分别保留 7、7、8、7 步的声明顺序。F 独立实跑结构、严格版本、选项和绑定断言，32 项通过。主控复跑 32 项通过并签收，放行 P3-02 和 P3-06。[验收证据](../../docs/acceptance/P3-01.md)

## 2026-11-11 · P2 阶段 · ACCEPTED

- 10/10 原子任务验收，累积 27/122。主控运行累计回归 380 项通过，0 失败、错误、跳过。
- T01、T02、T03、T05、T06 全部 PASS，真实引擎、输入指纹、错误阶段、独立几何与数值参考已核对。主控签署放行 P3-01。[P2 阶段报告](../../docs/acceptance/P2.md)

## 2026-11-11 · P2-10 · ACCEPTED

- B 交付类型化步骤模型和四份 JSON Schema，46 项测试通过。主控接入真实测量前置检查，共用协议回归 50 项通过。
- F 独立验证统一入口的 T01、T02、T03、T05、T06，5 项通过，0 失败或跳过。三项拒绝未启动计算，两个真实引擎的结果通过独立几何和面积参考。主控核对结果、请求和进程日志并签收，固定用例更新为 5 PASS、11 NOT_RUN。[验收证据](../../docs/acceptance/P2-10.md)

## 2026-11-11 · P2-07 · ACCEPTED

- A 实现真实投影转换与无覆盖发布，F 独立复跑 12 项通过。实测 EPSG:4326 道路转换成 EPSG:32650，源指纹不变，几何最大 Hausdorff 偏差约 1.36×10⁻⁹ m。主控 GIS 联合 51 项通过，签收。[验收及样例](../../docs/acceptance/P2-07.md) · [联合 JUnit](../../artifacts/test-results/p2-gis-junit.xml)

## 2026-11-11 · P2-08 参数绑定复验 · ACCEPTED

- B 使用真实 QGIS `--json help` 校验参数、成果角色和枚举索引。主控独立复跑拼错参数绑定，拦截前仅运行定义查询，无计算启动。B 专项回归 17 项通过、0 跳过。[专项报告](../../docs/acceptance/P2-08.md) · [主控复验](../../artifacts/test-results/p2-08-binding-recheck-junit.xml)
- 主控签收复验，放行 P2-10。

## 2026-11-11 · P2-08 · REWORK

- F 实跑将 `DISTANCE` 的绑定名换成 `DISTANCE_TYPO`。QGIS 仍退出 0，并将 500 m 请求按 10 m 默认距离执行，实际缓冲面积约 20,313.654849 m²。判定 P2-08 转 REWORK，P2-10 等待复验。

## 2026-11-11 · P2-08/P2-09 · 初次验收

- B 实现 QGIS JSON 适配，实际 QGIS 3.44.15 退出 0，指定图层重开有效，500 m 的缓冲面积与独立参考相差约 0.000000196 m²。专项 14 项和 P0 smoke 复验通过。[证据](../../docs/acceptance/P2-08.md)
- F 实现 Python 适配与面积样例。真实进程重开等积图斑并写入 1,000,000 与 100,000 m²，原输入指纹一致。专项 15 项通过。[证据](../../docs/acceptance/P2-09.md)
- 主控复跑共用交接和两适配器，31 项通过、0 跳过。核对两个实际结果和持久进程证据，签收并放行 P2-10。[联合 JUnit](../../artifacts/test-results/p2-adapters-junit.xml)

## 2026-11-11 · P2-05/P2-06 · ACCEPTED

- P2-05 主控复用真实 CRS 检查，以 PROJ 适用区和采样比例因子判定局部距离。真实 4326、3857、32650 和 3395 道路及覆盖检查专项 12 项通过。[证据](../../docs/acceptance/P2-05.md)
- P2-06 A 核对 EPSG 方法编号、PROJ 实际投影与适用区。真实 6933、5070、3035、8857 图斑通过，非等积与方法编号冲突被拒。专项 12 项，主控 GIS 联合 39 项通过。[证据](../../docs/acceptance/P2-06.md)
- 主控签收，放行 P2-07。固定用例留待生产链运行。

## 2026-11-11 · P2-02/P2-04 · ACCEPTED

- P2-02 GIS Agent 新增 `src/geopilot/tools/registry.py` 和专项测试。真实 QGIS 3.44.15 的 JSON 目录包含 `native:buffer`，只返回显式登记版本。缺依赖、重复键和契约隔离已核对。主控实跑 `11 passed in 1.87s`。[记录](../../docs/acceptance/P2-02.md)
- P2-04 A 实现 `src/geopilot/gis/validation/crs_validator.py`，F 独立测试。实读真实投影、经纬度、无 CRS、英尺 CRS，拒绝 Web Mercator 与等价定义。实现 Agent 补测移除方法编号的自定义 Pseudo-Mercator，修复后实跑 `15 passed in 1.75s`。[记录](../../docs/acceptance/P2-04.md)
- 主控签收，放行 P2-05/06 和 P2-08/09。T01–T06 留待实际端到端执行，未记为已通过。

## 2026-11-11 · P2-03 · ACCEPTED

- 主控新增 `src/geopilot/tools/parameter_normalizer.py`、`tests/tools/test_parameter_normalizer.py`，共用契约的类型、范围及枚举校验。独立复核无阻断。
- PASS：实际 YAML 规范化、单位、边界、缺项、额外字段、严格枚举与默认值隔离，主控 13 项通过，Agent-F 独立 13 项通过。首次缺生产模块退出 `2`。[证据](../../docs/acceptance/P2-03.md)
- 无数据库或 HTTP 改动。主控签收；P2-08、P2-09 仍待注册表验收。

## 2026-11-11 · P2-01 · ACCEPTED

- 方法 Agent 新增 `src/geopilot/domain/tool_contract.py` 和 `config/tools/qgis-buffer.yaml`，Agent-F 独立编写 `tests/contracts/test_tool_contract.py`。初次测试缺生产模块，退出 `2`。
- PASS：主控执行 `python -m pytest tests/contracts/test_tool_contract.py -q`，`53 passed in 0.39s`。Agent-F 补充嵌套字段拼写错误回归，最终独立运行 54 项通过、无失败或跳过。实际 YAML 的十个必填字段、六项参数和 JSON 往返已核对。[JUnit](../../artifacts/test-results/p2-01-junit.xml)
- 静态字段、端口、版本、默认值、范围和测量策略通过。`ParameterSpec.validate_value` 供规范化复用。本任务没有 GIS 计算、数据库或 HTTP 改动，T01–T16 仍为 NOT_RUN。主控签收，放行 P2-02、P2-03、P2-04。

## 2026-11-11 · P1 阶段 · ACCEPTED

- 10/10 原子任务验收。独立进程按引用重开真实 TIFF/GPKG、源文件指纹不变、源变化与历史引用判定、SQLite 回滚与目录隔离均有证据。
- 主控设置真实 QGIS 入口并运行 `bash scripts/test.sh -q --junitxml=artifacts/test-results/p1-stage-junit.xml`，`164 passed in 16.18s`、0 失败/错误/跳过。T01–T16 仍为 16 项 NOT_RUN。[阶段验收](../../docs/acceptance/P1.md)
- 签署放行 P2，先执行 P2-01。

## 2026-11-11 · P1-09 · ACCEPTED

- Agent-A 新增 `src/geopilot/data/resolver.py`，登记与重开复用双摘要检查，仓库按 `(id, version)` 查询。Agent-F 独立添加 `tests/assets/test_asset_resolver.py`。无 schema 或 HTTP API 改动。
- PASS：另一个 Python 进程实际重开 TIFF、GPKG，内容和元数据匹配。错误版本、缺失文件、缺失图层、内容变化及恢复原字节按契约返回。独立专项 `7 passed in 4.48s`，资产联合 `37 passed in 4.93s`。[证据](../../docs/acceptance/P1-09.md)
- 主控审查代码、报告和 JUnit 后签收。

## 2026-11-11 · P1-08 · ACCEPTED

- 主控修改 `src/geopilot/data/assets.py` 的事务内版本选择，新增 `tests/assets/test_asset_versioning.py`。无 schema 或 HTTP API 改动。
- PASS：两项测试先红后绿，Agent-F 独立复跑 2 项通过。真实 TIFF 像元变化追加 v2，触摸时间复用，恢复旧内容追加 v3，四路并发只插入一版，原方案和旧记录保持。[证据](../../docs/acceptance/P1-08.md)
- 主控签收，放行 P1-09。

## 2026-11-11 · P1-07 · ACCEPTED

- Agent-A 实现 `src/geopilot/data/assets.py` 与 `src/geopilot/persistence/asset_repository.py`，从 `fingerprint.py` 暴露可比较文件状态。Agent-F 独立实现登记测试。无 schema 或 HTTP 变更。
- PASS：初次、重复、并发登记、不同图层、缺 CRS、读取失败和变化均实测。对实际 PAM 字节改变但状态观测相同的回归先红后绿，改用元数据读取前后两次内容摘要并在写锁内核对状态。13 项登记与 15 项指纹通过。
- [复现与限制](../../docs/acceptance/P1-07.md)；[JUnit](../../artifacts/test-results/p1-07-junit.xml)。主控签收，放行 P1-08。

## 2026-11-11 · P1-04/P1-05/P1-06 · ACCEPTED

- P1-04 Agent-A 新增 `geopackage_reader.py`、专项测试和[证据](../../docs/acceptance/P1-04.md)。多图层、缺层、单图层推定、非空间表、未知 CRS 和误导路径/格式实测，17 项通过。
- P1-05 栅格 Agent 新增 `raster_reader.py`、专项测试和[证据](../../docs/acceptance/P1-05.md)。真实黄金文件、COG、逐波段 NoData、旋转网格、关闭句柄和无像元读取实测，17 项通过。
- P1-06 Agent-F 新增 `fingerprint.py`、专项测试和[证据](../../docs/acceptance/P1-06.md)。实际 Shapefile、GeoTIFF/COG 旁车、SQLite WAL、触摸时间与文件组变化实测，15 项通过。
- 主控审查源码及原始 JUnit。`python -m pytest tests/data_adapters tests/assets/test_fingerprint.py -q` 集成 `57 passed in 1.49s`，0 失败、0 跳过。[JUnit](../../artifacts/test-results/p1-readers-junit.xml)。没有 HTTP API 或数据库变更，放行 P1-07。

## 2026-11-11 · P1-03 本机路径复验 · ACCEPTED

- Agent-A 发现 URI/归档名称解析差异，主控新增两条防误读回归，原实现 `2 failed`，转 REWORK。先解析实际绝对文件，拒绝会被改写为 GDAL 虚拟文件的名称。
- PASS：专项复验 `8 passed in 0.52s`，未发起外部请求；复验后恢复 ACCEPTED。[测试证据](../../docs/acceptance/P1-03.md)

## 2026-11-11 · P1-03 · ACCEPTED

- 主控新增 `src/geopilot/data/vector_reader.py`、共用 `errors.py` 与 `tests/data_adapters/test_vector_reader.py`。实读 GeoJSON、无 CRS Shapefile、P0 GPKG 图层，验证字段缺失数与结构化错误。
- PASS：`python -m pytest tests/data_adapters/test_vector_reader.py -q`，`6 passed in 0.59s`。Agent-A 另在真实混合 GeoPackage 核对共用函数能正确读取非空间表、未定义 CRS、缺失层。无数据库和 HTTP API 改动。[样例证据](../../docs/acceptance/P1-03.md)

## 2026-11-11 · P1-02 · ACCEPTED

- Agent-A 新增 `src/geopilot/domain/data_asset.py`、`tests/models/test_data_asset.py` 和 [本机 schema 示例](../../docs/acceptance/P1-02.md)。未修改数据库或 HTTP API。
- PASS：主控重跑 `python -m pytest tests/models/test_data_asset.py -q`，`43 passed in 0.11s`；原 JUnit 同为 43 项、0 失败/错误/跳过。覆盖严格版本、枚举、绝对路径、三类元数据、未知 CRS 与 NoData 序列化。实际 JSON Schema 必填字段和 `kind` 判别映射经核对。
- 主控签收。放行 P1-03、P1-04、P1-05、P1-06。[JUnit](../../artifacts/test-results/p1-02-junit.xml)

## 2026-11-11 · P1-10 · ACCEPTED

- 运行目录 Agent 新增 `src/geopilot/persistence/workspace.py`、`tests/workspace/test_workspace.py` 与 [目录约定](../../docs/acceptance/P1-10.md)。API 和数据库无改动。
- PASS：Agent-F 复跑 20 项，主控与存储和资产模型联合复跑 68 项通过。不同运行目录隔离，6 个同 ID 并发中只允许一个创建；拒绝链接穿越和非法 ID，部分失败只清理本次创建的空目录。
- Linux POSIX 实测通过。P4 运行服务写文件时负责文件路径边界。主控签收。[JUnit](../../artifacts/test-results/p1-10-junit.xml)

## 2026-11-11 · P1-01 · ACCEPTED

- Agent-A 新增 `src/geopilot/persistence/schema.py` 和 [记录](../../docs/acceptance/P1-01.md)；Agent-F 先写 `tests/storage/test_schema.py`，原始红灯为生产模块不存在。
- PASS：真实 SQLite 初始化、幂等、版本/外键约束、冲突 DDL 回滚、拒绝高版本、连接关闭和未提交数据回滚。独立测试 7 项通过，主控复跑 `python -m pytest tests/storage/test_schema.py -q` 亦为 `7 passed`。8 并发调用均返回 v1，完整性检查 `ok`。二进制元数据及错误状态被拒绝。
- 按 V2.0 §8.1 与冻结契约建立 4 张元数据表，无 HTTP API 改动。主控签收，放行 P1-02、P1-10。

## 2026-11-11 · P0 阶段 · ACCEPTED

- 主控核对 7/7 原子任务、QGIS/Python 独立运行、真实文件、日志、退出码和手算依据。综合 `15 passed in 11.45s`，0 失败、0 跳过。签署放行 P1。[阶段验收](../../docs/acceptance/P0.md)
- [T01–T16](../../docs/acceptance/FIXED_CASES.md) 当前 16 项 NOT_RUN，阶段 smoke 和参考输入断言另计。

## 2026-11-11 · P0-03 · ACCEPTED

- Agent-B 新增 `scripts/smoke/qgis_smoke.py`、`tests/smoke/test_qgis_smoke.py`、QGIS 实跑产物与测地线预研；主控配置隔离 CLI。无业务接口变化。
- PASS：QGIS `--version`、`list`、`help native:buffer`、真实缓冲均退出 `0`。Agent-F 复开 GeoPackage，独立圆端边界 Hausdorff 距离 `0`，面积差 `2.62e-10 m²`。缺 CLI、未知算法正确报错。
- 专项 3 项通过，集成 smoke 5 项通过。T04 仍待 P6-09。[真实证据](../../artifacts/smoke/qgis/README.md)。主控签收。

## 2026-11-11 · P0-06 · ACCEPTED

- 主控新增 `tests/fixtures/raster/` 两份样本、构造器、独立预期、README 和 `tests/golden/test_raster_fixtures.py`。
- PASS：Agent-A 独立重开像元和掩膜精确匹配。DEM 目标区 9 个像元均有有效邻域，参考为 45°/100%、下坡 270°。双波段共同有效 10 个像元。专项 4 项通过，重写既有样本退出 `1`、文件哈希不变。
- [JUnit](../../artifacts/test-results/p0-06-junit.xml)；T09 为 NOT_RUN。无接口变化。主控签收。

## 2026-11-11 · P0-05 · ACCEPTED

- Agent-F 新增 `tests/fixtures/vector/` 的 GeoPackage、构造器、人工坐标、独立预期与 README，以及 `tests/golden/test_vector_fixtures.py`。
- PASS：4 图层 EPSG:32650，15 要素。`sqlite3 -readonly` 的 `integrity_check` 为 `ok`，独立 GDAL 重开成功。精确断言覆盖对象 ID、区外道路取舍、裁回、边界未归属、分区去重计数与圆端离散参考。专项 5 项通过，黄金样本集成 9 项通过。
- [复现与证据](../../tests/fixtures/vector/README.md)；T03、T07、T08 产品断言仍为 NOT_RUN。无接口变化。主控签收。

## 2026-11-11 · P0-04 · ACCEPTED

- Python GIS Agent 新增 `scripts/smoke/python_gis_smoke.py`、`tests/smoke/test_python_gis_smoke.py` 和 [实跑报告](../../docs/acceptance/P0-04.md)。输入、输出均有落盘文件和日志。
- PASS：`python scripts/smoke/python_gis_smoke.py` 退出 `0`。GeoPackage 和 GeoTIFF 分别真实写出、关闭、重开核对，独立 SQLite 和 GDAL CLI 也读取成功。pytest 专项 `2 passed in 2.20s`，验证真实格网、像元、单位、NoData 与已有目录保护。
- 主控审查源码、读取 [smoke 日志](../../artifacts/smoke/python-gis/run-r_qo209n/smoke.log)和测试。无接口、数据库或业务改动。产品固定用例 NOT_RUN，完整 smoke 联合执行留到 P0 退出。

## 2026-11-11 · P0-07 · ACCEPTED

- Agent-F 新增 `pyproject.toml`、`scripts/test.sh`、`tests/test_baseline.py` 和测试基线报告；主控批准新增收集测试入口，未修改产品契约。
- PASS：Agent-F 实跑 `python -m pytest -q`、指定 JUnit 输出、干净目录启动与空筛选退出码。主控重跑 `bash scripts/test.sh -q tests/test_baseline.py`，`1 passed in 1.97s`，解析 JUnit 得到 1 项、0 失败、0 错误、0 跳过。
- [测试基线](../../docs/baseline/TEST_BASELINE.md)与[原始 JUnit](../../artifacts/test-results/p0-07-junit.xml)。pytest-asyncio 自动加载发出弃用提醒；T01–T16 NOT_RUN。主控签收。

## 2026-11-11 · P0-02 · ACCEPTED

- Agent-A 新增 [环境记录](../../docs/baseline/ENVIRONMENT.md)，实测 Python、SQLite、Node 和 GIS 库版本；GDAL CLI 用现有 Conda 环境绝对路径返回 `3.10.2`。
- PASS：环境盘点各依赖有命令、入口和可用性记录。FAIL：主机和现有镜像的 `qgis_process --version` 探测退出 `127`。NOT_RUN：实际算法与 Python 文件读写，分别由 P0-03、P0-04 验证。
- 无 API、数据库或系统环境改动。主控签收环境盘点并放行烟雾测试开发，P0 退出仍需两条计算链通过。

## 2026-11-11 · P0-01 · ACCEPTED

- 主控新增 `docs/baseline/PROJECT_AUDIT.md`、`plans/01_ARCHITECTURE/05_REPOSITORY_PATH_MAP.md`；无业务代码、API 或数据库改动。
- PASS：读取设计 PDF、盘点目录与入口，确认任务文档/索引/看板集合相同，初始共 122 项。
- `git status --short` 返回非仓库，记录无分支；`python -m pytest -q` 返回 5、`no tests ran`，原有测试为 NOT_AVAILABLE。固定用例 T01–T16 为 NOT_RUN。
- 审查依据：[实际命令与证据](../../docs/baseline/PROJECT_AUDIT.md#核对命令与证据)。主控签收，放行 P0-02 与 P0-07 首批工作。

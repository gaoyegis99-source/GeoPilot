# 任务看板

状态：TODO / DOING / REVIEW / ACCEPTED / REWORK / BLOCKED / NOT_APPLICABLE / NOT_RUN

| Task ID | 阶段 | 任务 | Agent | 依赖 | 状态 | 验收证据 |
|---|---|---|---|---|---|---|
| P0-01 | P0 | 现有项目结构审计 | 主控 | 无；先读取产品设计 PDF 与仓库现状。 | ACCEPTED | [项目审计](../../docs/baseline/PROJECT_AUDIT.md)；[路径映射](../01_ARCHITECTURE/05_REPOSITORY_PATH_MAP.md) |
| P0-02 | P0 | 开发环境盘点 | Agent-A/B | P0-01。 | ACCEPTED | [环境记录及 QGIS CLI 恢复](../../docs/baseline/ENVIRONMENT.md) |
| P0-03 | P0 | QGIS Processing 独立验证 | Agent-B | P0-02。 | ACCEPTED | [真实运行、日志与 JUnit](../../artifacts/smoke/qgis/README.md)；[测地线预研](../../docs/baseline/GEODESIC_BUFFER_RESEARCH.md) |
| P0-04 | P0 | Python GIS 读写验证 | Agent-B | P0-02。 | ACCEPTED | [实跑报告](../../docs/acceptance/P0-04.md)；[JUnit](../../artifacts/test-results/p0-04-junit.xml) |
| P0-05 | P0 | 矢量黄金样本与参考答案 | Agent-F | P0-01。 | ACCEPTED | [矢量样本及手算参考](../../tests/fixtures/vector/README.md)；[JUnit](../../artifacts/test-results/p0-05-junit.xml) |
| P0-06 | P0 | 栅格黄金样本与参考答案 | Agent-F | P0-01。 | ACCEPTED | [双样本与手算参考](../../tests/fixtures/raster/README.md)；[JUnit](../../artifacts/test-results/p0-06-junit.xml) |
| P0-07 | P0 | 自动化测试与报告基线 | Agent-F | P0-01。 | ACCEPTED | [测试基线](../../docs/baseline/TEST_BASELINE.md)；[JUnit](../../artifacts/test-results/p0-07-junit.xml) |
| P1-01 | P1 | SQLite 初始化与迁移基线 | Agent-A | P0-07；P0-01。 | ACCEPTED | [实现与独立测试](../../docs/acceptance/P1-01.md)；[JUnit](../../artifacts/test-results/p1-01-junit.xml) |
| P1-02 | P1 | DataAsset 公共模型 | Agent-A | P0-01；P1-01。 | ACCEPTED | [模型与 schema 示例](../../docs/acceptance/P1-02.md)；[JUnit](../../artifacts/test-results/p1-02-junit.xml) |
| P1-03 | P1 | 本地矢量读取器 | Agent-A | P0-04；P0-05；P1-02。 | ACCEPTED | [读取器与本机路径回归](../../docs/acceptance/P1-03.md)；[JUnit](../../artifacts/test-results/p1-03-junit.xml) |
| P1-04 | P1 | GeoPackage 读取器 | Agent-A | P0-04；P1-02。 | ACCEPTED | [图层、边界、证据](../../docs/acceptance/P1-04.md)；17 项通过 |
| P1-05 | P1 | GeoTIFF/COG 元数据读取 | Agent-A | P0-04；P0-06；P1-02。 | ACCEPTED | [真实 GeoTIFF 和 COG](../../docs/acceptance/P1-05.md)；17 项通过 |
| P1-06 | P1 | 数据指纹计算 | Agent-A | P1-02。 | ACCEPTED | [策略及旁车验证](../../docs/acceptance/P1-06.md)；15 项通过 |
| P1-07 | P1 | 资产登记服务 | Agent-A | P1-01 至 P1-06。 | ACCEPTED | [登记与并发实测](../../docs/acceptance/P1-07.md)；13 项通过 |
| P1-08 | P1 | 资产版本管理 | Agent-A | P1-01、P1-06、P1-07。 | ACCEPTED | [版本、并发及历史引用](../../docs/acceptance/P1-08.md)；2 项通过 |
| P1-09 | P1 | 资产按引用重新打开 | Agent-A | P1-03 至 P1-08。 | ACCEPTED | [跨进程精确重开](../../docs/acceptance/P1-09.md)；7 项通过 |
| P1-10 | P1 | 运行工作目录管理 | Agent-A | P1-01；P0-01。 | ACCEPTED | [工作目录约定与实测](../../docs/acceptance/P1-10.md)；[JUnit](../../artifacts/test-results/p1-10-junit.xml) |
| P2-01 | P2 | ToolContract 模型 | Agent-C | P1-02；P1-01。 | ACCEPTED | [模型与示例](../../docs/acceptance/P2-01.md)；[JUnit](../../artifacts/test-results/p2-01-junit.xml)，54 项通过 |
| P2-02 | P2 | 工具注册表 | Agent-B | P2-01；P0-03。 | ACCEPTED | [真实 QGIS 注册证据](../../docs/acceptance/P2-02.md)，11 项通过 |
| P2-03 | P2 | 参数与枚举规范化 | Agent-B | P2-01。 | ACCEPTED | [规范化及独立复验](../../docs/acceptance/P2-03.md)，13 项通过 |
| P2-04 | P2 | CRS 与单位检查器 | Agent-B | P1-03、P1-05；P2-01。 | ACCEPTED | [真实 CRS 与单位证据](../../docs/acceptance/P2-04.md)，15 项通过 |
| P2-05 | P2 | 距离测量策略 | Agent-B | P2-04。 | ACCEPTED | [实际道路与适用范围](../../docs/acceptance/P2-05.md)，12 项通过 |
| P2-06 | P2 | 面积测量策略 | Agent-B | P2-04。 | ACCEPTED | [实际等积图斑](../../docs/acceptance/P2-06.md)，12 项通过 |
| P2-07 | P2 | 局部投影选择与转换 | Agent-B | P2-04、P2-05、P2-06。 | ACCEPTED | [重开真实投影成果](../../docs/acceptance/P2-07.md)，12 项通过 |
| P2-08 | P2 | QGIS 最小执行适配器 | Agent-B | P0-03；P2-01 至 P2-03。 | ACCEPTED | [真实算法定义复验与缓冲成果](../../docs/acceptance/P2-08.md)，17 项通过 |
| P2-09 | P2 | Python 最小执行适配器 | Agent-B | P0-04；P2-01 至 P2-03。 | ACCEPTED | [真实 Python 进程与面积 CSV](../../docs/acceptance/P2-09.md)，15 项通过 |
| P2-10 | P2 | 统一 StepRequest/StepResult 与错误模型 | 主控+Agent-B | P2-01、P2-08、P2-09。 | ACCEPTED | [模型、真实调度与 5 个固定用例](../../docs/acceptance/P2-10.md) |
| P3-01 | P3 | MethodDefinition 模型 | Agent-C | P2-01；P1-02。 | ACCEPTED | [V2.0 方法模型与 Schema](../../docs/acceptance/P3-01.md)，32 项通过 |
| P3-02 | P3 | 方法目录加载器 | Agent-C | P3-01。 | ACCEPTED | [多方法目录、错误定位与版本查询](../../docs/acceptance/P3-02.md)，16 项通过 |
| P3-03 | P3 | 方法配置静态检查 | Agent-C | P3-01、P3-02；P2-02。 | ACCEPTED | [真实目录和精确工具校验](../../docs/acceptance/P3-03.md)，22 项通过 |
| P3-04 | P3 | 步骤依赖拓扑校验 | Agent-C | P3-01、P3-02。 | ACCEPTED | [合法连接和拒绝证据](../../docs/acceptance/P3-04.md)，9 项通过 |
| P3-05 | P3 | 固定顺序与不可重排校验 | Agent-C | P3-01、P3-04。 | ACCEPTED | [七步顺序与变更定位](../../docs/acceptance/P3-05.md)，8 项通过 |
| P3-06 | P3 | AnalysisPlan 公共模型 | Agent-C | P1-02、P2-01、P3-01。 | ACCEPTED | [V2.0 方案模型与 Schema](../../docs/acceptance/P3-06.md)，40 项通过 |
| P3-07 | P3 | 方案草稿生成器 | Agent-C | P3-02、P3-06；任务上下文接口可用。 | ACCEPTED | [真实资产上下文与草稿缺项](../../docs/acceptance/P3-07.md)，13 项通过 |
| P3-08 | P3 | 参数绑定与工具请求编译 | Agent-C | P2-03、P3-04 至 P3-07。 | ACCEPTED | [真实两步 QGIS 交接](../../docs/acceptance/P3-08.md)，30 项通过 |
| P3-09 | P3 | 方案确认与快照 | Agent-C | P3-06 至 P3-08；P1-07。 | ACCEPTED | [确认与快照验收](../../docs/acceptance/P3-09.md)，12 项通过 |
| P3-10 | P3 | 方案版本与编辑失效 | Agent-C | P3-06、P3-09；P1-08。 | ACCEPTED | [版本与历史引用验收](../../docs/acceptance/P3-10.md)，9 项通过 |
| P4-01 | P4 | Run 与 RunStep 持久化模型 | Agent-D | P1-01；P2-10。 | ACCEPTED | [持久模型与 v2 迁移](../../docs/acceptance/P4-01.md)，模型与存储 22 项通过 |
| P4-02 | P4 | 运行创建服务 | Agent-D | P3-09、P3-10；P1-08、P1-10。 | ACCEPTED | [运行受理专项](../../docs/acceptance/P4-02.md)，5 项通过 |
| P4-03 | P4 | 单运行调度循环 | Agent-D | P4-01、P4-02；P3-08。 | ACCEPTED | [调度与恢复专项](../../docs/acceptance/P4-03.md)，4 项通过 |
| P4-04 | P4 | 按步骤顺序执行 | Agent-D | P4-03；P2-08 至 P2-10；P3-08。 | ACCEPTED | [真实后台步骤](../../docs/acceptance/P4-04.md)，2 项通过 |
| P4-05 | P4 | 命名输出解析与交接 | Agent-D | P4-04；P2-10。 | ACCEPTED | 主控签收，实际 GeoPackage 缺失、损坏及层类型不符均阻断；专项 9 PASS |
| P4-06 | P4 | 运行进度查询服务 | Agent-D | P4-01 至 P4-05。 | ACCEPTED | HTTP 独立双状态、取消意图、错误和报告编号；实际调度回归 42 PASS |
| P4-07 | P4 | 取消请求与进程终止 | Agent-D | P4-03、P4-04；P2-08、P2-09。 | ACCEPTED | 真实 GIS 在运行中终止，排队和步间取消均停止派发；联合 46 PASS |
| P4-08 | P4 | 超时与进程清理 | Agent-D | P2-08、P2-09；P4-04。 | ACCEPTED | 实际 Python GIS 进程组含静默后代终止，Python/QGIS 联合 42 PASS |
| P4-09 | P4 | 失败停止与部分成果保留 | Agent-D | P4-04、P4-05、P4-08。 | ACCEPTED | 真实失败链定位、终止后续、部分成果只作为诊断；联合 9 PASS |
| P4-10 | P4 | 手动重试 | Agent-D | P4-02、P4-09；P3-10。 | ACCEPTED | 原版本与新确认版本真实重新执行；关联 30 PASS |
| P4-11 | P4 | 成果登记服务 | Agent-A/D | P4-04、P4-05；P1-10。 | ACCEPTED | SQLite v3 整批登记与真实失败回滚，HTTP 复核来源和可读性 |
| P4-12 | P4 | 输入规则检查器 | Agent-F | P2-04 至 P2-07；P3-03。 | ACCEPTED | [真实输入规则](../../docs/acceptance/P4-12.md)，18 项通过 |
| P4-13 | P4 | 过程规则检查器 | Agent-F | P4-04；P2-10；P3-09。 | ACCEPTED | Python 和 QGIS 实际过程核对，主控独立复跑 11 PASS |
| P4-14 | P4 | 成果规则检查器 | Agent-F | P4-11；P0-05、P0-06。 | ACCEPTED | 实际矢量、表与栅格按容差检查，专项 14 PASS |
| P4-15 | P4 | 检查报告与双状态汇总 | Agent-F | P4-12 至 P4-14；P4-01。 | ACCEPTED | SQLite v4 原子报告，真实 QGIS HTTP 验证双状态，T16 PASS |
| P4-16 | P4 | 检查中断与单独重查 | Agent-D/F | P4-11、P4-15；P4-08。 | ACCEPTED | 真实中断/取消重查、原规则版本、指纹和历史报告；联合 44 PASS |
| P5-01 | P5 | 工作台路由与基础布局 | Agent-E | P4 API 契约稳定；P0-01。 | ACCEPTED | React 壳、真实 Chromium 桌面/窄屏、测试与构建通过 |
| P5-02 | P5 | 资产登记与选择面板 | Agent-E | P1-07、P4 API；P5-01。 | ACCEPTED | 真实 Chromium 与 GPKG 登记、版本、详情；组件 3 PASS |
| P5-03 | P5 | 方法目录浏览界面 | Agent-E | P3-02；P5-01。 | ACCEPTED | 真实 HTTP 与 Chromium 核对六个登记版本及缺 QGIS 诊断 |
| P5-04 | P5 | 方法驱动动态表单 | Agent-E | P3-01、P3-06；P5-03。 | ACCEPTED | 5 项专项；浏览器真实道路、属性与土地利用配置 |
| P5-05 | P5 | 方案预览与 issues 展示 | Agent-E | P3-07、P3-08；P5-04。 | ACCEPTED | 联合 8 项与真实三份草稿，核对版本、顺序、参数和字段聚焦 |
| P5-06 | P5 | 方案确认与运行分离交互 | Agent-E | P3-09；P4-02；P5-05。 | ACCEPTED | 10 项前端；真实浏览器属性与道路按准确快照运行成功 |
| P5-07 | P5 | 运行进度与日志面板 | Agent-E | P4-06、P4-07；P5-01。 | ACCEPTED | Chromium 实际 QGIS 日志、终态停止轮询和取消通过 |
| P5-08 | P5 | MapLibre 矢量结果显示 | Agent-E | P4-11；P5-01。 | ACCEPTED | 4 项专项；真实点线面、局部失败重试和双向成果联动 |
| P5-09 | P5 | 成果列表与下载 | Agent-E | P4-11；P5-01。 | ACCEPTED | 2 项测试，浏览器真实 GPKG 下载与 CSV 全部三行对账 |
| P5-10 | P5 | 规则检查明细界面 | Agent-E | P4-15；P5-01。 | ACCEPTED | 真实浏览器独立展示失败规则、输入证据与旧报告 |
| P5-11 | P5 | 页面刷新与任务恢复 | Agent-E | P4-06；P5-02 至 P5-10。 | ACCEPTED | 真运行中和完成刷新，原版本成果保留，拒绝跨任务 |
| P5-12 | P5 | 错误与渐进式详情体验 | Agent-E | P4-06、P4-09、P4-15。 | ACCEPTED | 浏览器真实失败重跑、检查重查、预览重试、返回草稿 |
| P6-01 | P6 | 空间关系边界黄金用例 | Agent-F | P0-05；P3 方法测试基础。 | ACCEPTED | [点线面黄金样本](../../docs/acceptance/P6-01.md)，12 项通过 |
| P6-02 | P6 | intersects 实现与验证 | Agent-B | P6-01；P2-04。 | ACCEPTED | [真实相交选择](../../docs/acceptance/P6-02.md)，17 项通过 |
| P6-03 | P6 | within 实现与验证 | Agent-B | P6-01；P6-02。 | ACCEPTED | [完整对象内部关系](../../docs/acceptance/P6-03.md)，5 项通过 |
| P6-04 | P6 | 完整对象关系判断与裁回分离 | Agent-C | P3-05、P3-08；P6-02、P6-03。 | ACCEPTED | 点线面双谓词、锁定顺序与两步文件交接；T13 PASS |
| P6-05 | P6 | 跨界面积拆分归属 | Agent-B | P2-06、P6-01。 | ACCEPTED | 等积面积实际分配、范围、重叠、守恒通过；主控复跑 24 PASS |
| P6-06 | P6 | 质心归属 | Agent-B | P6-01。 | ACCEPTED | [真实面质心归属](../../docs/acceptance/P6-06.md)，13 项通过 |
| P6-07 | P6 | 最大重叠归属 | Agent-B | P2-06、P6-01。 | ACCEPTED | 裁前归属、近似并列与真实等积 GeoPackage；专项 9 PASS |
| P6-08 | P6 | 原始对象标识与去重统计 | Agent-B | P6-04 至 P6-07。 | ACCEPTED | 三种归属逐 ID 与手算、空范围真实后台通过；T14 PASS |
| P6-09 | P6 | 测地线道路缓冲专项验证 | Agent-B | P0-03 的专项调研记录；P2-05；P2-08。 | ACCEPTED | 独立参考 16 探针、解析边界及真实运行；T04 PASS |
| P6-10 | P6 | 土地利用分类映射与叠加 | Agent-C/B | P2-06；P3-02 至 P3-08；P6-05。 | ACCEPTED | 实际四步、缺码前置、独立缺口/重叠及来源验证 |
| P6-11 | P6 | 转移矩阵与分区统计 | Agent-B | P6-10。 | ACCEPTED | 正式七步、面积、类别、分区及来源逐项实跑 |
| P7-01 | P7 | 栅格窗口读取器 | Agent-B | P0-06；P1-05。 | ACCEPTED | 真实栅格窗口 25 项专项、60 项关联通过 |
| P7-02 | P7 | 分析区邻域外扩计算 | Agent-B | P7-01；P1-05。 | ACCEPTED | 22 项专项、68 项关联，真实邻域覆盖与边界阻断 |
| P7-03 | P7 | DEM 水平/垂直单位检查 | Agent-B | P2-04；P7-01。 | ACCEPTED | 9 项专项、80 项关联，实际请求日志与单位换算 |
| P7-04 | P7 | DEM 外扩裁取与输入准备 | Agent-B | P7-01 至 P7-03。 | ACCEPTED | 两步真实外扩裁取；专项 8、联合 99 项 |
| P7-05 | P7 | 坡度计算 | Agent-B | P7-03、P7-04。 | ACCEPTED | 专项 11、联合 114，真实两单位与独立 GDAL 全像元一致 |
| P7-06 | P7 | 坡向计算 | Agent-B | P7-03、P7-04。 | ACCEPTED | 坡向和单位 13 项，GDAL 实比 9×270°、16 NoData |
| P7-07 | P7 | 裁回原分析区 | Agent-B | P7-05、P7-06。 | ACCEPTED | 专项 7、联合 123；T09 九个像元一致，十进制边界回归 |
| P7-08 | P7 | 栅格阈值提取 | Agent-B | P7-05 至 P7-07。 | ACCEPTED | 32 项独立重跑，实际 TIFF 分类端点与 NoData |
| P7-09 | P7 | 栅格面积统计 | Agent-B | P2-06；P7-08。 | ACCEPTED | 主控联合 29 项，真实 UTM→等积和错误目标阻断 |
| P7-10 | P7 | 栅格成果元数据与检查规则 | Agent-F | P4-11、P4-14；P7-01 至 P7-09。 | ACCEPTED | 七步坡度与坡向、成果登记、逐像元校验及重查通过 |
| P7-11 | P7 | 栅格预览生成与 MapLibre 接入 | Agent-E/B | P5-08；P7-10。 | ACCEPTED | 四份持久预览、真实 MapLibre 换色显隐定位及原 TIFF 指纹通过 |
| P7-12 | P7 | 原始像元取值接口 | Agent-A/E | P7-11；P4-11。 | ACCEPTED | 原像元点击、三态、迟响应和 20 条独立 GDAL 对照通过 |
| P8-01 | P8 | 传感器产品配置模型 | Agent-C | P7-01；P2-01。 | ACCEPTED | 五份版本化产品配置、独立 Schema 验证与主控 128 项通过 |
| P8-02 | P8 | 唯一配置查找服务 | Agent-C | P8-01。 | ACCEPTED | 精确产品键和版本回取、零命中与多命中；专项 18，联合 92 |
| P8-03 | P8 | 波段角色映射 | Agent-B | P8-02；P7-01。 | ACCEPTED | 真实暂存栈、原生 XML、精确波段及缺项阻断；主控 157 项通过 |
| P8-04 | P8 | 缩放与偏移处理 | Agent-B | P8-02、P8-03。 | ACCEPTED | 产品 XML 实读定标、Float64 掩膜和手算值通过；专项 11 |
| P8-05 | P8 | 质量掩膜生成 | Agent-B | P8-02、P8-03。 | ACCEPTED | 位与类别规则、真实最近邻样本和分块通过；专项 9 |
| P8-06 | P8 | NDVI 计算 | Agent-B | P8-04、P8-05。 | ACCEPTED | 原生 XML 实测、零分母、同格与异常范围；专项 14 |
| P8-07 | P8 | 双时相网格对齐 | Agent-B | P7-01、P8-06。 | ACCEPTED | 真跨 CRS、错位网格、bilinear 越界隔离和掩膜；专项 18 |
| P8-08 | P8 | 变化栅格计算 | Agent-B | P8-06、P8-07。 | ACCEPTED | 真实错位样本差值、共同掩膜、方向与负零；专项 16 |
| P8-09 | P8 | 变化分类与统计 | Agent-B | P8-08；P2-06。 | ACCEPTED | 三类开闭边界、原网格和真实等积面积；专项 16 |
| P8-10 | P8 | 遥感方法端到端接入 | Agent-C | P3 全部；P8-01 至 P8-09；P7-11。 | ACCEPTED | T10–T12、八步真执行、原生打包和浏览器通过；阶段联合 234 |
| P9-01 | P9 | 模型供应商适配接口 | Agent-G | P5 方案流程可手工完成；P3 公共模型稳定。 | ACCEPTED | 34 项专项，有限 JSON、统一错误和本机 mock |
| P9-02 | P9 | 允许外发摘要构建器 | Agent-G | P1 元数据；P3 任务上下文可用。 | ACCEPTED | 8 项通过，真实 GPKG/TIFF 白名单与本机引用 |
| P9-03 | P9 | 外发数据边界扫描器 | Agent-F/G | P9-02；P0-07。 | ACCEPTED | 50 项专项、152 项模型回归；完整序列化候选白名单 |
| P9-04 | P9 | 候选方法提示构建 | Agent-G | P3-02、P2-02；P9-02。 | ACCEPTED | 真注册表联合 11 项；动态候选、约束和精确版本 |
| P9-05 | P9 | 结构化模型输出解析 | Agent-G | P3-06；P9-01。 | ACCEPTED | 专项 52、联合 137；解析精确版本与独立说明 |
| P9-06 | P9 | 候选合法性与参数范围检查 | Agent-C/G | P9-04；P2-01 至 P2-03。 | ACCEPTED | 真注册表联合 8 项，固定展开与范围校验 |
| P9-07 | P9 | 模型错误、超时与重试策略 | Agent-G | P9-01、P9-05。 | ACCEPTED | 专项 48、联合 240；实际 Ollama 请求和完整 wire |
| P9-08 | P9 | 手工回退入口 | Agent-C/E | P3-07 至 P3-09；P5-04 至 P5-06。 | ACCEPTED | HTTP 3、前端 52；真浏览器超时保留草稿并实际 QGIS 通过 |
| P9-09 | P9 | 模型方案与手工方案一致性测试 | Agent-F/G | P9-01 至 P9-08；至少一个已验收方法。 | ACCEPTED | 联合 206；真实三次 QGIS，确切方案和等价结果 |
| P10-01 | P10 | 本地脚本草稿与快照 | Agent-A | P4-08；P1-10。 | ACCEPTED | 真 GPKG 精确版本、原子发布及失败回滚；24 PASS |
| P10-02 | P10 | 本地脚本进程执行 | Agent-B | P2-09；P10-01。 | ACCEPTED | 真实脚本筛选、超时组、非零、日志和输出清单；15 PASS |
| P10-03 | P10 | 脚本结果浏览 | Agent-E | P10-01、P10-02；P5 基础 UI。 | ACCEPTED | 6 项后端；浏览器真实脚本日志、原字节下载、预览和刷新 |
| P10-04 | P10 | 脚本手动收录向导 | Agent-C | P10-02；P2/P3 已验收。 | ACCEPTED | 专项 12、关联 152；真实试跑、手动正式配置和独立参考 |
| P10-05 | P10 | PostGIS 导入适配器 | Agent-A | P1 资产登记；P0 环境。 | ACCEPTED | 隔离真实 PostGIS 筛选、GPKG 登记与离线重开；5 PASS |
| P10-06 | P10 | PostGIS 快照版本管理 | Agent-A | P10-05；P1-08。 | ACCEPTED | 8 项专项和 124 项关联；真实同源两版本停库运行 |
| P10-07 | P10 | 脚本/PostGIS 扩展验收 | Agent-F | P10-01 至 P10-06。 | ACCEPTED | 真联合两项通过；缺 Docker 独立复测明确 NOT_RUN |
| P11-01 | P11 | 40 条通用需求追踪矩阵 | Agent-F | P0—P10 已有实现记录。 | ACCEPTED | 40/40 映射，36 PASS、4 PARTIAL，[矩阵](../../docs/traceability/requirements-matrix.md) |
| P11-02 | P11 | T01—T16 固定用例全量回归 | Agent-F | 对应 P2—P9 用例已实现。 | ACCEPTED | 16/16 PASS，122 项自动测试通过，[复跑证据](../../docs/acceptance/P11-02.md) |
| P11-03 | P11 | 已接入方法独立回归 | Agent-F | 相应方法阶段完成。 | ACCEPTED | 七种内置方法与扩展样例，120 项通过，[方法报告](../../docs/acceptance/P11-03.md) |
| P11-04 | P11 | 跨层接口一致性审计 | 主控+Agent-F | P1—P5；P7—P9 已接入部分。 | ACCEPTED | 8 个运行、25 个成果、87 个双端 GET，[跨层审计](../../docs/acceptance/interface-audit.md) |
| P11-05 | P11 | 全流程真实数据复算 | 主控+Agent-F | P5；至少一个完整方法通过 P3/P4。 | ACCEPTED | 真实道路两次、属性一次和 Chromium 双方法，[复现报告](../../docs/acceptance/P11-05.md) |
| P11-06 | P11 | 运行生命周期与故障演练 | Agent-D/F | P4-07 至 P4-16。 | ACCEPTED | 62 项通过、22 个运行复核、4 个实际 PID 停止，[生命周期](../../docs/acceptance/lifecycle.md) |
| P11-07 | P11 | 干净目录启动与使用文档 | 主控+Agent-E | P0—P5 已验收；最终选定方法可运行。 | ACCEPTED | 净目录 42 项后端烟雾、66 项前端、真实道路七步，[启动验证](../../docs/acceptance/P11-07.md) |
| P11-08 | P11 | 最终交付与已知限制报告 | 主控+Agent-F | P11-01 至 P11-07。 | ACCEPTED | 最终报告、可校验清单与 P11 退出验收，[交付](../../docs/release/FINAL_ACCEPTANCE.md) |

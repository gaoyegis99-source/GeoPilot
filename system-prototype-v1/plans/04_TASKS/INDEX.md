# 原子任务总索引

任务总数：122

## P0 环境与参考样本
- [P0-01 — 现有项目结构审计](P0/P0-01.md) — 主控
- [P0-02 — 开发环境盘点](P0/P0-02.md) — Agent-A/B
- [P0-03 — QGIS Processing 独立验证](P0/P0-03.md) — Agent-B
- [P0-04 — Python GIS 读写验证](P0/P0-04.md) — Agent-B
- [P0-05 — 矢量黄金样本与参考答案](P0/P0-05.md) — Agent-F
- [P0-06 — 栅格黄金样本与参考答案](P0/P0-06.md) — Agent-F
- [P0-07 — 自动化测试与报告基线](P0/P0-07.md) — Agent-F

## P1 数据与状态存储
- [P1-01 — SQLite 初始化与迁移基线](P1/P1-01.md) — Agent-A
- [P1-02 — DataAsset 公共模型](P1/P1-02.md) — Agent-A
- [P1-03 — 本地矢量读取器](P1/P1-03.md) — Agent-A
- [P1-04 — GeoPackage 读取器](P1/P1-04.md) — Agent-A
- [P1-05 — GeoTIFF/COG 元数据读取](P1/P1-05.md) — Agent-A
- [P1-06 — 数据指纹计算](P1/P1-06.md) — Agent-A
- [P1-07 — 资产登记服务](P1/P1-07.md) — Agent-A
- [P1-08 — 资产版本管理](P1/P1-08.md) — Agent-A
- [P1-09 — 资产按引用重新打开](P1/P1-09.md) — Agent-A
- [P1-10 — 运行工作目录管理](P1/P1-10.md) — Agent-A

## P2 契约与基础测量
- [P2-01 — ToolContract 模型](P2/P2-01.md) — Agent-C
- [P2-02 — 工具注册表](P2/P2-02.md) — Agent-B
- [P2-03 — 参数与枚举规范化](P2/P2-03.md) — Agent-B
- [P2-04 — CRS 与单位检查器](P2/P2-04.md) — Agent-B
- [P2-05 — 距离测量策略](P2/P2-05.md) — Agent-B
- [P2-06 — 面积测量策略](P2/P2-06.md) — Agent-B
- [P2-07 — 局部投影选择与转换](P2/P2-07.md) — Agent-B
- [P2-08 — QGIS 最小执行适配器](P2/P2-08.md) — Agent-B
- [P2-09 — Python 最小执行适配器](P2/P2-09.md) — Agent-B
- [P2-10 — 统一 StepRequest/StepResult 与错误模型](P2/P2-10.md) — 主控+Agent-B

## P3 通用方法执行链
- [P3-01 — MethodDefinition 模型](P3/P3-01.md) — Agent-C
- [P3-02 — 方法目录加载器](P3/P3-02.md) — Agent-C
- [P3-03 — 方法配置静态检查](P3/P3-03.md) — Agent-C
- [P3-04 — 步骤依赖拓扑校验](P3/P3-04.md) — Agent-C
- [P3-05 — 固定顺序与不可重排校验](P3/P3-05.md) — Agent-C
- [P3-06 — AnalysisPlan 公共模型](P3/P3-06.md) — Agent-C
- [P3-07 — 方案草稿生成器](P3/P3-07.md) — Agent-C
- [P3-08 — 参数绑定与工具请求编译](P3/P3-08.md) — Agent-C
- [P3-09 — 方案确认与快照](P3/P3-09.md) — Agent-C
- [P3-10 — 方案版本与编辑失效](P3/P3-10.md) — Agent-C

## P4 运行管理与接口
- [P4-01 — Run 与 RunStep 持久化模型](P4/P4-01.md) — Agent-D
- [P4-02 — 运行创建服务](P4/P4-02.md) — Agent-D
- [P4-03 — 单运行调度循环](P4/P4-03.md) — Agent-D
- [P4-04 — 按步骤顺序执行](P4/P4-04.md) — Agent-D
- [P4-05 — 命名输出解析与交接](P4/P4-05.md) — Agent-D
- [P4-06 — 运行进度查询服务](P4/P4-06.md) — Agent-D
- [P4-07 — 取消请求与进程终止](P4/P4-07.md) — Agent-D
- [P4-08 — 超时与进程清理](P4/P4-08.md) — Agent-D
- [P4-09 — 失败停止与部分成果保留](P4/P4-09.md) — Agent-D
- [P4-10 — 手动重试](P4/P4-10.md) — Agent-D
- [P4-11 — 成果登记服务](P4/P4-11.md) — Agent-A/D
- [P4-12 — 输入规则检查器](P4/P4-12.md) — Agent-F
- [P4-13 — 过程规则检查器](P4/P4-13.md) — Agent-F
- [P4-14 — 成果规则检查器](P4/P4-14.md) — Agent-F
- [P4-15 — 检查报告与双状态汇总](P4/P4-15.md) — Agent-F
- [P4-16 — 检查中断与单独重查](P4/P4-16.md) — Agent-D/F

## P5 通用 Web 工作台
- [P5-01 — 工作台路由与基础布局](P5/P5-01.md) — Agent-E
- [P5-02 — 资产登记与选择面板](P5/P5-02.md) — Agent-E
- [P5-03 — 方法目录浏览界面](P5/P5-03.md) — Agent-E
- [P5-04 — 方法驱动动态表单](P5/P5-04.md) — Agent-E
- [P5-05 — 方案预览与 issues 展示](P5/P5-05.md) — Agent-E
- [P5-06 — 方案确认与运行分离交互](P5/P5-06.md) — Agent-E
- [P5-07 — 运行进度与日志面板](P5/P5-07.md) — Agent-E
- [P5-08 — MapLibre 矢量结果显示](P5/P5-08.md) — Agent-E
- [P5-09 — 成果列表与下载](P5/P5-09.md) — Agent-E
- [P5-10 — 规则检查明细界面](P5/P5-10.md) — Agent-E
- [P5-11 — 页面刷新与任务恢复](P5/P5-11.md) — Agent-E
- [P5-12 — 错误与渐进式详情体验](P5/P5-12.md) — Agent-E

## P6 矢量能力补齐
- [P6-01 — 空间关系边界黄金用例](P6/P6-01.md) — Agent-F
- [P6-02 — intersects 实现与验证](P6/P6-02.md) — Agent-B
- [P6-03 — within 实现与验证](P6/P6-03.md) — Agent-B
- [P6-04 — 完整对象关系判断与裁回分离](P6/P6-04.md) — Agent-C
- [P6-05 — 跨界面积拆分归属](P6/P6-05.md) — Agent-B
- [P6-06 — 质心归属](P6/P6-06.md) — Agent-B
- [P6-07 — 最大重叠归属](P6/P6-07.md) — Agent-B
- [P6-08 — 原始对象标识与去重统计](P6/P6-08.md) — Agent-B
- [P6-09 — 测地线道路缓冲专项验证](P6/P6-09.md) — Agent-B
- [P6-10 — 土地利用分类映射与叠加](P6/P6-10.md) — Agent-C/B
- [P6-11 — 转移矩阵与分区统计](P6/P6-11.md) — Agent-B

## P7 DEM 与栅格预览
- [P7-01 — 栅格窗口读取器](P7/P7-01.md) — Agent-B
- [P7-02 — 分析区邻域外扩计算](P7/P7-02.md) — Agent-B
- [P7-03 — DEM 水平/垂直单位检查](P7/P7-03.md) — Agent-B
- [P7-04 — DEM 外扩裁取与输入准备](P7/P7-04.md) — Agent-B
- [P7-05 — 坡度计算](P7/P7-05.md) — Agent-B
- [P7-06 — 坡向计算](P7/P7-06.md) — Agent-B
- [P7-07 — 裁回原分析区](P7/P7-07.md) — Agent-B
- [P7-08 — 栅格阈值提取](P7/P7-08.md) — Agent-B
- [P7-09 — 栅格面积统计](P7/P7-09.md) — Agent-B
- [P7-10 — 栅格成果元数据与检查规则](P7/P7-10.md) — Agent-F
- [P7-11 — 栅格预览生成与 MapLibre 接入](P7/P7-11.md) — Agent-E/B
- [P7-12 — 原始像元取值接口](P7/P7-12.md) — Agent-A/E

## P8 遥感变化
- [P8-01 — 传感器产品配置模型](P8/P8-01.md) — Agent-C
- [P8-02 — 唯一配置查找服务](P8/P8-02.md) — Agent-C
- [P8-03 — 波段角色映射](P8/P8-03.md) — Agent-B
- [P8-04 — 缩放与偏移处理](P8/P8-04.md) — Agent-B
- [P8-05 — 质量掩膜生成](P8/P8-05.md) — Agent-B
- [P8-06 — NDVI 计算](P8/P8-06.md) — Agent-B
- [P8-07 — 双时相网格对齐](P8/P8-07.md) — Agent-B
- [P8-08 — 变化栅格计算](P8/P8-08.md) — Agent-B
- [P8-09 — 变化分类与统计](P8/P8-09.md) — Agent-B
- [P8-10 — 遥感方法端到端接入](P8/P8-10.md) — Agent-C

## P9 模型入口
- [P9-01 — 模型供应商适配接口](P9/P9-01.md) — Agent-G
- [P9-02 — 允许外发摘要构建器](P9/P9-02.md) — Agent-G
- [P9-03 — 外发数据边界扫描器](P9/P9-03.md) — Agent-F/G
- [P9-04 — 候选方法提示构建](P9/P9-04.md) — Agent-G
- [P9-05 — 结构化模型输出解析](P9/P9-05.md) — Agent-G
- [P9-06 — 候选合法性与参数范围检查](P9/P9-06.md) — Agent-C/G
- [P9-07 — 模型错误、超时与重试策略](P9/P9-07.md) — Agent-G
- [P9-08 — 手工回退入口](P9/P9-08.md) — Agent-C/E
- [P9-09 — 模型方案与手工方案一致性测试](P9/P9-09.md) — Agent-F/G

## P10 脚本与可选数据源
- [P10-01 — 本地脚本草稿与快照](P10/P10-01.md) — Agent-A
- [P10-02 — 本地脚本进程执行](P10/P10-02.md) — Agent-B
- [P10-03 — 脚本结果浏览](P10/P10-03.md) — Agent-E
- [P10-04 — 脚本手动收录向导](P10/P10-04.md) — Agent-C
- [P10-05 — PostGIS 导入适配器](P10/P10-05.md) — Agent-A
- [P10-06 — PostGIS 快照版本管理](P10/P10-06.md) — Agent-A
- [P10-07 — 脚本/PostGIS 扩展验收](P10/P10-07.md) — Agent-F

## P11 原型与方法验收
- [P11-01 — 40 条通用需求追踪矩阵](P11/P11-01.md) — Agent-F
- [P11-02 — T01—T16 固定用例全量回归](P11/P11-02.md) — Agent-F
- [P11-03 — 已接入方法独立回归](P11/P11-03.md) — Agent-F
- [P11-04 — 跨层接口一致性审计](P11/P11-04.md) — 主控+Agent-F
- [P11-05 — 全流程真实数据复算](P11/P11-05.md) — 主控+Agent-F
- [P11-06 — 运行生命周期与故障演练](P11/P11-06.md) — Agent-D/F
- [P11-07 — 干净目录启动与使用文档](P11/P11-07.md) — 主控+Agent-E
- [P11-08 — 最终交付与已知限制报告](P11/P11-08.md) — 主控+Agent-F

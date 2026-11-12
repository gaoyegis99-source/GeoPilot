# GeoPilot P12 深度审计报告

- 仓库：`gaoyegis99-source/GeoPilot`
- 分支：`main`
- 原型目录：`system-prototype-v1/`
- 审计基准：2026-11-12 可见 GitHub 快照
- 方法：目录树、源码、配置、需求矩阵、任务看板、阶段验收、用户指南、测试报告和交付 manifest 静态审查
- 限制：未在用户本机克隆仓库执行测试；不将仓库记录冒充本轮独立复跑。

## 1. 执行摘要

GeoPilot V1 已形成可运行的单用户本机 Web GIS 工作台原型。核心链路包括数据资产登记与版本、任务上下文、版本化方法目录、工具契约、方案草稿/确认/编译、QGIS/Python 子进程执行、运行生命周期、检查报告、成果追踪、MapLibre 展示和模型辅助。

仓库当前看板记录 122/122 原子任务 ACCEPTED；最新需求矩阵 40/40 PASS；固定用例台账 T01—T16 全部 PASS。P11 阶段记录真实 QGIS/Python 与 Chromium 验收。**不建议重做 P0—P11。**

但核心原型验收不等于商业产品成熟。下一轮应聚焦证据一致性、CI、性能/资源边界、运行可靠性、方法生态、Agent 反馈迭代和产品化。

## 2. 仓库规模与模块

可见目录树统计：
- `src/geopilot/`：155 个 Python 源码文件
- `tests/`：146 个 Python 测试文件
- `frontend/`：75 个文件
- `config/`：61 个文件
- `docs/acceptance/`：138 个验收文档
- `plans/04_TASKS/`：122 个原子任务文档
- `config/methods/`：9 个 YAML 文件，其中道路缓冲有多个版本；验收材料按 7 种内置方法类型归类，另有脚本扩展示例。

主要代码域：api、application、artifacts、checks、data、domain、execution、gis、llm、methods、persistence、plans、preview、remote_sensing、runs、scheduler、scripts、tools。

## 3. 架构判断

### 已形成的产品内核
- 数据：本地资产、元数据、指纹、版本、PostGIS 快照。
- 上下文：角色、范围、字段、预期成果候选筛选。
- 方法/工具：MethodDefinition、ToolContract、版本化目录和 runner binding。
- 方案：AnalysisPlan、draft/issues、编译、确认快照和版本。
- 执行：QGIS Processing、Python GIS、步骤请求/结果。
- 运行：排队、单活动调度、取消、超时、失败停止、重试和恢复。
- 检查：输入、过程、成果检查，执行状态与检查状态独立。
- 成果：登记、元数据、来源追踪和下载。
- 前端：React/TypeScript、MapLibre、动态表单、运行/成果/检查和任务恢复。
- 模型：Ollama/mock provider、出站数据边界、结构化回复、手工回退。

### 架构优势
1. 方法定义与工具实现分离。
2. 用户确认与执行分离，保留方案快照。
3. GIS 计算在本地 QGIS/Python 子进程运行。
4. 执行状态与检查状态分离。
5. 正式成果与诊断文件分离。
6. 脚本人工收录流程是方法生态的起点。

### 能力边界
- `scheduler/loop.py` 的领取逻辑在已有 RUNNING 或 CHECKING 工作时不领取下一项，属于单活动串行策略，不是并行 GIS 作业调度。
- 没有看到大数据、长时间和并发压力测试的系统化证据。
- provider 目录可见 mock 与 Ollama；未见多个云模型生产适配器。
- 脚本收录依赖人工审查，不是自动发现/自动发布插件市场。
- QGIS 启动依赖用户配置本机 CLI；开发机包装器依赖特定缓存镜像，不等于跨平台安装器。
- 服务绑定 `127.0.0.1`，定位为本机单用户工作台，不是远程多用户 SaaS。

## 4. 验收材料核对

- 最新 `docs/traceability/requirements-matrix.md`：40 条状态均为 PASS。
- `docs/acceptance/FIXED_CASES.md`：T01—T16 记录 16 PASS、0 FAIL、0 NOT_RUN。
- `plans/06_PROGRESS/TASK_BOARD.md`：122 个任务行均 ACCEPTED。
- `P11-05.md`：记录 Chromium 经工作台完成道路缓冲两次、属性筛选一次的真实端到端任务。
- `P11-07.md`：记录干净源码副本、新 venv、空 SQLite 和七步道路任务启动验证。
- `lifecycle.md`：记录失败、取消、超时、重启、重试和重查演练。

证据限制：多个验收文档引用大量 JUnit、JSON、截图和临时运行目录。GitHub 可见 `artifacts/test-results/` 根目录只有 7 个 XML；部分引用可能位于 `artifacts/smoke/` 或其他证据子目录。P12 必须扫描引用路径并分类，不能简单认定所有证据缺失。

## 5. 发现项与风险登记

| ID | 级别 | 发现 | 依据 | 判断 |
|---|---|---|---|---|
| AUD-01 | P1 | 最终验收报告统计滞后 | FINAL_ACCEPTANCE 仍写 36 PASS/4 PARTIAL；后续补齐报告和最新矩阵为 40 PASS/0 PARTIAL | 文档事实冲突，统一 |
| AUD-02 | P1 | 测试数量口径分散 | 最终报告记后端 1,595+1、前端 66；后续补齐报告记后端 1,646、前端 67 | 可能是不同轮次/收集范围，需同 commit 复跑 |
| AUD-03 | P1 | 证据路径未形成机器审计闭环 | 部分最终 JUnit 引用不在根 test-results 可见清单 | 建链接/证据扫描器 |
| AUD-04 | P1 | 未见 GitHub Actions workflow | 当前递归树未发现 `.github/workflows/` | 本地测试已有，提交级自动回归未证明 |
| AUD-05 | P1 | 缺少大数据/并发基准 | FINAL_ACCEPTANCE 明确未做大数据或并发压力测试 | 性能容量未知 |
| AUD-06 | P1 | 当前单活动运行 | scheduler/loop.py 有活动工作时不领取新任务 | V1 可接受，但不能宣称并发 GIS 作业 |
| AUD-07 | P2 | 模型 provider 生态较窄 | provider 可见 mock.py、ollama.py | 抽象已具备，供应商覆盖有限 |
| AUD-08 | P2 | 方法收录仍是人工审核流程 | method-authoring README 要求人工核对脚本、参考输出和正式配置 | 可做开发者工具，保留人工审批 |
| AUD-09 | P2 | QGIS 环境可移植性有限 | README 要求配置本机 qgis_process | 需要环境诊断与部署矩阵 |
| AUD-10 | P2 | 依赖治理自动化证据不足 | requirements 与 package-lock 固定，但未见依赖扫描/升级工作流 | 建受控依赖审计 |
| AUD-11 | P2 | 缺少真实用户研究 | 当前主要是开发者样本和 Chromium 验收 | 需要 GIS 用户任务观察 |
| AUD-12 | P3 | 配置/Schema/文档存在漂移风险 | 方法、工具、传感器配置与文档分散 | 建一致性检查 |

P0 表示数据丢失、错误计算或越权执行等关键风险；本次静态审计未确认新的 P0 缺陷。部分 AUD 是未验证/产品边界，不应直接标记为代码缺陷。

## 6. 第二轮路线

- **P12 审计闭环**：核对证据、统一统计、检查配置/API 漂移、建立 CI 和性能测试基线。
- **P13 运行可靠性与性能**：多规模基准、资源遥测、内存/临时文件、缓存失效、队列压力；是否并发由实测决定。
- **P14 方法与工具生态**：开发者 CLI、配置模板、lint、样例包、隔离试跑、人工审批发布和回滚。
- **P15 Agent 自主分析**：多轮澄清、执行反馈解释、替代方案、失败后计划修订；代码生成仅在隔离环境试点。
- **P16 产品化与用户验证**：环境诊断、部署矩阵、真实用户任务、错误恢复体验和可复现交付。

## 7. 非目标
1. 不立即转向多用户远程 SaaS。
2. 不未经资源/锁模型评估就把单活动调度改成并发。
3. 不允许模型直接执行任意 Python/GIS 代码。
4. 不自动信任或自动发布脚本。
5. 不以增加算法数量替代方法质量。
6. 不以测试数量增长替代真实 GIS 正确性。

## 8. 结论
V1 核心原型不需要推倒重来。P0—P11 视为既有资产。P12 首先固定 commit、对账统计、扫描证据、统一复跑和 CI；P12 签收后再放行 P13—P16。

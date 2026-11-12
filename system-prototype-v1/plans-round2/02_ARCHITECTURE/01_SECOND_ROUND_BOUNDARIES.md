# 第二轮架构边界

## V1 已有能力（复用，不重写）
- `src/geopilot/data/`：资产、读取、指纹、版本、PostGIS 快照
- `src/geopilot/application/`：上下文、方案运行、检查证据
- `src/geopilot/domain/`：公共领域模型
- `src/geopilot/methods/`：方法定义、加载、校验
- `src/geopilot/plans/`：方案编译、确认、版本
- `src/geopilot/execution/`：QGIS/Python runner
- `src/geopilot/scheduler/`：单活动任务调度、取消、步骤执行
- `src/geopilot/checks/`：检查与重查
- `src/geopilot/artifacts/`：成果登记与来源
- `src/geopilot/llm/`：模型请求、出站边界、解析
- `frontend/src/`：现有工作台

## 第二轮增量
- `scripts/quality/`：证据一致性和交付验证工具
- `benchmarks/`：可重复数据规模、性能和资源基准
- `src/geopilot/observability/`：仅在 P13 证明必要时新增
- `scripts/methods/`：方法开发者 CLI
- Agent 规划模块仅在 P15 安全设计通过后增加
- `docs/round2/`：审计、性能、生态和用户验证文档

不得复制 V1 data/methods/plans/scheduler 体系。

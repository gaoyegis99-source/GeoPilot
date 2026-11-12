# GeoPilot 原型路径映射

所有产品路径相对于 `/home/mtd/liweigis/GeoPilot/system-prototype-v1`。2026-11-11 审计时只有 `plans/`，表内拟建路径在对应任务验收前均为未实现。设计原件位于父目录 `../docs/`。

| 逻辑模块 | 审计时路径 | 拟建路径 | 写入责任 |
|---|---|---|---|
| 后端/API 与应用服务 | 无 | `src/geopilot/api/`、`src/geopilot/application/` | 主控冻结 `/api` 契约；D 写运行服务、A 写数据应用服务 |
| 公共模型 | 无 | `src/geopilot/domain/` | 主控指定单个责任人按任务写入 |
| 数据适配和登记 | 无 | `src/geopilot/data/` | A |
| SQLite、迁移及工作目录 | 无 | `src/geopilot/persistence/`，本地数据根目录 `workspace/` | A，D 按冻结存储契约接入 |
| GIS 计算与测量 | 无 | `src/geopilot/gis/` | B |
| 执行器和调度 | 无 | `src/geopilot/execution/` | B 写适配器，D 写调度，主控分配文件 |
| 方法目录与方案 | 无 | `src/geopilot/methods/`、`src/geopilot/plans/`、`config/methods/` | C |
| Web GIS 界面 | 无 | `frontend/` | E |
| 模型适配 | 无 | `src/geopilot/llm/` | G |
| 测试、参考样本 | 无 | `tests/`、`tests/fixtures/{vector,raster}/`、`tests/golden/`、`tests/smoke/` | F 或按任务分配互斥测试文件 |
| 环境和阶段证据 | `plans/` 为初始计划 | `docs/baseline/`、`docs/acceptance/`、`artifacts/` | 各任务责任人写自己的证据，主控签收 |
| 开发入口 | 无 | `pyproject.toml`、`scripts/test.sh`、`scripts/smoke/` | 主控冻结共享配置，单任务独占文件 |

路径确定后先验证可导入和运行，再将验收证据写入看板。Python 测试使用 `python -m pytest`，P0-07 的统一入口目标为 `bash scripts/test.sh`，默认 JUnit 输出 `artifacts/test-results/junit.xml`。QGIS 用实际可执行入口完成 P0-03，环境记录将注明完整命令。

P0–P3 已落地的实际路径如下。后续模块按上表与对应原子任务推进。

| 已验收职责 | 实际入口与文件 |
|---|---|
| 环境与参考样本 | `scripts/smoke/{qgis_smoke,python_gis_smoke}.py`、`tests/fixtures/{vector,raster}/`、`tests/{smoke,golden}/` |
| 资产与版本契约 | `src/geopilot/domain/data_asset.py` |
| 本机读取 | `src/geopilot/data/{vector_reader,geopackage_reader,raster_reader,errors}.py` |
| 指纹、登记与重开 | `src/geopilot/data/{fingerprint,assets,resolver}.py` |
| SQLite、资产仓库、运行目录 | `src/geopilot/persistence/{schema,asset_repository,workspace}.py` |
| 工具契约、注册及参数 | `src/geopilot/domain/tool_contract.py`、`src/geopilot/tools/{registry,parameter_normalizer}.py`、`config/tools/` |
| 实际 CRS、测量和投影 | `src/geopilot/gis/validation/crs_validator.py`、`src/geopilot/gis/measurement/{coverage,distance,area,projection}.py` |
| 单步执行与交接 | `src/geopilot/domain/{step_io,errors}.py`、`src/geopilot/execution/{runner,qgis_runner,python_runner,step_support}.py`、`config/runner_bindings/` |
| 方法与方案 | `src/geopilot/methods/{models,loader,validator}.py`、`src/geopilot/plans/`、`src/geopilot/persistence/plan_repository.py`、`config/methods/` |
| 同步运行与检查 | `src/geopilot/application/{task_context,run_inputs,plan_runner,run_checks,method_preconditions}.py` |
| 测试与验收 | `scripts/test.sh`、`docs/acceptance/{P0,P1,P2,FIXED_CASES}.md`、`artifacts/{test-results,schemas}/` |

## P11 验收命令的实际入口

| 任务 | 实际 pytest 入口 |
|---|---|
| P11-02 | `tests/golden`、真实测量/道路/裁回/遥感/空间关系/来源 ID 节点、模型边界和真实 HTTP/QGIS 节点，完整命令见验收记录 |
| P11-06 | `tests/scheduler tests/runners/test_process_timeout.py tests/checks/test_recheck.py tests/checks/test_recheck_service.py tests/api/test_check_reports.py tests/api/test_run_status.py tests/runs/test_retry.py tests/runs/test_run_api.py`，覆盖计划中的调度和检查报告演练 |
| P11-03 | `tests/methods`，另含道路与属性方法应用入口、对象归属和脚本扩展集成 |
| P11-04 | `tests/integration`，前端从 `frontend/` 运行 Vitest 和 build |
| P11-08 | 项目根 `python -m pytest -q`，前端 `frontend/` 的 `npm run build` |
| P11-05 | `tests/e2e` 与 README 的本机入口，复现包在 `artifacts/reproduction/` |

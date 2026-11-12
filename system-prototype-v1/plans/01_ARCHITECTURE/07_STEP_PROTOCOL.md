# P2 步骤执行交接

按 V2.0 §8.9，P2-08/09 共享 JSON 交接形状，P2-10 在其验收后补齐 Pydantic 模型及统一入口，保留原依赖。请求键为 `run_id`、`step_key`、`tool: {id, version}`、`workdir_ref`、`inputs`、`parameters`、`output_targets`、`log_path`、`cancel_path`。输入与输出引用均为 `{path, layer?}`。路径相对本次运行根，输入真实存在，输出使用新文件，日志、请求、结果及各成果位置不得冲突。运行器仅按精确登记版本启动。P2-08/09 是适配层，测量前置条件由 P2-10 统一入口调用真实策略后再派发。

共享 `geopilot.execution.step_support.execute_adapter(request, *, registry, workdir, runner, invoke)` 校验本机交接、精确工具与参数，保存规范化请求和结果。`invoke(PreparedStep)` 负责具体命令及协议，成功返回实际环境版本表。`PreparedStep` 提供 `request`、`contract`、`workdir`、`log_path`、`request_path`、`result_path`。共用 `run_process(step, argv, *, timeout, stdin=None, env=None)` 返回进程 stdout，日志包含 argv、实际 stdout/stderr、退出码。`AdapterError(message, *, code, field=None, exit_code=None, phase="execute")` 描述失败。

完整结果含 `run_id`、`step_key`、`status`、`exit_code`、`outputs`（`{path, layer, type}`）、`log_path`、`warnings`、`error`、`started_at`、`finished_at`、`environment`。P2 可产生 `SUCCEEDED` 或 `FAILED`，P4 接取消。

`geopilot.execution.qgis_runner.run_qgis(request, *, registry, workdir, bindings, qgis_process=None, timeout=60)` 使用可信本机绑定的 `inputs`、`parameters`、`outputs` 名称映射和 `enum_values` 映射，按 CLI 的 JSON 协议调用。先查询真实 `--json help` 核对映射与当前算法，再启动计算。`geopilot.execution.python_runner.run_python(request, *, registry, workdir, implementations, timeout=60)` 仅从调用方显式配置的实现标识到本地脚本路径映射中取脚本，独立进程接收 `--request`、`--result`，子进程清单确认 `outputs` 的实际路径。两者均保存同格式结果并实读声明成果。启动失败、超时、非零退出、结果缺失和不一致分别以错误字段记录。

## P2-10 类型化入口

`geopilot.domain.step_io` 定义 `StepRequest`、`StepFileRef`、`StepOutput`、`StepResult` 和 `ExecutionStatus`。请求引用使用相对路径和可选图层，`tool` 为 `ToolRef`，参数为有限 JSON 值。结果为终态信封，开始结束时间带时区且有序。成功需要退出 0、日志和空错误；失败需有与结果步骤、日志一致的错误，退出码可为空或为 0。执行状态与后续检查状态独立。 `geopilot.domain.errors.ErrorDetail` 提供 `code`、`phase`、`step_key`、`field`、`message`、`suggestion`、`log_path` 及可选 JSON `observed`。

`geopilot.execution.runner.run_step(request, *, registry, workdir, qgis_bindings=None, python_implementations=None, qgis_process=None, timeout=60)` 是方案运行的类型化单步入口。以实际输入元数据核对测量前置条件，然后派发已登记的运行器，返回 `StepResult`。通过时在步骤日志记录实际角色、测量方式、CRS 和单位。拒绝时记录 `preflight` 错误，适配器不启动。已有 `run_qgis` 和 `run_python` 用于适配层验证。

| 附录 B 扩展错误码 | 含义 |
|---|---|
| `INVALID_STEP_REQUEST` | 请求结构、角色或运行内路径无效 |
| `INVALID_PARAMETER` | 规范化参数、测量参数或可信本机绑定不符合声明 |
| `TOOL_UNAVAILABLE` | 工具确切版本未登记、依赖不可用或运行器不匹配 |
| `OUTPUT_INVALID` | 成果、清单或环境报告存在，但内容或类型不符合请求 |

模型导出的 JSON Schema 和真实结果验证见 [P2-10](../../docs/acceptance/P2-10.md)。

P3 中同一 QGIS 算法可支持端口不同的确切工具版本。可信本机 `qgis_bindings` 优先查 `工具 ID@版本`，随后查原有实现 ID。两种绑定均核对契约端口与当前实际算法定义。

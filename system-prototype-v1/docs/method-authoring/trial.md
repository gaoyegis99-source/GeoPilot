# 方法隔离试跑

`trial` 在独立项目中用 V1 服务运行一个 [样例包](reference-packages.md) 的正例或预期失败用例。运行环境使用项目 GIS 依赖和 `requirements-test.txt`。V1 API 由进程内 ASGI 客户端调用，无需启动服务器。

```bash
trial_parent=$(mktemp -d)
.venv/bin/python scripts/methods/method_cli.py trial \
  --package tests/method_dev/fixtures/dem-terrain/manifest.json \
  --work-dir "$trial_parent/dem" \
  --timeout 60

.venv/bin/python scripts/methods/method_cli.py trial \
  --package tests/method_dev/fixtures/attribute-filter/manifest.json \
  --work-dir "$trial_parent/vector" \
  --qgis-process /path/to/qgis_process \
  --timeout 60

.venv/bin/python scripts/methods/method_cli.py trial \
  --package tests/method_dev/fixtures/dem-terrain/manifest.json \
  --work-dir "$trial_parent/negative" \
  --case missing-vertical-unit
```

- `--package` 是样例清单。省略 `--case` 时选择清单正例。指定失败用例时，只有 V1 返回的实际阶段和 issue code 均匹配，且 SQLite 中没有运行记录，才算通过。
- `--method` 指定外部 UTF-8 方法 YAML，其 ID、版本和输入角色必须匹配样例包。省略时，从项目的内置方法目录选择精确版本。候选在隔离配置中通过 `lint` 后，只引用项目已有的工具契约、runner 绑定和本地实现。
- `--work-dir` 必须是现有父目录下尚未存在的路径。配置、源码、样例包、默认正式数据目录和 `GEOPILOT_DATA_DIR` 指定的正式数据目录均为受保护路径。每次重试选择新目录。
- `--qgis-process` 适用于引用 QGIS 工具的试跑，按显式值、`GEOPILOT_QGIS_PROCESS`、`PATH` 的顺序查找。使用受信任且保持前台运行的本地 QGIS Processing 入口。试跑所管理的范围为本机 V1 子进程及其进程组。
- `--timeout` 是正的有限秒数，默认为 120。单调时钟从工作目录创建后开始计时，覆盖配置装配、发现、资产登记、草稿、确认、执行和独立比较。包和方法的初步校验发生在此之前。

样例输入复制到 `inputs/`，校验指纹并移除文件写权限后，才通过 V1 登记。方法和项目配置复制到 `config/`。项目数据库、运行工作区与 QGIS 配置目录均位于试跑目录中。结束时复核原样例包和所读取源配置的指纹。正例依次经过 V1 草稿、确认、运行与检查，再使用样例包的独立参考比较成果。

目录内的 `report.json` 使用 `schema_version: 1`，包含精确方法及工具版本、输入和配置指纹、关键选项、声明和实际环境、源文件复核、实际阶段和问题、运行与检查状态、独立比较结果及复跑参数。用 `replay_argv` 复跑时，把 `<new-trial-directory>` 替换为新路径，核对两次报告的指纹及环境。实际环境与声明环境的匹配结果单独记录。

| 退出码 | 结果 |
|---|---|
| `0` | 正例通过时，`status` 为 `PASS` 且 `verdict` 为 `positive_match`。匹配预期失败时，`verdict` 为 `expected_failure_matched`。 |
| `1` | 样例包、配置、运行、超时、预期失败或独立比较有误。 |
| `2` | CLI 用法、时限值、目标目录或候选文件路径不合要求。 |

标准输出沿用 CLI JSON 信封。通过时 `data` 含报告和路径，失败时 `data` 为 `null`，具体错误位于 `errors`。工作目录创建后出现的运行错误会保留 `report.json` 和已生成的诊断，路径可从 CLI 错误中取得。工作目录创建前拒绝的请求只返回 CLI 错误。 [CLI 协议](cli.md)

执行阶段达到时限时，请求 V1 取消并等待调度线程和受管进程退出。报告包含取消受理、最终状态和工人租约检查结果。QGIS 目录发现有独立进程组，发现失败或超时保留 `discovery/process.log`。阶段之间检查剩余时间。进程终止宽限期和同步 GIS 读取或检查返回所需的时间可能让记录的总耗时超过设置值。运行内日志位于 `workspace/tasks/<task>/runs/<run>/steps/`。

试跑证据可供人工审核，正式审批发布使用单独流程。

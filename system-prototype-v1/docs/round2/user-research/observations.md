# 观察记录与汇总

复制 [空白记录](observations.blank.json) 到本机研究目录。真实观察使用 `record_kind: observed`，测试记录使用 `synthetic` 并另存。每个会话对应一个参与者，使用 U001 到 U999 的匿名编号。一次研究中不得重用编号。参与者重试某一步记作同一任务的耗时和事件。

以下对象示例使用占位值，复制到 `sessions` 后填写真实日期、版本和观察结果。 `gis_use` 可取 `occasional`（偶尔使用 GIS）或 `regular`（经常使用）。`environment.commit` 填完整的 40 位提交哈希。 `os` 和 `browser` 为浏览器端的版本，`qgis` 为后端实际执行器版本。省略主机名、账户和文件路径。

```json
{
  "participant_id": "U001",
  "trial_date": "YYYY-MM-DD",
  "gis_use": "regular",
  "environment": {"commit": "完整 Git SHA", "os": "系统版本", "browser": "浏览器版本", "qgis": "QGIS 版本"},
  "tasks": {
    "register_assets": {"outcome": "incomplete", "elapsed_seconds": 0, "help_requests": 0, "assisted": false, "errors": []},
    "select_method": {"outcome": "not_attempted", "elapsed_seconds": null, "help_requests": 0, "assisted": false, "errors": []},
    "confirm_plan": {"outcome": "not_attempted", "elapsed_seconds": null, "help_requests": 0, "assisted": false, "errors": []},
    "run_plan": {"outcome": "not_attempted", "elapsed_seconds": null, "help_requests": 0, "assisted": false, "errors": []},
    "inspect_checks": {"outcome": "not_attempted", "elapsed_seconds": null, "help_requests": 0, "assisted": false, "errors": []},
    "download_result": {"outcome": "not_attempted", "elapsed_seconds": null, "help_requests": 0, "assisted": false, "errors": []}
  }
}
```

| 字段 | 填写规则 |
|---|---|
| `outcome` | `completed` 为达到任务完成条件，`incomplete` 为已尝试未达到，`not_attempted` 为尚未尝试。 |
| `elapsed_seconds` | 已尝试填非负秒数，最多 86400，允许小数。未尝试填 `null`。 |
| `help_requests` | 参与者请求帮助的非负整数次数。未尝试填 0。 |
| `assisted` | 是否收到主持提供的实际操作或方向信息。未尝试填 `false`。 |
| `errors` | 事件数组。每条如 `{"code":"WRONG_LAYER","recovery":"self_recovered"}`。恢复枚举为 `self_recovered`、`assisted_recovery`、`unresolved`。`assisted_recovery` 要求本任务 `assisted: true`。未尝试填空数组。 |

六个任务均为必填，首项必须尝试过。首次出现 `not_attempted` 后所有任务都标为 `not_attempted`。报告按主持手册的实际完成条件录入。 [主持手册](moderator.md)

在原型根目录运行，输出 JSON 到新的报告路径。

```bash
.venv/bin/python scripts/quality/summarize_user_trials.py /path/to/observations.json > /path/to/summary.json
```

合法文件退出 0。空白的真实观察表输出 `NOT_RUN`、0 个会话及空比率。测试输入输出 `SYNTHETIC`。已录入真实观察输出 `OBSERVED`，仍需主持检查招募条件、环境偏差和备注后解释。无效输入退出 1，错误写到标准错误，无 JSON 结果。

完成率以所有已开始会话为分母。全程完成要求六项均完成，无协助完成要求所有已完成任务均无实际协助。任务中位耗时只用已尝试任务，包括未完成。全程中位耗时只用完成六项的会话，并以六项计时之和计算。任务报告分别提供求助人数和次数、收到协助人数、发生错误人数和事件数、恢复类别。少于 5 个会话时标记小样本限制。所有结果只解释这个样例和本次样本的观察。

# 方法开发者 CLI 契约

从原型目录使用项目 Python 环境运行。示例中的候选、数据库和工作区应指向同一次本机试验。

```bash
python scripts/methods/method_cli.py inspect --candidate review/candidate.yaml
python scripts/methods/method_cli.py validate --candidate review/candidate.yaml \
  --database project.sqlite --workspace workspace
python scripts/methods/method_cli.py report --candidate review/candidate.yaml \
  --database project.sqlite --workspace workspace
python scripts/methods/method_cli.py template --tool qgis-buffer@1 --method buffer-review@1
python scripts/methods/method_cli.py lint --method review/method.yaml
```

每个路径参数都指向显式本机路径。相对路径以调用进程的当前目录为基准。候选是单份 UTF-8 YAML，由 V1 严格 YAML 加载器和 `RegistrationCandidate` 校验，重复键会被拒绝。工具、方法和试验中的输入资产均使用精确版本。

| 命令 | 校验范围 | 成功数据 |
|---|---|---|
| `inspect` | 解析候选并校验模型结构 | `scope: candidate_structure` 和标准化的 `candidate`。可在已有试验证据之前查看。 |
| `validate` | 使用当前服务的规则注册集，通过 V1 手工收录校验核对已保存试验的成功状态、快照、参数、输入版本和独立参考结果 | `scope: manual_registration` 和 V1 `RegistrationEvidence`，含精确版本、脚本 SHA-256 和各输出指纹。 |
| `report` | 每次调用都进行与 `validate` 相同的完整核对 | 完整证据、标准化候选和 `disposition: manual_review_required`。 |
| `template` | 从精确版本的本机工具契约生成方法草稿 | `scope: method_template`、来源工具、草稿和待填项。 [模板说明](templates.md) |
| `lint` | 检查单份方法草稿或本机目录中的方法定义 | `scope: method_static_lint`、通过检查的方法和单列的历史示例。 [静态检查说明](lint.md) |

各命令只读配置或显式输入，结果写到标准输出。 `report` 可重定向到独立审查文件，候选和现有试验目录仍可供复核。报告重述候选中明确提供的值和已核对指纹，工作区内的脚本文本、资产源路径、数据库原始记录及环境变量不会被加入成功报告。正式登记需要维护者核对并明确批准。

`validate` 和 `report` 需要停写且无 sidecar 的回滚日志模式 SQLite 数据库。V1 校验器用 SQLite `mode=ro` 打开数据库，WAL 模式在只读连接时仍可能生成 `-wal` 和 `-shm`。CLI 会在连接前拒绝 WAL 文件头或现有 `-wal`、`-shm`、`-journal`。遇到 `DATABASE_REVIEW_COPY_REQUIRED` 时，请提供一致的独立审查副本，仅在副本上完成 checkpoint 和回滚日志模式设置，并保留对应试验工作区。审查期间保持副本停写。

## 机器接口

除 `--help` 和 `--version` 外，每次调用在标准输出返回一个 JSON 对象。 `--version` 返回 `GeoPilot method CLI 1`。

```json
{
  "schema_version": 1,
  "command": "validate",
  "status": "error",
  "data": null,
  "errors": [
    {
      "code": "REFERENCE_MISMATCH",
      "field": "reference.script_sha256",
      "message": "Script SHA-256 differs from the reference"
    }
  ]
}
```

| 退出码 | 含义 |
|---:|---|
| 0 | 命令在其声明范围内成功。 `status: ok`、`data` 为结果对象、`errors: []`。 |
| 1 | YAML、工具或方法配置、候选模型或现有试验证据被校验拒绝。 `status: error`、`data: null`。 |
| 2 | 用法错误或输入路径、数据库无法安全读取。 `status: error`、`data: null`。 |

每个错误包含 `code`、`field`、`message`，`lint` 另提供配置文件 `path`。 `field` 使用文档中的点分路径，列表用从零开始的索引，根为 `$`。参数解析错误使用 `CLI_USAGE` 和根路径，此时 `command` 为 `null`。候选 YAML 及模型错误分别为 `CANDIDATE_PARSE_ERROR` 和 `INVALID_CANDIDATE`，V1 校验错误沿用原诊断码和字段。调用方根据退出码、错误码和字段分支，消息用于展示。现有字段的含义或类型发生不兼容变化时提升 `schema_version`。

## 后续命令的接入点

- P14-04 的样例包把独立参考、容差和环境信息带入验证证据。P14-05 的显式试运行入口通过现有 runner 写入指定隔离工作区，报告区分试跑结果与人工收录验证。
- P14-06 保持既有精确版本引用可追溯。P14-07 的审批、发布和回滚通过单独的显式操作记录，保留人为决策及审计事件。

新增命令沿用同一 JSON 结果结构及退出码语义。读取项目或写入试跑目录的命令应分别声明路径和副作用，现有三个命令继续作为只读审查入口。

# 根据已登记工具生成方法模板

```bash
python scripts/methods/method_cli.py template --tool qgis-buffer@1 --method buffer-review@1
```

`--tool` 选择工具的精确 `ID@VERSION`，`--method` 声明拟编写的方法 ID 和正整数版本。CLI 从原型的 `config/tools/` 读取契约，也可通过 `--config` 指定包含 `tools/` 的本机配置根。相对路径按调用时的当前目录解释。工具由契约的 ID 和版本定位，文件名可以不同。

成功结果采用 CLI 版本 1 的 JSON 封装。 `data.scope` 为 `method_template`，其余字段分别用于编辑和复核。

| 字段 | 用法 |
|---|---|
| `data.tool` | 来自工具目录的已校验 `ToolContract`。它提供参数类型、边界、单位、真实默认值、测量约束和已登记检查规则。 |
| `data.method` | 可独立提取的 `MethodDefinition` 草稿。单步引用指定工具的精确版本，以原端口建立输入、输出和成果绑定。可调整角色名称及其绑定，并按实际任务补充方法级检查。 |
| `data.todos` | 与方法 Schema 分开的待填写事项。每项包含 `code`、`field` 和 `message`。 |

已有默认值按原契约保留，包含 `false` 和 `0`。缺少默认值的必填参数生成方案选项绑定，默认值留空，并列出 `REQUIRED_OPTION_VALUE`。例如缓冲距离的草稿保留数值类型、米单位和大于零的约束，实际距离在编制方案时提供。可选参数缺少默认值时不生成绑定，并列出 `OPTIONAL_PARAMETER_UNBOUND`。若方法需要该参数，可以增加相应选项和绑定。

仅将 `data.method` 保存为单独方法文档。JSON 对象也可作为 YAML 输入。 `data.tool` 和 `data.todos` 可与完整报告一同留在审查目录。模板命令只向标准输出写入 JSON，生成的草稿由维护者继续编辑、试跑和人工审核。示例的独立草稿及待填项见 [真实缓冲模板](../round2/methods/p14-02-example.json)。

| 错误码 | 字段 | 退出码 |
|---|---|---:|
| `INVALID_REFERENCE` | `tool` 或 `method` | 1 |
| `TOOL_NOT_REGISTERED` | `tool` | 1 |
| `INVALID_TOOL_CONFIG`、`DUPLICATE_TOOL_REFERENCE` | `config` | 1 |
| `INPUT_READ_ERROR` | `config` | 2 |

模板生成复用 V1 配置模型。真实 GIS 试跑、版本兼容、审批和正式发布由相应阶段核验。 [CLI 结果封装与审查契约](cli.md)

# 方法配置静态检查

在原型目录使用项目 Python 环境。默认检查 `config/methods/` 中的目录和 `config/tools/` 中的精确工具版本。也可为一份准备试跑的 UTF-8 YAML 指定独立路径。

```bash
python scripts/methods/method_cli.py lint
python scripts/methods/method_cli.py lint --config config --method review/method.yaml
```

`--config` 指向包含 `tools/` 的配置根。目录模式同时需要 `methods/`。显式相对路径按调用目录解析。目录模式拒绝重复方法或工具精确版本。单文件模式检查所选 YAML 及当前工具目录，版本发布冲突交给登记流程。

静态检查沿用严格 YAML 加载器、`MethodDefinition`、`ToolContract`、工具注册表及 V1 绑定校验。它检查精确工具版本、工具端口和类型、参数与成果绑定、输入角色、生产步骤及其输出、声明顺序和依赖循环。存在步骤变体时逐个检查展开后的顺序和依赖。检查规则、工具规则和前置条件均须已注册。固定顺序标志保留在报告中，具体计划相对于声明的顺序由编译器核对。 [V1 方法校验](../../src/geopilot/methods/validator.py) · [P12 静态配置审计](../round2/audit/p12-09.md)

目录模式将原路径下的 `road-buffer-statistics@1` 单列在 `catalog_examples`，并标为 `catalog_example_only`。该分类要求文件路径、方法 ID、精确版本和 P12 记录的 10 个诊断码及字段完全相符。任何新增诊断、改名或替换成其他错误都会使 lint 失败。单文件模式对该历史示例也会报告错误。正式可执行方法版本位于 `data.methods`。

## JSON 结果

结果沿用方法 CLI 版本 1，`command` 为 `lint`。成功时退出 0，`data.scope` 为 `method_static_lint`，`data.selection` 为 `catalog` 或 `single_method`。`tools_checked` 记录工具版本数，方法记录包含 `id`、`version`、`path`、`locked_order`。历史例外的诊断位于对应的 `catalog_examples[].diagnostics`。

| 退出码 | 结果 |
|---:|---|
| 0 | 所选范围内静态校验成功。 |
| 1 | YAML、模型、重复精确版本或静态引用被拒绝。 |
| 2 | 用法错误或输入路径不可读。 |

错误结果为 `status: error`、`data: null`。每项诊断包含 `code`、点分 `field`、`message` 和 `path`。配置根内的路径以 `config/` 开头，独立方法文件显示其绝对路径。字段索引从 0 开始，根字段为 `$`。变体依赖字段如 `step_variants.choices.geodesic.steps.4.inputs.source.step`。用法错误沿用 CLI 通用 `CLI_USAGE` 诊断。

按错误码区分问题类型，例如 `TOOL_NOT_REGISTERED` 为缺少精确版本，`UNKNOWN_ASSET_ROLE` 为未声明角色，`FUTURE_STEP_REFERENCE` 为引用了后置步骤，`DEPENDENCY_CYCLE` 为依赖循环，`UNKNOWN_CHECK_RULE` 为方法引用了未注册的检查规则。

lint 只读取 YAML 和本地工具配置并写标准输出，无 QGIS 探测、GIS 试跑、候选生成或正式登记。实际引擎和独立参考仍由试跑验证，正式收录须另行批准。 [统一 CLI 契约](cli.md)

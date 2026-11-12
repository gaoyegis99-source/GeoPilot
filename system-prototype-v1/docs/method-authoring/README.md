# 手动收录本地脚本

1. 在本地脚本区保存版本和参数，运行一次有代表性的固定样例。记录运行编号、脚本 SHA-256、确切输入资产版本、日志和结果文件。
2. 复制 [收录模板](manual-registration.yaml) 到本机审查目录。按实际用途填写工具的输入输出端口、参数类型与单位、测量策略、前提和规则。用方法步骤绑定精确工具版本、输入角色、选项和成果。候选方法中的步骤可以引用同一拟收录工具多次。
3. 按手工坐标、独立计算或独立参考文件填写 `reference.basis` 和全部输出端口。矢量参考使用已知 `crs` 和按 `source_feature_id` 索引的 `features` WKT，校验器比较精确 ID 和坐标。其他输出可以用 `geopilot.data.fingerprint.fingerprint_file` 对独立参考文件组计算 `fingerprint`。指纹适用于可精确比较字节的输出，地理数据的元数据、压缩和编码差异也会改变指纹。
4. 使用只读校验器复核同一次真实运行。候选通过后，人工审查脚本本身、适用数据范围和固定样例。在隔离的工具配置中实现并核对正式适配协议，正式启用由维护者决定。

从原型目录使用项目 Python 运行以下命令。候选结构预览和可保存的审查报告见 [CLI 命令契约](cli.md)。

```bash
python scripts/methods/method_cli.py validate --candidate review/manual-registration.yaml \
  --database project.sqlite --workspace workspace
```

以下为直接调用同一 V1 校验器的 Python 接口。

```python
from pathlib import Path

import yaml

from geopilot.application.run_checks import RUN_CHECKS
from geopilot.scripts.registration import RegistrationCandidate, validate_manual_registration

candidate = RegistrationCandidate.model_validate(
    yaml.safe_load(Path("manual-registration.yaml").read_text(encoding="utf-8"))
)
evidence = validate_manual_registration(
    "project.sqlite", "workspace", candidate, registered_rules=RUN_CHECKS,
)
print(evidence.model_dump_json(indent=2))
```

`validate_manual_registration` 检查试验快照和退出码、已登记输入版本和暂存指纹、候选方法端口与步骤依赖，以及实际可重开的成果。返回值是包含脚本、候选工具和方法版本以及已核对输出指纹的审查记录。试验结果清单保留文件大小，校验时重新读取成果并与独立参考比较。

本地试验脚本接收 `--request snapshot/request.json`。正式 Python 工具另接收 `--result`，按请求内的 `output_targets` 生成成果，并在子清单声明对应输出。专项测试用同一段脚本文本分别经过本地脚本进程和隔离配置中的方案编译、确认与正式 Python 进程。[双入口参考测试](../../tests/scripts/test_manual_registration.py)

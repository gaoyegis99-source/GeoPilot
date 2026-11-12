# P16-03 可用性观察准备与验证

2026-11-13 依用户决定废弃真实 GIS 用户浏览器测评。本页保存原准备及技术演练证据。 [范围决定](../../../plans-round2/04_TASKS/P16/P16-03.md)

- 执行角色 E/F，主持材料和汇总工具已通过主控复核。实际目标用户观察状态为 NOT_RUN。
- 测试基于提交 `b98eb0c8a302d379f2171fb8520e162e35143882` 与本项新增文件。环境为 Linux 5.15.0-139-generic x86_64、Python 3.13.13、pytest 8.3.4。
- 主持任务使用 V1 道路缓冲统计 v3 的局部投影分支。观察员据独立固定样本核查四层输入、七步方案、执行和检查结果、下载 CSV 的西区 3、东区 2、未归属 1。原型及其数据文件未修改。样本 GeoPackage SHA-256 为 `52bf921e66f43866e5dade81ae5b489a79dbbc7671f82c10cc50ec537c8ba39c`。 [受试任务单](participant-task.md) · [主持手册](moderator.md) · [固定用例复验](../audit/p12-07.md) · [样本参考](../../../tests/fixtures/vector/expected.json)

## 实际执行

在原型根目录运行。专项使用独立临时目录，结束后清理。

```bash
set -o pipefail
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY' 2>&1 | tee docs/round2/user-research/p16-03-pytest.log
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

with TemporaryDirectory(prefix="geopilot-p16-03-") as base:
    result = subprocess.run([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "tests/quality/test_summarize_user_trials.py", f"--basetemp={Path(base) / 'pytest'}",
        "--junitxml=docs/round2/user-research/p16-03-junit.xml",
    ])
sys.exit(result.returncode)
PY
.venv/bin/python scripts/quality/summarize_user_trials.py docs/round2/user-research/observations.blank.json > docs/round2/user-research/p16-03-not-run.json
.venv/bin/python scripts/quality/summarize_user_trials.py docs/round2/user-research/observations.synthetic.json > docs/round2/user-research/p16-03-synthetic-summary.json
```

| 核验 | 退出码 | 结果 |
|---|---:|---|
| 汇总工具专项 | 0 | 18 通过、0 失败、0 错误、0 跳过，0.90 秒。测试覆盖完成率分母、未尝试状态、实际协助、求助计数、恢复分类、耗时、空白记录、重复编号、重复 JSON 键、缺字段和非法数据。 [JUnit](p16-03-junit.xml) · [原始日志](p16-03-pytest.log) |
| 主控复验 | 0 | `7ca7594` 加本项文件，按同一测试文件复跑 18 通过、0 失败、0 错误、0 跳过，1.95 秒。样本 SHA-256 与原记录一致。 [JUnit](p16-03-review-junit.xml) · [原始日志](p16-03-review.log) |
| 空白观察记录汇总 | 0 | 0 个会话，`NOT_RUN`，完成率为 `null`。 [原始输出](p16-03-not-run.json) |
| 合成工具测试记录汇总 | 0 | `SYNTHETIC`，覆盖一次全程完成和一次中途停止，用于核对汇总逻辑。 [合成输入](observations.synthetic.json) · [原始输出](p16-03-synthetic-summary.json) |

## 交接

新增 [汇总工具](../../../scripts/quality/summarize_user_trials.py)、[专项测试](../../../tests/quality/test_summarize_user_trials.py)、[记录格式](observations.md)、[空白模板](observations.blank.json)、主持与受试材料及本目录原始证据。新增独立 JSON 输入输出 CLI。工作台、后端 API、数据库、生产配置和正式成果保持原样。回滚可撤销本批新增文件。

用户已确认可以安排参与者。建议邀请至少 5 名平时使用 GIS、此前未使用 GeoPilot 的人员，每人独立完成约 30 分钟的浏览器任务。主持人准备运行 QGIS 的后端和样本，参与者从工作台登记、运行、检查并下载 CSV。实际会话结束后，提交匿名观察记录及对应的下载 CSV。当前为 0 个真实会话，任务待实测。 [任务单](participant-task.md) · [匿名记录模板](observations.blank.json)

主持手册将浏览器端操作系统用于排查兼容问题。复验把 `observations.synthetic.json` 首个会话的 `environment.os` 改为 `null`，原 CLI 退出 1。修改后退出 0，汇总与原合成输入逐字节一致。22 项专项通过，浏览器版本与后端 QGIS 版本继续必填。测试使用 `9fb885d76566cca3f1d2cbc4dfa03f82d5c9b3a9` 加本次修改，Python 3.13.13。回滚可撤销此项提交。 [命令及原始日志](p16-03-browser-os.log) · [JUnit](p16-03-browser-os-junit.xml)

## 候选包浏览器演练与下载复核

主控在技术候选包 `44b17fcd5dc09ec9d3d713f2dddf8fdbcf3600b0` 解包后，用新的项目目录、回环服务和独立 Chromium 141.0.7390.37 上下文复跑受试任务。后端为 Python 3.13.13、QGIS 3.44.15，前端为锁定依赖。浏览器登记 4 个 GeoPackage 图层、选择道路缓冲统计 v3、生成并确认 7 步方案。执行 `SUCCEEDED`、检查 `PASSED`，展开方法规则后下载正式 CSV，浏览器与后端原件逐字节一致。分区计数为西区 3、东区 2、未归属 1，总计 6。前两次自动化脚本分别修正方法版本定位和检查折叠面板操作，第三次退出 0。 [候选包验收](../deployment/p16-06-tooling.md#同提交技术候选包验收)

D 实现可选 `--downloads-dir`，主控复验源码 `458a801`。pytest 8.3.4 的 35 项专项通过，退出 0，0 失败、错误和跳过。用演练下载文件和标为合成的观察夹具复核，得到 1 个计数核验通过。空白真实观察表保持 0 个会话和 `NOT_RUN`。未启用复核时空白及合成汇总与旧命令逐字节一致。撤销实现提交可回滚可选 CLI 参数。 [填写与复核说明](observations.md) · [原始日志、JUnit、下载和复现命令](p16-03-rehearsal-evidence.tar.gz)

演练证据包 SHA-256 为 `b9dc57cf375e1ab0c27f28190ffe875559f8c14c4e2bb2168dc071c79b611917`。解包后 `sha256sum -c SHA256SUMS` 退出 0。自动化演练用于确认当时的主持流程和结果口径。

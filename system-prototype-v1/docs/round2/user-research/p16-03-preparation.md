# P16-03 可用性观察准备与验证

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

实际研究需要确定招募渠道并安排目标 GIS 用户按主持手册完成独立会话，建议至少 5 人。当前没有真实会话，完成率、求助、错误和耗时没有可用于用户研究的观察值。任务总体保持待实测。

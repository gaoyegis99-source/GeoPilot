# P0-07 自动化测试基线

- 执行人：Agent-F
- 执行日期：2026-11-11（Asia/Shanghai）
- 提交状态：REVIEW
- 环境：Python 3.13.13、pytest 8.3.4、Bash

`pyproject.toml` 将测试发现限定在 `tests/`，并允许导入 `src/` 下的产品包。`tests/test_baseline.py` 核对任务文件、索引链接、清册及看板的任务编号一一对应。`scripts/test.sh` 从项目根目录启动 pytest，保留其退出码并默认写入 `artifacts/test-results/junit.xml`。脚本可接受 pytest 参数，可用 `GEOPILOT_PYTHON` 指定 Python 可执行文件。

需要写入文件的测试使用 pytest `tmp_path`，真实 GIS smoke 如需留存产物则显式传入专用证据目录。基线断言只读计划文件。验证脚本时用 `mktemp -d` 创建独立测试目录，复制计划和本任务的三份测试文件，在另一个空目录以绝对路径调用脚本。

| 实际执行 | 输出摘要 | 退出码 |
|---|---|---:|
| `bash -n scripts/test.sh` | Bash 语法检查通过 | 0 |
| `python -m pytest -q` | `1 passed in 0.07s` | 0 |
| `python -m pytest --junitxml=artifacts/test-results/junit.xml` | 发现 1 项，`1 passed in 0.12s`，XML 解析确认 1 项、0 失败、0 错误 | 0 |
| 干净复制目录，从空调用目录执行 `bash "$project/scripts/test.sh" -q` | `1 passed in 0.04s`，默认 XML 落在复制项目内 | 0 |
| 同一复制目录运行 `bash "$project/scripts/test.sh" -q -k nonexistent_p0_07_test --junitxml="$tmp/no-tests.xml"` | `1 deselected in 0.10s`，参数传递生效，XML 已写入指定路径 | 5（预期） |
| `bash scripts/test.sh -q --junitxml=artifacts/test-results/p0-07-junit.xml tests/test_baseline.py` | `1 passed in 0.20s`，XML 解析确认 1 项、0 失败、0 错误 | 0 |

保留的机器可读证据为 [P0-07 JUnit](../../artifacts/test-results/p0-07-junit.xml)。本任务新增 `pyproject.toml`、`scripts/test.sh`、`tests/test_baseline.py`、本报告及两份测试生成的 XML。无修改或删除文件，无产品 API、数据库或配置契约改动。

当前环境自动加载的 pytest-asyncio 在启动时报告循环范围弃用警告。基线测试是同步测试，执行通过。固定 GIS 用例 T01–T16 为 NOT_RUN。

P0 交叉测试时发现宿主 `/tmp/pytest-of-mtd` 还保留其他项目的临时目录。主控加入 `tests/conftest.py`，将默认 `tmp_path` 根限制为项目的 `artifacts/test-results/tmp/`，每次 pytest 使用独立编号目录。显式 `--basetemp` 或 `PYTEST_DEBUG_TEMPROOT` 仍生效。使用直接 `python -m pytest` 验证创建了项目内临时目录，栅格样本 4 项通过。

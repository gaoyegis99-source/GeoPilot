# P13-03 大档道路成果 CSV 回读修复

校正坐标范围后的大档道路探针固定生产源码 `37cb329be04eaddfeb23ac62789926d0fcdb63aa`，输入 GeoPackage 摘要为 `7211b7e75c9560acdab8198c555d62af732f1de3e9fe15317f9ab55706d21775`。真实运行 `a950e8765e8f4c07a6ab6e1711850817` 的七步均退出 0。汇总步骤记录 98,304 个入选目标、32,768 个分区、16,384 个未归属目标。随后成果登记报 `OUTPUT_INVALID`，字段为 `artifacts.zone_statistics.path`，终态为 `FAILED / NOT_CHECKED`。按冻结样本和原写出方式独立重建未归属 ID 字段，得到 573,440 个字符并复现原 131,072 字符限制。 [探针诊断](p13-03-L-road-csv-limit.json)

修复沿用标准 CSV 读法，在解释器初始化共享读取模块时一次性把读取上限提高到至少 8,388,608 字符。成果登记、计划与工具检查、表格预览使用同一策略。表格预览仍按现有行数参数读取，并最多返回 8 MiB 的 UTF-8 单元格文本。预览达到字节预算时使用已有的 `truncated` 字段，完整文件继续从原下载入口获取。

未归属字段保留全部 ID，测试逐值比对了预览返回值与源字段，并逐字节核对下载原件。检查用例另用真实 GeoPackage 验证了长 Unicode ID 与统计行的一致性。超过读取上限或格式异常的 CSV 仍按原错误码拒绝登记。撤销提交即可恢复旧读取策略，长字段回读依赖本项修复。

## 专项证据

执行主控与 F 交叉审查，基于 `740e7f356fa906ce2437f25ba9faeb30d16d075e` 加本项源码与测试。Python 3.13.13、pytest 8.3.4，专项使用独立临时目录。改动前两项均复现 131,072 字符错误。修复首次复跑的两项失败来自测试夹具缺少写事务和局部变量遮蔽。修正夹具后专项 19 项通过、18 项未选、退出 0。Python CI 接入两个回归入口，静态矩阵校验通过。 [修复前日志](p13-03-csv-before.log.gz) · [修复前 JUnit](p13-03-csv-before-junit.xml.gz) · [修复初次日志](p13-03-csv-initial-repair.log.gz) · [修复初次 JUnit](p13-03-csv-initial-repair-junit.xml.gz) · [复跑日志](p13-03-csv-pytest.log) · [复跑 JUnit](p13-03-csv-junit.xml)

```bash
csv_replay_tmp=$(mktemp -d /dev/shm/geopilot-csv-regression.XXXXXX)
export PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
export OPENBLAS_NUM_THREADS=1 GDAL_NUM_THREADS=1 OMP_NUM_THREADS=1
nice -n 19 taskset -c 31 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/artifacts/test_registration.py tests/application/test_run_checks.py tests/checks/test_tool_checks.py \
  -k 'registration or csv or point_count' \
  --basetemp="$csv_replay_tmp/pytest" --junitxml="$csv_replay_tmp/junit.xml"
.venv/bin/python scripts/quality/validate_ci_matrix.py
```

完整大档道路复验使用同一份输入和原生 QGIS 环境，待 DEM 大档终态后执行。P13-03 正式基准继续使用冻结的原始生产源码。

# P13-04 等积面积复现输入

三份 GeoTIFF 是 P13-04 真实 V1 冷预热中保留的未发布分类中间产物，供后续面积计算修复使用。文件逐字节复制，`manifest.json` 固定 SHA-256、格网、类别、NoData、来源提交、run ID、步骤和工具版本。原运行均以 `FAILED/NOT_CHECKED` 结束，子进程因生产 60 秒时限退出 -15。 [清单](manifest.json) · [P13-04 原始证据](../../../docs/round2/performance/p13-04.md)

| 文件 | 网格 | 类别像元 | NoData 像元 | 来源 |
|---|---|---:|---:|---|
| `dem-m.tif` | EPSG:32650，2046 × 2046，10 m | 类别 7 有 4,183,812 | 2,304 | `dem-terrain-analysis@1` 第六步 `extract_by_threshold`，下一步等积面积超时 |
| `dem-l.tif` | EPSG:32650，8190 × 8190，10 m | 类别 7 有 67,039,236 | 36,864 | `dem-terrain-analysis@1` 第六步 `extract_by_threshold`，下一步等积面积超时 |
| `remote-l.tif` | EPSG:32632，3072 × 3072，30 m | 类别 1、2、3 各 1,572,864 | 4,718,592 | `remote-sensing-index-change@1` 第八步 `calculate_change` 的分类暂存 TIFF，所在步骤超时 |

三份输入均为 UInt8、256 × 256 像元块、NoData 255。类别 7 对应闭区间 45°；遥感类别 1、2、3 对应减少、不变、增加。校验器复用 P13-04 独立参考，逐块精确比较类别与掩膜。面积参考通过合并后的 DEM 外环及 NoData 空洞，或遥感列条带，在 EPSG:6933 中独立积分。两次积分将最长分段从 50 m 加密到 25 m，短边至少分成 16 段。比较清单面积时使用相对容差 `2e-7`、绝对容差 `0.02 m²`。

在原型根目录使用已安装的项目 Python 环境运行。命令只打开样本并计算独立参考，可通过 `--directory` 指向复制后的包目录。

```bash
env PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 GDAL_NUM_THREADS=1 PROJ_NETWORK=OFF \
  .venv/bin/python scripts/bench/verify_area_fixtures.py
```

复验证据使用仅含清单、TIFF、校验器、`_raster_reference.py`、`fingerprint.py` 和专项测试的新目录。以下为在该目录内执行的命令，`repo` 为原型根目录绝对路径。

```bash
export PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1 GDAL_NUM_THREADS=1 PROJ_NETWORK=OFF
/usr/bin/time -p nice -n 19 taskset -c 31 "$repo/.venv/bin/python" scripts/bench/verify_area_fixtures.py
test_root="$(mktemp -d /dev/shm/geopilot-area-fixtures-pytest.XXXXXX)"
/usr/bin/time -p nice -n 19 taskset -c 31 "$repo/.venv/bin/python" -m pytest -q -p no:cacheprovider \
  "--basetemp=$test_root/pytest" tests/benchmarks/test_verify_area_fixtures.py
```

原始输出包含独立面积、逐类计数和最大像元误差。 [复验原始输出](verification.log)

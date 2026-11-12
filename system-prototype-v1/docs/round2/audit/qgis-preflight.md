# QGIS Processing 复跑环境探测

主控在审计提交 `c12898f` 上恢复 V1 使用的 QGIS 3.44.15 CLI。启动路径限于审计工作树的 `.environments/qgis-deb/`。包从 [QGIS 官方 LTR 仓库](https://qgis.org/ubuntu-ltr/) 获取，SHA-256 与 [V1 环境记录](../../baseline/ENVIRONMENT.md#p0-03-cli-恢复记录) 及官方包索引相同，为 `4f92abee00069919d56b525e8f2d8d10b1e725649978d0ba7e6f8990ec7d094f`。

将包内 `usr/bin/qgis_process` 和 `usr/bin/qgis_process.bin` 只读挂载到既有镜像 `sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87` 中的相同路径。包装器按调用用户 UID/GID 执行，挂载当前工作目录并沿用其绝对路径，容器断网。首次 `--json help native:buffer` 失败，镜像预设的 `/tmp/qgis-profile/profiles` 不允许调用 UID 写入。为每个临时容器设置 `QGIS_CUSTOM_CONFIG_PATH=/tmp/geopilot-qgis` 后，JSON 算法定义正常返回。

- `qgis_process --version` 退出 0，报告 QGIS 3.44.15（44b4ef05178）、Qt 5.15.13、Python 3.12.3、GDAL 3.8.4、PROJ 9.4.0、GEOS 3.12.1、SQLite 3.45.1、Ubuntu 24.04.5。
- `qgis_process --json help native:buffer` 退出 0，返回 `native:buffer` 和 9 项参数。
- Python 3.13.13 和 pytest 8.3.4。专项测试退出 0，3 通过、0 失败、0 跳过。实算 GeoPackage 缓冲并验证 CRS、边界和面积，另验证 CLI 缺失及保留已存在的输出。 [JUnit](qgis-preflight-junit.xml) · [原始输出](qgis-preflight.log)

```bash
GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process" \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest -q -p no:cacheprovider tests/smoke/test_qgis_smoke.py \
  --junitxml=docs/round2/audit/qgis-preflight-junit.xml
```

本次仅核查环境与真实 QGIS 调用。P12-05 全量回归和 P12-07 固定用例单独记录。

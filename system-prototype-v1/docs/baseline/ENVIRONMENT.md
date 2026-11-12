# P0-02 开发环境盘点

2026-11-11（Asia/Shanghai），检查在 `/home/mtd/liweigis/GeoPilot/system-prototype-v1` 执行。主机为 Ubuntu 20.04.6 LTS、x86_64、glibc 2.31。`git status --short --branch` 报当前目录及其父目录没有 Git 仓库。下表版本均为该次执行的实测值。

| 组件 | 启动入口、环境 | 复查命令和结果 |
|---|---|---|
| 开发 Python | `/home/mtd/miniconda3/bin/python` 和 `python3` 均解析到 `/home/mtd/miniconda3/bin/python3.13`（Miniconda base） | `python --version`、`python3 --version` 均为 `Python 3.13.13`。`python -m pip --version` 返回 pip `26.2.1`。 |
| 系统 Python | `/usr/bin/python3` | `/usr/bin/python3 --version` 为 `Python 3.8.10`。 |
| GDAL CLI | 默认 `PATH` 缺失。既有 `liweigis` 环境入口为 `/home/mtd/miniconda3/envs/liweigis/bin/gdalinfo` | `gdalinfo --version` 退出 `127`。用绝对路径重查返回 `GDAL 3.10.2, released 2025/02/11`，退出 `0`。 |
| GDAL Python 绑定 | `/home/mtd/miniconda3/envs/liweigis/bin/python`（Python 3.9.23） | `from osgeo import gdal` 可导入，`gdal.VersionInfo("--version")` 为 `GDAL 3.10.2, released 2025/02/11`。Miniconda base 的 `osgeo` 不可导入。 |
| SQLite | `/home/mtd/miniconda3/bin/sqlite3`；开发 Python 的 `sqlite3` 标准库 | `sqlite3 --version` 为 `3.51.2`，开发 Python 执行 `SELECT sqlite_version()` 返回 `3.51.2`。`liweigis` 环境的 Python SQLite 为 `3.53.1`。 |
| Node.js / npm | `/home/mtd/.local/bin/node`、`/home/mtd/.local/bin/npm`，链接至 NVM v22.23.1 安装 | `node --version` 为 `v22.23.1`，`npm --version` 为 `10.9.8`，`node -p 'process.platform + " " + process.arch + " " + process.version'` 返回 `linux x64 v22.23.1`。 |
| 环境与容器工具 | `/home/mtd/miniconda3/bin/{conda,mamba}`、`/home/mtd/.local/bin/uv`、`/usr/bin/docker` | `conda --version` 为 `26.3.2`、`mamba --version` 为 `2.6.0`、`uv --version` 为 `0.8.14`、`docker --version` 为 `28.1.1`。 |
| QGIS Processing CLI | 默认 `PATH` 无 `qgis` 或 `qgis_process`。开发 Python 无 `qgis` 绑定 | `command -v qgis`、`command -v qgis_process` 无路径。`qgis_process --version` 返回“未找到命令”，退出 `127`。两份缓存镜像均可导入 PyQGIS `3.44.15-Solothurn`，镜像入口探测仍返回 `127`，详见下文。 |

## Python 核心库

`python -m pip freeze` 返回 450 行、退出 `0`。保留与原型有关的条目。以下库也已在同一解释器实际导入。

| 依赖 | 版本 | 补充运行证据 |
|---|---|---|
| GeoPandas | `1.1.3` | 成功导入 |
| Pyogrio | `0.12.1` | GDAL `3.11.4`，`list_drivers()["GPKG"]` 为 `rw` |
| Rasterio | `1.4.3` | GDAL `3.9.3`，驱动表中 `GTiff` 为 `GeoTIFF` |
| Shapely | `2.1.2` | GEOS `3.13.1` |
| PyProj | `3.7.2` | PROJ `9.5.1`，`CRS.from_epsg(32650).name` 为 `WGS 84 / UTM zone 50N` |
| NumPy / pandas | `2.4.4` / `3.0.2` | 成功导入 |
| Pydantic / PyYAML | `2.13.5` / `6.0.3` | 成功导入 |
| FastAPI / Uvicorn | `0.141.1` / `0.32.1` | 成功导入 |
| pytest | `8.3.4` | 成功导入 |

```bash
set -o pipefail
python -m pip freeze | awk 'BEGIN{IGNORECASE=1} /^(geopandas|rasterio|shapely|pyproj|pyogrio|numpy|pandas|pydantic|fastapi|uvicorn|pytest|pyyaml|gdal)==/ {print} END {print "pip freeze rows:", NR}'
python -c 'import geopandas, numpy, pandas, pydantic, fastapi, pyogrio, pyproj, pytest, rasterio, shapely, sqlite3, uvicorn, yaml; print("SQLite", sqlite3.sqlite_version, "Pyogrio GDAL", pyogrio.__gdal_version__, "Rasterio GDAL", rasterio.__gdal_version__, "GEOS", shapely.geos_version_string, "PROJ", pyproj.proj_version_str)'
/home/mtd/miniconda3/envs/liweigis/bin/python -c 'import sys, sqlite3; from osgeo import gdal; print(sys.version.split()[0], sqlite3.sqlite_version, gdal.VersionInfo("--version"))'
```

## QGIS 镜像检查

使用本机镜像运行无网络、无目录挂载的临时容器，只执行入口探测。

```bash
for ref in groundwater-v7-spatial-qgis:0.15.5 groundwater-v11-deepseek-20261015-executor-qgis-worker:latest; do
  docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$ref"
  docker run --pull=never --rm --network none --entrypoint /bin/sh -e QT_QPA_PLATFORM=offscreen "$ref" -c '
    if command -v qgis_process; then exec qgis_process --version; fi
    /usr/bin/python3 -c "from qgis.core import Qgis; print(Qgis.QGIS_VERSION)"
    printf "%s\n" "qgis_process missing from image PATH"
    exit 127'
  printf '%s exit: %s\n' "$ref" "$?"
done
```

两份镜像架构均为 `linux/amd64`，ID 分别为 `sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87`、`sha256:3b773f27a5bf1ecc2794a92e1d5b96af248917eb6ae77b94a8418b8d9ad79a54`。两者实际输出 `3.44.15-Solothurn` 和 `qgis_process missing from image PATH`，均退出 `127`。

## 缺项与验收建议

- `qgis_process` 缺失阻断 P0-03 的算法列表、参数帮助和 `native:buffer` 实跑。建议使用项目隔离的 QGIS 安装或具备 CLI 的本机镜像。`qgis_process list`、`qgis_process help native:buffer` 及实际算法为 `NOT_RUN`，等待 P0-03。
- 默认 `gdalinfo` 入口不可用，核查可用既有 Conda 环境的绝对路径。记录各进程实际使用的 GDAL 版本。GPKG 与 GeoTIFF 的读写和重开验证为 `NOT_RUN`，等待 P0-04。
- 建议 P0-02 进入 `REVIEW`。新增 `docs/baseline/ENVIRONMENT.md`，提交环境可用性和缺项证据，API、数据库、配置及主机环境无变更。

## P0-03 CLI 恢复记录

主控从 [QGIS 官方 Ubuntu LTR 源](https://qgis.org/ubuntu-ltr/dists/noble/main/binary-amd64/Packages.gz) 获取与本机缓存 QGIS 库相同版本的 `qgis_3.44.15+40noble_amd64.deb`。`sha256sum -c` 验证 `4f92abee00069919d56b525e8f2d8d10b1e725649978d0ba7e6f8990ec7d094f`，`dpkg-deb -f` 返回 `qgis`、`1:3.44.15+40noble`。从包中提取 `usr/bin/qgis_process` 和 `usr/bin/qgis_process.bin` 到 `.environments/qgis-deb/`，绑定到已有镜像 `sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87`。容器断网运行，以本机用户身份写入单次测试目录。

本机入口为 `.environments/qgis-deb/qgis_process`。调用者用绝对路径配置 `GEOPILOT_QGIS_PROCESS`，把本次读写文件放在调用目录并使用相对文件参数。另一环境可以配置其自身的原生 `qgis_process`，用相同 [Processing CLI 验证](https://docs.qgis.org/3.44/en/docs/user_manual/processing/standalone.html)确认版本、算法和输出。

实测 `--version` 返回 QGIS `3.44.15-Solothurn`（`44b4ef05178`）、Python `3.12.3`、GDAL `3.8.4`、PROJ `9.4.0`、GEOS `3.12.1`、SQLite `3.45.1`。`list` 含 `native:buffer`；`help native:buffer` 显示 `DISTANCE`、`SEGMENTS`、`OUTPUT`，探测均退出 `0`。实际计算证据由 P0-03 验收记录收录。主机默认 `PATH` 仍无 QGIS；该备用入口依赖本机缓存镜像。

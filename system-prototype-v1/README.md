# GeoPilot 系统原型 v1

GeoPilot 是本机 Web GIS 工作台。登记本机数据后，选择分析方法、填写参数、确认方案并运行。后端调用真实 QGIS Processing 或 Python GIS，保存运行、检查和成果版本。浏览器使用 MapLibre 查看结果和原像元。

## 安装依赖

验证环境为 Linux x86_64、Python 3.13.13、Node.js 22.23.1、npm 10.9.8。Python 运行和测试依赖已固定版本。`requirements-test.txt` 包含运行依赖与复验所需的 pytest、HTTP 测试客户端。前端依赖由 `frontend/package-lock.json` 固定。[运行依赖](requirements.txt) · [测试依赖](requirements-test.txt) · [净目录验证](docs/acceptance/P11-07.md)

在原型根目录执行。

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements-test.txt
.venv/bin/python -m pip check
cd frontend
npm ci --include=dev --no-audit --no-fund
npm run build
cd ..
```

### 配置 QGIS Processing

安装适合本机的 QGIS，定位实际 `qgis_process`。将下面的示例路径替换为其绝对路径。在无窗口环境使用 `QT_QPA_PLATFORM=offscreen`。验证使用 QGIS 3.44.15，并实际调用了 `native:buffer`、`native:extractbylocation` 和 `native:extractbyattribute`。[QGIS 安装指南](https://qgis.org/resources/installation-guide/) · [Processing 命令行](https://docs.qgis.org/3.44/en/docs/user_manual/processing/standalone.html)

```bash
export GEOPILOT_QGIS_PROCESS=/absolute/path/to/qgis_process
export QT_QPA_PLATFORM=offscreen
test -x "$GEOPILOT_QGIS_PROCESS"
"$GEOPILOT_QGIS_PROCESS" --version
"$GEOPILOT_QGIS_PROCESS" --json help native:buffer
```

开发机可使用 `.environments/qgis-deb/qgis_process`。该入口需要可用的 Docker、同目录 `usr/bin` 下两份提取的 QGIS 可执行文件，以及本机已缓存的匹配镜像。包装器用当前运行目录作为读写挂载，以无网络临时容器执行。移到另一台机器时可配置该机的原生入口，或显式提供上述本机组件。[开发机包装器与镜像记录](docs/baseline/ENVIRONMENT.md#p0-03-cli-恢复记录)

```bash
docker image inspect --format '{{.Id}}' sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87
export GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process"
"$GEOPILOT_QGIS_PROCESS" --version
```

## 启动工作台

终端一在原型根目录启动后端。`GEOPILOT_DATA_DIR` 可指向任何本机可写目录，首次启动时创建 SQLite。

```bash
export GEOPILOT_DATA_DIR="$PWD/local-data"
mkdir -p "$GEOPILOT_DATA_DIR"
PYTHONPATH="$PWD/src" .venv/bin/python -m geopilot.api.server \
  --database "$GEOPILOT_DATA_DIR/project.sqlite" \
  --workspace "$GEOPILOT_DATA_DIR/workspace" \
  --config "$PWD/config" --qgis-process "$GEOPILOT_QGIS_PROCESS" --port 8000
```

终端二在原型根目录启动前端。`GEOPILOT_API_URL` 指向后端的回环地址，Vite 将 `/api` 请求代理过去。

```bash
cd frontend
GEOPILOT_API_URL=http://127.0.0.1:8000 npm run dev -- --port 5173 --strictPort
```

打开工作台。若调整后端端口，请同步调整前端的代理目标。服务面向同一台机器的单用户工作流。[打开工作台](http://127.0.0.1:5173/)

```bash
curl -fsS http://127.0.0.1:8000/openapi.json > /dev/null
curl -fsS http://127.0.0.1:5173/api/methods
```

SQLite 保存资产、方案、运行和检查记录。正式运行的输入副本、步骤请求、日志和成果位于 `workspace/tasks/<task_id>/runs/<run_id>/`。登记的原数据继续放在其本机路径，执行时按确认的版本暂存。

## 跑一个样例

使用内置 `tests/fixtures/vector/road-buffer.gpkg`，按指南登记四个图层并运行「道路缓冲统计 v3」的局部投影模式。v3 也提供 WGS84 椭球测地线模式。500 米局部投影示例的参考结果是 6 个建筑点，西区 3、东区 2、未归属 1。[工作台操作、原生遥感打包与可选功能](docs/user-guide/workbench.md) · [样例数据与参考](tests/fixtures/vector/README.md)

## 复验

从原型根目录运行后端测试。从 `frontend` 目录运行前端测试和构建。QGIS 测试使用已配置的实际 CLI。PostGIS 可选用例的环境在操作指南中单独说明。[净目录验证记录](docs/acceptance/P11-07.md)

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
GEOPILOT_PYTHON="$PWD/.venv/bin/python" bash scripts/test.sh tests/smoke tests/golden -q
cd frontend
npm test -- --run
npm run build
```

# GeoPilot 安装与启动验证矩阵

QGIS Processing、Python GIS 及项目数据库由 GeoPilot 后端运行。浏览器打开工作台，前端把 API 请求代理给后端。当前本机原型的服务监听回环地址，浏览器在同一台机器上打开页面。验证时为后端安装和浏览器交互分别留存证据。

## 后端安装与启动

在原型目录运行，使用测试环境已有的 Git、Python、Node.js、npm 和可执行的 QGIS Processing 入口。输出目录的父目录应已存在，输出目录由验证命令新建。

    python scripts/install-check/run_matrix.py \
      --label ubuntu-native \
      --ref HEAD \
      --qgis-process /usr/bin/qgis_process \
      --output /tmp/geopilot-matrix-ubuntu-native

命令从指定提交解出生产源码、配置、前端和锁定依赖清单。Python venv、npm 缓存、项目数据库和工作区均在专用临时目录中。运行依次检查 Python 安装、pip 依赖一致性、前端安装和构建、环境诊断、后端 OpenAPI、前端 HTML 及方法目录代理。诊断实查 QGIS Processing 的缓冲工具帮助、GDAL/PROJ 及驱动。退出后清理临时工作区，并在指定输出目录保留报告、逐步日志和服务返回的方法目录。

| 后端行 | Python | Node.js / npm | QGIS / GDAL / PROJ | 结果 |
|---|---|---|---|---|
| Ubuntu 20.04.6 x86_64 宿主，QGIS 通过隔离包装器 | 3.13.13 | 22.23.1 / 10.9.8 | 3.44.15 / 3.8.4 / 9.4.0 | PASS。 [原始报告](../round2/deployment/p16-02-ubuntu20/report.json) |
| Ubuntu 24.04.5 x86_64 容器，QGIS 原生 CLI | 3.12.3 | 22.23.1 / 10.9.8 | 3.44.15 / 3.8.4 / 9.4.0 | PASS，诊断提示 Python 版本与基线不同。 [原始报告](../round2/deployment/p16-02-ubuntu24/report.json) |

两行均使用 Pyogrio/GDAL 3.11.4、Rasterio/GDAL 3.9.3、PyProj/PROJ 9.5.1 及 Rasterio/PROJ 9.4.1。其他系统、体系结构和依赖组合为 NOT_TESTED。

## 浏览器交互

按原型 [启动说明](../../README.md#启动工作台)启动服务后，用带 Chromium 的 Playwright 测试环境运行。

    GEOPILOT_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs \
      node scripts/install-check/check_browser.mjs http://127.0.0.1:5173/

浏览器检查使用全新上下文。检查工作台渲染、浏览器对前端代理的实际请求和真实方法目录中的版本选择。测试进程的浏览器 PATH 中无 QGIS 入口，浏览器环境不继承后端 QGIS 或 Python 配置。用户日常打开工作台只需要浏览器和正在运行的服务，Playwright 用于自动复验。

Chromium 141.0.7390.37 实测通过。工作台和目录请求均返回 HTTP 200，成功选择道路缓冲统计 v3。其他浏览器和跨主机部署为 NOT_TESTED。 [原始结果](../round2/deployment/p16-02-browser/browser.log) · [验收及复现记录](../round2/deployment/p16-02.md)

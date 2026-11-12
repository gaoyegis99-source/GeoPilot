# P3 道路工具与运行输入

2026-11-11，主控核对 V2.0 §8.5 七步的实际工具和资产暂存。保留 v1 结构示例，新增绑定了七步的 `road-buffer-statistics` v2。v2 的首次样例限定单点对象计数、显式局部投影和并列未归属，面积与多种归属验收由 P6 扩展。[v2 方法配置](../../config/methods/road-buffer-statistics.v2.yaml)

- 真实 QGIS 3.44.15 外扩 EPSG:32650 分析区 500 m，范围为 `(499500, 4399500, 502500, 4402500)`。空间取数选择 `road-edge` 与 `road-main`。已测道路缓冲和点空间关系的实际进程，裁回前七个点 ID 与独立参考完全一致。运行 `GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/runners/test_road_selection.py tests/runners/test_qgis_runner.py -q --junitxml=artifacts/test-results/p3-qgis-tools-junit.xml`，18 项通过。[JUnit](../../artifacts/test-results/p3-qgis-tools-junit.xml)
- 真实 Python 子进程从 EPSG:4326 重开选中道路，使用确认的 EPSG:32650 目标复用 P2-07，重开输出的 ID、几何和 CRS 符合参考，源指纹不变。运行 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/runners/test_prepare_measurement_lines.py tests/runners/test_python_runner.py -q --junitxml=artifacts/test-results/p3-measurement-worker-junit.xml`，16 项通过。[JUnit](../../artifacts/test-results/p3-measurement-worker-junit.xml)
- 运行输入重新核对注册资产版本，将文件组复制到本次运行目录。同源多图层共享一份副本，图层选择各自保留。实测 Shapefile 旁车与 GeoPackage 指纹及重开元数据一致，源内容变化时输入目录保持为空。运行 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/application/test_run_inputs.py tests/assets/test_fingerprint.py -q --junitxml=artifacts/test-results/p3-run-inputs-junit.xml`，17 项通过。[JUnit](../../artifacts/test-results/p3-run-inputs-junit.xml)
- 真实输入用于复核所选 CRS 与字段，`tests/application/test_method_preconditions.py` 2 项通过。通过实际 QGIS 目录登记 v2 七步契约，四个角色均为 `READY`，草稿无问题。[JUnit](../../artifacts/test-results/p3-method-preconditions-junit.xml)

初次外扩与取数测试退出 1，缺少契约；测量输入准备先行测试同样因缺少契约失败。输入暂存与方法前提测试先行退出 2，缺少实现。主要新增工具 YAML、按确切版本的 QGIS 绑定、Python 入口与运行输入模块；Python 适配器将项目源码目录提供给已登记子进程。数据库 schema 与 HTTP API 无变动。

裁回和分区计数的实际证据单列。正式 T07/T08 由已确认方案驱动的整链运行验收。[裁回工具](P3-clip-tool.md) · [分区计数工具](P3-count-tool.md)

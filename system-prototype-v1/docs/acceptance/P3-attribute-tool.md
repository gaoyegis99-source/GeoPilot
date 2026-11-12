# P3 属性筛选工具实跑

作为 V2.0 §11.6 第二种方法的运行准备，主控新增 `config/tools/qgis-extract-by-attribute.yaml`、同名运行绑定和 `tests/runners/test_attribute_filter.py`。现有属性筛选方法引用确切 v1，映射到真实 QGIS `native:extractbyattribute`。

- 测试先行因契约 YAML 不存在失败，退出 1。
- 用真实 QGIS 入口运行 `GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/runners/test_attribute_filter.py -q --junitxml=artifacts/test-results/p3-attribute-tool-junit.xml`，退出 0，`1 passed in 10.03s`。[JUnit](../../artifacts/test-results/p3-attribute-tool-junit.xml)
- 实际步骤按 `source_feature_id = building-west` 从真实 P0 点图层筛出一个要素，退出 0，成果可重开。源与结果 CRS 相同，几何精确一致，原文件字节保持不变。日志记录实际等值索引 0 和筛选值。

已验证单步工具。完整方法的草稿、确认、编译及结果复用仍需 P3 阶段运行。

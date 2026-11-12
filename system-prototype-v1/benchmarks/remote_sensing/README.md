# 双日期遥感性能基准

使用 P13-01 的 EPSG:32632 双日期 GeoTIFF。Landsat 8 C2 02 首期网格为 30 m、四个 UInt16 波段，Sentinel-2 05.11 次期网格为 10 m、三个 UInt16 波段，左上向西和北偏移 7 m。20 m 原生 SCL 以最近邻映射到 10 m 波段。S/M/L 首期边长为 192、768、3,072 像元，次期边长为 578、2,306、9,218 像元。每档实际块、波段、数据字节、原生网格、QA 来源和输入首尾指纹保存在报告。 [输入配方](../datasets/remote-sensing.yaml) · [共同协议](../README.md)

```bash
python scripts/bench/run_raster.py --profile benchmarks/datasets/remote-sensing.yaml
```

执行与缓存选项同 DEM。每档每状态使用两轮预热及五轮正式测量。可以选择独立工具或完整方法范围，原始记录按层级、状态、样本和操作保存。 [运行选项与资源口径](../raster/README.md)

## 独立工具

通过 V1 产品缩放和 QA API 准备两个独立原生网格，再分别计时 `write_ndvi`、`align_ndvi`、`write_ndvi_change`。前置缩放和 QA 写入另记耗时。首期 NDVI 与次期 NDVI 分别以原生像元数计算吞吐。对齐和变化以首期网格像元数计算。

QA 验算使用 Landsat 云位、红光填充、Sentinel 原生 SCL 最近邻类别和次期覆盖范围。反射率和 NDVI 从已知 DN 与固定校准公式独立推导。最近邻对齐根据两个网格的像元中心和 7 m 偏移独立取索引。连续值允许 1e-6 绝对误差，位置、掩膜及分类精确匹配。六种相位像元数相同，只有增加、减少、不变三个相位为公共有效。变化方向为次期减首期，三种有效相位的差值分别为 0.5、约 -0.14705882 和 0。

## 完整方法

用正式 API 执行 `remote-sensing-index-change@1` 的八步计划，固定最近邻、阈值 0.1 和 EPSG:6933。核对正式检查规则、成果角色、输入来源、两个精确传感器配置和校准报告。重新打开质量掩膜、原生 NDVI、对齐栅格、变化量及变化类别，逐块逐像元比较。按首期列的相位把每种类别合并成纵向条带，在 EPSG:6933 中独立积分每条带边界。条带内部共享边抵消。加密策略和面积容差同 DEM。

零分母、窄 AOI 和双线性在既有 V1 微型测试覆盖。这里的固定性能配方使用最近邻和整矩形 AOI。 [V1 正确性样本](../../tests/methods/test_remote_sensing_change.py) · [性能证据](../../docs/round2/performance/p13-04.md)

# P8-10 原生遥感产品打包

2026-11-12 主控签收原生产品打包子项。`pack_native_product(metadata_xml_path, destination, *, profile, registry, granule_id=None)` 读取已解包产品内的 UTF‑8 XML。按 XML 精确选择本地原生文件，核对唯一配置、实际驱动、无符号 DN 类型、CRS、仿射和尺寸，再生成一个 P1 可登记的 UInt16 GeoTIFF。Landsat 的四个 30 米波段须同格。Sentinel‑2 的 B04/B08 须同一 10 米格，SCL 须同 CRS、同原点和覆盖范围，尺寸为光谱格的一半，再以最近邻逐块对齐。[实现](../../src/geopilot/remote_sensing/product_pack.py) · [Copernicus 产品规范](https://sentiwiki.copernicus.eu/web/s2-products)

结果保存原 DN 和原 XML 文本。`GEOPILOT_PRODUCT.NATIVE_SOURCES` 记录元数据、各原生波段及 GDAL 伴随文件的相对路径和独立内容指纹，并附原生格网、掩膜类型、实际重采样和配置引用。函数返回值持有本机源文件的绝对路径。原生光谱填充 DN 保留，已匹配配置的逐带填充掩膜由该波段单独解释。需要传递的源 GDAL 掩膜取交集写入内部共同掩膜，输出 NoData 为 `None`，Landsat 合法 QA `0` 保留。[GDAL GeoTIFF 内部掩膜](https://gdal.org/en/stable/drivers/raster/gtiff.html#internal-nodata-masks)

本机调用示例

```bash
PYTHONPATH=src python -m geopilot.remote_sensing.product_pack \
  /local/product.SAFE/MTD_MSIL2A.xml /local/packed-product.tif \
  --profile sentinel-2-level-2a --config-dir config/sensors
```

多 granule 产品用 `--granule-id` 精确选择。CLI 返回成品路径、产品 ID、granule ID 和配置引用，成品通过既有资产登记入口写入 P1。

## 实际验证

Python 3.13.13、NumPy 2.4.4、Rasterio 1.4.3、GDAL 3.9.3。先行测试因 `geopilot.remote_sensing.product_pack` 缺失退出 2。晚增 JP2 掩膜的真实回归先复现正式输出误发布，修正后在发布前按 `PRODUCT_SOURCE_CHANGED` 阻断。最终专项 `16 passed in 1.70s`，联合 `281 passed in 37.05s`。[专项测试](../../tests/remote_sensing/test_product_pack.py) · [专项 JUnit](../../artifacts/test-results/p8-10-product-pack-junit.xml) · [联合 JUnit](../../artifacts/test-results/p8-10-product-pack-joint-junit.xml)

```bash
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
python -m pytest tests/remote_sensing/test_product_pack.py -q -p no:cacheprovider \
  --junitxml=artifacts/test-results/p8-10-product-pack-junit.xml
python -m pytest tests/remote_sensing tests/data_adapters/test_raster_reader.py \
  tests/assets/test_asset_registration.py tests/assets/test_asset_resolver.py tests/gis/raster/test_window_io.py \
  -q -p no:cacheprovider --junitxml=artifacts/test-results/p8-10-product-pack-joint-junit.xml
```

- Landsat 真实单带 TIFF 重开核对四个原 DN 阵列，包括光谱 `65535` 和合法 QA `0`。内部与外部源掩膜均保留为结果内部掩膜。红光填充处的近红外仍独立定标为 `0.35`，质量掩膜为 `[[0, 1, 0], [0, 1, 0]]`。原 XML 的 CRLF 和全部源指纹一致。真实 SQLite 登记后移走原生目录，按资产版本仍可重开成品。
- Sentinel‑2 使用 JP2OpenJPEG 无损写成真实的 4 × 6 光谱 JP2 和 2 × 3 SCL JP2。SCL 原始类别与真实外置 GDAL 掩膜均按 20→10 米最近邻处理，重开后分别等于独立的 2×2 展开。标签保留原 `.jp2` 相对路径、伴随掩膜指纹、20 米源格和 `nearest`。[GDAL JP2OpenJPEG 无损选项](https://gdal.org/en/stable/drivers/raster/jp2openjpeg.html#lossless-compression)
- 真实 CLI 子进程退出 0，stdout 的配置 ID/版本与成品一致。错驱动、错格网、源文件缺失、产品目录外链接、同一物理文件被多带引用、会掩掉合法 QA `0` 的 NoData、配置未知及处理期间原文件变化均阻断发布。已有目标或伴随文件保持原内容。

## 原生场景对照与边界

只读检查了公开 Landsat 9 `LC09_L2SP_044034_20240727_02_T1` 的 SR_B4、QA_PIXEL、QA_RADSAT GeoTIFF 头，均为同一 30 米 EPSG:32610 网格。SR_B4 的原生 GDAL NoData 为 `0`，两个 QA 波段为 `None`。另检查了 Sentinel‑2A PB05.11 `T32UMA` 的原生 JP2 头，B04/B08 为 10980×10980、10 米、UInt16，SCL 为 5490×5490、20 米、UInt8，三者同 EPSG:32632 和 `(399960, 5600040)` 原点。公开场景对照读取文件头，专项打包使用合成 DN 的真实原生格式文件。[Landsat 场景](https://planetarycomputer.microsoft.com/api/stac/v1/collections/landsat-c2-l2/items/LC09_L2SP_044034_20240727_02_T1) · [Sentinel‑2 瓦片元数据](https://sentinel-s2-l2a.s3.amazonaws.com/tiles/32/U/MA/2024/8/31/0/metadata.xml)

处理使用 256 × 256 输出窗口，需为新 GeoTIFF 提供目标盘空间并读取原文件计算内容指纹。成品掩膜表示参与共同分析的源支持范围，原生文件各自的掩膜及其来源可按本机指纹回查。正式方法与固定用例证据由 P8-10 主报告汇总。

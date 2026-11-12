# 栅格黄金样本

两份小型 GeoTIFF 使用人工像元，坐标系是 WGS 84 / UTM zone 50N（EPSG:32650）。网格为北向上，左上角为 `(500000, 4400070)`，水平像元为 `10 m × 10 m`。`expected.json` 记录写入前锁定的完整值、单位和掩膜。存储值均可精确表示，网格及像元采用精确比较。

| 文件 | 大小（列 × 行） | 参考依据 |
|---|---|---|
| `dem-plane.tif` | 7 × 7 | 第 `c` 列为 `100 + 10c` 米（从 0 起算）。左上和右下角为 `-9999`，其余 47 个像元有效。 |
| `two-band.tif` | 4 × 3 | 红光原始 DN 为 `101 + 10r + c`，近红外为 `201 + 10r + c`。红光 `(r=1,c=2)`、近红外 `(r=2,c=0)` 为 `65535`，共同有效 10 个像元。 |

DEM 的分析窗口为行列索引 `2..4`，外扩 1 个像元后的行列索引为 `1..5`，四条目标边均保有完整邻域。每向东 10 米高程升 10 米，南北向坡度为零，因此解析坡度为 45 度（100%），下坡方向为 270 度。该值供 P7 的 T09 核对；P0 测试验证输入、掩膜与邻域。多波段样本记录合成 DN，传感器和产品的处理配置在 P8 验证。

重建到新目录并验证源文件和生成文件：

```bash
raster_output=$(mktemp -d /tmp/geopilot-raster-fixtures-XXXXXX)
python tests/fixtures/raster/generate.py "$raster_output"
python -m pytest tests/golden/test_raster_fixtures.py -q
```

初始样本使用 Python 3.13.13、Rasterio 1.4.3 / GDAL 3.9.3 生成。生成器拒绝覆盖已有同名文件。

# T13 空间关系边界黄金样本

`spatial-relations.gpkg` 使用 EPSG:32650，坐标单位为米。四个图层均使用稳定文本字段 `source_feature_id`。选择时将 `points`、`lines` 或 `polygons` 作为完整目标，将 `area` 的 `area-with-hole` 作为判断范围。[GeoPackage](spatial-relations.gpkg) · [人工坐标](source.json) · [精确预期 ID](expected.json)

| 图层 | 几何 | 要素数 | `intersects` | `within` | 仅接触 |
|---|---|---:|---:|---:|---:|
| `area` | Polygon | 1 | | | |
| `points` | Point | 6 | 4 | 1 | 3 |
| `lines` | LineString | 10 | 8 | 3 | 3 |
| `polygons` | Polygon | 12 | 10 | 4 | 3 |

## 独立参考依据

以下坐标均以 `(500000, 4400000)` 为原点。判断范围是闭方形 `[0,100] × [0,100]` 扣除开方孔 `(40,60) × (40,60)`。外框和孔壁均属于判断范围的边界。根据 V2.0 §5.4，有公共点即满足 `intersects`。满足 `within` 还要求目标没有部分位于范围外，且目标内部与范围内部有交集。期望 ID 直接由下列坐标和区间关系推导，构造器只序列化几何。

| 目标 | 坐标关系 | `intersects` | `within` |
|---|---|---|---|
| `point-inside` | `(20,20)` 位于内部 | 是 | 是 |
| `point-shell-edge`、`point-shell-corner`、`point-hole-edge` | 分别在外边 `(0,25)`、外角 `(100,100)` 和孔壁 `(40,50)` | 是 | 否 |
| `point-outside`、`point-in-hole` | `(101,20)` 越过外框，`(50,50)` 位于开孔 | 否 | 否 |
| `line-inside` | `y=20` 上的 `[10,30]` 全在内部 | 是 | 是 |
| `line-shell-endpoint-inside`、`line-hole-endpoint-inside` | 端点分别在外边和孔壁，开线段均在范围内部 | 是 | 是 |
| `line-shell-edge`、`line-hole-edge` | 线段整条沿 `x=0` 外边或 `x=40` 孔壁 | 是 | 否 |
| `line-touch-corner` | `y=0` 上的 `[-10,0]` 只接触 `(0,0)` | 是 | 否 |
| `line-crosses-shell` | `y=30` 上的 `[-10,110]` 穿过范围，两端均在外部 | 是 | 否 |
| `line-crosses-hole` | `y=50` 上的 `[30,70]` 两端在内部，但穿过 `40<x<60` 的开孔 | 是 | 否 |
| `line-disjoint`、`line-in-hole` | 线段分别完全在外框以西或在开孔中 | 否 | 否 |
| `polygon-inside` | 矩形 `[10,20] × [10,20]` 完全在内部 | 是 | 是 |
| `polygon-shell-edge-inside`、`polygon-hole-edge-inside` | 分别共享 `x=0` 外边或 `x=40` 孔壁，矩形内部都在范围内部 | 是 | 是 |
| `polygon-shell-edge-outside`、`polygon-shell-vertex-outside`、`polygon-hole-edge-outside` | 分别只共享外边、外角或孔壁，矩形内部均在范围外 | 是 | 否 |
| `polygon-crosses-shell`、`polygon-crosses-hole` | 矩形分别越过 `x=0` 外框或 `x=40` 孔壁，均有内部面积落在范围外 | 是 | 否 |
| `polygon-encloses-hole` | `[30,70] × [30,70]` 四角在范围内，矩形内部包含开孔 | 是 | 否 |
| `polygon-with-matching-hole` | 沿用 `[30,70] × [30,70]` 外框并去除该方孔，剩余内部与范围内部相交 | 是 | 是 |
| `polygon-disjoint`、`polygon-in-hole` | 矩形分别完全在外框以东或在开孔中 | 否 | 否 |

`expected.json` 将两组业务谓词的精确结果保存在 `relations.<predicate>.<layer>`。`boundary_only_ids` 保存只有边界公共点且内部不相交的 ID，供 `touches` 诊断；穿越外框或孔的目标不在这组 ID 内。

## 重建

构造器需要 GeoPandas、Pyogrio 和 Shapely，目标父目录须已存在。已有文件拒绝覆盖。重建后的图层、CRS、要素 ID 和坐标一致，GeoPackage 容器元数据可能随写入环境或时间变化。

```bash
t13_fixture_tmp=$(mktemp -d)
python tests/fixtures/vector/t13/build_fixture.py "$t13_fixture_tmp/spatial-relations.gpkg"
python -m pytest tests/golden/T13 -q
```

样本同时用于后续 P6-02、P6-03 的生产谓词实跑。[P6-01 验收证据](../../../../docs/acceptance/P6-01.md)

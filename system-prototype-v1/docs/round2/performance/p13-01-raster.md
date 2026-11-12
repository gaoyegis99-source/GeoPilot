# P13-01 DEM 与双日期遥感规模样本

- 执行 Agent A/B，状态 REVIEW。生成和专项复验以审计提交 9a862cd8d445827e3daf647134989a52f64ff1c1 的工作树为基础，含本次新增生成器。生产源码、配置和 P12 数据配方与已签收的 51a2c49 相同。
- Linux 5.15.0-139-generic x86_64、Python 3.13.13、pytest 8.3.4、NumPy 2.4.4、Rasterio 1.4.3 / GDAL 3.9.3、Pyogrio 0.12.1 / GDAL 3.11.4。
- [基准协议](../../../benchmarks/README.md) · [DEM 配方](../../../benchmarks/datasets/raster.yaml) · [遥感配方](../../../benchmarks/datasets/remote-sensing.yaml)

## 真实生成与校验

两个生成器均按 256 × 256 块写入和重开核验，并把 GDAL 缓存限制在 32 MiB。遥感对齐参考按首期块读取对应的有限次期窗口。文件使用未压缩 GeoTIFF，使规则合成数据的重复模式仍保留各档实际磁盘 I/O 规模。各档写到专用新目录，结果包含相对文件路径、角色、文件大小、实际 SHA-256、独立逻辑像元摘要、CRS 和 transform。AOI 使用 GPKG 和固定日期。正式产物最后发布清单，已有目录与符号链接均拒绝覆盖。

| 负载 | 档 | 首期或 DEM 单边 | 独立预期有效像元 | 入清单数据文件总字节 | 退出码 |
|---|---|---:|---:|---:|---:|
| DEM | S | 512 | 259,956 | 1,147,480 | 0 |
| DEM | M | 2,048 | 4,183,812 | 16,876,600 | 0 |
| DEM | L | 8,192 | 67,039,236 | 268,542,520 | 0 |
| 遥感 | S | 192 | 18,432 | 4,694,849 | 0 |
| 遥感 | M | 768 | 294,912 | 47,429,084 | 0 |
| 遥感 | L | 3,072 | 4,718,592 | 661,303,697 | 0 |

DEM 每档以独立平面和 NoData 周期核对所有块，AOI 内的理论坡度为 45°、100%，坡向为 270°。次期 Sentinel-2 保留真实 20 m SCL 参考文件和由其最近邻展开的 10 m SCL。20 m TIFF 在清单中标为 native_quality_reference，供核查，不作为方法输入。Landsat 原样保留 QA 的 0 值和红光 DN 填充值，Sentinel 以内部掩膜表示首期覆盖外的 10 m 边缘。两个配对产品通过 V1 XML、配置和波段解析。六种相位的 QA、公共掩膜、解析 NDVI 变化和值域按实际对齐像元逐块核对。

外层复验进程读取每个生成目录的清单，逐文件独立重算字节数和 SHA-256，核对生成器 stdout 与实际清单。证据保留六份清单，临时大文件自动清理。两项 L 档生成和复验进程的峰值 RSS 分别为 205,256 和 227,652 KiB。这是生成器自检观察，P13-04 的方法性能测量另行执行。 [六份原始清单](p13-01-raster-manifests.json) · [原始命令、耗时及退出码](p13-01-raster-generation.log)

## 专项回归

实际生产 Horn 坡度对生成的 S 档 DEM 得到 259,956 个有效 45° 像元，孤立 NoData 的 3 × 3 邻域均无效。S 档遥感经生产缩放、质量掩膜、NDVI、对齐、变化和等积分类后，3 个变化类别各有 6,144 像元，另外 18,432 像元为 NoData。测试还复核栅格网格、原文件指纹、跨目录重复生成字节相同、已有目录和符号链接拒绝覆盖，以及非法档位不创建输出。

专项 pytest 收集 9 项，9 通过、0 失败、0 错误、0 跳过，退出 0，用时 9.62 秒。 [JUnit](p13-01-raster-junit.xml) · [原始日志](p13-01-raster-pytest.log)

## 复跑与回滚

在原型目录运行。生成命令以独立临时父目录为目标；验证 harness 按两种 family 和三档 tier 循环，保留 stdout 清单和 GNU time 的 stderr。

    export PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
    scratch=$(mktemp -d /tmp/geopilot-p13-01-replay.XXXXXX)
    for family in raster remote_sensing; do
      for tier in S M L; do
        /usr/bin/time -f 'exit=%x max_rss_kib=%M elapsed_s=%e' .venv/bin/python "scripts/bench/generate_${family}.py" --tier "$tier" --output "$scratch/$family-$tier"
      done
    done
    .venv/bin/python -m pytest -q -p no:cacheprovider tests/benchmarks/test_generate_rasters.py --junitxml=docs/round2/performance/p13-01-raster-junit.xml
    git diff --exit-code 51a2c49 9a862cd8d445827e3daf647134989a52f64ff1c1 -- src config benchmarks/datasets

新增 [公共生成 I/O](../../../scripts/bench/_raster_fixtures.py)、[DEM CLI](../../../scripts/bench/generate_raster.py)、[遥感 CLI](../../../scripts/bench/generate_remote_sensing.py)、[专项测试](../../../tests/benchmarks/test_generate_rasters.py)、本文及上述原始证据。接口新增两个基准专用 CLI 与各档 manifest.json，不改生产 API、数据库、算法或调度。撤销本项新增文件即可回滚。实际存储和物理摘要绑定测量环境；不同 GDAL 构建间比较应核对逻辑摘要。未运行全档的完整方法性能与资源基线。

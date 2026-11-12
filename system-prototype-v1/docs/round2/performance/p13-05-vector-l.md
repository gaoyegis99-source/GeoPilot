# P13-05 修正版矢量 L 档复测

正式批次按点相交、线相交、按面积分配依次测量。原版为 `977ab412d9f6e7558d22a5c431fac0ada90b07be`，修正版为 `a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc`。原生环境使用 Python 3.12.3、QGIS 3.44.15 和 `/dev/sdb1` ext4。每侧冷、暖各 2 次预热和 5 次正式计量。执行命令为 `bash artifacts/test-results/p13-05-vector-l-plan.a2d2e68.C905zE/run-l.sh --run`。

## 点相交原版

原版在 12:25:46–12:51:08 UTC 完成 14 个运行，容器退出 1，日志写入退出 0。14 次均 `FAILED/NOT_CHECKED`。空间筛选成功，裁回步骤均在 60 秒达到 `PROCESS_TIMEOUT`，子进程退出 -15。主控和 B 独立核对 14 个运行 ID，均与 P13-03 的 140 个历史 L 样本不同。输入 manifest 的 SHA-256 `ada61ccadb58accc19039b9f30c24db3d25ac0dc11d09987148105e10dac9c24` 与 P13-03 原始 L 样本一致。 [独立复算数据](p13-05-vector-l-point-base.json)

| 正式计量组 | 失败数 | 端到端中位数（秒） | 端到端 P95（秒） | 裁回步骤中位数（秒） | RSS partial |
| --- | ---: | ---: | ---: | ---: | ---: |
| 冷态 | 5 | 96.612 | 97.374 | 60.667 | 3 / 5 |
| 暖态 | 5 | 96.772 | 97.139 | 60.689 | 3 / 5 |

墙钟分位按失败样本计算。原版严格自比较保留失败分布，成功分位留空。离线解包后复算结果与实时复算逐字节一致，冻结比较器回放也匹配原输出，均退出 0。原始分片位于 `/mnt/datab/geopilot-p13-05-vector-l.a2d2e68.C905zE/t13-points-intersects/base/attempt.zz4iJj/`。包含原始 JSONL、输入清单、回执、审计脚本与 SHA-256 清单的便携证据位于 `artifacts/test-results/p13-05-vector-l-portable-audit.jDmYpU/point-base-evidence.tar.gz`，摘要为 `234b5ecb2682e1af308110c84668d608406b2dead8d83d66b09c4280fcd5181d`。

## 点相交修正版

修正版于 12:51:10 UTC 启动。已核验的两次冷态预热和前两次正式计量均 `SUCCEEDED/PASSED`。每次筛选及裁回各保留 65,536 个点，两项成果和检查报告与独立参考一致。前两次正式计量分别为 38.090、38.552 秒，完整冷热分布仍在采集。B 的独立审计和已核对的原始候选样本保存在 `artifacts/test-results/p13-05-vector-l-portable-audit.jDmYpU/`。后续栅格四组继续等待 L 矢量释放测量锁。 [栅格队列](p13-05-raster-s.md)

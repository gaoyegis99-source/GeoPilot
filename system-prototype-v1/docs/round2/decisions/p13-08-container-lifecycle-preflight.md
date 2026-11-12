# P13-08 外部容器生命周期复验

F 使用 `6f90b6f8f6685b7babee18bad8da8e9dd7c7679d` 的 `capture_process` 和工作进程租约，执行一个本地进程与三个 Docker 客户端。Docker 客户端及服务端均为 28.1.1，Python 3.13.13，固定镜像 `sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87`。容器只运行 `sleep`，使用本机已存在镜像、禁用网络、单 CPU 配额、无项目挂载。探针退出 0。 [原始逐事件记录](../performance/p13-08-daemon-lifecycle.jsonl)

| 终止方式 | 本地进程退出码 | 本地租约释放后 |
|---|---:|---|
| 本地进程超时 | -15 | 进程已退出 |
| Docker 客户端超时 | -9 | 容器仍在运行 |
| 通过取消回调终止 Docker 客户端 | -9 | 容器仍在运行 |
| SIGKILL Docker 客户端 | -9 | 容器仍在运行 |

三例外部容器均核对完整容器 ID、唯一名称、所有权标签及镜像。按已核对的 ID 显式删除均退出 0。按 ID 和名称的六次后续查询均确认容器已移除。主控从原始记录核对退出状态、租约、容器状态及删除对象一致。

本地租约能证明本地工作进程退出。涉及外部 daemon 的执行还需验证容器已终止。此证据供 P13-08 的取消与隔离边界使用，并发与容量决策仍待前置任务。未改变调度或生产配置。

原型目录可运行以下命令。探针脚本及校验清单位于同一目录。

```bash
env PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 nice -n 19 taskset -c 31 \
  .venv/bin/python -B artifacts/test-results/p13-08-daemon-lifecycle.05XC8M/probe.py
```

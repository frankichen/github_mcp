# MyGithut12 在线 SQLite 备份与 CI I/O 隔离

## 2026-10-17 事故结论

直接原因已通过暂停/恢复对照确认：Controller 容器内一次性 `python -`
备份子进程（宿主机 PID 3185697）持续写盘，阻塞同一 NVMe 上的 Podman。
源 `/data/mygithub12.db` 只读；目标为
`/data/backups/pzpp316-workspace-session-repair-20261017T075717Z/mygithub12-pre-repair.sqlite (deleted)`。
运行约 47 分钟，物理写入 228,518,629,376 字节，逻辑写入约 795 GB，
目标仅约 3.6 GB。删除目录项没有终止持有文件描述符的备份。
当时磁盘利用率约 99%，写等待最高 430ms；停止该子进程后恢复。

生命周期缺陷可在本机 AgentDock 源码中核实：
`internal/tool/command/session/shell_unix.go` 使用 `exec.CommandContext`，
`runner.go` 的停止操作调用 `cmd.Process.Kill()`。
这不会把 Docker daemon 创建的容器 exec 进程一起取消。
执行日志可见同期调用超时；命令会话结束不代表容器内工作结束。
本次没有修改 AgentDock 或 Laya 文件。

原始 stdin Python 脚本和完整参数未被审计留存，因此不能把某个具体
`backup(pages=...)` 参数认定为原脚本事实。隔离测试已经确定一种与写入
放大一致的机制：没有固定读事务时，分批 online backup 遇到源 WAL 提交
会重新复制；每步都提交写入，可令剩余页数长期不减少。
相同写入条件下，固定读快照可以正常完成。此证据区分了已观测的故障、
可复现的 SQLite 机制和无法回溯的原脚本细节。

## 支持的备份入口

部署文件：

- `services/github-action-service/scripts/sqlite_backup.py`
- `services/github-action-service/deploy/mygithub-sqlite-backup@.service`

在 Controller 宿主机安装，先确认 `/data` 对应的物理设备确实是
`/dev/nvme0n1`（否则修改 unit 中两个带宽设备路径），再执行：

```sh
sudo install -d -m 700 /opt/mygithub-maintenance /var/backups/mygithub12
sudo install -m 755 services/github-action-service/scripts/sqlite_backup.py /opt/mygithub-maintenance/sqlite_backup.py
sudo install -m 644 services/github-action-service/deploy/mygithub-sqlite-backup@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start --no-block mygithub-sqlite-backup@repair-20261017T090000Z.service
sudo systemctl status mygithub-sqlite-backup@repair-20261017T090000Z.service
sudo journalctl -u mygithub-sqlite-backup@repair-20261017T090000Z.service
```

每次使用唯一实例 ID。没有 timer，不会自动启动或自动重试生产全库备份。
输出为 `/var/backups/mygithub12/mygithub12-<ID>.sqlite`，权限 0600。
只有日志 JSON `status=passed` 且 unit 成功退出才能用于后续修复。
文件存在时拒绝覆盖；同一源库在共享锁目录中只允许一个备份。

## 保护机制

1. 源库以 `mode=ro` 打开，`BEGIN` 后真实读取固定 WAL 快照。
   不使用 `immutable=1`，不跳过 WAL，不锁住业务 Writer。
   unit 对主数据库文件强制只读，源目录允许 SQLite 管理 WAL/SHM 侧文件；
   WAL 模式在侧文件暂时不存在时也需要此目录权限。不会修改源库逻辑数据。
2. 剩余页数回退、快照页数改变、临时目标删除/替换立即失败。
3. 默认逻辑复制上限 16 MiB/s，CLI 最大 32 MiB/s；默认/最大总时限 300 秒。
   独立 supervisor 硬超时，worker 自己有 alarm，父进程被杀后由 Linux
   parent-death signal 结束 worker；校验也在总时限内。
4. systemd cgroup 额外限制读 32 MB/s、写 16 MB/s、内存 256 MB、
   CPU 50%，330 秒停止整个进程组，5 秒后强制清理。调用终端断开不会取消这些限制。
5. 私有 `.partial` 输出完成 `quick_check`、fsync 后原子发布，禁止覆盖。
   失败不发布文件；正常失败会清理自己的 partial。SIGKILL/断电可能留下
   partial，绝不可把它当作备份。固定快照会延后 WAL 回收，但最多持有 300 秒。

如果 300 秒内无法完成，应安排受控维护窗口或更快的隔离备份介质，
不能把时限改为无限或反复自动重试。控制面没有新增自动修复、业务状态写入
或 CI 结果改写。

## 监控、异常处理及边界

检查 `journalctl -u 'mygithub-sqlite-backup@*'` 的失败状态；备份失败必须
阻止依赖该备份的运维修复继续。`systemctl show <unit> -p Result -p ControlGroup`
和 cgroup 的 `cgroup.procs` 可核对是否已退出。
如需取消，使用 `systemctl stop <unit>`，不要只杀外层 `docker exec`，
不要先删输出。进程退出后才能处理遗留 partial。

这是宿主机维护组件，不需要重启 Controller/Worker，也不改变公网能力版本。
任意持有 root/Docker 权限的脚本仍可绕过该入口；本修复没有宣称能阻止
所有特权命令。仓库 `AGENTS.md` 强制后续本仓库运维使用这个受控入口。

回滚：确认没有活动备份后，移除模板及 `/opt/mygithub-maintenance/sqlite_backup.py`
并 `systemctl daemon-reload`。保留已验证备份。回滚不应恢复无时限的 ad hoc
备份方式；Controller 容器、数据库和 de Nginx 都无需回滚。

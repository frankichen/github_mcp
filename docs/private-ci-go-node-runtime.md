# Private CI：Go + Node 受控运行时

## 原因与约束

`frankichen/sxt` 的通知模块真实模拟器测试需在 `go test ./...` 过程中执行 `node`，但原 Go CI 容器只包含 Go，造成失败。不得删掉或跳过该测试，也不得缩小 `repo-auto-check` 范围。

此变更通过 Controller/Worker 两端固定配置的 `go-node-runtime` hook，选择 Worker 自行预热的 `localhost/private-ci-go-node:1.26.4-node22`，保留原 Go 全部检查和独立 Node 工作区检查。镜像名、执行命令与支持的 Go 版本均由 CI 代码固定；缺失或版本变化时 fail closed，不回退到不含 Node 的旧镜像。

## 运维预热与启用（必须另行授权）

该 PR 仅变更基础设施源码，不安装镜像、不重启 Worker、不发布生产控制面。运维在核验准确代码 SHA、基镜像 Digest 且取得授权后，在每台 Worker 的 rootless Podman 存储里预加载 `node:22` 和 `golang:1.26.4` 并执行：

```sh
podman build --pull=never -t localhost/private-ci-go-node:1.26.4-node22 -f services/private-ci-agent/deploy/Dockerfile.go-node services/private-ci-agent/deploy
podman run --rm --pull=never --network=none localhost/private-ci-go-node:1.26.4-node22 sh -ec 'node --version && go version'
```

双 Worker 镜像与版本核验通过后，使用固定运维发布入口分发 Controller/Worker 配置，检查心跳和隔离边界；在 `frankichen/sxt` 最终准确 PR HEAD 上执行全量 `repo-auto-check`。若任一步失败，保持原服务与合并门禁不变，禁止伪造或跳过测试。回滚需单独授权，不能删除业务数据、凭据或 CI 记录。

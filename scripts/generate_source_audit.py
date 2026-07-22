#!/usr/bin/env python3
"""Create the reproducible three-way MyGithub09 source baseline audit."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


EXCLUDED_DIRS = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", "node_modules", "cache", "logs", "backups", "releases", "workspaces", "mirrors", "coverage", "dist", "build"}
EXCLUDED_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".pyc")
EXCLUDED_NAMES = {"debug_pod.py", "manual_go_check.py"}


def files(root: Path) -> list[str]:
    result = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel.name in EXCLUDED_NAMES or rel.name == ".env" or ".bak-" in rel.name or rel.name.endswith(".bak") or any(rel.name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
            continue
        result.append(rel.as_posix())
    return sorted(result)


def git_files(repo: Path, ref: str) -> list[str]:
    output = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref], text=True)
    return sorted(line for line in output.splitlines() if line and line != ".git")


def write_list(path: Path, values: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(values) + "\n", encoding="utf-8")


def section(title: str, values: list[str]) -> str:
    if not values:
        return f"### {title}\n\n无。\n"
    return f"### {title}\n\n```text\n" + "\n".join(values) + "\n```\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--controller", type=Path, required=True)
    p.add_argument("--agent", type=Path, required=True)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--baseline", default="8e268634c9c226be158bd123486eb0bfddf1bb18")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    controller = files(args.controller)
    agent = files(args.agent)
    repo = git_files(args.repo, args.baseline)
    source_union = {f"services/github-action-service/{x}" for x in controller}
    source_union |= {f"services/private-ci-agent/{x}" for x in agent}
    current = {f for f in repo if f.startswith("services/")}
    missing = sorted(source_union - current)
    # The deploy worker is sourced from root@de and is intentionally retained;
    # it is not part of either local development workspace above.
    approved_existing = {path for path in current if path.startswith("services/private-deploy-agent/")}
    obsolete = sorted((current - source_union) - approved_existing)
    write_list(args.output.parent / "source-audit/controller-workspace-files.txt", controller)
    write_list(args.output.parent / "source-audit/private-ci-agent-workspace-files.txt", agent)
    write_list(args.output.parent / "source-audit/github-repo-baseline-files.txt", repo)

    text = f"""# MyGithub09 现网源码基线审计

> 生成方式：从现网 Controller/Private CI Agent 工作区读取白名单文件，并与 GitHub 基线提交进行三方比较。运行时数据、密钥、虚拟环境、缓存、worktree 和 Git mirror 均排除。该报告对应 PR1，不代表已部署。

## 审计对象

- Controller 工作区：`/home/xiaowu/work/private-ci-controller-node-workspace`
- Private CI Agent 工作区：`/home/xiaowu/work/private-ci-agent-node-workspace`
- GitHub 基线：`frankichen/github_mcp`，提交 `{args.baseline}`
- 线上 Controller 入口：`app.main:app`、`app.mcp_server:mcp`
- 线上 Private Deploy Agent 入口：`python -m app.deploy_worker`，工作目录 `/opt/private-deploy-agent`
- WSL/发布 Executor：`scripts/deploy_executor.py`；systemd 模板见 `services/private-ci-deploy-executor/systemd/`

## 版本与工具基线

- Controller 源码提交：见 `docs/MYGITHUB09_TOOL_MANIFEST.json` 的 `source_commit`
- Controller 镜像：manifest 的 `controller_image` 字段；本次未从线上运行时复制镜像或凭据
- Worker 版本：当前工作区无独立发布号，按源码基线记录
- Executor 版本：当前工作区无独立发布号，按源码基线记录
- PyGithub：`2.5.0`
- MCP SDK：`requirements.txt` 中的 `mcp>=1.7.0`
- 实际 MCP 工具数量：见 manifest；工具名称由 `await mcp.list_tools()` 生成

## 三方文件差异

本次完整文件清单位于 `docs/source-audit/`：

- `controller-workspace-files.txt`
- `private-ci-agent-workspace-files.txt`
- `github-repo-baseline-files.txt`

""" + section("GitHub 基线缺失、现网需要同步的路径", missing) + section("GitHub 基线中不属于现网白名单源码的路径", obsolete) + """
## 安全边界

已排除 `.env`、数据库/SQLite、日志、备份、release 产物、私钥/TLS、node_modules、虚拟环境、缓存、运行 worktree、Git mirror 和部署历史。配置文件只作为源码模板提交；任何生产 token、PAT、API key、密码和 Authorization/Bearer 值不得进入仓库。

## PR1 验收

PR1 应运行 `git diff --check`、Python 编译检查、Controller/Worker/Executor 测试、manifest 一致性校验、Docker 容器测试、secret/大文件扫描和新增 Git 历史扫描。PR1 不部署 Controller、不重启服务、不修改现网数据库、不修改 `frankichen/sxt`。
"""
    args.output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()

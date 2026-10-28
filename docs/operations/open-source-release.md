# 开源发布准备

代码主干：`integrate/netpulse`，继承自 `0810` 分支。`main`、`chore/open-source-prep` 与 `0810` 的原始提交全部保留在私有仓库；公开发布使用重新导出的文件快照，不推送原仓库的分支、标签或 Git 历史。

`main/chore` 与 `0810` 没有共同祖先（`0810` 的根提交 `aabe47c` 是独立历史），无法直接 `git merge`。当前方案：以 `0810` 为功能主干，把 `chore` 的开源化工程资产移植过来；两者的历史在 `integrate/netpulse` 的合并提交中建立血缘，供后续逐文件比对。

## 已完成的整理

- 引入 Apache-2.0 `LICENSE`、`CONTRIBUTING.md`、`SECURITY.md`、`THIRD_PARTY_NOTICES.md`。
- 引入 `scripts/audit_release.py` 与 `scripts/export_release.py`：从当前提交导出不含 Git 历史的源码快照，并生成 SHA-256 清单。
- 配置模板统一使用通用 `LLM_API_KEY`；`WANGTIAO2026_API_KEY`、`DASHSCOPE_API_KEY`、`OPENAI_API_KEY` 保留为回退，`infra/config.py` 同步支持。
- 导出白名单排除 `docs/delivery/`（竞赛交付材料）、`artifacts/`（含真实公网 IP 的验收结果）和 `tools/`（竞赛专用 QoS 验收脚本）。
- 清理工作树中扫描到的本机路径与高熵占位值；`.release-audit/` 加入忽略规则。
- 新增离线 CI（`Source checks`）与手动网络集成工作流。

## 审计与验证记录（2026-10-28）

| 检查 | 结果 |
|---|---|
| 发布源扫描 | 439 个受控文件；0 项密钥候选，0 项待复核 |
| 后端离线测试 | CI（Ubuntu，先执行 `p4/compile.sh`）：903 passed、1 skipped；本机 Windows 离线：776 passed、12 skipped，5 项 P4Info 依赖用例需先编译 |
| 前端 | `node --test` 56 项通过；`vite build` 成功 |
| P4 / Java / Pipeconf | CI 中 P4 源码编译成功；Java/Pipeconf 构建未在 CI 中执行 |
| 真实网络 / 真实 LLM | 未验证，需目标实验环境；见 `.github/workflows/network-integration.yml` |
| 已知平台限制 | Windows 上符号链接、可执行位与 POSIX 进程组用例无法运行；CI 在 Ubuntu 执行 |

发布前必须在 CI 或 Linux 环境重跑上述检查，并以该次结果为准。本表只记录当前工作树的离线结果，不代表完整功能验收通过。

## 生成可审查快照

确认工作区干净并提交主干后，在仓库根目录执行：

```bash
python3 scripts/audit_release.py --output .release-audit/current.json
python3 scripts/export_release.py ../idn-System-public-candidate
```

导出器只读取当前提交的受控源文件，排除私人资料、环境文件、缓存、数据和二进制产物；不复制 `.git`，不发布到任何远端。目标必须是仓库外的新目录；发现候选密钥、待复核路径、符号链接或未提交修改时拒绝导出。`RELEASE_MANIFEST.json` 记录每个导出文件的 SHA-256，可独立核对内容。

在导出目录按 README 的“快速开始”重新生成 P4/Pipeconf 产物，并在本地安装、构建和验证。复制本机缓存或旧二进制不能代替源码构建。Linux Docker 构建若出现 DNS 解析失败，应检查守护进程的 DNS/网络配置；可用 `docker build --network=host` 区分构建网络问题。

## 正式公开前的条件

1. 仓库已有 Apache-2.0 `LICENSE`；仍需确认所有贡献者、自有资源及[第三方材料](../../THIRD_PARTY_NOTICES.md)的发布授权。
2. 在目标实验环境通过未跳过的真实网络下发、遥测、带宽和默认 Watcher 自愈检查，确认部署说明可复现。旧镜像中的公开演示凭据不能作为生产认证。
3. 对最终文件集重新审计、核对 manifest，并在新目录初始化全新 Git 历史。此时再创建公开仓库并上传。

```bash
# 完成授权与验收后，在导出目录执行；确认清单包含现有 LICENSE。
git init -b main
git add .
git commit -m "Initial public source release"
```

发布授权和完整功能验收尚未确认，当前交付物是开源候选源码，不应直接将原仓库改为公开。

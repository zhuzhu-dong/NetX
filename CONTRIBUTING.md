# 参与开发

本项目使用 Apache-2.0 许可证。贡献代码前请确认你有权提交该代码，并保留第三方版权与许可声明。

从目标分支创建短期工作分支，每次提交只处理一个问题。涉及重构时保留现有 REST/SSE 契约、前端路由和 ONOS REST 下发路径，并说明行为变化及验证结果。

## 本地验证

使用 Python 3.12、Node.js 20 和独立虚拟环境。完整步骤见 [README](README.md) 的“快速开始”。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r idn-system/requirements.txt
# 首次运行测试前生成 P4Info（需要 Docker 或 p4c，不需要启动 ONOS/Mininet）
bash p4/compile.sh
python -m unittest discover -s tests/release -v
(cd idn-system && python -m pytest tests -m 'not integration' -q)
(cd frontend && npm ci && npm test && npm run build)
python scripts/audit_release.py
```

网络集成测试会修改仿真流表和流量，只在独立 ONOS/Mininet 环境运行。`IDN_MOCK_MODE=true` 只替换 LLM，不能替代真实基础设施。

不要提交真实凭据、用户输入记录、数据库、日志、私人附件或构建产物。新增配置项时同步 `.env.example`；不要把服务端凭据放入 `VITE_*` 环境变量。安全问题请遵循 [SECURITY.md](SECURITY.md)。

# NetPulse Backend

此目录包含 NetPulse 的 Python/FastAPI 后端、意图智能体、控制编排、数据模型、网络基础设施适配器、持久化层和后端测试。

## 目录职责

- `agents/`：自然语言理解、语义解析、网络感知、规划、冲突/安全检查、策略编译、执行验证与 Assurance；
- `api/`：FastAPI 路由、对话、执行跟踪、系统状态、恢复与运维 API；
- `orchestrator/`：Intent 控制平面、工作流节点、更新和正式撤销；
- `models/`：Intent、IntentPlan、NetworkPolicy、ResourceBinding、生命周期和网络领域模型；
- `infra/`：资源账本、QoS 期望态、P4/ONOS/TC 读回、fencing、Reconciler、恢复和 telemetry；
- `mcp_servers/`：ONOS、P4Info、Flow/P4Runtime 与 telemetry 适配器；
- `persistence/`：SQLite 持久化与迁移；
- `services/`：对话结果、当前意图投影、Assurance evidence、偏好关系和等待意图归属；
- `config/`：ServiceProfile、拓扑路径、站点别名及 schema 配置；
- `tests/`：后端测试。

## 从仓库根目录运行

复制根目录 `.env.example` 为本机 `.env`，仅在本机填写部署认证配置；不要提交 `.env`。随后使用两个 Compose 文件启动完整系统：

```bash
docker compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d
```

运行后端测试：

```bash
docker compose -f docker-compose.infra.yml -f docker-compose.app.yml \
  exec -T idn-system python -m pytest -q
```

完整架构、API、P4/ONOS/TC、验收与运行说明位于根目录 `README.md` 和 `docs/delivery/`。认证材料、运行数据库和容器卷均不属于 GitHub 交付物。

"""
mcp_servers/__init__.py
=======================

MCP 工具服务层 —— 标准化工具总线入口。

# 系统角色
封装与外部系统的所有南向通信，通过 MCP（Model Context Protocol）
协议向 Agent 提供标准化的工具和资源接口。

# 子模块
- onos_server.py：ONOS REST API 封装（拓扑、设备、流表查询）
- telemetry_server.py：网络遥测数据采集（INT、P4 计数器、KPI）
- flow_adapter.py：IDN 内部模型 → ONOS FlowRule JSON
- flow_delivery.py：南向下发策略抽象（ONOS REST / gRPC 等）
- p4info_registry.py：P4Info 运行时注册表（解析 P4Info.txt）
- client.py：MCP 客户端（stdio 子进程 + MCP 握手协议）
"""

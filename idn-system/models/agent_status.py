"""
models/agent_status.py
======================

Agent 执行状态的共享枚举定义。

# 系统角色
定义所有 Agent 执行结果的三种标准状态，供整个系统的 Agent 调用方
和编排层统一使用。避免各模块自行定义状态字符串导致不一致。

# 状态语义
- OK：Agent 以完整能力正常完成（全量 LLM + 确定规则均成功）
- DEGRADED：Agent 以降低的质量完成（如 LLM 调用失败，启用了确定性降级规则）
- FAILED：Agent 无法完成，工作流应停止或升级

# 与其他模块交互
- agents/base_agent.py 及其子类：Agent 执行完成后返回此状态
- orchestrator/nodes/*.py：编排节点根据此状态决定后续路由
- api/worker.py：将状态转换为前端可读信息
"""
from enum import Enum


class AgentStatus(str, Enum):
    """
    Agent 执行结果状态枚举

    采用 str+Enum 混合继承，使枚举值可直接用于 JSON 序列化，
    无需在 API 层做额外转换。

    三种状态降级路径：
    OK ──(部分失败)──> DEGRADED ──(完全失败)──> FAILED

    DEGRADED 是 IDN-System 的一个关键设计：当 LLM 路径失败时，
    系统退回确定性规则路径（如基于 YANG 模板的规则生成），
    保证基础功能可用，但不期待最优效果。
    """
    OK = "ok"             # 正常完成，全量能力
    DEGRADED = "degraded" # 降级完成，能力受限（启用了 fallback 规则）
    FAILED = "failed"     # 失败，无法继续

"""
models/telemetry.py
===================

遥测数据与 KPI 指标数据模型。

# 系统角色
定义 VERIFY 阶段使用的遥测数据结构和 KPI 约束比对逻辑。
StatusVerifier 通过 MCP 采集 INT（带内遥测）和 P4 计数器数据后，
使用 IntentConstraints.is_satisfied_by() 判断 KPI 是否达标。

# 主要类型
- KPIMetrics：关键性能指标（时延、丢包率、带宽利用率、吞吐量、抖动、队列深度）
- INTHopRecord：INT 逐跳记录（设备ID、端口、时间戳、队列占用）
- TelemetryData：遥测数据缓冲区（INT 路径 + 聚合 KPI + 设备计数器）
- IntentConstraints：意图约束条件（max_latency_ms 等），用于 KPI 比对基准值

# 主要依赖
- pydantic.BaseModel / Field
- datetime（采集时间戳）

# 与其他模块交互
- mcp_servers/telemetry_server.py：INT 遥测数据采集
- agents/status_verifier.py：采集遥测并调用 is_satisfied_by() 验证 KPI
- infra/p4_counter_reader.py：读取 P4 计数器值
- agents/root_cause_analyzer.py：当 KPI 不达标时分析根因

# 设计意图
- INTHopRecord 支持逐跳分析，精确定位问题链路
- IntentConstraints.is_satisfied_by() 返回 (布尔值, 违规列表)，
  违规列表直接可用于前端展示和 ReAct 重试策略调整
- end_to_end_latency_ms 计算属性从 INT 逐跳记录自动聚合
"""
from __future__ import annotations
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class KPIMetrics(BaseModel):
    """关键性能指标 (Key Performance Indicators)"""
    latency_ms: Optional[float] = Field(default=None, description="端到端时延 (ms)")
    packet_loss_rate: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="丢包率 (0~1, e.g. 0.01 = 1%)"
    )
    bandwidth_utilization: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="带宽利用率 (0~1)"
    )
    throughput_mbps: Optional[float] = Field(default=None, description="实际吞吐量 (Mbps)")
    jitter_ms: Optional[float] = Field(default=None, description="网络抖动 (ms)")
    queue_depth: Optional[int] = Field(default=None, description="队列深度")
    int_hop_count: Optional[int] = Field(default=None, description="INT逐跳记录数量")
    int_path_latency_ms: Optional[float] = Field(default=None, description="INT逐跳累计时延 (ms)")
    int_max_queue_occupancy: Optional[int] = Field(default=None, description="INT最大单跳队列占用")


class INTHopRecord(BaseModel):
    """带内遥测(INT)单跳记录"""
    device_id: str = Field(..., description="交换机ID")
    ingress_port: str = Field(..., description="入端口")
    egress_port: str = Field(..., description="出端口")
    ingress_timestamp_ns: int = Field(..., description="入端口时间戳 (纳秒)")
    egress_timestamp_ns: int = Field(..., description="出端口时间戳 (纳秒)")
    queue_id: int = Field(default=0, description="队列ID")
    queue_occupancy: int = Field(default=0, description="队列占用 (cells)")

    @property
    def hop_latency_ns(self) -> int:
        """单跳时延 (纳秒)"""
        return self.egress_timestamp_ns - self.ingress_timestamp_ns


class TelemetryData(BaseModel):
    """
    遥测数据缓冲区
    由Status Verifier通过MCP调用telemetry-mcp-server采集
    """
    collection_id: str = Field(
        default_factory=lambda: f"TEL-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        description="采集批次ID"
    )
    intent_id: str = Field(..., description="关联的意图ID")
    collection_time: datetime = Field(
        default_factory=datetime.utcnow,
        description="采集时间"
    )
    sample_count: int = Field(default=0, description="采样次数")

    # INT路径遥测
    int_path_records: List[INTHopRecord] = Field(
        default_factory=list,
        description="INT路径逐跳遥测记录"
    )
    int_quality_status: str = Field(default="UNAVAILABLE", description="INT质量状态")
    int_quality_reason: str = Field(default="没有可用的INT记录", description="INT质量诊断")
    int_used_for_decision: bool = Field(default=False, description="INT是否进入KPI/路径决策")
    int_validation_checks: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="INT格式与合理性校验明细",
    )

    # 聚合KPI指标
    kpi: KPIMetrics = Field(
        default_factory=KPIMetrics,
        description="聚合后的KPI指标"
    )

    # 设备级计数器
    device_counters: Dict[str, Dict[str, int]] = Field(
        default_factory=dict,
        description="各设备P4计数器 {device_id: {counter_name: value}}"
    )

    # 原始数据
    raw_int_data: Optional[Dict] = Field(
        default=None,
        description="原始INT数据 (调试用)"
    )

    @property
    def end_to_end_latency_ms(self) -> Optional[float]:
        """计算端到端时延 (ms)"""
        if not self.int_path_records:
            return self.kpi.latency_ms
        total_ns = sum(r.hop_latency_ns for r in self.int_path_records)
        return total_ns / 1_000_000  # ns -> ms


class IntentConstraints(BaseModel):
    """意图约束条件 (用于KPI比对的基准值)"""
    max_latency_ms: Optional[float] = Field(default=None, description="最大允许时延 (ms)")
    max_packet_loss_rate: Optional[float] = Field(default=None, description="最大允许丢包率")
    min_bandwidth_mbps: Optional[float] = Field(default=None, description="最小保障带宽 (Mbps)")
    max_bandwidth_mbps: Optional[float] = Field(default=None, description="最大允许带宽 (Mbps)")
    max_jitter_ms: Optional[float] = Field(default=None, description="最大允许抖动 (ms)")

    def is_satisfied_by(self, kpi: KPIMetrics) -> tuple[bool, List[str]]:
        """
        判断KPI指标是否满足意图约束
        
        Returns:
            (is_satisfied, violation_list)
        """
        violations = []
        if self.max_latency_ms is not None:
            if kpi.latency_ms is None:
                violations.append(f"时延指标缺失: 无法验证 <= {self.max_latency_ms}ms")
            elif kpi.latency_ms > self.max_latency_ms:
                violations.append(
                    f"时延超限: 实际{kpi.latency_ms:.2f}ms > 约束{self.max_latency_ms}ms"
                )
        if self.max_packet_loss_rate is not None:
            if kpi.packet_loss_rate is None:
                violations.append(f"丢包率指标缺失: 无法验证 <= {self.max_packet_loss_rate}")
            elif kpi.packet_loss_rate > self.max_packet_loss_rate:
                violations.append(
                    f"丢包率超限: 实际{kpi.packet_loss_rate:.4f} > 约束{self.max_packet_loss_rate}"
                )
        if self.min_bandwidth_mbps is not None:
            if kpi.throughput_mbps is None:
                violations.append(f"带宽指标缺失: 无法验证 >= {self.min_bandwidth_mbps}Mbps")
            elif kpi.throughput_mbps < self.min_bandwidth_mbps:
                violations.append(
                    f"带宽不足: 实际{kpi.throughput_mbps:.1f}Mbps < 约束{self.min_bandwidth_mbps}Mbps"
                )
        if self.max_bandwidth_mbps is not None:
            if kpi.throughput_mbps is None:
                violations.append(f"吞吐量指标缺失: 无法验证 <= {self.max_bandwidth_mbps}Mbps")
            elif kpi.throughput_mbps > self.max_bandwidth_mbps:
                violations.append(
                    f"限速未达成: 实际{kpi.throughput_mbps:.1f}Mbps > 上限{self.max_bandwidth_mbps}Mbps"
                )
        if self.max_jitter_ms is not None:
            if kpi.jitter_ms is None:
                violations.append(f"抖动指标缺失: 无法验证 <= {self.max_jitter_ms}ms")
            elif kpi.jitter_ms > self.max_jitter_ms:
                violations.append(
                    f"抖动超限: 实际{kpi.jitter_ms:.2f}ms > 约束{self.max_jitter_ms}ms"
                )
        return (len(violations) == 0, violations)

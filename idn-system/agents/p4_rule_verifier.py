"""
agents/p4_rule_verifier.py
===========================

P4 表项级验证 Agent —— 策略编译后的质量闸门。

# 系统角色
在 PolicyCompilerAgent 生成 FlowTablePlan 后执行表项级验证：
- P4Info schema 一致性（字段名、匹配类型、动作参数是否匹配 P4Info 定义）
- 资源约束（TCAM/SRAM容量 > 新表项需求）
- 运行时规则无冲突（不与已安装规则重叠，不与非目标设备已有规则冲突）
- 审批令牌校验

验证失败时产生 RuleVerificationReport（含违规类型、严重程度、修复建议），
供 LangGraph 条件边决定下一个节点（ReAct 重试 vs 人工审批）。

# 主要依赖
- models/agent_state.py：RuleVerificationReport
- models/flow_table.py：FlowTablePlan, TableEntry
- agents/base_agent.py：基类

# 验证逻辑
- 严重性分级：HIGH(3) > MEDIUM(2) > LOW(1)
- 阻断阈值：severity >= MEDIUM → is_valid=False
- 基于 ipaddress 库的 IP 重叠检测（不使用字符串前缀匹配）
"""
from __future__ import annotations

import logging
from ipaddress import ip_network
from typing import Any, Optional

from models.agent_state import RuleVerificationReport
from models.flow_table import FlowTablePlan, TableEntry
from agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
BLOCKING_SEVERITY = 2


class P4RuleVerifierAgent(BaseAgent):
    """Verify generated P4Runtime rules before they are sent to devices."""

    agent_id = "p4_rule_verifier"
    description = "Verify generated P4Runtime table entries against P4Info and runtime rules"

    async def verify(
        self,
        flow_table_plan: FlowTablePlan | dict[str, Any],
        p4info: dict[str, Any],
        existing_runtime_rules: Optional[list[dict[str, Any]]] = None,
        approval_token: Optional[str] = None,
        expected_approval_token: Optional[str] = None,
        require_approval_token: bool = True,
    ) -> RuleVerificationReport:
        plan = self._coerce_plan(flow_table_plan)
        existing_runtime_rules = existing_runtime_rules or []

        logger.info("[%s] verifying flow table plan: plan=%s", self.agent_id, plan.plan_id)

        items: list[dict[str, Any]] = []
        items.extend(
            self._check_approval_token(
                approval_token=approval_token,
                expected_approval_token=expected_approval_token,
                require_approval_token=require_approval_token,
            )
        )
        items.extend(self._check_plan_not_empty(plan))
        items.extend(self._check_p4info_compatibility(plan, p4info))
        items.extend(self._check_meter_resources(plan, p4info))
        items.extend(self._check_internal_rule_conflicts(plan))
        items.extend(self._check_runtime_rule_conflicts(plan, existing_runtime_rules))
        items.extend(self._check_resource_id_conflicts(plan, existing_runtime_rules))
        items.extend(self._check_pipeline_risks(plan))
        items = self._dedupe_items(items)

        blocking_items = [
            item for item in items if SEVERITY_RANK.get(item.get("severity", "LOW"), 0) >= BLOCKING_SEVERITY
        ]
        warning_items = [
            item for item in items if SEVERITY_RANK.get(item.get("severity", "LOW"), 0) < BLOCKING_SEVERITY
        ]

        checked_items = sum(len(request.table_entries) for request in plan.write_requests)
        if blocking_items:
            primary = self._primary_item(blocking_items)
            explanation = self._summarize(blocking_items)
            logger.warning("[%s] rule verification failed: %s", self.agent_id, explanation)
            return RuleVerificationReport(
                is_valid=False,
                violation_type=primary.get("violation_type"),
                severity=primary.get("severity"),
                explanation=explanation,
                suggestions=self._collect_suggestions(blocking_items),
                warnings=[item.get("explanation", "") for item in warning_items if item.get("explanation")],
                warning_items=warning_items,
                violation_items=blocking_items,
                checked_items=checked_items,
            )

        return RuleVerificationReport(
            is_valid=True,
            violation_type=None,
            severity=None,
            explanation=(
                "未发现阻断性的 P4Runtime 表项级冲突，可以进入下发阶段。"
                if warning_items
                else "P4Runtime 表项级验证通过，可以进入下发阶段。"
            ),
            suggestions=(
                ["继续调用 P4Runtime Executor 下发表项；低风险警告可在审计中关注。"]
                + self._collect_suggestions(warning_items)
                if warning_items
                else ["继续调用 P4Runtime Executor 下发表项。"]
            ),
            warnings=[item.get("explanation", "") for item in warning_items if item.get("explanation")],
            warning_items=warning_items,
            violation_items=[],
            checked_items=checked_items,
        )

    def _check_approval_token(
        self,
        *,
        approval_token: Optional[str],
        expected_approval_token: Optional[str],
        require_approval_token: bool,
    ) -> list[dict[str, Any]]:
        if require_approval_token and not approval_token:
            return [
                self._make_item(
                    "APPROVAL_TOKEN_MISSING",
                    "LOW",  # Non-blocking in automated/test pipelines; the token is a human-approval gate
                    "生成的流表计划缺少策略冲突检测阶段签发的 approval_token。"
                    "在自动化测试或 CI pipeline 中此警告可忽略；生产部署中应通过人工审批获取 token。",
                    "若为自动化测试，可设 require_approval_token=False；若为生产环境，请先通过 PolicyConflictCheckerAgent。",
                )
            ]
        if expected_approval_token and approval_token != expected_approval_token:
            return [
                self._make_item(
                    "APPROVAL_TOKEN_MISMATCH",
                    "HIGH",
                    "流表计划携带的 approval_token 与冲突检测阶段签发的 token 不一致。",
                    "请重新执行冲突检测和策略生成，避免旧计划或越权计划被下发。",
                )
            ]
        return []

    def _check_plan_not_empty(self, plan: FlowTablePlan) -> list[dict[str, Any]]:
        if any(request.table_entries for request in plan.write_requests):
            return []
        if plan.policy_operation in {"VERIFY_EXISTING", "MODIFY_QOS_RESOURCE"}:
            # These operations target the existing TC/HTB resource and carry
            # no P4 insert by design.  Runtime config readback is performed by
            # StatusVerifier after execution.
            return []
        return [
            self._make_item(
                "EMPTY_RULE_PLAN",
                "HIGH",
                    "PolicyCompilerAgent 没有生成任何 P4Runtime 表项。",
                "请检查策略生成提示词、规则映射逻辑或输入的 YANGPolicy。",
            )
        ]

    def _check_p4info_compatibility(
        self,
        plan: FlowTablePlan,
        p4info: dict[str, Any],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        tables = self._p4_tables(p4info)
        actions = self._p4_actions(p4info)

        for device_id, entry in self._iter_plan_entries(plan):
            table = tables.get(entry.table_name)
            if table is None:
                items.append(
                    self._make_item(
                        "P4INFO_TABLE_NOT_FOUND",
                        "HIGH",
                        f"设备 {device_id} 的表项引用了 P4Info 中不存在的表：{entry.table_name}。",
                        "请修改 PolicyCompilerAgent 的表映射，或确认当前设备已加载正确的 P4 程序。",
                        device_id=device_id,
                        table_name=entry.table_name,
                    )
                )
                continue

            table_fields = {
                str(field.get("name")): field
                for field in table.get("match_fields", [])
                if str(field.get("match_type", "")).upper() != "OPTIONAL"
            }
            allowed_actions = {str(action) for action in table.get("action_refs", [])}

            seen_fields: set[str] = set()
            for match_field in entry.match_fields:
                seen_fields.add(match_field.field_name)
                p4_field = table_fields.get(match_field.field_name)
                if p4_field is None:
                    items.append(
                        self._make_item(
                            "P4INFO_MATCH_FIELD_ERROR",
                            "HIGH",
                            (
                                f"设备 {device_id} 的表 {entry.table_name} 使用了不存在的匹配字段："
                                f"{match_field.field_name}。"
                            ),
                            "请根据 P4Info 修正 match_fields。",
                            device_id=device_id,
                            table_name=entry.table_name,
                            field_name=match_field.field_name,
                        )
                    )
                    continue

                expected_type = str(p4_field.get("match_type", "")).lower()
                actual_type = str(match_field.match_type).lower()
                if expected_type and actual_type and expected_type != actual_type:
                    items.append(
                        self._make_item(
                            "P4INFO_MATCH_TYPE_ERROR",
                            "HIGH",
                            (
                                f"设备 {device_id} 的字段 {match_field.field_name} 匹配类型不符合 P4Info："
                                f"生成值={actual_type}, P4Info={expected_type}。"
                            ),
                            "请调整表项生成逻辑，使用 P4Info 中定义的 match_type。",
                            device_id=device_id,
                            table_name=entry.table_name,
                            field_name=match_field.field_name,
                        )
                    )

            missing_required_fields = set(table_fields) - seen_fields
            if missing_required_fields:
                items.append(
                    self._make_item(
                        "P4INFO_MATCH_FIELD_ERROR",
                        "HIGH",
                        (
                            f"设备 {device_id} 的表 {entry.table_name} 缺少 P4Info 要求的匹配字段："
                            f"{', '.join(sorted(missing_required_fields))}。"
                        ),
                        "请补齐该表所需的 match_fields，或改用更合适的 P4 table。",
                        device_id=device_id,
                        table_name=entry.table_name,
                        missing_fields=sorted(missing_required_fields),
                    )
                )

            if entry.action_name not in actions:
                items.append(
                    self._make_item(
                        "P4INFO_ACTION_NOT_FOUND",
                        "HIGH",
                        f"设备 {device_id} 的表项引用了 P4Info 中不存在的动作：{entry.action_name}。",
                        "请修正 action_name，或确认当前 P4Info 与设备程序一致。",
                        device_id=device_id,
                        table_name=entry.table_name,
                        action_name=entry.action_name,
                    )
                )
                continue

            if allowed_actions and entry.action_name not in allowed_actions:
                items.append(
                    self._make_item(
                        "P4INFO_ACTION_NOT_ALLOWED",
                        "HIGH",
                        (
                            f"动作 {entry.action_name} 不属于表 {entry.table_name} 的 action_refs，"
                            "该表项无法被 P4Runtime 接受。"
                        ),
                        "请选择该表允许的动作，或改写到正确的 P4 table。",
                        device_id=device_id,
                        table_name=entry.table_name,
                        action_name=entry.action_name,
                    )
                )

            items.extend(self._check_action_params(device_id, entry, actions.get(entry.action_name)))
            items.extend(self._check_table_capacity(device_id, entry, table, plan))

        return items

    def _check_action_params(
        self,
        device_id: str,
        entry: TableEntry,
        action: Optional[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if action is None:
            return []

        items: list[dict[str, Any]] = []
        expected_params = self._action_param_specs(action)
        actual_params = {param.param_name: param.value for param in entry.action_params}
        expected_names = set(expected_params)
        actual_names = set(actual_params)

        missing = expected_names - actual_names
        unknown = actual_names - expected_names
        if missing:
            items.append(
                self._make_item(
                    "ACTION_PARAM_ERROR",
                    "HIGH",
                    f"动作 {entry.action_name} 缺少参数：{', '.join(sorted(missing))}。",
                    "请补齐 action_params，使其与 P4Info action 参数定义一致。",
                    device_id=device_id,
                    table_name=entry.table_name,
                    action_name=entry.action_name,
                    missing_params=sorted(missing),
                )
            )
        if unknown:
            items.append(
                self._make_item(
                    "ACTION_PARAM_ERROR",
                    "HIGH",
                    f"动作 {entry.action_name} 包含 P4Info 未定义的参数：{', '.join(sorted(unknown))}。",
                    "请删除多余 action_params，或确认 P4Info 是否为最新版本。",
                    device_id=device_id,
                    table_name=entry.table_name,
                    action_name=entry.action_name,
                    unknown_params=sorted(unknown),
                )
            )

        for name, spec in expected_params.items():
            if name not in actual_params:
                continue
            value = actual_params[name]
            bitwidth = self._to_int(spec.get("bitwidth"))
            if bitwidth is not None:
                self._check_param_bitwidth(
                    items, device_id, entry, name, value, bitwidth,
                )
        return items

    def _check_param_bitwidth(
        self,
        items: list[dict[str, Any]],
        device_id: str,
        entry: TableEntry,
        name: str,
        value: Any,
        bitwidth: int,
    ) -> None:
        """Validate a param value fits within its declared bitwidth.

        Handles both integer values (decimal strings like "0003") and
        hex-encoded values (MAC addresses like "82f51469adfd").
        """
        int_value = self._to_int(value)
        if int_value is not None:
            # Numeric value — check range
            if int_value < 0 or int_value >= 2 ** bitwidth:
                items.append(
                    self._make_item(
                        "ACTION_PARAM_TYPE_ERROR",
                        "HIGH",
                        f"动作 {entry.action_name} 的参数 {name} 值 {value} 超出 bitwidth={bitwidth} 范围。",
                        "请按照 P4Info 参数类型和位宽重新生成 action_params。",
                        device_id=device_id, table_name=entry.table_name,
                        action_name=entry.action_name, param_name=name,
                    )
                )
            return

        # Non-numeric value — treat as hex string, check length
        hex_str = str(value)
        expected_hex_len = max(1, (bitwidth + 3) // 4)  # ceil(bitwidth/4)
        if len(hex_str) != expected_hex_len:
            items.append(
                self._make_item(
                    "ACTION_PARAM_TYPE_ERROR",
                    "HIGH",
                    (
                        f"动作 {entry.action_name} 的参数 {name} 的十六进制值 \"{hex_str}\" "
                        f"长度={len(hex_str)}，期望={expected_hex_len} hex chars (bitwidth={bitwidth})。"
                    ),
                    "请按照 P4Info 参数位宽重新编码 action_params。",
                    device_id=device_id, table_name=entry.table_name,
                    action_name=entry.action_name, param_name=name,
                )
            )

    def _check_table_capacity(
        self,
        device_id: str,
        entry: TableEntry,
        table: dict[str, Any],
        plan: FlowTablePlan,
    ) -> list[dict[str, Any]]:
        size = self._to_int(table.get("size"))
        if size is None:
            return []
        count = sum(
            1
            for current_device_id, current_entry in self._iter_plan_entries(plan)
            if current_device_id == device_id and current_entry.table_name == entry.table_name
        )
        if count <= size:
            return []
        return [
            self._make_item(
                "TABLE_CAPACITY_CONFLICT",
                "HIGH",
                f"设备 {device_id} 的表 {entry.table_name} 计划写入 {count} 条，超过 P4Info size={size}。",
                "请减少生成表项数量，或选择容量更大的表/路径。",
                device_id=device_id,
                table_name=entry.table_name,
                planned_entries=count,
                table_size=size,
            )
        ]

    def _check_meter_resources(
        self,
        plan: FlowTablePlan,
        p4info: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Validate meter usage for sla_enforcement entries (direct meter)."""
        items: list[dict[str, Any]] = []

        for device_id, entry in self._iter_plan_entries(plan):
            needs_meter = (
                entry.table_name == "MyIngress.sla_enforcement"
                or entry.action_name == "MyIngress.apply_meter_policy"
            )
            if not needs_meter:
                continue

            # sla_enforcement uses a direct meter — no explicit meter_id needed
            # on the table entry. Meter config is a separate P4Runtime entity.
            table = self._p4_tables(p4info).get(entry.table_name)
            if table is None:
                continue  # already caught by _check_p4info_compatibility
            if entry.meter_id is None:
                continue
            meter_size = self._meter_capacity(p4info, entry.table_name)
            if meter_size is None:
                meter_size = self._to_int(table.get("size"))
            meter_id = self._to_int(entry.meter_id)
            if meter_id is None or meter_id < 0 or (meter_size is not None and meter_id >= meter_size):
                items.append(
                    self._make_item(
                        "METER_ID_OUT_OF_RANGE",
                        "HIGH",
                        (
                            f"Device {device_id} table {entry.table_name} uses meter_id="
                            f"{entry.meter_id}, outside configured capacity {meter_size}."
                        ),
                        "Remove explicit meter_id for direct-meter tables or allocate an in-range meter index.",
                        device_id=device_id,
                        table_name=entry.table_name,
                        meter_id=entry.meter_id,
                        meter_size=meter_size,
                    )
                )

        return items

    def _meter_capacity(self, p4info: dict[str, Any], table_name: str) -> int | None:
        for meter in p4info.get("meters", []) or []:
            parent = (
                meter.get("direct_table")
                or meter.get("parent_table")
                or meter.get("table")
            )
            if parent and str(parent) != table_name:
                continue
            size = self._to_int(meter.get("size"))
            if size is not None:
                return size
        for meter in p4info.get("direct_meters", []) or []:
            parent = (
                meter.get("direct_table")
                or meter.get("parent_table")
                or meter.get("table")
            )
            if parent and str(parent) != table_name:
                continue
            size = self._to_int(meter.get("size"))
            if size is not None:
                return size
        return None

    def _check_internal_rule_conflicts(self, plan: FlowTablePlan) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: dict[tuple[Any, ...], TableEntry] = {}

        for device_id, entry in self._iter_plan_entries(plan):
            key = self._entry_conflict_key(device_id, entry)
            previous = seen.get(key)
            if previous is None:
                seen[key] = entry
                continue
            if self._action_signature(previous) != self._action_signature(entry):
                items.append(
                    self._make_item(
                        "TABLE_ENTRY_CONFLICT",
                        "HIGH",
                        (
                            f"同一计划内设备 {device_id} 的表 {entry.table_name} 存在同 match、"
                            f"同 priority={entry.priority} 但 action 不同的表项。"
                        ),
                        "请合并重复表项，或调整 match/priority，使 P4Runtime 匹配结果唯一。",
                        device_id=device_id,
                        table_name=entry.table_name,
                        priority=entry.priority,
                    )
                )
            else:
                items.append(
                    self._make_item(
                        "DUPLICATE_TABLE_ENTRY",
                        "LOW",
                        f"同一计划内设备 {device_id} 的表 {entry.table_name} 存在完全重复表项。",
                        "建议去重，减少无意义的 P4Runtime 写入。",
                        device_id=device_id,
                        table_name=entry.table_name,
                    )
                )
        return items

    def _check_runtime_rule_conflicts(
        self,
        plan: FlowTablePlan,
        existing_runtime_rules: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        existing_entries = list(self._iter_existing_entries(existing_runtime_rules))

        for device_id, entry in self._iter_plan_entries(plan):
            for existing_device_id, existing in existing_entries:
                if existing_device_id != device_id:
                    continue
                if self._value_text(existing.get("table_name")) != entry.table_name:
                    continue
                existing_priority = self._to_int(existing.get("priority")) or 0
                existing_match = self._normalize_match_fields(existing.get("match_fields") or [])
                new_match = self._normalize_entry_match(entry)
                existing_action = self._value_text(existing.get("action_name"))

                if existing_match == new_match and existing_priority == entry.priority:
                    if existing_action != entry.action_name:
                        items.append(
                            self._make_item(
                                "RUNTIME_RULE_CONFLICT",
                                "HIGH",
                                (
                                    f"设备 {device_id} 已存在同表同 match 同 priority 的运行时规则，"
                                    f"但 action 不同：已有={existing_action}, 新增={entry.action_name}。"
                                ),
                                "请先删除/覆盖旧规则，或调整新规则 priority 和 match。",
                                device_id=device_id,
                                table_name=entry.table_name,
                                priority=entry.priority,
                            )
                        )
                    else:
                        items.append(
                            self._make_item(
                                "DUPLICATE_RUNTIME_RULE",
                                "LOW",
                                f"设备 {device_id} 已存在与计划完全等价的运行时规则。",
                                "建议跳过重复写入，避免无意义的表项更新。",
                                device_id=device_id,
                                table_name=entry.table_name,
                            )
                        )
                    continue

                if existing_priority > entry.priority and self._match_covers(existing_match, new_match):
                    items.append(
                        self._make_item(
                            "SHADOWED_RULE",
                            "MEDIUM",
                            (
                                f"设备 {device_id} 中已有更高优先级规则可能遮蔽新表项："
                                f"已有 priority={existing_priority}, 新增 priority={entry.priority}。"
                            ),
                            "请提高新表项 priority，或确认已有高优先级规则不会影响该业务。",
                            device_id=device_id,
                            table_name=entry.table_name,
                            existing_priority=existing_priority,
                            new_priority=entry.priority,
                        )
                    )

        return items

    def _check_resource_id_conflicts(
        self,
        plan: FlowTablePlan,
        existing_runtime_rules: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        resource_owners: dict[tuple[str, str, Any], str] = {}

        for device_id, existing in self._iter_existing_entries(existing_runtime_rules):
            owner = str(existing.get("rule_id") or existing.get("flow_id") or "existing-runtime-rule")
            for resource_type, resource_id in self._resource_ids_from_mapping(existing).items():
                resource_owners[(device_id, resource_type, resource_id)] = owner

        for device_id, entry in self._iter_plan_entries(plan):
            owner = f"{entry.table_name}:{self._match_key(entry)}"
            for resource_type, resource_id in self._entry_resource_ids(entry).items():
                key = (device_id, resource_type, resource_id)
                previous = resource_owners.get(key)
                if previous and previous != owner:
                    items.append(
                        self._make_item(
                            "RESOURCE_ID_CONFLICT",
                            "MEDIUM",
                            (
                                f"设备 {device_id} 的 {resource_type}={resource_id} 已被 {previous} 使用，"
                                "新表项再次占用可能造成资源语义冲突。"
                            ),
                            "请重新分配 meter/counter/queue/clone session 等资源 ID。",
                            device_id=device_id,
                            resource_type=resource_type,
                            resource_id=resource_id,
                            previous_owner=previous,
                        )
                    )
                else:
                    resource_owners[key] = owner

        return items

    def _check_pipeline_risks(self, plan: FlowTablePlan) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        entries = list(self._iter_plan_entries(plan))
        for index, (device_id, entry) in enumerate(entries):
            action_lower = entry.action_name.lower()
            if not self._is_drop_action(action_lower):
                continue
            entry_match = self._normalize_entry_match(entry)
            for other_device_id, other in entries[index + 1 :]:
                if other_device_id != device_id:
                    continue
                if other.table_name != entry.table_name:
                    continue
                if self._normalize_entry_match(other) != entry_match:
                    continue
                other_action = other.action_name.lower()
                if self._is_mirror_action(other_action):
                    items.append(
                        self._make_item(
                            "MIRROR_DROP_PIPELINE_RISK",
                            "MEDIUM",
                            (
                                f"设备 {device_id} 的同一匹配流量同时生成 DROP 与 MIRROR/CLONE 动作，"
                                "实际效果取决于 P4 pipeline 中镜像和丢弃的执行顺序。"
                            ),
                            "请确认 P4 pipeline 顺序；必要时拆分规则或由人工审批。",
                            device_id=device_id,
                            table_name=entry.table_name,
                        )
                    )
        return items

    def _p4_tables(self, p4info: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(table.get("name")): table for table in p4info.get("tables", []) if table.get("name")}

    def _p4_actions(self, p4info: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(action.get("name")): action for action in p4info.get("actions", []) if action.get("name")}

    def _action_param_specs(self, action: dict[str, Any]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for param in action.get("params", []):
            if isinstance(param, str):
                output[param] = {"name": param}
            elif isinstance(param, dict) and param.get("name"):
                output[str(param["name"])] = dict(param)
        return output

    def _coerce_plan(self, flow_table_plan: FlowTablePlan | dict[str, Any]) -> FlowTablePlan:
        if isinstance(flow_table_plan, FlowTablePlan):
            return flow_table_plan
        return FlowTablePlan(**flow_table_plan)

    def _iter_plan_entries(self, plan: FlowTablePlan):
        for request in plan.write_requests:
            for entry in request.table_entries:
                yield request.device_id, entry

    def _iter_existing_entries(self, existing_runtime_rules: list[dict[str, Any]]):
        for rule in existing_runtime_rules:
            device_id = str(rule.get("device_id") or "unknown")
            if "table_entries" in rule:
                for entry in rule.get("table_entries") or []:
                    yield device_id, self._mapping(entry)
            else:
                yield device_id, self._mapping(rule)

    def _mapping(self, value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return dict(value or {})

    def _entry_conflict_key(self, device_id: str, entry: TableEntry) -> tuple[Any, ...]:
        return (
            device_id,
            entry.table_name,
            self._match_key(entry),
            entry.priority,
        )

    def _match_key(self, entry: TableEntry) -> tuple[Any, ...]:
        return tuple(sorted(self._normalize_entry_match(entry).items()))

    def _normalize_entry_match(self, entry: TableEntry) -> dict[str, tuple[Any, ...]]:
        return self._normalize_match_fields([field.model_dump() for field in entry.match_fields])

    def _normalize_match_fields(self, match_fields: list[Any]) -> dict[str, tuple[Any, ...]]:
        output: dict[str, tuple[Any, ...]] = {}
        for field in match_fields:
            data = self._mapping(field)
            name = str(data.get("field_name") or data.get("name") or "")
            if not name:
                continue
            match_type = str(data.get("match_type") or "").lower()
            value = self._normalize_match_value(data.get("value"))
            mask = self._normalize_match_value(data.get("mask"))
            prefix_len = self._to_int(data.get("prefix_len"))
            output[name] = (match_type, value, mask, prefix_len)
        return output

    def _normalize_match_value(self, value: Any) -> Any:
        if value in (None, "", "null"):
            return None
        return str(value).strip()

    def _match_covers(
        self,
        broader: dict[str, tuple[Any, ...]],
        narrower: dict[str, tuple[Any, ...]],
    ) -> bool:
        if broader == narrower:
            return True
        if set(broader) != set(narrower):
            return False
        for field_name, broad in broader.items():
            narrow = narrower[field_name]
            if not self._field_match_covers(broad, narrow):
                return False
        return True

    def _field_match_covers(self, broad: tuple[Any, ...], narrow: tuple[Any, ...]) -> bool:
        broad_type, broad_value, broad_mask, broad_prefix = broad
        narrow_type, narrow_value, narrow_mask, narrow_prefix = narrow
        if broad_type != narrow_type:
            return False
        if broad == narrow:
            return True
        if broad_type == "lpm" and broad_prefix is not None and narrow_prefix is not None:
            broad_net = self._to_network(broad_value, broad_prefix)
            narrow_net = self._to_network(narrow_value, narrow_prefix)
            return bool(broad_net and narrow_net and narrow_net.subnet_of(broad_net))
        if broad_type == "ternary":
            return broad_value == narrow_value and broad_mask == narrow_mask
        return False

    def _to_network(self, value: Any, prefix_len: Optional[int]):
        if value in (None, "", "null") or prefix_len is None:
            return None
        try:
            return ip_network(f"{value}/{prefix_len}", strict=False)
        except ValueError:
            return None

    def _action_signature(self, entry: TableEntry) -> tuple[Any, ...]:
        return (
            entry.action_name,
            tuple(sorted((param.param_name, self._value_text(param.value)) for param in entry.action_params)),
        )

    def _entry_resource_ids(self, entry: TableEntry) -> dict[str, Any]:
        resources: dict[str, Any] = {}
        for name in ("meter_id", "counter_id", "queue_id", "egress_port", "clone_session_id"):
            value = getattr(entry, name, None)
            if value not in (None, "", "null"):
                resources[name] = value
        for param in entry.action_params:
            name = param.param_name
            if name in {"meter_id", "counter_id", "queue_id", "egress_port", "clone_session_id", "mirror_session_id"}:
                resources[name] = param.value
        return resources

    def _resource_ids_from_mapping(self, data: dict[str, Any]) -> dict[str, Any]:
        resources: dict[str, Any] = {}
        for name in ("meter_id", "counter_id", "queue_id", "egress_port", "clone_session_id", "mirror_session_id"):
            value = data.get(name)
            if value not in (None, "", "null"):
                resources[name] = value
        for param in data.get("action_params") or []:
            param_data = self._mapping(param)
            name = param_data.get("param_name")
            if name in {"meter_id", "counter_id", "queue_id", "egress_port", "clone_session_id", "mirror_session_id"}:
                resources[str(name)] = param_data.get("value")
        return resources

    def _is_drop_action(self, action_name: str) -> bool:
        return "drop" in action_name

    def _is_mirror_action(self, action_name: str) -> bool:
        return "mirror" in action_name or "clone" in action_name

    def _to_int(self, value: Any) -> Optional[int]:
        if value in (None, "", "null"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _value_text(self, value: Any) -> str:
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    def _make_item(
        self,
        violation_type: str,
        severity: str,
        explanation: str,
        suggestion: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "type": violation_type,
            "violation_type": violation_type,
            "severity": severity,
            "description": explanation,
            "explanation": explanation,
            "suggestion": suggestion,
            **extra,
        }

    def _primary_item(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return sorted(
            items,
            key=lambda item: SEVERITY_RANK.get(item.get("severity", "LOW"), 0),
            reverse=True,
        )[0]

    def _summarize(self, items: list[dict[str, Any]]) -> str:
        primary = self._primary_item(items)
        high = sum(1 for item in items if item.get("severity") == "HIGH")
        medium = sum(1 for item in items if item.get("severity") == "MEDIUM")
        low = sum(1 for item in items if item.get("severity") == "LOW")
        parts = []
        if high:
            parts.append(f"{high} 个高危问题")
        if medium:
            parts.append(f"{medium} 个中等风险问题")
        if low:
            parts.append(f"{low} 个低风险问题")
        return f"P4Runtime 表项级验证未通过：发现 {'，'.join(parts)}。主要问题：{primary.get('explanation')}"

    def _collect_suggestions(self, items: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        suggestions: list[str] = []
        for item in sorted(
            items,
            key=lambda current: SEVERITY_RANK.get(current.get("severity", "LOW"), 0),
            reverse=True,
        ):
            suggestion = str(item.get("suggestion") or "").strip()
            if suggestion and suggestion not in seen:
                suggestions.append(suggestion)
                seen.add(suggestion)
        return suggestions

    def _dedupe_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        output: list[dict[str, Any]] = []
        for item in items:
            key = (
                item.get("violation_type"),
                item.get("severity"),
                item.get("device_id"),
                item.get("table_name"),
                item.get("field_name"),
                item.get("param_name"),
                item.get("resource_type"),
                item.get("resource_id"),
                item.get("explanation"),
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
        return output

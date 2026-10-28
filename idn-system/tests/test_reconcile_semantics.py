from infra.reconcile_semantics import canonicalize_egress_desired, canonicalize_egress_observed, canonicalize_p4_desired, canonicalize_p4_observed, reconcile_semantic_diff


def test_p4_volatile_counters_timestamps_and_order_are_no_change():
    desired = canonicalize_p4_desired(device_id="d", table="t", selector={"source_ip":"192.168.1.1", "destination_ip":"10.0.0.1", "protocol":"udp", "destination_port":5004}, priority=101, action="a", action_params=[{"param_id":1,"value":"2e"}], dscp=46, counter_binding="counter", owner="p", generation="g", scope="s")
    observed = canonicalize_p4_observed({"device_id":"d", "table_name":"t", "selector":{"source_prefix":"192.168.1.1/32", "destination_prefix":"10.0.0.1/32", "ip_protocol":17, "destination_port":5004}, "priority":101, "action_name":"a", "action_parameters":[{"value":"2e","param_id":1}], "dscp":46, "direct_counter_name":"counter", "counter_packets":99, "observed_at":"later"}, owner="p", generation="g", scope="s")
    assert not reconcile_semantic_diff(reconcile_id="x", recovery_id=None, resource_kind="P4", desired=desired, observed=observed, ignored_volatile_fields=())["write_required"]


def test_egress_units_tos_stats_and_order_are_no_change_but_real_rate_is_changed():
    desired = canonicalize_egress_desired(root_rate="49Mbit", root_ceil="49Mbit", target_classid="1:100", target_rate="30Mbit", target_ceil="49Mbit", default_classid="1:fffe", default_rate="1Mbit", default_ceil="19Mbit", filter_kind="flower_dscp_plus_five_tuple", filter_pref=100, selector={"source_ip":"192.168.10.10", "destination_ip":"10.0.0.10", "protocol":"udp", "destination_port":5004}, dscp=46, owner="p", scope="s", generation="g")
    raw = {"root_rate_mbps":"49000Kbit", "root_ceil_mbps":"49000000bit", "class_id":"0001:0100", "rate_mbps":"30Mbit", "ceil_mbps":"49Mbit", "default_class_id":"1:fffe", "default_rate_mbps":1, "default_ceil_mbps":19, "filter_kind":"flower_dscp_plus_five_tuple", "filter_priority":100, "classification_key":{"source_ip":"192.168.10.10/32", "destination_ip":"10.0.0.10/32", "protocol":17, "destination_port":5004, "dscp":"0xb8/fc"}, "stats":{"bytes":999}}
    observed = canonicalize_egress_observed(raw, owner="p", scope="s", generation="g")
    assert not reconcile_semantic_diff(reconcile_id="x", recovery_id=None, resource_kind="EGRESS", desired=desired, observed=observed, ignored_volatile_fields=())["write_required"]
    raw["rate_mbps"] = 1
    changed = canonicalize_egress_observed(raw, owner="p", scope="s", generation="g")
    assert reconcile_semantic_diff(reconcile_id="x", recovery_id=None, resource_kind="EGRESS", desired=desired, observed=changed, ignored_volatile_fields=())["write_required"]

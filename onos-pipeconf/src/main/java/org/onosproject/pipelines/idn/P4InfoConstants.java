package org.onosproject.pipelines.idn;

/**
 * Auto-generated from idn_basic.p4info.txt — DO NOT EDIT BY HAND.
 *
 * <p>Run {@code p4/compile.sh} after modifying {@code p4/idn_basic.p4}
 * to regenerate this file.
 *
 * <p>Generator: {@code p4/generate_java_constants.py}
 */
public final class P4InfoConstants {

    private P4InfoConstants() {}

    // ── Tables ───────────────────────────────────────────────
    public static final int TABLE_ID_MyEgress_int_switch_config = 34139498;
    public static final int TABLE_ID_MyIngress_acl = 37519123;
    public static final int TABLE_ID_MyIngress_arp_proxy = 50047537;
    public static final int TABLE_ID_MyIngress_bandwidth_policy = 36728593;
    public static final int TABLE_ID_MyIngress_host_mac_resolution = 36301554;
    public static final int TABLE_ID_MyIngress_ipv4_routing = 37066206;
    public static final int TABLE_ID_MyIngress_sla_enforcement = 38513872;

    // ── Actions ──────────────────────────────────────────────
    public static final int ACTION_ID_MyEgress_push_int_telemetry = 26724011;
    public static final int ACTION_ID_MyIngress_acl_deny = 22544214;
    public static final int ACTION_ID_MyIngress_apply_meter_bytes_policy = 24308734;
    public static final int ACTION_ID_MyIngress_apply_meter_policy = 33052744;
    public static final int ACTION_ID_MyIngress_arp_reply = 22921650;
    public static final int ACTION_ID_MyIngress_drop_packet = 32003175;
    public static final int ACTION_ID_MyIngress_punt_arp = 31748219;
    public static final int ACTION_ID_MyIngress_record_violation = 25531251;
    public static final int ACTION_ID_MyIngress_rewrite_dst_mac = 32579445;
    public static final int ACTION_ID_MyIngress_set_dscp = 29261492;
    public static final int ACTION_ID_MyIngress_set_nhop = 24952588;
    public static final int ACTION_ID_NoAction = 21257015;

    // ── Counters ─────────────────────────────────────────────
    public static final int COUNTER_ID_MyEgress_diag_egress_emitted = 312805492;
    public static final int COUNTER_ID_MyEgress_diag_egress_entered = 310695262;
    public static final int COUNTER_ID_MyIngress_diag_ingress_dropped = 303202277;
    public static final int COUNTER_ID_MyIngress_diag_ingress_forwarded = 318553190;
    public static final int COUNTER_ID_MyIngress_diag_invalid_egress_spec = 318479627;
    public static final int COUNTER_ID_MyIngress_diag_ipv4_udp_parsed = 312584596;
    public static final int COUNTER_ID_MyIngress_diag_set_dscp_called = 302458082;
    public static final int COUNTER_ID_MyIngress_diag_target_classifier_hit = 315086251;
    public static final int COUNTER_ID_MyIngress_diag_target_five_tuple_seen = 307935029;

    // ── Direct Counters ──────────────────────────────────────
    public static final int COUNTER_ID_MyIngress_qos_classifier_counter = 320825904;

    // ── Direct Meters ────────────────────────────────────────
    public static final int METER_ID_MyIngress_host_meter = 355791019;
    public static final int METER_ID_MyIngress_host_meter_bytes = 366379332;

    // ── Controller Packet Metadata ───────────────────────────
    public static final int CTRL_META_ID_packet_in = 81826293;
    public static final int CTRL_META_ID_packet_out = 76689799;

    // ── Registers ────────────────────────────────────────────
    public static final int REGISTER_ID_MyIngress_violation_counter = 373816183;

}

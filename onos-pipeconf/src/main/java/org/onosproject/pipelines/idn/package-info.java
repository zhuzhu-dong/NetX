/**
 * Custom ONOS pipeconf for the idn_basic.p4 pipeline.
 * <p>
 * This package provides:
 * <ul>
 *   <li>{@link IdnPipeconf} — registers {@code org.onosproject.pipelines.idn-basic} pipeconf</li>
 *   <li>{@link IdnInterpreter} — maps between ONOS abstractions and P4Runtime messages</li>
 *   <li>{@link IdnPipeliner} — processes FlowObjectives (pass-through; P4 data-plane handles forwarding)</li>
 * </ul>
 * <p>
 * Tables (MyIngress):
 * <ul>
 *   <li>{@code ipv4_routing} — IPv4 forwarding with ECMP (dstAddr: lpm)</li>
 *   <li>{@code arp_proxy} — ARP proxy (targetProtoAddr: exact)</li>
 *   <li>{@code sla_enforcement} — SLA/QoS enforcement (intent_type: exact, direct PACKETS meter)</li>
 *   <li>{@code bandwidth_policy} — bandwidth rate limiting (srcAddr/dstAddr: ternary, direct BYTES meter)</li>
 *   <li>{@code acl} — post-routing security gate (srcAddr/dstAddr/protocol/ingress_port: ternary)</li>
 * </ul>
 * <p>
 * Tables (MyEgress):
 * <ul>
 *   <li>{@code int_insertion} — INT telemetry insertion</li>
 * </ul>
 * <p>
 * Meters: {@code host_meter} (PACKETS, on sla_enforcement), {@code host_meter_bytes} (BYTES, on bandwidth_policy).
 */
package org.onosproject.pipelines.idn;

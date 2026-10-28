package org.onosproject.pipelines.idn;

import org.onlab.packet.Ethernet;
import org.onlab.util.ImmutableByteSequence;
import org.onosproject.net.ConnectPoint;
import org.onosproject.net.DeviceId;
import org.onosproject.net.PortNumber;
import org.onosproject.net.driver.DriverData;
import org.onosproject.net.driver.DriverHandler;
import org.onosproject.net.flow.TrafficTreatment;
import org.onosproject.net.flow.criteria.Criterion;
import org.onosproject.net.flow.instructions.Instruction;
import org.onosproject.net.flow.instructions.Instructions;
import org.onosproject.net.flow.instructions.PiInstruction;
import org.onosproject.net.packet.DefaultInboundPacket;
import org.onosproject.net.packet.InboundPacket;
import org.onosproject.net.packet.OutboundPacket;
import org.onosproject.net.pi.model.PiMatchFieldId;
import org.onosproject.net.pi.model.PiPacketMetadataId;
import org.onosproject.net.pi.model.PiPacketOperationType;
import org.onosproject.net.pi.model.PiPipelineInterpreter;
import org.onosproject.net.pi.model.PiTableId;
import org.onosproject.net.pi.model.PiActionId;
import org.onosproject.net.pi.runtime.PiAction;
import org.onosproject.net.pi.runtime.PiPacketMetadata;
import org.onosproject.net.pi.runtime.PiPacketOperation;
import org.onosproject.net.pi.runtime.PiTableAction;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.ByteBuffer;
import java.util.Collection;
import java.util.List;
import java.util.Optional;

public class IdnInterpreter implements PiPipelineInterpreter {

    private static final Logger log = LoggerFactory.getLogger(IdnInterpreter.class);

    private static final int PORT_BITWIDTH = 32;

    private static final String PREFIX = "MyIngress.";

    // static final String TABLE_IPV4_LPM     = PREFIX + "ipv4_routing";
    // static final String TABLE_DSCP_MARKER  = PREFIX + "sla_enforcement";
    // static final String TABLE_METER_POLICY = PREFIX + "meter_policy";

    static final String TABLE_IPV4_LPM        = PREFIX + "ipv4_routing";
    static final String TABLE_ARP_PROXY      = PREFIX + "arp_proxy";
    static final String TABLE_METER_POLICY    = PREFIX + "sla_enforcement";
    static final String TABLE_BANDWIDTH_POLICY = PREFIX + "bandwidth_policy";
    static final String TABLE_ACL            = PREFIX + "acl";
    static final String TABLE_HOST_MAC_RESOLUTION = PREFIX + "host_mac_resolution";
    static final String TABLE_INT_SWITCH_CONFIG = "MyEgress.int_switch_config";

    private DriverData data;
    private DriverHandler handler;

    @Override
    public DriverData data() { return data; }

    @Override
    public void setData(DriverData data) { this.data = data; }

    @Override
    public DriverHandler handler() { return handler; }

    @Override
    public void setHandler(DriverHandler handler) { this.handler = handler; }

    @Override
    public PiAction mapTreatment(TrafficTreatment treatment, PiTableId piTableId)
            throws PiInterpreterException {
        // PiInstruction: used by intent-based forwarding (P4 action already encoded)
        for (var instr : treatment.allInstructions()) {
            if (instr instanceof PiInstruction) {
                PiTableAction ta = ((PiInstruction) instr).action();
                if (ta instanceof PiAction) {
                    return (PiAction) ta;
                }
            }
        }
        // set_nhop requires port + srcMac + dstMac.  A standard OUTPUT-only
        // treatment contains no router/neighbor MACs; translating it used to
        // omit both parameters, which BMv2 encoded as all-zero Ethernet MACs.
        // IDN routing rules must arrive as a complete PiInstruction instead.
        if (piTableId.id().equals(TABLE_IPV4_LPM)) {
            for (var instr : treatment.allInstructions()) {
                if (instr.type() == Instruction.Type.OUTPUT) {
                    PortNumber port = ((Instructions.OutputInstruction) instr).port();
                    throw new PiInterpreterException(
                            "Refusing incomplete ipv4_routing OUTPUT treatment for port "
                            + port + ": MyIngress.set_nhop requires port, srcMac, and dstMac");
                }
            }
        }
        // ACL table: default allow (NoAction); deny must come as PiInstruction
        if (piTableId.id().equals(TABLE_ACL)) {
            return PiAction.builder()
                    .withId(PiActionId.of("NoAction"))
                    .build();
        }
        throw new PiInterpreterException(
                "No translatable instruction in treatment for table " + piTableId.id());
    }

    @Override
    public Optional<PiMatchFieldId> mapCriterionType(Criterion.Type type) {
        switch (type) {
            case IPV4_DST:
                return Optional.of(PiMatchFieldId.of("hdr.ipv4.dstAddr"));
            case IPV4_SRC:
                return Optional.of(PiMatchFieldId.of("hdr.ipv4.srcAddr"));
            default:
                return Optional.empty();
        }
    }

    @Override
    public Optional<PiTableId> mapFlowRuleTableId(int flowTableType) {
        // Map P4Info table ID to PiTableId (numeric IDs from compiled p4info.txt)
        switch (flowTableType) {
            case P4InfoConstants.TABLE_ID_MyIngress_ipv4_routing:
                return Optional.of(PiTableId.of(TABLE_IPV4_LPM));
            case P4InfoConstants.TABLE_ID_MyIngress_arp_proxy:
                return Optional.of(PiTableId.of(TABLE_ARP_PROXY));
            case P4InfoConstants.TABLE_ID_MyIngress_sla_enforcement:
                return Optional.of(PiTableId.of(TABLE_METER_POLICY));
            case P4InfoConstants.TABLE_ID_MyIngress_bandwidth_policy:
                return Optional.of(PiTableId.of(TABLE_BANDWIDTH_POLICY));
            case P4InfoConstants.TABLE_ID_MyIngress_acl:
                return Optional.of(PiTableId.of(TABLE_ACL));
            case P4InfoConstants.TABLE_ID_MyIngress_host_mac_resolution:
                return Optional.of(PiTableId.of(TABLE_HOST_MAC_RESOLUTION));
            case P4InfoConstants.TABLE_ID_MyEgress_int_switch_config:
                return Optional.of(PiTableId.of(TABLE_INT_SWITCH_CONFIG));
            default:
                log.warn("Unknown flowTableType {}", flowTableType);
                return Optional.empty();
        }
    }

    @Override
    public Collection<PiPacketOperation> mapOutboundPacket(OutboundPacket packet)
            throws PiInterpreterException {
        TrafficTreatment treatment = packet.treatment();
        PortNumber outPort = null;
        for (Instruction instr : treatment.allInstructions()) {
            if (instr.type() == Instruction.Type.OUTPUT) {
                outPort = ((Instructions.OutputInstruction) instr).port();
                break;
            }
        }
        if (outPort == null) {
            throw new PiInterpreterException("No OUTPUT instruction in packet-out treatment");
        }

        if (outPort.isLogical()) {
            log.info("PKT-OUT SKIP port={} (logical)", outPort);
            return List.of();
        }

        // Log outbound packet details
        byte[] data = packet.data().array();
        int dataLen = packet.data().remaining();
        String etherInfo = "?";
        if (dataLen >= 14) {
            StringBuilder mac = new StringBuilder();
            for (int i = 0; i < 6; i++) mac.append(String.format("%02x:", data[i]));
            String dst = mac.substring(0, 17);
            mac.setLength(0);
            for (int i = 6; i < 12; i++) mac.append(String.format("%02x:", data[i]));
            String src = mac.substring(0, 17);
            int etype = ((data[12] & 0xFF) << 8) | (data[13] & 0xFF);
            etherInfo = String.format("dst=%s src=%s etherType=0x%04x", dst, src, etype);
        }
        // Raw hex of first 16 bytes for comparison with PKT-IN RAW-HDR
        StringBuilder hex = new StringBuilder();
        for (int i = 0; i < Math.min(16, dataLen); i++) {
            hex.append(String.format("%02x ", data[i]));
        }
        log.info("PKT-OUT device={} port={} dataRemaining={} arrayLen={} {} firstBytes=[{}]",
                handler().data().deviceId(), outPort.toLong(), dataLen, data.length,
                etherInfo, hex.toString().trim());

        ImmutableByteSequence portVal;
        try {
            portVal = ImmutableByteSequence.copyFrom(outPort.toLong()).fit(PORT_BITWIDTH);
        } catch (ImmutableByteSequence.ByteSequenceTrimException e) {
            throw new PiInterpreterException("Failed to encode egress_port: " + e.getMessage());
        }

        PiPacketMetadata egressMeta = PiPacketMetadata.builder()
                .withId(PiPacketMetadataId.of("egress_port"))
                .withValue(portVal)
                .build();

        PiPacketOperation op = PiPacketOperation.builder()
                .withType(PiPacketOperationType.PACKET_OUT)
                .withData(ImmutableByteSequence.copyFrom(packet.data()))
                .withMetadata(egressMeta)
                .build();

        return List.of(op);
    }

    @Override
    public InboundPacket mapInboundPacket(PiPacketOperation operation, DeviceId deviceId)
            throws PiInterpreterException {
        // Read ingress_port from PiPacketMetadata
        PortNumber inPort = null;
        for (PiPacketMetadata m : operation.metadatas()) {
            if (m.id().equals(PiPacketMetadataId.of("ingress_port"))) {
                ByteBuffer buf = m.value().asReadOnlyBuffer();
                long port = 0;
                while (buf.hasRemaining()) {
                    port = (port << 8) | (buf.get() & 0xFF);
                }
                inPort = PortNumber.portNumber(port);
                break;
            }
        }
        if (inPort == null) {
            throw new PiInterpreterException(
                    "Missing ingress_port metadata in packet-in — "
                    + "check P4Info controller_packet_metadata mapping");
        }

        // Parse Ethernet from payload
        byte[] payload = operation.data().asArray();

        // Debug: print first 20 bytes to find Ethernet header offset
        StringBuilder hex = new StringBuilder();
        for (int i = 0; i < Math.min(24, payload.length); i++) {
            hex.append(String.format("%02x ", payload[i]));
        }
        log.info("RAW-HDR device={} port={} firstBytes=[{}]", deviceId, inPort.toLong(), hex.toString().trim());

        Ethernet ethPkt;
        try {
            ethPkt = Ethernet.deserializer().deserialize(payload, 0, payload.length);
        } catch (Exception e) {
            throw new PiInterpreterException(
                    "Failed to deserialize Ethernet from packet-in: " + e.getMessage());
        }

        // LLDP self-loop diagnostic: extract chassisId, portId from TLV
        if (ethPkt.getEtherType() == 0x88CC) {
            logLldpInfo(deviceId, inPort, ethPkt, payload);
        } else {
            log.info("PKT-IN {} port={} etherType=0x{} size={} srcMac={} dstMac={}",
                    deviceId, inPort.toLong(),
                    Integer.toHexString(ethPkt.getEtherType() & 0xFFFF),
                    payload.length, ethPkt.getSourceMAC(), ethPkt.getDestinationMAC());
        }

        return new DefaultInboundPacket(
                new ConnectPoint(deviceId, inPort), ethPkt,
                ByteBuffer.wrap(payload));
    }

    /** Log LLDP chassisId, portId, TTL, and MAC to diagnose self-loop storms. */
    private void logLldpInfo(DeviceId deviceId, PortNumber inPort,
                             Ethernet eth, byte[] payload) {
        String srcMac = eth.getSourceMAC().toString();
        String dstMac = eth.getDestinationMAC().toString();
        String chassisId = "?";
        String chassisSubtype = "?";
        String portId = "?";
        String portSubtype = "?";
        int ttl = -1;
        String orgTlv = null;

        // LLDP TLV starts at byte 14 (after Ethernet header)
        int offset = 14;
        while (offset + 2 <= payload.length) {
            int tlvType = (payload[offset] >> 1) & 0x7F;
            int tlvLen  = ((payload[offset] & 0x01) << 8) | (payload[offset + 1] & 0xFF);
            offset += 2;
            if (offset + tlvLen > payload.length) break;
            if (tlvType == 1) { // Chassis ID
                chassisSubtype = String.valueOf(payload[offset] & 0xFF);
                chassisId = tlvValueString(payload, offset, tlvLen);
            } else if (tlvType == 2) { // Port ID
                portSubtype = String.valueOf(payload[offset] & 0xFF);
                portId = tlvValueString(payload, offset, tlvLen);
            } else if (tlvType == 3) { // TTL
                ttl = ((payload[offset] & 0xFF) << 8) | (payload[offset + 1] & 0xFF);
            } else if (tlvType == 127) { // Organizationally Specific
                orgTlv = hexString(payload, offset, tlvLen);
            } else if (tlvType == 0) { // End of LLDPDU
                break;
            }
            offset += tlvLen;
        }

        log.info("LLDP {} port={} srcMac={} dstMac={} chassis(sub={} id={}) port(sub={} id={}) ttl={} orgTlv={}",
                deviceId, inPort.toLong(), srcMac, dstMac,
                chassisSubtype, chassisId, portSubtype, portId, ttl, orgTlv);
    }

    private static String tlvValueString(byte[] data, int offset, int len) {
        if (len <= 1) return "(empty)";
        StringBuilder sb = new StringBuilder();
        for (int i = 1; i < len; i++) {
            byte b = data[offset + i];
            if (b >= 0x20 && b < 0x7F) sb.append((char) b);
            else sb.append(String.format("\\x%02x", b));
        }
        return sb.toString();
    }

    private static String hexString(byte[] data, int offset, int len) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < len && i < 32; i++) {
            sb.append(String.format("%02x", data[offset + i]));
        }
        if (len > 32) sb.append("...");
        return sb.toString();
    }

    @Override
    public Optional<Integer> mapLogicalPortNumber(org.onosproject.net.PortNumber port) {
        return Optional.of((int) port.toLong());
    }
}

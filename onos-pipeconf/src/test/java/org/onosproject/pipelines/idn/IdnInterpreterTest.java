package org.onosproject.pipelines.idn;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import java.nio.ByteBuffer;
import java.util.Collection;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.onlab.packet.Ethernet;
import org.onlab.util.ImmutableByteSequence;
import org.onosproject.net.ConnectPoint;
import org.onosproject.net.DeviceId;
import org.onosproject.net.PortNumber;
import org.onosproject.net.driver.DriverData;
import org.onosproject.net.driver.DriverHandler;
import org.onosproject.net.flow.TrafficTreatment;
import org.onosproject.net.flow.instructions.Instruction;
import org.onosproject.net.flow.instructions.Instructions;
import org.onosproject.net.packet.DefaultInboundPacket;
import org.onosproject.net.packet.InboundPacket;
import org.onosproject.net.packet.OutboundPacket;
import org.onosproject.net.pi.model.PiPacketMetadataId;
import org.onosproject.net.pi.model.PiPacketOperationType;
import org.onosproject.net.pi.model.PiPipelineInterpreter.PiInterpreterException;
import org.onosproject.net.pi.model.PiTableId;
import org.onosproject.net.pi.runtime.PiPacketMetadata;
import org.onosproject.net.pi.runtime.PiPacketOperation;

class IdnInterpreterTest {

    private IdnInterpreter interpreter;

    @BeforeEach
    void setUp() {
        interpreter = new IdnInterpreter();
        DriverHandler handler = mock(DriverHandler.class);
        DriverData data = mock(DriverData.class);
        when(data.deviceId()).thenReturn(DeviceId.deviceId("device:bmv2:1"));
        when(handler.data()).thenReturn(data);
        interpreter.setHandler(handler);
    }

    @Test
    void mapFlowRuleTableId_shouldMapP4InfoTableIds() {
        assertEquals(
                PiTableId.of(IdnInterpreter.TABLE_IPV4_LPM),
                interpreter.mapFlowRuleTableId(P4InfoConstants.TABLE_ID_MyIngress_ipv4_routing).orElseThrow());
        assertEquals(
                PiTableId.of(IdnInterpreter.TABLE_METER_POLICY),
                interpreter.mapFlowRuleTableId(P4InfoConstants.TABLE_ID_MyIngress_sla_enforcement).orElseThrow());
        assertEquals(
                PiTableId.of(IdnInterpreter.TABLE_ARP_PROXY),
                interpreter.mapFlowRuleTableId(P4InfoConstants.TABLE_ID_MyIngress_arp_proxy).orElseThrow());
        assertEquals(
                PiTableId.of(IdnInterpreter.TABLE_BANDWIDTH_POLICY),
                interpreter.mapFlowRuleTableId(P4InfoConstants.TABLE_ID_MyIngress_bandwidth_policy).orElseThrow());
        assertEquals(
                PiTableId.of(IdnInterpreter.TABLE_INT_SWITCH_CONFIG),
                interpreter.mapFlowRuleTableId(P4InfoConstants.TABLE_ID_MyEgress_int_switch_config).orElseThrow());
        assertTrue(interpreter.mapFlowRuleTableId(123).isEmpty());
    }

    @Test
    void mapTreatment_shouldRejectOutputOnlyIpv4Action() {
        TrafficTreatment treatment = mock(TrafficTreatment.class);
        when(treatment.allInstructions())
                .thenReturn(List.of(Instructions.createOutput(PortNumber.portNumber(2))));

        PiInterpreterException error = assertThrows(
                PiInterpreterException.class,
                () -> interpreter.mapTreatment(
                        treatment, PiTableId.of(IdnInterpreter.TABLE_IPV4_LPM)));
        assertTrue(error.getMessage().contains("srcMac"));
        assertTrue(error.getMessage().contains("dstMac"));
    }

    // ──────────────────────────────────────────────────────
    // mapOutboundPacket tests
    // ──────────────────────────────────────────────────────

    @Test
    void mapOutboundPacket_shouldReturnPacketOutWithCorrectEgressPort() throws Exception {
        byte[] ethernetBytes = new byte[] {
            (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff,
            0x00, 0x01, 0x02, 0x03, 0x04, 0x05,
            (byte) 0x88, (byte) 0xcc
        };

        PortNumber outPort = PortNumber.portNumber(3);
        TrafficTreatment treatment = mock(TrafficTreatment.class);
        when(treatment.allInstructions())
                .thenReturn(List.of(Instructions.createOutput(outPort)));

        OutboundPacket outboundPacket = new org.onosproject.net.packet.DefaultOutboundPacket(
                DeviceId.deviceId("device:bmv2:1"), treatment, ByteBuffer.wrap(ethernetBytes));

        Collection<PiPacketOperation> result = interpreter.mapOutboundPacket(outboundPacket);

        assertEquals(1, result.size());
        PiPacketOperation op = result.iterator().next();
        assertEquals(PiPacketOperationType.PACKET_OUT, op.type());

        PiPacketMetadata egressMeta = op.metadatas().stream()
                .filter(m -> m.id().equals(PiPacketMetadataId.of("egress_port")))
                .findFirst()
                .orElseThrow(() -> new AssertionError("Missing egress_port metadata"));

        byte[] expectedMeta = ImmutableByteSequence.copyFrom(3).fit(32).asArray();
        assertArrayEquals(expectedMeta, egressMeta.value().asArray());

        byte[] allData = op.data().asArray();
        assertArrayEquals(ethernetBytes, allData);
    }

    @Test
    void mapOutboundPacket_shouldHandleLowPortNumber() throws Exception {
        byte[] ethernetBytes = new byte[] { 0x00, 0x01, 0x02, 0x03 };

        PortNumber outPort = PortNumber.portNumber(1);
        TrafficTreatment treatment = mock(TrafficTreatment.class);
        when(treatment.allInstructions())
                .thenReturn(List.of(Instructions.createOutput(outPort)));

        OutboundPacket pkt = new org.onosproject.net.packet.DefaultOutboundPacket(
                DeviceId.deviceId("device:bmv2:1"), treatment, ByteBuffer.wrap(ethernetBytes));

        Collection<PiPacketOperation> result = interpreter.mapOutboundPacket(pkt);
        PiPacketOperation op = result.iterator().next();

        PiPacketMetadata egressMeta = op.metadatas().stream()
                .filter(m -> m.id().equals(PiPacketMetadataId.of("egress_port")))
                .findFirst()
                .orElseThrow();

        byte[] expectedMeta = ImmutableByteSequence.copyFrom(1).fit(32).asArray();
        assertArrayEquals(expectedMeta, egressMeta.value().asArray());
        assertArrayEquals(ethernetBytes, op.data().asArray());
    }

    @Test
    void mapOutboundPacket_shouldHandleHighPortNumber() throws Exception {
        byte[] ethernetBytes = new byte[] { 0x00, 0x01, 0x02, 0x03 };

        PortNumber outPort = PortNumber.portNumber(256);
        TrafficTreatment treatment = mock(TrafficTreatment.class);
        when(treatment.allInstructions())
                .thenReturn(List.of(Instructions.createOutput(outPort)));

        OutboundPacket pkt = new org.onosproject.net.packet.DefaultOutboundPacket(
                DeviceId.deviceId("device:bmv2:1"), treatment, ByteBuffer.wrap(ethernetBytes));

        Collection<PiPacketOperation> result = interpreter.mapOutboundPacket(pkt);
        PiPacketOperation op = result.iterator().next();

        PiPacketMetadata egressMeta = op.metadatas().stream()
                .filter(m -> m.id().equals(PiPacketMetadataId.of("egress_port")))
                .findFirst()
                .orElseThrow();

        byte[] expectedMeta = ImmutableByteSequence.copyFrom(256).fit(32).asArray();
        assertArrayEquals(expectedMeta, egressMeta.value().asArray());
        assertArrayEquals(ethernetBytes, op.data().asArray());
    }

    // ──────────────────────────────────────────────────────
    // mapInboundPacket tests
    // ──────────────────────────────────────────────────────

    @Test
    void mapInboundPacket_shouldExtractIngressPortAndEthernet() throws Exception {
        // 14-byte Ethernet header + 4-byte dummy payload so deserializer has room
        byte[] ethernetBytes = new byte[] {
            (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff,
            0x00, 0x01, 0x02, 0x03, 0x04, 0x05,
            (byte) 0x88, (byte) 0xcc,
            0x00, 0x00, 0x00, 0x00
        };

        PiPacketMetadata ingressMeta = PiPacketMetadata.builder()
                .withId(PiPacketMetadataId.of("ingress_port"))
                .withValue(ImmutableByteSequence.copyFrom(5).fit(32))
                .build();

        PiPacketOperation pktOp = PiPacketOperation.builder()
                .withType(PiPacketOperationType.PACKET_IN)
                .withData(ImmutableByteSequence.copyFrom(ethernetBytes))
                .withMetadata(ingressMeta)
                .build();

        DeviceId deviceId = DeviceId.deviceId("device:bmv2:1");

        InboundPacket result = interpreter.mapInboundPacket(pktOp, deviceId);

        assertEquals(deviceId, result.receivedFrom().deviceId());
        assertEquals(PortNumber.portNumber(5), result.receivedFrom().port());

        Ethernet eth = result.parsed();
        assertNotNull(eth);
        assertEquals((short) 0x88CC, eth.getEtherType());
    }

    @Test
    void mapInboundPacket_shouldExtractPortZeroCorrectly() throws Exception {
        byte[] ethernetBytes = new byte[] {
            (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff,
            0x00, 0x01, 0x02, 0x03, 0x04, 0x05,
            (byte) 0x88, (byte) 0xcc,
            0x00, 0x00, 0x00, 0x00
        };

        PiPacketMetadata ingressMeta = PiPacketMetadata.builder()
                .withId(PiPacketMetadataId.of("ingress_port"))
                .withValue(ImmutableByteSequence.copyFrom(0).fit(32))
                .build();

        PiPacketOperation pktOp = PiPacketOperation.builder()
                .withType(PiPacketOperationType.PACKET_IN)
                .withData(ImmutableByteSequence.copyFrom(ethernetBytes))
                .withMetadata(ingressMeta)
                .build();

        DeviceId deviceId = DeviceId.deviceId("device:bmv2:s_core");

        InboundPacket result = interpreter.mapInboundPacket(pktOp, deviceId);

        assertEquals(deviceId, result.receivedFrom().deviceId());
        assertEquals(PortNumber.portNumber(0), result.receivedFrom().port());
        assertNotNull(result.parsed());
    }
}

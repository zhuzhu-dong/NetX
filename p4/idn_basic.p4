/* -*- P4_16 -*- */
#include <core.p4>
#include <v1model.p4>
#include "int_schema.p4"

/* ── 常量定义 ── */
const bit<16> ETHERTYPE_IPV4   = 0x0800;
const bit<16> ETHERTYPE_ARP  = 0x0806;
const bit<16> ETHERTYPE_IPV6 = 0x86DD;
const bit<16> ETHERTYPE_LLDP = 0x88CC;
const bit<9>  CPU_PORT         = 255;

/* ── 报头定义 ── */
header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

// Agent 意图报头 (承载于以太网与 IPv4 之间)
header intent_t {
    bit<8>  intent_type;    // 意图类型 (例如: 1-低延迟, 2-高吞吐, 3-高安全)
    bit<8>  priority;       // 意图优先级
    bit<8>  telemetry_flag; // 是否开启 INT 遥测 (1-开启, 0-关闭)
    bit<8>  next_proto;     // 下层协议类型 (通常为 IPv4)
}

// INT 基础报头
header int_header_t {
    bit<16> magic;          // 0x4944, rejects heuristic/legacy decoding
    bit<8>  version;        // canonical schema version
    bit<8>  hop_count;      // number of serialized hop records
    bit<8>  max_hops;       // maximum records allowed in this packet
    bit<8>  flags;          // packet-level validity/status bitmap
}

// INT v2 hop record. Widths and units are defined by config/int_schema.json.
header int_hop_t {
    bit<32> switch_id;
    bit<16> ingress_port;
    bit<16> egress_port;
    bit<48> ingress_timestamp; // microseconds (v1model global timestamp)
    bit<48> egress_timestamp;  // microseconds (v1model global timestamp)
    bit<32> queue_occupancy;   // packets at dequeue
    bit<16> flags;
}

// ARP 报头 (硬件/协议地址长度固定为 6/4)
header arp_t {
    bit<16> hwType;        // 硬件类型 (1=Ethernet)
    bit<16> protoType;     // 协议类型 (0x0800=IPv4)
    bit<8>  hwAddrLen;    // 硬件地址长度 (6)
    bit<8>  protoAddrLen; // 协议地址长度 (4)
    bit<16> opcode;        // 操作码 (1=Request, 2=Reply)
    bit<48> senderHwAddr;   // 发送方 MAC
    bit<32> senderProtoAddr; // 发送方 IP
    bit<48> targetHwAddr;   // 目标 MAC
    bit<32> targetProtoAddr; // 目标 IP
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> length;
    bit<16> checksum;
}

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
    bit<3>  reserved;
    bit<3>  ecn;
    bit<6>  tcpFlags;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgentPtr;
}

// 控制器交互报头
@controller_header("packet_in")
header packet_in_header_t {
    bit<32> ingress_port;
}

@controller_header("packet_out")
header packet_out_header_t {
    bit<32> egress_port;
}

/* ── 结构体定义 ── */
struct headers {
    packet_in_header_t  packet_in;
    packet_out_header_t packet_out;
    ethernet_t          ethernet;
    arp_t               arp;          // ARP 代理
    intent_t            intent;       // 意图层
    int_header_t        int_header;
    int_hop_t[10]       int_hops;     // 声明为报头栈 (Header Stack)
    ipv4_t              ipv4;
    udp_t               udp;
    tcp_t               tcp;
}

// 用于管线内传递状态的元数据
struct metadata {
    bit<32> meter_color;        // 0-Green, 1-Yellow, 2-Red  (PACKETS meter for SLA)
    bit<32> meter_color_bytes;  // 0-Green, 1-Yellow, 2-Red  (BYTES meter for bandwidth)
    bit<1>  intent_violation;   // SLA 违约标志
    bit<8>  int_hop_rem;
    bit<32> arp_tmp;            // ARP reply swap temp
    bit<2>  ipv4_action;        // 0=miss, 1=set_nhop(forward), 2=drop_packet(blackhole)
    bit<1>  diag_target;        // diagnostic-only target selector marker
}

/* ── Parser (解析器) ── */
parser MyParser(packet_in packet,
                out headers hdr,
                inout metadata meta,
                inout standard_metadata_t standard_metadata) {

    state start {
        transition select(standard_metadata.ingress_port) {
            CPU_PORT: parse_packet_out;
            default: parse_ethernet;
        }
    }

    state parse_packet_out {
        packet.extract(hdr.packet_out);
        transition parse_ethernet;
    }

    state parse_ethernet {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_IPV4: parse_ipv4;
            ETHERTYPE_INTENT: parse_intent; // 引导解析意图报文
            ETHERTYPE_ARP:    parse_arp;         // 解析 ARP 以支持代理应答
            ETHERTYPE_LLDP:   accept;            // 认出是LLDP，直接允许进入管线
            default: accept;
        }
    }

    // 解析意图与 INT 结构
    state parse_intent {
        packet.extract(hdr.intent);
        transition select(hdr.intent.telemetry_flag) {
            1: parse_int_header;
            default: parse_intent_next;
        }
    }

    state parse_int_header {
        packet.extract(hdr.int_header);
        // 将报头里记录的实际跳数赋值给元数据计数器
        meta.int_hop_rem = hdr.int_header.hop_count;

        // 检查是否为 0，如果为 0 直接跳出，否则进入循环解析
        transition select(meta.int_hop_rem) {
            0: parse_intent_next;
            1 .. INT_MAX_HOPS: parse_int_hop_loop;
            default: accept;
        }
    }

    // Parser 循环状态机
    state parse_int_hop_loop {
        // 使用 .next 逐个解出栈中的基础报头元素
        packet.extract(hdr.int_hops.next);
        // 计数器减 1
        meta.int_hop_rem = meta.int_hop_rem - 1;

        // 判定是否继续循环
        transition select(meta.int_hop_rem) {
            0: parse_intent_next;       // 减到 0 了，大功告成，解析下一层协议
            default: parse_int_hop_loop; // 没减完，继续循环轨道
        }
    }
    state parse_intent_next {
        transition select(hdr.intent.next_proto) {
            0x08: parse_ipv4; // 假设 0x08 代表 IPv4
            default: accept;
        }
    }

    state parse_arp {
        packet.extract(hdr.arp);
        transition accept;
    }

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            6: parse_tcp;
            17: parse_udp;
            default: accept;
        }
    }

    state parse_udp {
        packet.extract(hdr.udp);
        transition accept;
    }

    state parse_tcp {
        packet.extract(hdr.tcp);
        transition accept;
    }
}

/* ── Checksum Verification ── */
control MyVerifyChecksum(inout headers hdr, inout metadata meta) {
    apply {
        // 实际硬件中需要校验 IPv4 Checksum，此处简化
    }
}

/* ── Ingress Pipeline (入站管线) ── */
control MyIngress(inout headers hdr,
                  inout metadata meta,
                  inout standard_metadata_t standard_metadata) {

    // Diagnostic counters are observational only. Index 0 represents the
    // current target selector (192.168.10.10 -> 10.0.0.10 UDP/5004).
    // They do not participate in matching, action selection or forwarding.
    counter(1, CounterType.packets_and_bytes) diag_ipv4_udp_parsed;
    counter(1, CounterType.packets_and_bytes) diag_target_five_tuple_seen;
    counter(1, CounterType.packets_and_bytes) diag_target_classifier_hit;
    counter(1, CounterType.packets_and_bytes) diag_set_dscp_called;
    counter(1, CounterType.packets_and_bytes) diag_ingress_forwarded;
    counter(1, CounterType.packets_and_bytes) diag_ingress_dropped;
    counter(1, CounterType.packets_and_bytes) diag_invalid_egress_spec;

    // 动作：基础 IPv4 转发（含 L2 MAC 重写以支持多跳正确交付）
    // TTL underflow protection is handled in the apply block before routing.
    action set_nhop(bit<9> port, bit<48> srcMac, bit<48> dstMac) {
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
        standard_metadata.egress_spec = port;
        hdr.ethernet.srcAddr = srcMac;
        hdr.ethernet.dstAddr = dstMac;
        meta.ipv4_action = 1;  // routed: forward
    }

    // 动作：丢弃数据包 (LPM blackhole route 或 ACL deny)
    action drop_packet() {
        mark_to_drop(standard_metadata);
        meta.ipv4_action = 2;  // routed: blackhole (skip ACL)
    }

    // 动作：ACL 拒绝 (post-routing deny)
    action acl_deny() {
        mark_to_drop(standard_metadata);
    }

    // 动作：L2 目的 MAC 重写 — 解决最后一跳交付问题。
    // 控制面填入 host IP→MAC 映射，数据面在路由后自动改写，
    // 无需在 set_nhop 中嵌入主机 MAC。
    action rewrite_dst_mac(bit<48> mac) {
        hdr.ethernet.dstAddr = mac;
    }

    // 动作：Meter 读取与标记 (PACKETS — SLA 意图合规检测)
    direct_meter<bit<32>>(MeterType.packets) host_meter;
    action apply_meter_policy() {
        host_meter.read(meta.meter_color);
    }

    // 动作：Bandwidth Meter (BYTES — 带宽限速 QoS)
    direct_meter<bit<32>>(MeterType.bytes) host_meter_bytes;
    action apply_meter_bytes_policy() {
        host_meter_bytes.read(meta.meter_color_bytes);
    }

    // 动作：DSCP 标记 — 用于 SET_PRIORITY QoS 策略
    action set_dscp(bit<6> dscp_value) {
        // The counter is action-scoped. Diagnostic test windows use a single
        // target set_dscp entry, so its delta is selector-specific.
        diag_set_dscp_called.count(0);
        // IPv4 diffserv contains DSCP in the high six bits and ECN in the
        // low two bits.  Preserve ECN instead of writing the raw DSCP value
        // into the full TOS byte.
        hdr.ipv4.diffserv = (((bit<8>)dscp_value) << 2) | (hdr.ipv4.diffserv & 0x03);
    }

    // Counts packets and bytes that hit the business classifier.  ONOS can
    // read this direct counter together with the owning flow entry.
    direct_counter(CounterType.packets_and_bytes) qos_classifier_counter;

    // 寄存器：记录全局意图违约次数，供 Agent 轮询读取
    register<bit<32>>(1024) violation_counter;
    action record_violation(bit<32> flow_idx) {
        bit<32> current_count;
        violation_counter.read(current_count, flow_idx);
        violation_counter.write(flow_idx, current_count + 1);
        meta.intent_violation = 1;
    }

    /* ── 表定义 ── */
    // Table 1 (in apply order): LLDP → handled inline in apply block (packet-in to CPU)

    // Table 2: ARP 代理表
    // 控制面主动下发 IP→MAC 映射，数据面直接应答 ARP 请求。
    // 未命中时 inline punt 已预置 packet_in + CPU_PORT，default NoAction 不覆盖。
    action arp_reply(bit<48> target_mac) {
        // Build ARP reply: save original targetProtoAddr BEFORE overwriting it.
        // Request:  sender=10.0.0.10, target=10.0.0.1
        // Reply:    sender=10.0.0.1@target_mac,  target=10.0.0.10@original_sender_HW
        meta.arp_tmp = hdr.arp.targetProtoAddr;      // save gateway IP (10.0.0.1)
        hdr.arp.targetHwAddr = hdr.arp.senderHwAddr; // requestor MAC → reply target HW
        hdr.arp.targetProtoAddr = hdr.arp.senderProtoAddr; // requestor IP → reply target IP
        hdr.arp.senderHwAddr = target_mac;           // gateway MAC
        hdr.arp.senderProtoAddr = meta.arp_tmp;      // gateway IP (the saved target)
        hdr.arp.opcode = 2; // Reply
        hdr.ethernet.dstAddr = hdr.ethernet.srcAddr;
        hdr.ethernet.srcAddr = target_mac;
        standard_metadata.egress_spec = standard_metadata.ingress_port;
        hdr.packet_in.setInvalid();  // suppress spurious packet-in to controller
    }

    action punt_arp() {
        hdr.packet_in.setValid();
        hdr.packet_in.ingress_port = (bit<32>)standard_metadata.ingress_port;
        standard_metadata.egress_spec = CPU_PORT;
    }

    table arp_proxy {
        key = { hdr.arp.targetProtoAddr: exact; }
        actions = { arp_reply; punt_arp; NoAction; }
        size = 256;
        const default_action = NoAction(); // miss → inline punt already set CPU_PORT + packet_in
    }

    // Table 3: IPv4 LPM 路由表 (ECMP)
    // 每条 LPM 前缀可指向多个下一跳 (ECMP)。
    // 控制面写入 member (set_nhop + port) → group (member 集合 + 权重) → table entry (引用 group)。
    // 数据面根据五元组 hash 从 group 中选 member，决定转发端口。
    table ipv4_routing {
        key = { hdr.ipv4.dstAddr: lpm; }
        actions = { set_nhop; drop_packet; NoAction; }
        size = 1024;
        default_action = NoAction();
    }

    // Table 4: SLA 策略表 (QoS/限速闭环) — 路由前执行
    // 匹配字段使用 ternary 支持通配：一条全零规则即可匹配所有流量。
    // intent_type 设为 optional — 无 intent 头时不参与匹配 (等价于 wildcard)。
    table sla_enforcement {
        key = {
            hdr.ipv4.srcAddr: ternary;
            hdr.ipv4.dstAddr: ternary;
            hdr.ipv4.protocol: ternary;
            hdr.udp.dstPort: ternary;
            hdr.tcp.dstPort: ternary;
        }
        actions = {
            apply_meter_policy;          // PACKETS meter — 意图 SLA
            set_dscp;                    // DSCP 标记  — 优先级策略
            NoAction;
        }
        meters = host_meter;
        counters = qos_classifier_counter;
        size = 1024;
        default_action = NoAction();
    }

    // Table 4.5: 带宽限速表 (BYTES meter) — SLA 之后、路由之前执行
    // 独立建表以挂载 BYTES-unit direct meter，对超过带宽上限的流量降级或丢弃。
    table bandwidth_policy {
        key = {
            hdr.ipv4.srcAddr: ternary;
            hdr.ipv4.dstAddr: ternary;
        }
        actions = {
            apply_meter_bytes_policy;   // 读取 BYTES meter → meta.meter_color_bytes
            NoAction;
        }
        meters = host_meter_bytes;
        size = 512;
        default_action = NoAction();
    }

    // Table 4.6: 主机 MAC 解析表 — 路由后自动改写 L2 目的 MAC。
    // 匹配目的 IP（LPM），命中则用 rewrite_dst_mac 覆写 dst MAC。
    // 控制面为每个主机 IP 下发一条 /32 条目，数据面在路由决策后自动执行。
    table host_mac_resolution {
        key = { hdr.ipv4.dstAddr: lpm; }
        actions = { rewrite_dst_mac; NoAction; }
        size = 256;
        default_action = NoAction();
    }

    // Table 5: ACL 访问控制表 (post-routing security gate)
    // 在路由决策之后执行，作为最后一道安全检查关卡。
    // 匹配字段使用 ternary 以支持灵活的 subnet/host/protocol 过滤规则。
    // NoAction = allow (默认允许), acl_deny = 显式拒绝。
    table acl {
        key = {
            hdr.ipv4.srcAddr: ternary;
            hdr.ipv4.dstAddr: ternary;
            hdr.ipv4.protocol: ternary;
            standard_metadata.ingress_port: ternary;
        }
        actions = { NoAction; acl_deny; }
        const default_action = NoAction();  // allow by default
        size = 256;
    }

    apply {
        // ── 控制器 Packet-Out 逻辑 ──
        if (standard_metadata.ingress_port == CPU_PORT) {
            hdr.packet_in.setInvalid();
            if (hdr.packet_out.isValid() && hdr.packet_out.egress_port != 0) {
                standard_metadata.egress_spec = (bit<9>)hdr.packet_out.egress_port;
                hdr.packet_out.setInvalid();
            }
            return;
        }

        // ── LLDP: 上送控制器用于拓扑发现 ──
        if (hdr.ethernet.etherType == ETHERTYPE_LLDP) {
            hdr.packet_in.setValid();
            hdr.packet_in.ingress_port = (bit<32>)standard_metadata.ingress_port;
            standard_metadata.egress_spec = CPU_PORT;
            return;
        }

        // ── ARP Proxy: 内联预置 punt，命中则代理应答覆盖 ──
        if (hdr.arp.isValid() && hdr.arp.opcode == 1) {
            hdr.packet_in.setValid();
            hdr.packet_in.ingress_port = (bit<32>)standard_metadata.ingress_port;
            standard_metadata.egress_spec = CPU_PORT;
            // hit → arp_reply 覆写 egress_spec 为 ingress_port 并清除 packet_in
            // miss → inline punt (CPU_PORT + packet_in) 保持不变
            arp_proxy.apply();
            return;
        }

        // ── IPv4 处理管线 ──
        if (hdr.ipv4.isValid()) {
            if (hdr.udp.isValid()) {
                diag_ipv4_udp_parsed.count(0);
                if (hdr.ipv4.srcAddr == 0xc0a80a0a &&
                    hdr.ipv4.dstAddr == 0x0a00000a &&
                    hdr.ipv4.protocol == 17 &&
                    hdr.udp.dstPort == 5004) {
                    meta.diag_target = 1;
                    diag_target_five_tuple_seen.count(0);
                }
            }
            // 0. INT 遥测触发：IP 协议号 253 → 视为 INT 探测包
            if (hdr.ipv4.protocol == INT_IP_PROTOCOL && !hdr.intent.isValid()) {
                hdr.intent.setValid();
                hdr.intent.intent_type = 0;
                hdr.intent.priority = 0;
                hdr.intent.telemetry_flag = 1;
                hdr.intent.next_proto = 0x08;  // encapsulated protocol is IPv4
                hdr.ethernet.etherType = ETHERTYPE_INTENT;
                hdr.int_header.setValid();
                hdr.int_header.magic = IDN_INT_MAGIC;
                hdr.int_header.version = IDN_INT_VERSION;
                hdr.int_header.hop_count = 0;
                hdr.int_header.max_hops = INT_MAX_HOPS;
                hdr.int_header.flags = 0;
            }

            // 1. SLA 策略执行 (QoS/限速) — 对所有 IPv4 流量生效
            if (sla_enforcement.apply().hit && meta.diag_target == 1) {
                diag_target_classifier_hit.count(0);
            }

            // 闭环控制：触碰红线 (meter RED) → 记录违规并丢弃
            if (meta.meter_color == 2) {
                if (meta.diag_target == 1) {
                    diag_ingress_dropped.count(0);
                }
                record_violation(0);
                drop_packet();
                return; // 终止后续处理
            }
            // 黄线 (meter YELLOW) → DSCP 降级标记为 AF11
            if (meta.meter_color == 1) {
                hdr.ipv4.diffserv = 10;
            }

            // 1.5. Bandwidth 限速策略 (BYTES meter) — SLA 之后、路由之前
            bandwidth_policy.apply();
            if (meta.meter_color_bytes == 2) {
                if (meta.diag_target == 1) {
                    diag_ingress_dropped.count(0);
                }
                drop_packet();
                return; // 带宽超限 → 丢弃
            }
            if (meta.meter_color_bytes == 1) {
                hdr.ipv4.diffserv = 10; // 带宽黄线 → DSCP 降级
            }

            if (hdr.ipv4.ttl <= 1) {
                    // TTL 过期，上送控制器或直接丢弃 (标准做法是回送 ICMP Time Exceeded)
                    hdr.packet_in.setValid();
                    hdr.packet_in.ingress_port = (bit<32>)standard_metadata.ingress_port;
                    standard_metadata.egress_spec = CPU_PORT;
                    return; 
                }

            // 2. 基础路由转发 (LPM + ECMP)
            ipv4_routing.apply();

            // 3. 路由未命中 → PacketIn 上送控制器
            if (meta.ipv4_action == 0) {
                hdr.packet_in.setValid();
                hdr.packet_in.ingress_port = (bit<32>)standard_metadata.ingress_port;
                standard_metadata.egress_spec = CPU_PORT;
                return;
            }

            // 4. 黑洞路由 → 跳过 ACL，直接丢弃
            if (meta.ipv4_action == 2) {
                if (meta.diag_target == 1) {
                    diag_ingress_dropped.count(0);
                }
                return;
            }

            // 5. 主机 MAC 解析 — 路由决策后，自动将 dst MAC 改写为目的主机真实 MAC。
            //    控制面按 host IP→MAC 下发 /32 条目，数据面 LPM 匹配后自动改写。
            host_mac_resolution.apply();

            // 6. ACL 安全检查 (最后一道关卡)
            acl.apply();
            if (meta.diag_target == 1) {
                if (standard_metadata.egress_spec == 0 ||
                    standard_metadata.egress_spec == 511) {
                    diag_ingress_dropped.count(0);
                    diag_invalid_egress_spec.count(0);
                } else {
                    diag_ingress_forwarded.count(0);
                }
            }
        } else {
            // 本拓扑是显式 IPv4-only。不要把 IPv6 NDP 从交换机互联口
            // 上送 HostProvider，否则每次 veth 重建都会产生新的伪 Host。
            if (hdr.ethernet.etherType == ETHERTYPE_IPV6) {
                mark_to_drop(standard_metadata);
            } else {
                // 其他控制报文仍保留 Packet-In，且携带 32-bit ingress_port。
                hdr.packet_in.setValid();
                hdr.packet_in.ingress_port = (bit<32>)standard_metadata.ingress_port;
                standard_metadata.egress_spec = CPU_PORT;
            }
        }
    }
}

/* ── Egress Pipeline (出站管线 - 处理 INT 与遥测) ── */
control MyEgress(inout headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t standard_metadata) {

    counter(1, CounterType.packets_and_bytes) diag_egress_entered;
    counter(1, CounterType.packets_and_bytes) diag_egress_emitted;

    // 动作：压入当前交换机的遥测数据
    action push_int_telemetry(bit<32> switch_id) {
        // 利用 push_front 在栈顶压入新的 header
        hdr.int_hops.push_front(1);
        // push_front() leaves the new stack element invalid. Field writes do
        // not make it valid, so explicitly include this hop in the deparser.
        hdr.int_hops[0].setValid();

        // 填充当前节点的遥测信息 (在 Egress 阶段可以获取出队队列深度)
        hdr.int_hops[0].switch_id = switch_id;
        hdr.int_hops[0].ingress_port = (bit<16>)standard_metadata.ingress_port;
        hdr.int_hops[0].egress_port = (bit<16>)standard_metadata.egress_port;
        hdr.int_hops[0].ingress_timestamp = standard_metadata.ingress_global_timestamp;
        hdr.int_hops[0].egress_timestamp = standard_metadata.egress_global_timestamp;
        hdr.int_hops[0].queue_occupancy = (bit<32>)standard_metadata.deq_qdepth;
        hdr.int_hops[0].flags = IDN_INT_HOP_VALID;

        // 更新跳数
        hdr.int_header.hop_count = hdr.int_header.hop_count + 1;
    }

    // One explicit entry per switch egress port binds the per-device numeric
    // identity. Missing entries produce no record and are repaired by the
    // reconciler; switch_id=0/topology inference is never accepted as evidence.
    table int_switch_config {
        key = { standard_metadata.egress_port: exact; }
        actions = { push_int_telemetry; NoAction; }
        size = 64;
        default_action = NoAction();
    }

    apply {
        if (meta.diag_target == 1) {
            diag_egress_entered.count(0);
        }
        // INT 遥测：当 intent header 有效且 telemetry_flag=1 时，
        // 每台交换机在出方向压入逐跳遥测数据（无需控制面下发流表）
        if (hdr.intent.isValid() && hdr.intent.telemetry_flag == 1 &&
            hdr.int_header.isValid() &&
            hdr.int_header.magic == IDN_INT_MAGIC &&
            hdr.int_header.version == IDN_INT_VERSION) {
            if (hdr.int_header.hop_count < hdr.int_header.max_hops &&
                hdr.int_header.hop_count < INT_MAX_HOPS) {
                int_switch_config.apply();
            }
        }
        if (meta.diag_target == 1) {
            diag_egress_emitted.count(0);
        }
    }
}

/* ── Checksum Computation (重新计算校验和) ── */
control MyComputeChecksum(inout headers hdr, inout metadata meta) {
    apply {
        // TTL 被修改后，必须更新 IPv4 Checksum 否则数据包会被网卡丢弃
        update_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.version, hdr.ipv4.ihl, hdr.ipv4.diffserv,
              hdr.ipv4.totalLen, hdr.ipv4.identification, hdr.ipv4.flags,
              hdr.ipv4.fragOffset, hdr.ipv4.ttl, hdr.ipv4.protocol,
              hdr.ipv4.srcAddr, hdr.ipv4.dstAddr },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
    }
}

/* ── Deparser (重组器) ── */
control MyDeparser(packet_out packet, in headers hdr) {
    apply {
        // 照协议封装层级顺序 emit，否则会破坏协议结构
        packet.emit(hdr.packet_in);
        packet.emit(hdr.packet_out);
        packet.emit(hdr.ethernet);
        packet.emit(hdr.arp);         // ARP reply
        packet.emit(hdr.intent);      // 发送意图头
        packet.emit(hdr.int_header);  // 发送 INT 基础头
        packet.emit(hdr.int_hops);    // 发送整个 INT 报头栈
        packet.emit(hdr.ipv4);        // 最后发送 IPv4 和 Payload
        packet.emit(hdr.udp);
        packet.emit(hdr.tcp);
    }
}

/* ── Main ── */
V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;

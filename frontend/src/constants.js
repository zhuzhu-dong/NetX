/** 共享常量 */
export const STORAGE_KEYS = {
  /** localStorage key: 本地账号 */
  ACCOUNT: 'netpulse-local-account',
  /** localStorage key: 登录会话 */
  SESSION: 'netpulse-auth-session',
  /** localStorage key: 用户档案 */
  PROFILES: 'netpulse-user-profiles',
  /** localStorage key: 模型选择 */
  MODEL: 'netpulse-selected-model',
  /** localStorage key: 对话缓存 */
  CONV_CACHE: 'netpulse-local-conversation-cache',
  /** localStorage key: 对话标题 */
  CONV_TITLES: 'netpulse-local-conversation-titles',
}

/**
 * 统一的流水线阶段映射 (前后台协议)。
 * 后台 ExecutionStage: sense / parse / check / generate / execute / verify
 * 前端 Pipeline key: S1-S6
 */
export const STAGE_MAP = {
  sense:    { key: 'S1', name: 'Sense',  agent: '网络拓扑感知' },
  parse:    { key: 'S2', name: 'Parse',  agent: '意图约束解析' },
  check:    { key: 'S3', name: 'Check',  agent: '冲突与风险检测' },
  generate: { key: 'S4', name: 'Plan',   agent: '策略生成' },
  execute:  { key: 'S5', name: 'Act',    agent: '流表下发' },
  verify:   { key: 'S6', name: 'Verify', agent: '闭环验证' },
}

/** 从前端 key 查找后台 stage 名称 */
export function stageKeyToName(key) {
  for (const [name, def] of Object.entries(STAGE_MAP)) {
    if (def.key === key) return name
  }
  return null
}

/** 从后台 stage 名称获取前端 key */
export function stageNameToKey(name) {
  return STAGE_MAP[name]?.key || null
}

/** API 超时 (毫秒) */
export const API_TIMEOUT_MS = 30000
/** 提交意图超时 (毫秒) */
export const SUBMIT_TIMEOUT_MS = 15000
/** 乐观更新临时 ID 过期时间 (毫秒) */
export const OPTIMISTIC_TIMEOUT_MS = 30000
/** SSE 心跳超时阈值 (毫秒) — 超过此值无事件则降级为轮询 */
export const SSE_HEARTBEAT_TIMEOUT_MS = 6000
/** SSE watchdog 检查间隔 (毫秒) */
export const SSE_WATCHDOG_INTERVAL_MS = 2000
/** 意图长时间没有阶段变化时，开始向用户展示慢响应提示 */
export const INTENT_SLOW_NOTICE_MS = 12000
/** 意图持续没有阶段变化时，明确提示正在通过轮询确认状态 */
export const INTENT_STALLED_NOTICE_MS = 30000

// ── 网络状态统一映射 (节点 + 链路共用) ──────────────────────────────────────────────

/** 节点/链路状态 → 中文标签 */
export const NETWORK_STATE_LABEL = {
  normal: '在线',
  degraded: '降级',
  failed: '故障',
  attack: '异常',
}

/** 节点/链路状态 → 健康百分比 (0-100) */
export const NETWORK_STATE_HEALTH = {
  normal: 100,
  degraded: 55,
  failed: 15,
  attack: 5,
}

/** 链路角色 → 中文标签 */
export const LINK_ROLE_LABEL = {
  primary: '主链路',
  backup: '备份链路',
  protected: '保护链路',
}

/** 节点类型 → 中文标签 */
export const NODE_TYPE_LABEL = {
  switch: '网络交换机',
  host: '终端主机',
  agent: '智能体核心',
}

/** 欢迎语池 — 每次页面加载随机选取一条，避免千篇一律 */
export const WELCOME_MESSAGES = [
  '我可以把自然语言网络目标转成多智能体执行流程，并在拓扑视图里同步展示节点与链路状态。',
  '输入任意网络意图（带宽保障、流量调度、故障隔离），我会协同多个智能体完成闭环编排。',
  '我能解析网络拓扑、监控实时 KPI，并基于 LLM 多智能体协作生成策略与验证结果。',
  '告诉我你的网络目标 — 保障视频会议、阻断异常流量、优化链路负载 — 我来调度执行。',
  '从感知拓扑到生成策略再到闭环验证，多智能体 STAV 流水线已就绪，输入意图开始。',
]

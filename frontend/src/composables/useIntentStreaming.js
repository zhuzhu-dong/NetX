/**
 * useIntentStreaming.js — SSE + polling dual-channel for intent progress
 *
 * Extracted from intentStore.js.  Owns SSE subscription lifecycle, HTTP
 * polling fallback, heartbeat watchdog — all the transport plumbing.
 * Business-logic side-effects (milestone push, A2A append, token buffer,
 * log emission) are injected as callbacks so the composable stays
 * decoupled from Pinia state.
 *
 * Usage (inside a Pinia store):
 *   const streaming = useIntentStreaming({
 *     onStageChange: (intentId, data) => { ... },
 *     onMilestonePush: (intentId, entry) => { ... },
 *     onA2AAppend: (intentId, message) => { ... },
 *     onThinkingToken: (data) => { ... },
 *     onStreamEnd: () => { ... },
 *     onLog: (msg) => { ... },
 *     onApplyDetail: (data) => record | null,
 *     isTerminalStatus: (status) => boolean,
 *   })
 *   streaming.start(intentId)
 */

import * as api from '../api/client'
import {
  INTENT_SLOW_NOTICE_MS,
  INTENT_STALLED_NOTICE_MS,
  SSE_HEARTBEAT_TIMEOUT_MS,
  SSE_WATCHDOG_INTERVAL_MS,
} from '../constants'

const INTENT_POLL_MS = 2000

export function useIntentStreaming(callbacks = {}) {
  const {
    onStageChange,
    onMilestonePush,
    onA2AAppend,
    onThinkingToken,
    onStreamEnd,
    onLog,
    onApplyDetail,
    onTransportState,
    isTerminalStatus,
  } = callbacks

  const _sseSubs = {}          // cleanup functions per intent
  const _pollTimers = {}       // polling intervals (fallback)
  const _sseHeartbeats = {}    // last SSE event timestamp per intent
  const _sseReconnecting = new Set() // intents currently in SSE reconnect

  /** Whether SSE is in a reconnecting state for the given intent. */
  function isReconnecting(intentId) {
    return _sseReconnecting.has(intentId)
  }

  function _log(msg) {
    if (onLog) onLog(msg)
  }

  function start(intentId) {
    if (_sseSubs[intentId]) return

    let sseAlive = true
    _sseHeartbeats[intentId] = Date.now()
    let pollStarted = false
    let lastProgressAt = Date.now()
    let lastProgressSignature = ''
    let transportState = ''
    let consecutivePollErrors = 0

    const emitTransportState = (state, details = {}) => {
      if (state === transportState && !details.force) return
      transportState = state
      if (onTransportState) {
        onTransportState(intentId, {
          state,
          elapsedMs: Date.now() - lastProgressAt,
          ...details,
        })
      }
    }

    const noteProgress = (source) => {
      lastProgressAt = Date.now()
      consecutivePollErrors = 0
      emitTransportState('active', { source })
    }

    const onAnyEvent = ({ progress = true, source = 'sse' } = {}) => {
      _sseHeartbeats[intentId] = Date.now()
      sseAlive = true
      _sseReconnecting.delete(intentId)
      if (progress) noteProgress(source)
      else if (transportState === 'degraded' || transportState === 'polling') {
        emitTransportState('active', { source })
      }
    }

    _sseSubs[intentId] = api.subscribeIntentEvents(intentId, {
      onStageChange(data) {
        onAnyEvent({ source: 'stage' })
        if (onStageChange) onStageChange(intentId, data)
        if (onMilestonePush) {
          onMilestonePush(intentId, {
            stage: data.stage || '',
            status: data.status || 'running',
            summary: data.summary || `Stage: ${data.stage || data.agent_id || '...'}`,
          })
        }
      },
      onA2AMessage(data) {
        onAnyEvent({ source: 'a2a' })
        if (onA2AAppend) onA2AAppend(intentId, data)
      },
      onHeartbeat(_data) {
        onAnyEvent({ progress: false, source: 'heartbeat' })
      },
      onPhaseProgress(data) {
        onAnyEvent({ source: 'phase' })
        if (data.message) {
          _log(`${intentId.slice(-8)} ${data.message}`)
          if (onMilestonePush) {
            onMilestonePush(intentId, {
              stage: data.stage || data.agent_id || '',
              status: 'running',
              summary: data.message,
            })
          }
        }
      },
      onThinkingToken(data) {
        onAnyEvent({ source: 'token' })
        if (data.token && onThinkingToken) onThinkingToken(intentId, data)
      },
      onStreamEnd(_data) {
        onAnyEvent({ source: 'stream-end' })
        if (onStreamEnd) onStreamEnd()
      },
      onError(error) {
        sseAlive = false
        _sseReconnecting.add(intentId)
        emitTransportState('polling', { error: error?.message || 'SSE disconnected' })
      },
    })

    // ── Optimistic dual-channel: start HTTP polling immediately alongside SSE ──
    function _startPollingFallback() {
      if (pollStarted) return
      pollStarted = true
      const maxPolls = 600
      let count = 0

      const pollOnce = async () => {
        count++
        try {
          const data = await api.getIntent(intentId)
          consecutivePollErrors = 0
          const milestones = data.milestones || data.snapshot?.milestones || []
          const latestMilestone = milestones[milestones.length - 1] || {}
          const progressSignature = JSON.stringify([
            data.status || '',
            milestones.length,
            latestMilestone.stage || '',
            latestMilestone.status || '',
            Boolean(data.snapshot?.needs_clarification),
            Boolean(data.result),
          ])
          if (progressSignature !== lastProgressSignature) {
            lastProgressSignature = progressSignature
            noteProgress('poll')
          } else if (transportState === 'degraded' || transportState === 'polling') {
            emitTransportState('active', { source: 'poll' })
          }

          const record = onApplyDetail ? onApplyDetail(data) : null
          const status = data.status
          if (!record || (isTerminalStatus ? isTerminalStatus(status) : false) || status === 'pending_approval' || status === 'pending_clarification') {
            if (status === 'pending_clarification') {
              _log(`${intentId.slice(-8)} 需用户澄清意图：${data.snapshot?.clarification_question || '请补充说明'}`)
            }
            // Fully release the old stream. In particular, clarification must
            // be able to call start() again after the user provides details.
            stop(intentId)
          }
        } catch (error) {
          consecutivePollErrors += 1
          if (consecutivePollErrors >= 2) {
            emitTransportState('degraded', {
              error: error?.message || 'Intent status request failed',
              attempts: consecutivePollErrors,
            })
          }
          if (count >= maxPolls) {
            emitTransportState('stalled', {
              error: error?.message || 'Intent status request timed out',
              force: true,
            })
            stop(intentId)
          }
        }
      }

      _pollTimers[intentId] = setInterval(pollOnce, INTENT_POLL_MS)
      void pollOnce()
    }

    // Start polling immediately (dual-channel), not after a delay
    _startPollingFallback()

    // ── Heartbeat watchdog: if SSE stays dead, ensure polling is active ──
    const watchdog = setInterval(() => {
      const heartbeatElapsed = Date.now() - (_sseHeartbeats[intentId] || 0)
      const progressElapsed = Date.now() - lastProgressAt
      if (!sseAlive || heartbeatElapsed > SSE_HEARTBEAT_TIMEOUT_MS) {
        _startPollingFallback()
      }

      if (transportState === 'degraded') return
      if (progressElapsed >= INTENT_STALLED_NOTICE_MS) {
        emitTransportState('stalled')
      } else if (progressElapsed >= INTENT_SLOW_NOTICE_MS) {
        emitTransportState('slow')
      }
    }, SSE_WATCHDOG_INTERVAL_MS)

    // Patch the cleanup to also kill the watchdog
    const originalCleanup = _sseSubs[intentId]
    _sseSubs[intentId] = () => {
      clearInterval(watchdog)
      _sseReconnecting.delete(intentId)
      try { originalCleanup() } catch (_) { /* ok */ }
    }
  }

  function stop(intentId) {
    if (_pollTimers[intentId]) { clearInterval(_pollTimers[intentId]); delete _pollTimers[intentId] }
    if (_sseSubs[intentId]) { try { _sseSubs[intentId]() } catch (_) { /* ok */ } delete _sseSubs[intentId] }
    delete _sseHeartbeats[intentId]
    _sseReconnecting.delete(intentId)
  }

  function cleanup() {
    Object.values(_sseSubs).forEach((fn) => { try { fn() } catch (_) { /* ok */ } })
    Object.values(_pollTimers).forEach((fn) => { try { clearInterval(fn) } catch (_) { /* ok */ } })
  }

  return {
    start,
    stop,
    cleanup,
    isReconnecting,
  }
}

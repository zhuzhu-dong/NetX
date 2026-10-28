<template>
  <main class="cover-wrapper" :class="{ 'hyper-jump': isLeaving }">
    
    <div class="page-bracket pb-tl"><span></span><i></i></div>
    <div class="page-bracket pb-tr"><span></span><i></i></div>
    <div class="page-bracket pb-bl"><span></span><i></i></div>
    <div class="page-bracket pb-br"><span></span><i></i></div>

    <div class="hud-overlay">
      <div class="hud-visual visual-tl">
        <div class="hud-ornament ornament-flow">
          <span class="hud-line line-a"></span>
          <span class="hud-line line-b"></span>
          <span class="hud-line line-c"></span>
          <i class="hud-node node-a"></i>
          <i class="hud-node node-b"></i>
        </div>
      </div>
      
      <div class="hud-visual visual-tr">
        <div class="hud-ornament ornament-orbit">
          <span class="orbit-ring ring-a"></span>
          <span class="orbit-ring ring-b"></span>
          <i class="orbit-core"></i>
        </div>
      </div>

      <div class="hud-visual visual-bl">
        <div class="hud-ornament ornament-matrix">
          <span v-for="i in 12" :key="i"></span>
        </div>
      </div>

      <div class="hud-visual visual-br">
        <div class="hud-ornament ornament-wave">
          <span></span><span></span><span></span><span></span><span></span>
          <i></i>
        </div>
        <div class="hud-label">TELEMETRY_LATENCY (μs)</div>
        <div class="telemetry-graph">
          <div class="bar bar-a"></div><div class="bar bar-b"></div><div class="bar bar-c"></div><div class="bar bar-d"></div><div class="bar bar-e"></div>
        </div>
      </div>
    </div>

    <div class="architect-board vertical-mode unfold-vertical">
      <div class="scan-line"></div>
      
      <div class="internal-grid"></div>

      <div class="panel-deco-line left">
        <div class="deco-tick" v-for="i in 5" :key="'l'+i"></div>
      </div>
      <div class="panel-deco-line right">
        <div class="deco-tick" v-for="i in 5" :key="'r'+i"></div>
      </div>

      <div class="alignment-mark tl"></div><div class="alignment-mark tr"></div>
      <div class="alignment-mark bl"></div><div class="alignment-mark br"></div>
      
      <div class="bg-watermark-center pan-watermark">P4</div>

      <div class="panel-visual-top">
        <div class="ethereal-logo-container">
          <svg class="ethereal-orbit spin-slow-reverse" viewBox="0 0 260 260">
            <circle cx="130" cy="130" r="110" fill="none" stroke="#e2e8f0" stroke-width="1.5" stroke-dasharray="2 10" />
            <circle cx="130" cy="130" r="95" fill="none" stroke="#38bdf8" stroke-width="1" stroke-dasharray="20 15" opacity="0.8" />
          </svg>
          
          <div class="ethereal-wave wave-1"></div>
          <div class="ethereal-wave wave-2"></div>
          <div class="ethereal-wave wave-3"></div>
          
          <div class="ethereal-core-wrapper">
            <div class="ethereal-core pulse-breathing">
              <div class="core-highlight"></div>
            </div>
          </div>
          
          <div class="energy-particle particle-1"></div>
          <div class="energy-particle particle-2"></div>
          <div class="energy-particle particle-3"></div>
        </div>
      </div>

      <div class="panel-content-bottom">
        <div class="header-section text-center">
          <h1 class="main-title animated-title text-reveal" aria-label="NetPulse">
            <span style="--i: 0">N</span>
            <span style="--i: 1">e</span>
            <span style="--i: 2">t</span>
            <span style="--i: 3">P</span>
            <span style="--i: 4">u</span>
            <span style="--i: 5">l</span>
            <span style="--i: 6">s</span>
            <span style="--i: 7">e</span>
          </h1>
          
          <div class="slogan-container text-reveal delay-1">
            <div class="line-decorator line-expand-center"></div>
            <h2 class="slogan-zh">意图驱动 · 多智能体自治网络</h2>
            <div class="line-decorator line-expand-center"></div>
          </div>
          <p class="description text-reveal delay-2 float-text">
            打破传统拓扑边界。依托 P4 可编程硬件与大型语言模型 (LLM) 深度耦合，在量子级时间维度内实现全网微秒级自愈与意图的动态重映射。
          </p>
        </div>

        <div class="action-area text-reveal delay-3">
          <button class="premium-btn" @click="triggerSystemBoot" :disabled="isLeaving">
            <div class="btn-bg-liquid"></div>
            <span class="btn-text">{{ isLeaving ? 'KERNEL OVERRIDING...' : 'INITIALIZE SYSTEM' }}</span>
          </button>
        </div>
      </div>
    </div>

    <transition name="boot-gate-fade">
      <div v-if="isBootChecking" class="boot-gate-overlay">
        <div class="boot-simple-wait">
          <div class="boot-spinner" aria-hidden="true"></div>
          <span>请稍后</span>
        </div>
      </div>
    </transition>
  </main>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { waitForSystemReady } from '../api/client'

const router = useRouter()
const isLeaving = ref(false)
const isBootChecking = ref(false)

const triggerSystemBoot = async () => {
  if (isLeaving.value || isBootChecking.value) return
  const destination = '/dashboard'

  isBootChecking.value = true
  isLeaving.value = true
  const ready = await waitForSystemReady()
  if (!ready) {
    window.dispatchEvent(new CustomEvent('netpulse-toast', {
      detail: '后台服务未连接，部分功能暂不可用',
    }))
  }
  setTimeout(() => router.push(destination), 450)
}
</script>

<style scoped>
.cover-wrapper { 
  height: 100vh; width: 100vw; display: flex; justify-content: center; align-items: center; 
  perspective: 2000px; overflow: hidden; position: relative; z-index: 10; 
  user-select: none;
  -webkit-user-select: none;
}

/* ================= 页面四角边框 (全局包裹) ================= */
.page-bracket {
  position: absolute;
  width: 132px;
  height: 132px;
  z-index: 5;
  pointer-events: none;
  transition: all 0.5s ease;
  opacity: 0.92;
}
.page-bracket::before,
.page-bracket::after {
  content: '';
  position: absolute;
  background: linear-gradient(90deg, rgba(56, 189, 248, 0.9), rgba(129, 140, 248, 0.35), transparent);
  border-radius: 999px;
  box-shadow: 0 0 18px rgba(56, 189, 248, 0.18);
}
.page-bracket::before {
  top: 0;
  left: 0;
  width: 132px;
  height: 2px;
}
.page-bracket::after {
  top: 0;
  left: 0;
  width: 2px;
  height: 132px;
  background: linear-gradient(180deg, rgba(56, 189, 248, 0.9), rgba(129, 140, 248, 0.35), transparent);
}
.page-bracket span::before,
.page-bracket span::after,
.page-bracket i::before,
.page-bracket i::after {
  content: '';
  position: absolute;
  border-radius: 999px;
}
.page-bracket span::before {
  top: 12px;
  left: 0;
  width: 72px;
  height: 1px;
  background: linear-gradient(90deg, rgba(15, 23, 42, 0.28), rgba(56, 189, 248, 0.42), transparent);
}
.page-bracket span::after {
  top: 0;
  left: 12px;
  width: 1px;
  height: 72px;
  background: linear-gradient(180deg, rgba(15, 23, 42, 0.28), rgba(56, 189, 248, 0.42), transparent);
}
.page-bracket i {
  position: absolute;
  top: -4px;
  left: -4px;
  width: 18px;
  height: 18px;
  border-top: 2px solid rgba(15, 23, 42, 0.2);
  border-left: 2px solid rgba(15, 23, 42, 0.2);
  border-radius: 6px 0 0 0;
}
.page-bracket i::before {
  top: 16px;
  left: 16px;
  width: 5px;
  height: 5px;
  background: #38bdf8;
  box-shadow: 0 0 14px rgba(56, 189, 248, 0.85);
}
.page-bracket i::after {
  top: 25px;
  left: 25px;
  width: 46px;
  height: 1px;
  background: linear-gradient(90deg, rgba(129, 140, 248, 0.58), transparent);
  transform: rotate(45deg);
  transform-origin: left center;
}
.pb-tl { top: 36px; left: 36px; }
.pb-tr { top: 36px; right: 36px; transform: scaleX(-1); }
.pb-bl { bottom: 36px; left: 36px; transform: scaleY(-1); }
.pb-br { bottom: 36px; right: 36px; transform: scale(-1); }

/* ================= 页面四角 HUD (全局定位) ================= */
.hud-overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 6; pointer-events: none; }
.hud-visual { 
  position: absolute; width: auto; height: 80px; opacity: 0; 
  animation: fadeUp 1.5s ease forwards; display: flex; flex-direction: column; 
  align-items: flex-start; gap: 10px; 
}
.hud-label { font-family: monospace; font-size: 0.65rem; color: #64748b; font-weight: bold; letter-spacing: 2px; }

.visual-tl { top: 62px; left: 62px; }
.visual-tr { top: 62px; right: 62px; align-items: flex-end; }
.visual-bl { bottom: 62px; left: 62px; }
.visual-br { bottom: 62px; right: 62px; align-items: flex-end; }

.hud-ornament {
  position: relative;
  width: 118px;
  height: 72px;
  border: 1px solid rgba(148, 163, 184, 0.22);
  border-radius: 8px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.62), rgba(248, 250, 252, 0.28)),
    radial-gradient(circle at 30% 25%, rgba(56, 189, 248, 0.12), transparent 48%);
  box-shadow: 0 12px 28px rgba(15, 23, 42, 0.05), inset 0 1px 0 rgba(255,255,255,0.82);
  backdrop-filter: blur(8px);
  overflow: hidden;
}
.hud-ornament::before {
  content: '';
  position: absolute;
  inset: 8px;
  border: 1px solid rgba(56, 189, 248, 0.16);
  border-radius: 5px;
}
.hud-ornament::after {
  content: '';
  position: absolute;
  left: 14px;
  right: 14px;
  top: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.72), transparent);
}
.visual-br > .hud-label,
.visual-br > .telemetry-graph { display: none; }

.ornament-flow { display: flex; align-items: center; justify-content: center; }
.hud-line {
  position: absolute;
  left: 18px;
  right: 18px;
  height: 2px;
  border-radius: 999px;
  background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.82), rgba(129, 140, 248, 0.42), transparent);
  overflow: hidden;
}
.hud-line::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, #ffffff, transparent);
  transform: translateX(-100%);
  animation: hudSweep 2.8s ease-in-out infinite;
}
.line-a { top: 22px; }
.line-b { top: 35px; opacity: 0.72; }
.line-c { top: 48px; opacity: 0.48; }
.node-a,
.node-b {
  position: absolute;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #38bdf8;
  box-shadow: 0 0 14px rgba(56, 189, 248, 0.82);
}
.node-a { left: 22px; top: 32px; }
.node-b { right: 24px; top: 32px; background: #818cf8; }

.ornament-orbit { display: flex; align-items: center; justify-content: center; }
.orbit-ring {
  position: absolute;
  border-radius: 50%;
  border: 1px solid rgba(56, 189, 248, 0.32);
}
.ring-a {
  width: 48px;
  height: 48px;
  border-top-color: rgba(129, 140, 248, 0.85);
  animation: spinCW 9s linear infinite;
}
.ring-b {
  width: 32px;
  height: 32px;
  border-right-color: rgba(56, 189, 248, 0.85);
  animation: spinCCW 7s linear infinite;
}
.orbit-core {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #ffffff;
  box-shadow: 0 0 18px rgba(56, 189, 248, 0.9), 0 0 32px rgba(129, 140, 248, 0.35);
}

.ornament-matrix {
  display: grid;
  grid-template-columns: repeat(4, 8px);
  grid-auto-rows: 8px;
  place-content: center;
  gap: 7px;
}
.ornament-matrix span {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: rgba(56, 189, 248, 0.45);
  animation: matrixSoft 2s ease-in-out infinite alternate;
}
.ornament-matrix span:nth-child(3n) { background: rgba(129, 140, 248, 0.58); animation-delay: 0.35s; }
.ornament-matrix span:nth-child(4n) { opacity: 0.34; animation-delay: 0.7s; }

.ornament-wave {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.ornament-wave span {
  width: 4px;
  height: 34px;
  border-radius: 999px;
  background: linear-gradient(to top, rgba(56, 189, 248, 0.16), rgba(129, 140, 248, 0.82), rgba(56, 189, 248, 0.16));
  animation: waveSoft 1.8s ease-in-out infinite alternate;
}
.ornament-wave span:nth-child(2) { animation-delay: 0.15s; }
.ornament-wave span:nth-child(3) { animation-delay: 0.3s; }
.ornament-wave span:nth-child(4) { animation-delay: 0.45s; }
.ornament-wave span:nth-child(5) { animation-delay: 0.6s; }
.ornament-wave i {
  position: absolute;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  border: 1px solid rgba(56, 189, 248, 0.2);
  border-top-color: rgba(56, 189, 248, 0.68);
  animation: spinCW 8s linear infinite;
}
@keyframes hudSweep { 45%, 100% { transform: translateX(100%); } }
@keyframes matrixSoft { from { opacity: 0.25; transform: scale(0.86); } to { opacity: 1; transform: scale(1.08); } }
@keyframes waveSoft { from { transform: scaleY(0.34); opacity: 0.42; } to { transform: scaleY(1); opacity: 1; } }

/* HUD 1: P4 Pipeline */
.p4-pipeline { display: flex; gap: 6px; border: 1px solid rgba(56, 189, 248, 0.3); padding: 5px; border-radius: 3px; background: rgba(255,255,255,0.5); }
.packet { width: 18px; height: 6px; background: #38bdf8; animation: parsePkt 2s infinite steps(4); opacity: 0.2; }
.pkt-1 { animation-delay: 0s; } .pkt-2 { animation-delay: 0.25s; } .pkt-3 { animation-delay: 0.5s; } .pkt-4 { animation-delay: 0.75s; }
@keyframes parsePkt { 50% { opacity: 1; background: #818cf8; box-shadow: 0 0 8px #818cf8; } }

/* HUD 2: Agent Topology */
.agent-nodes { width: 70px; height: 70px; }
.node-pulse { animation: nodeBlink 1.5s infinite alternate; }
.delay-1 { animation-delay: 0.4s; } .delay-2 { animation-delay: 0.8s; }
@keyframes nodeBlink { 0% { opacity: 0.4; } 100% { opacity: 1; filter: drop-shadow(0 0 6px #38bdf8); } }

/* HUD 3: 全新左下角 Quantum Matrix */
.quantum-matrix { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; padding: 4px; border: 1px solid rgba(129, 140, 248, 0.3); border-radius: 4px; }
.q-dot { width: 8px; height: 8px; border-radius: 2px; background: #818cf8; animation: qBlink 1.5s infinite alternate; }
.q-dot:nth-child(odd) { animation-delay: 0.3s; background: #38bdf8; }
.q-dot:nth-child(even) { animation-delay: 0.7s; }
.q-dot:nth-child(3n) { animation-delay: 1.1s; background: #0ea5e9; }
@keyframes qBlink { 0% { opacity: 0.2; transform: scale(0.8); } 100% { opacity: 1; transform: scale(1.1); box-shadow: 0 0 6px currentColor; } }

/* HUD 4: Telemetry Latency */
.telemetry-graph { display: flex; align-items: flex-end; gap: 4px; height: 35px; }
.bar { width: 6px; background: linear-gradient(to top, #38bdf8, #818cf8); border-top: 1.5px solid #0f172a; animation: barJump 1s infinite alternate ease-in-out; }
.bar-a { height: 40%; animation-duration: 0.8s; } .bar-b { height: 80%; animation-duration: 1.1s; } .bar-c { height: 50%; animation-duration: 0.9s; } .bar-d { height: 100%; animation-duration: 1.3s; } .bar-e { height: 60%; animation-duration: 0.7s; }
@keyframes barJump { 0% { height: 15%; } 100% { height: 100%; } }

/* ================= 竖版主面板 ================= */
.vertical-mode { 
  position: relative; width: min(680px, calc(100vw - 190px)); height: min(840px, calc(100vh - 100px)); 
  background: rgba(255, 255, 255, 0.96); border: 1px solid rgba(226, 232, 240, 0.8); 
  box-shadow: 0 56px 120px -22px rgba(15, 23, 42, 0.18), 0 0 0 1px rgba(255, 255, 255, 0.8); 
  display: flex; flex-direction: column; overflow: hidden; border-radius: 16px; z-index: 10;
  backdrop-filter: blur(10px); 
  transform-origin: center;
}
.architect-board::before {
  content: '';
  position: absolute;
  inset: -2px;
  z-index: 30;
  opacity: 0;
  pointer-events: none;
  background:
    linear-gradient(90deg, transparent, rgba(255,255,255,0.95), transparent),
    radial-gradient(circle at center, rgba(56, 189, 248, 0.38), transparent 58%);
  transform: translateX(-120%) skewX(-16deg);
}
.architect-board::after {
  content: '';
  position: absolute;
  left: 50%;
  top: 50%;
  z-index: 31;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  opacity: 0;
  pointer-events: none;
  background: #ffffff;
  box-shadow:
    0 0 24px rgba(255, 255, 255, 0.95),
    0 0 52px rgba(56, 189, 248, 0.9),
    0 0 96px rgba(129, 140, 248, 0.62);
  transform: translate(-50%, -50%) scale(0.4);
}

/* 面板内部复杂几何网格 (加深透明度) */
.internal-grid {
  position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  background-image: 
    linear-gradient(rgba(148, 163, 184, 0.1) 1px, transparent 1px),
    linear-gradient(90deg, rgba(148, 163, 184, 0.1) 1px, transparent 1px);
  background-size: 30px 30px; z-index: 0; pointer-events: none;
}

/* 面板两侧科技感装饰线 */
.panel-deco-line { 
  position: absolute; top: 15%; bottom: 15%; width: 2px; 
  background: linear-gradient(to bottom, transparent, rgba(56, 189, 248, 0.4), transparent); 
  z-index: 1; display: flex; flex-direction: column; justify-content: space-around; align-items: center;
}
.panel-deco-line.left { left: 40px; }
.panel-deco-line.right { right: 40px; }
.deco-tick { width: 8px; height: 2px; background: #38bdf8; border-radius: 1px; opacity: 0.6; }

/* 扫描线 */
.scan-line { position: absolute; top: -100%; left: 0; width: 100%; height: 3px; background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.6), transparent); box-shadow: 0 0 25px rgba(56, 189, 248, 0.8); animation: scanLine 6s cubic-bezier(0.4, 0, 0.2, 1) infinite; z-index: 20; pointer-events: none; }
@keyframes scanLine { 0% { top: -10%; } 100% { top: 110%; } }

/* 内部四角准星 */
.alignment-mark { position: absolute; width: 20px; height: 20px; z-index: 5; }
.alignment-mark::before, .alignment-mark::after { content: ''; position: absolute; background: #cbd5e1; }
.alignment-mark::before { top: 0; left: 0; width: 100%; height: 1px; }
.alignment-mark::after { left: 0; top: 0; width: 1px; height: 100%; }
.tl { top: 25px; left: 25px; border-right: none; border-bottom: none; }
.tr { top: 25px; right: 25px; transform: rotate(90deg); }
.bl { bottom: 25px; left: 25px; transform: rotate(-90deg); }
.br { bottom: 25px; right: 25px; transform: rotate(180deg); }

.bg-watermark-center { position: absolute; top: 18%; left: 50%; transform: translateX(-50%); font-size: 22rem; font-weight: 900; color: #f1f5f9; z-index: 0; user-select: none; pointer-events: none; }
.pan-watermark { animation: watermarkPanCenter 10s ease-in-out infinite alternate; }
@keyframes watermarkPanCenter { 0% { transform: translateX(-50%) scale(0.95); opacity: 0.5; } 100% { transform: translateX(-50%) scale(1.05); opacity: 0.9; } }

/* ================= 顶部：高级以太之心 ================= */
.panel-visual-top { flex: 0.92; position: relative; background: linear-gradient(180deg, rgba(248,250,252,0.8) 0%, transparent 100%); display: flex; justify-content: center; align-items: flex-end; padding-bottom: 34px; z-index: 2; }
.ethereal-logo-container { position: relative; width: 310px; height: 310px; display: flex; justify-content: center; align-items: center; cursor: default; }

.ethereal-orbit { position: absolute; width: 100%; height: 100%; z-index: 1; }
.spin-slow-reverse { animation: spinCCW 24s linear infinite; transform-origin: center; }

.ethereal-wave { position: absolute; width: 70px; height: 70px; border-radius: 50%; border: 1.5px solid rgba(56, 189, 248, 0.5); background: radial-gradient(circle, rgba(129, 140, 248, 0.15) 0%, transparent 70%); animation: etherealRipple 7s cubic-bezier(0.2, 0.6, 0.3, 1) infinite; z-index: 2; }
.wave-1 { animation-delay: 0s; } .wave-2 { animation-delay: 2.3s; } .wave-3 { animation-delay: 4.6s; }
@keyframes etherealRipple { 0% { transform: scale(1); opacity: 1; } 100% { transform: scale(3.8); opacity: 0; border-color: rgba(129, 140, 248, 0); } }

/* 鼠标交互包裹层 */
.ethereal-core-wrapper {
  position: absolute; width: 100%; height: 100%; display: flex; justify-content: center; align-items: center;
  transition: transform 0.6s cubic-bezier(0.34, 1.56, 0.64, 1); z-index: 5;
}
.ethereal-logo-container:hover .ethereal-core-wrapper {
  transform: scale(1.4); /* 鼠标移入吐气变大 */
}

.ethereal-core { position: absolute; width: 80px; height: 80px; border-radius: 50%; background: radial-gradient(circle at 30% 30%, #ffffff 0%, #bae6fd 30%, #818cf8 70%, #312e81 100%); box-shadow: 0 0 40px rgba(56, 189, 248, 0.7), inset -5px -5px 15px rgba(0, 0, 0, 0.2); display: flex; justify-content: center; align-items: center; }
.core-highlight { position: absolute; width: 30px; height: 30px; border-radius: 50%; background: #ffffff; box-shadow: 0 0 25px #ffffff, 0 0 50px #bae6fd; filter: blur(2px); z-index: 1;}

.pulse-breathing { animation: coreBreath 4.5s cubic-bezier(0.4, 0, 0.2, 1) infinite alternate; }
@keyframes coreBreath { 0% { transform: scale(0.85); box-shadow: 0 0 25px rgba(56, 189, 248, 0.5); } 100% { transform: scale(1.15); box-shadow: 0 0 60px rgba(129, 140, 248, 0.9), 0 0 90px rgba(56, 189, 248, 0.5); } }

/* 星点更加清晰明亮 */
.energy-particle { position: absolute; width: 8px; height: 8px; border-radius: 50%; background: #ffffff; box-shadow: 0 0 15px #38bdf8; z-index: 6; opacity: 0.9; }
.particle-1 { animation: orbitP1 7s linear infinite; } 
.particle-2 { width: 6px; height: 6px; background: #e0e7ff; box-shadow: 0 0 12px #818cf8; animation: orbitP2 10s linear infinite; }
.particle-3 { width: 5px; height: 5px; background: #e0f2fe; box-shadow: 0 0 10px #38bdf8; animation: orbitP3 14s linear infinite; }
@keyframes orbitP1 { 0% { transform: rotate(0deg) translateX(110px) rotate(0deg); } 100% { transform: rotate(360deg) translateX(110px) rotate(-360deg); } }
@keyframes orbitP2 { 0% { transform: rotate(180deg) translateX(130px) rotate(-180deg); } 100% { transform: rotate(-180deg) translateX(130px) rotate(180deg); } }
@keyframes orbitP3 { 0% { transform: rotate(90deg) translateX(95px) rotate(-90deg); } 100% { transform: rotate(450deg) translateX(95px) rotate(-450deg); } }

/* ================= 底部：动态标题排版 ================= */
.panel-content-bottom { flex: 1.08; padding: 0 72px 56px 72px; display: flex; flex-direction: column; justify-content: space-between; z-index: 2; align-items: center; }
.text-center { text-align: center; display: flex; flex-direction: column; align-items: center; }

/* 炫酷动态标题：加入字间距的呼吸缩放与阴影 */
.main-title { position: relative; font-size: 5.15rem; font-weight: 900; margin: 0; line-height: 1; }
.animated-title {
  display: inline-flex;
  gap: 0.01em;
  transform-origin: center;
  filter: drop-shadow(0 14px 34px rgba(56, 189, 248, 0.26));
  animation: titlePulse 4.2s ease-in-out infinite alternate;
}
.animated-title span {
  position: relative;
  display: inline-block;
  background: linear-gradient(110deg, #0f172a 0%, #0284c7 26%, #818cf8 50%, #38bdf8 72%, #0f172a 100%);
  background-size: 260% auto;
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  animation: titleFlow 5.8s linear infinite, letterGlow 3.6s ease-in-out infinite;
  animation-delay: calc(var(--i) * 70ms), calc(var(--i) * 80ms);
}
.animated-title span::after {
  content: '';
  position: absolute;
  top: 8%;
  bottom: 5%;
  left: -24%;
  width: 36%;
  transform: skewX(-18deg);
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.75), transparent);
  filter: blur(1px);
  opacity: 0;
  animation: letterSheen 5.8s ease-in-out infinite;
  animation-delay: calc(var(--i) * 70ms);
}
.animated-title::after {
  content: '';
  position: absolute;
  left: 8%;
  right: 8%;
  bottom: -12px;
  height: 4px;
  border-radius: 999px;
  background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.9), rgba(129, 140, 248, 0.9), transparent);
  transform: scaleX(0.35);
  opacity: 0.65;
  animation: titleUnderline 2.8s ease-in-out infinite alternate;
}
@keyframes titleFlow { 0% { background-position: 0% center; } 100% { background-position: 260% center; } }
@keyframes titlePulse { 
  0% { filter: drop-shadow(0 10px 24px rgba(56, 189, 248, 0.22)); } 
  100% { filter: drop-shadow(0 18px 42px rgba(129, 140, 248, 0.48)); } 
}
@keyframes letterGlow { 0%, 100% { opacity: 0.92; } 50% { opacity: 1; } }
@keyframes letterSheen { 0%, 18% { transform: translateX(0) skewX(-18deg); opacity: 0; } 34% { opacity: 0.55; } 52%, 100% { transform: translateX(260%) skewX(-18deg); opacity: 0; } }
@keyframes titleUnderline { 0% { transform: scaleX(0.28); opacity: 0.35; } 100% { transform: scaleX(1); opacity: 0.9; } }

.slogan-container { display: flex; align-items: center; gap: 20px; margin: 20px 0 25px 0; }
.slogan-zh { font-size: 1.25rem; font-weight: 800; margin: 0; color: #334155; letter-spacing: 3px; }
.line-decorator { width: 0; height: 3px; background: linear-gradient(90deg, #38bdf8, #818cf8); border-radius: 2px; }
.line-expand-center { animation: expandLineCenter 1s cubic-bezier(0.16, 1, 0.3, 1) 0.8s forwards; }
@keyframes expandLineCenter { to { width: 40px; } }

.description { font-size: 1.05rem; color: #64748b; line-height: 1.8; font-weight: 500; margin: 0; }
.float-text { animation: floatTiny 4s ease-in-out infinite; }
@keyframes floatTiny { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-5px); } }

.text-reveal { opacity: 0; clip-path: inset(100% 0 0 0); animation: textRevealAnim 1.2s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
.delay-1 { animation-delay: 0.3s; } .delay-2 { animation-delay: 0.6s; } .delay-3 { animation-delay: 0.9s; }
@keyframes textRevealAnim { 100% { opacity: 1; clip-path: inset(0 0 0 0); } }

/* 高级按钮 (修复了溢出切割问题) */
.action-area { width: 100%; max-width: 390px; min-height: 88px; margin-top: auto; margin-bottom: 0; padding-top: 10px; overflow: visible; }
.action-area.text-reveal { clip-path: none; }
.premium-btn { 
  position: relative; width: 100%; height: 68px; background: #ffffff; 
  border: 1px solid rgba(148, 163, 184, 0.5); border-radius: 100px; 
  cursor: pointer; overflow: hidden; display: flex; align-items: center; justify-content: center; 
  box-shadow: 0 10px 20px -5px rgba(15, 23, 42, 0.1); transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1); 
}
.btn-text { position: relative; z-index: 2; font-size: 1.1rem; font-weight: 800; letter-spacing: 2px; color: #0f172a; transition: color 0.4s; }
.btn-bg-liquid { position: absolute; top: 0; left: -100%; width: 100%; height: 100%; background: linear-gradient(135deg, #38bdf8, #818cf8); transition: left 0.6s cubic-bezier(0.4, 0, 0.2, 1); z-index: 1; }

.premium-btn:hover:not(:disabled) { border-color: #818cf8; transform: translateY(-2px); box-shadow: 0 18px 34px -8px rgba(129, 140, 248, 0.42); }
.premium-btn:hover:not(:disabled) .btn-bg-liquid { left: 0; }
.premium-btn:hover:not(:disabled) .btn-text { color: #ffffff; text-shadow: 0 0 10px rgba(255,255,255,0.5); }

@keyframes spinCW { 100% { transform: rotate(360deg); } }
@keyframes spinCCW { 100% { transform: rotate(-360deg); } }
@keyframes fadeUp { 0% { opacity: 0; transform: translateY(20px); } 100% { opacity: 1; transform: translateY(0); } }
.unfold-vertical { animation: unfoldV 1.2s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
@keyframes unfoldV { 0% { transform: scaleY(0.01) scaleX(0); opacity: 0; } 40% { transform: scaleY(0.01) scaleX(1); opacity: 1; } 100% { transform: scaleY(1) scaleX(1); opacity: 1; } }

/* 跃迁离场动画 */
.hyper-jump .architect-board {
  pointer-events: none;
  animation: panelExitFold 0.92s cubic-bezier(0.86, 0, 0.07, 1) forwards;
}
.hyper-jump .architect-board::before {
  animation: exitFlashSweep 0.62s cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
.hyper-jump .architect-board::after {
  animation: exitCoreBurst 0.9s cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
.hyper-jump .panel-visual-top,
.hyper-jump .panel-content-bottom,
.hyper-jump .panel-deco-line,
.hyper-jump .alignment-mark,
.hyper-jump .bg-watermark-center {
  animation: contentCollapse 0.72s cubic-bezier(0.86, 0, 0.07, 1) forwards;
}
.hyper-jump .hud-overlay { opacity: 0; transform: scale(1.1); transition: 0.6s ease-in; }
.hyper-jump .page-bracket { opacity: 0; transform: scale(1.2); }
@keyframes panelExitWarp {
  0% {
    opacity: 1;
    transform: translateZ(0) scale(1);
    filter: blur(0);
    clip-path: inset(0 0 0 0 round 16px);
  }
  34% {
    opacity: 1;
    transform: translateZ(90px) scale(1.025);
    filter: brightness(1.08) saturate(1.1);
    clip-path: inset(0 0 0 0 round 16px);
  }
  68% {
    opacity: 0.82;
    transform: translateZ(180px) scaleX(0.62) scaleY(1.04);
    filter: blur(4px) brightness(1.3);
    clip-path: inset(0 18% 0 18% round 14px);
  }
  100% {
    opacity: 0;
    transform: translateZ(260px) scaleX(0.04) scaleY(1.18);
    filter: blur(18px) brightness(1.8);
    clip-path: inset(0 49% 0 49% round 999px);
  }
}
@keyframes panelExitFold {
  0% {
    opacity: 1;
    transform: translateZ(0) rotateX(0deg) scale(1);
    filter: blur(0) brightness(1);
    clip-path: inset(0 0 0 0 round 16px);
  }
  28% {
    opacity: 1;
    transform: translateZ(60px) rotateX(0deg) scaleX(1.015);
    filter: brightness(1.12);
    clip-path: inset(0 0 0 0 round 16px);
  }
  58% {
    opacity: 0.92;
    transform: translateZ(120px) rotateX(68deg) scaleX(0.96);
    filter: blur(2px) brightness(1.25);
    clip-path: inset(18% 0 18% 0 round 14px);
  }
  100% {
    opacity: 0;
    transform: translateZ(210px) rotateX(88deg) scaleX(0.82);
    filter: blur(14px) brightness(1.8);
    clip-path: inset(49% 0 49% 0 round 999px);
  }
}
@keyframes exitFlashSweep {
  0% { opacity: 0; transform: translateX(-130%) skewX(-16deg); }
  18% { opacity: 0.9; }
  100% { opacity: 0; transform: translateX(130%) skewX(-16deg); }
}
@keyframes exitCoreBurst {
  0% { opacity: 0; transform: translate(-50%, -50%) scale(0.35); }
  38% { opacity: 1; transform: translate(-50%, -50%) scale(1); }
  100% { opacity: 0; transform: translate(-50%, -50%) scale(10); }
}
@keyframes contentCollapse {
  0% { opacity: 1; transform: scale(1); filter: blur(0); }
  100% { opacity: 0; transform: scale(0.92); filter: blur(8px); }
}

.boot-gate-overlay {
  position: absolute;
  inset: 0;
  z-index: 120;
  display: grid;
  place-items: center;
  background: transparent;
  pointer-events: all;
}
.boot-simple-wait {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: #0f172a;
  font-size: 18px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-shadow: 0 2px 14px rgba(255, 255, 255, 0.9);
}
.boot-spinner {
  width: 58px;
  height: 58px;
  border-radius: 999px;
  border: 4px solid rgba(56, 189, 248, 0.18);
  border-top-color: rgba(14, 165, 233, 0.96);
  border-right-color: rgba(99, 102, 241, 0.82);
  animation: bootSpin 0.85s linear infinite;
}
.boot-gate-fade-enter-active, .boot-gate-fade-leave-active { transition: opacity 0.22s ease; }
.boot-gate-fade-enter-from, .boot-gate-fade-leave-to { opacity: 0; }
@keyframes bootSpin { to { transform: rotate(360deg); } }

</style>

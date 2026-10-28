<template>
  <div class="app-wrapper">
    <div class="base-bg"></div>

    <div class="macro-orb orb-1"></div>
    <div class="macro-orb orb-2"></div>

    <div class="holographic-grid"></div>

    <canvas ref="canvasRef" class="star-canvas"></canvas>

    <transition name="auth-frame-fade">
      <div v-if="showAuthFrame" class="auth-frame-layer">
        <div class="auth-page-bracket auth-pb-tl"><span></span><i></i></div>
        <div class="auth-page-bracket auth-pb-tr"><span></span><i></i></div>
        <div class="auth-page-bracket auth-pb-bl"><span></span><i></i></div>
        <div class="auth-page-bracket auth-pb-br"><span></span><i></i></div>

        <div class="auth-hud-overlay">
          <div class="auth-hud-visual auth-visual-tl">
            <div class="auth-hud-ornament auth-ornament-flow">
              <span class="auth-hud-line auth-line-a"></span>
              <span class="auth-hud-line auth-line-b"></span>
              <span class="auth-hud-line auth-line-c"></span>
              <i class="auth-hud-node auth-node-a"></i>
              <i class="auth-hud-node auth-node-b"></i>
            </div>
          </div>

          <div class="auth-hud-visual auth-visual-tr">
            <div class="auth-hud-ornament auth-ornament-orbit">
              <span class="auth-orbit-ring auth-ring-a"></span>
              <span class="auth-orbit-ring auth-ring-b"></span>
              <i class="auth-orbit-core"></i>
            </div>
          </div>

          <div class="auth-hud-visual auth-visual-bl">
            <div class="auth-hud-ornament auth-ornament-matrix">
              <span v-for="i in 12" :key="`auth-matrix-${i}`"></span>
            </div>
          </div>

          <div class="auth-hud-visual auth-visual-br">
            <div class="auth-hud-ornament auth-ornament-wave">
              <span></span><span></span><span></span><span></span><span></span>
              <i></i>
            </div>
          </div>
        </div>
      </div>
    </transition>

    <router-view class="view-layer" />

    <transition name="toast-fade">
      <div v-if="toastMessage" class="global-toast">{{ toastMessage }}</div>
    </transition>
  </div>
</template>

<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const showAuthFrame = computed(() => route.name === 'login')
const canvasRef = ref(null)

// ── Global toast ────────────────────────────────────────────────────────────
const toastMessage = ref('')
let toastTimer = null
function showToast(detail) {
  toastMessage.value = detail
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastMessage.value = '' }, 4000)
}
let ctx, animationId
let width, height
let particles = []
let packets = [] 

const mouse = { x: -1000, y: -1000 }
const HOVER_RADIUS = 120
const HOVER_RADIUS_SQ = HOVER_RADIUS * HOVER_RADIUS
const LINK_DISTANCE = 140
const LINK_DISTANCE_SQ = LINK_DISTANCE * LINK_DISTANCE
const REPEL_DISTANCE = 60
const REPEL_DISTANCE_SQ = REPEL_DISTANCE * REPEL_DISTANCE
const FRAME_INTERVAL_MS = 1000 / 30
const CANVAS_SCALE = 0.72
let radarRadius = 0
let resizeTimeout = null
let isPageVisible = true
let lastFrameAt = 0

const particleCellKey = (cellX, cellY) => `${cellX}:${cellY}`

const buildParticleGrid = () => {
  const grid = new Map()
  for (let i = 0; i < particles.length; i++) {
    const p = particles[i]
    p.cellX = Math.floor(p.x / LINK_DISTANCE)
    p.cellY = Math.floor(p.y / LINK_DISTANCE)
    const key = particleCellKey(p.cellX, p.cellY)
    if (!grid.has(key)) grid.set(key, [])
    grid.get(key).push(i)
  }
  return grid
}

const nearbyParticleIndices = (grid, particle) => {
  const indices = []
  for (let dx = -1; dx <= 1; dx++) {
    for (let dy = -1; dy <= 1; dy++) {
      const bucket = grid.get(particleCellKey(particle.cellX + dx, particle.cellY + dy))
      if (bucket) indices.push(...bucket)
    }
  }
  return indices
}

class Particle {
  constructor(x, y) {
    this.x = x
    this.y = y
    this.vx = (Math.random() - 0.5) * 0.25
    this.vy = (Math.random() - 0.5) * 0.25
    this.radius = Math.random() * 1.5 + 1.5
    this.glowIntensity = 0 
  }

  update() {
    const dx = mouse.x - this.x
    const dy = mouse.y - this.y
    const distanceSq = dx * dx + dy * dy

    if (distanceSq < HOVER_RADIUS_SQ) {
      this.glowIntensity = Math.min(this.glowIntensity + 0.03, 1)
      this.vx += dx * 0.00015 
      this.vy += dy * 0.00015
      this.vx *= 0.94
      this.vy *= 0.94
    } else {
      this.glowIntensity = Math.max(this.glowIntensity - 0.015, 0)
    }

    const currentSpeed = Math.sqrt(this.vx * this.vx + this.vy * this.vy)
    if (currentSpeed > 0.4) {
      this.vx = (this.vx / currentSpeed) * 0.4
      this.vy = (this.vy / currentSpeed) * 0.4
    }

    this.x += this.vx
    this.y += this.vy

    if (this.x < 0 || this.x > width) this.vx *= -1
    if (this.y < 0 || this.y > height) this.vy *= -1
  }

  draw() {
    ctx.beginPath()
    const currentRadius = this.radius + this.glowIntensity * 1.2
    ctx.arc(this.x, this.y, currentRadius, 0, Math.PI * 2)
    
    if (this.glowIntensity > 0.15) {
      ctx.fillStyle = `rgba(129, 140, 248, ${0.2 + this.glowIntensity * 0.3})`
      ctx.shadowBlur = 8 * this.glowIntensity
      ctx.shadowColor = '#818cf8'
    } else {
      ctx.fillStyle = 'rgba(14, 165, 233, 0.45)'
      ctx.shadowBlur = 0
    }
    ctx.fill()
  }
}

const initParticles = () => {
  particles = []
  packets = [] 
  
  const area = width * height
  let particleCount = Math.floor(area / 9000)
  particleCount = Math.max(15, Math.min(400, particleCount))
  
  const cols = Math.ceil(Math.sqrt(particleCount * (width / height)))
  const rows = Math.ceil(particleCount / cols)
  const cellWidth = width / cols
  const cellHeight = height / rows

  let count = 0
  for (let i = 0; i < rows; i++) {
    for (let j = 0; j < cols; j++) {
      if (count >= particleCount) break
      const x = j * cellWidth + cellWidth / 2 + (Math.random() - 0.5) * cellWidth * 0.7
      const y = i * cellHeight + cellHeight / 2 + (Math.random() - 0.5) * cellHeight * 0.7
      particles.push(new Particle(x, y))
      count++
    }
  }
}

const handleMouseMove = (e) => {
  mouse.x = e.clientX
  mouse.y = e.clientY
}

const handleMouseOut = () => {
  mouse.x = -1000
  mouse.y = -1000
}

const initCanvas = () => {
  const canvas = canvasRef.value
  ctx = canvas.getContext('2d')
  
  const resize = () => {
    width = window.innerWidth
    height = window.innerHeight
    if (canvas) {
      canvas.width = Math.floor(width * CANVAS_SCALE)
      canvas.height = Math.floor(height * CANVAS_SCALE)
      ctx.setTransform(CANVAS_SCALE, 0, 0, CANVAS_SCALE, 0, 0)
    }
    
    clearTimeout(resizeTimeout)
    resizeTimeout = setTimeout(() => {
      initParticles()
    }, 300)
  }
  
  window.addEventListener('resize', resize)
  
  width = window.innerWidth
  height = window.innerHeight
  canvas.width = Math.floor(width * CANVAS_SCALE)
  canvas.height = Math.floor(height * CANVAS_SCALE)
  ctx.setTransform(CANVAS_SCALE, 0, 0, CANVAS_SCALE, 0, 0)
  initParticles()
}

const drawMagneticRadar = () => {
  if (mouse.x > 0 && mouse.y > 0) {
    radarRadius += 0.4 
    if (radarRadius > HOVER_RADIUS) radarRadius = 0

    ctx.beginPath()
    ctx.arc(mouse.x, mouse.y, radarRadius, 0, Math.PI * 2)
    const opacity = 1 - (radarRadius / HOVER_RADIUS)
    ctx.strokeStyle = `rgba(129, 140, 248, ${opacity * 0.2})`
    ctx.lineWidth = 1
    ctx.stroke()
  }
}

const animate = (timestamp = 0) => {
  animationId = requestAnimationFrame(animate)
  if (!isPageVisible) return
  if (timestamp - lastFrameAt < FRAME_INTERVAL_MS) return
  lastFrameAt = timestamp

  ctx.clearRect(0, 0, width, height)

  drawMagneticRadar()

  for (let i = 0; i < particles.length; i++) {
    particles[i].update()
    particles[i].draw()
  }

  const grid = buildParticleGrid()

  if ((Math.random() < 0.09 || packets.length < 2) && particles.length > 0 && packets.length < 14) {
    const p1Index = Math.floor(Math.random() * particles.length)
    const p1 = particles[p1Index]
    const neighbors = nearbyParticleIndices(grid, p1)
      .filter((index) => {
        if (index === p1Index) return false
        const p2 = particles[index]
        const dx = p1.x - p2.x
        const dy = p1.y - p2.y
        return dx * dx + dy * dy < LINK_DISTANCE_SQ
      })
    if (neighbors.length > 0) {
      const p2 = particles[neighbors[Math.floor(Math.random() * neighbors.length)]]
      packets.push({ p1, p2, progress: 0 })
    }
  }

  for (let i = 0; i < particles.length; i++) {
    const nearby = nearbyParticleIndices(grid, particles[i])
    for (const j of nearby) {
      if (j <= i) continue
      const dx = particles[i].x - particles[j].x
      const dy = particles[i].y - particles[j].y
      const distanceSq = dx * dx + dy * dy

      if (distanceSq > 0 && distanceSq < REPEL_DISTANCE_SQ) {
        const distance = Math.sqrt(distanceSq)
        const repelForce = (REPEL_DISTANCE - distance) / REPEL_DISTANCE * 0.003
        particles[i].vx += (dx / distance) * repelForce
        particles[i].vy += (dy / distance) * repelForce
        particles[j].vx -= (dx / distance) * repelForce
        particles[j].vy -= (dy / distance) * repelForce
      }

      if (distanceSq < LINK_DISTANCE_SQ) {
        const distance = Math.sqrt(distanceSq)
        ctx.beginPath()
        ctx.moveTo(particles[i].x, particles[i].y)
        ctx.lineTo(particles[j].x, particles[j].y)
        
        const opacity = (1 - distance / LINK_DISTANCE) * 0.12
        const maxGlow = Math.max(particles[i].glowIntensity, particles[j].glowIntensity)
        
        if (maxGlow > 0) {
          ctx.strokeStyle = `rgba(129, 140, 248, ${opacity + (maxGlow * 0.15)})`
          ctx.lineWidth = 0.8 + maxGlow * 0.3
        } else {
          ctx.strokeStyle = `rgba(14, 165, 233, ${opacity * 0.9})`
          ctx.lineWidth = 0.6
        }
        ctx.stroke()
      }
    }
  }

  for (let i = packets.length - 1; i >= 0; i--) {
    let pkt = packets[i]
    pkt.progress += 0.045 
    
    if (pkt.progress >= 1) {
      packets.splice(i, 1) 
      continue
    }
    
    const x = pkt.p1.x + (pkt.p2.x - pkt.p1.x) * pkt.progress
    const y = pkt.p1.y + (pkt.p2.y - pkt.p1.y) * pkt.progress
    
    ctx.beginPath()
    ctx.arc(x, y, 2, 0, Math.PI * 2)
    ctx.fillStyle = '#818cf8'
    ctx.shadowBlur = 10
    ctx.shadowColor = '#818cf8'
    ctx.fill()
    ctx.shadowBlur = 0
  }
}

const handleVisibilityChange = () => {
  isPageVisible = !document.hidden
}

function handleToastEvent(e) { showToast(e.detail) }

onMounted(() => {
  const navigationEntry = performance.getEntriesByType?.('navigation')?.[0]
  initCanvas()
  animate()
  window.addEventListener('mousemove', handleMouseMove)
  window.addEventListener('mouseout', handleMouseOut)
  document.addEventListener('visibilitychange', handleVisibilityChange)
  window.addEventListener('netpulse-toast', handleToastEvent)
})

onUnmounted(() => {
  cancelAnimationFrame(animationId)
  window.removeEventListener('mousemove', handleMouseMove)
  window.removeEventListener('mouseout', handleMouseOut)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  window.removeEventListener('netpulse-toast', handleToastEvent)
  if (toastTimer) clearTimeout(toastTimer)
})
</script>

<style>
body {
  margin: 0;
  padding: 0;
  background-color: #f8fafc;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  overflow: hidden;
}

.app-wrapper {
  position: relative;
  width: 100vw;
  height: 100vh;
}

.base-bg {
  position: absolute;
  top: 0; left: 0; width: 100%; height: 100%;
  background: radial-gradient(circle at 50% 0%, #ffffff 0%, #f1f5f9 100%);
  z-index: 0;
  pointer-events: none;
}

.macro-orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(140px);
  z-index: 1;
  opacity: 0.25; 
  animation: floatOrb 20s infinite alternate ease-in-out;
  pointer-events: none;
}
.orb-1 {
  width: 50vw; height: 50vh;
  background: #38bdf8; 
  top: -10%; left: -10%;
}
.orb-2 {
  width: 60vw; height: 60vh;
  background: #8997de; 
  bottom: -20%; right: -10%;
  animation-delay: -10s;
}

@keyframes floatOrb {
  0% { transform: translate(0, 0) scale(1); }
  100% { transform: translate(60px, 40px) scale(1.1); }
}

.holographic-grid {
  position: absolute;
  top: 0; left: 0; width: 100%; height: 100%;
  background-image: 
    linear-gradient(rgba(14, 165, 233, 0.09) 1px, transparent 1px),
    linear-gradient(90deg, rgba(14, 165, 233, 0.09) 1px, transparent 1px);
  background-size: 40px 40px;
  z-index: 2;
  mask-image: radial-gradient(circle at center, black 20%, transparent 80%);
  -webkit-mask-image: radial-gradient(circle at center, black 20%, transparent 80%);
  pointer-events: none;
}

.star-canvas {
  position: absolute;
  top: 0; left: 0;
  width: 100%; height: 100%;
  z-index: 3;
  pointer-events: none;
}

.view-layer {
  position: relative;
  z-index: 10;
  width: 100%; height: 100%;
}

/* Auth entrance persistent HUD frame */
.auth-frame-layer {
  position: fixed;
  inset: 0;
  z-index: 12;
  pointer-events: none;
  overflow: hidden;
}

.auth-frame-fade-enter-active { transition: opacity 0.72s ease, filter 0.72s ease; }
.auth-frame-fade-leave-active {
  transition: opacity 0.55s ease, transform 0.55s cubic-bezier(0.86, 0, 0.07, 1), filter 0.55s ease;
}
.auth-frame-fade-enter-from { opacity: 0; filter: blur(8px); }
.auth-frame-fade-leave-to {
  opacity: 0;
  transform: scale(1.025);
  filter: blur(10px) brightness(1.15);
}

.auth-page-bracket {
  position: absolute;
  width: 132px;
  height: 132px;
  z-index: 2;
  pointer-events: none;
  opacity: 0.92;
  animation: authCornerDeploy 0.82s cubic-bezier(0.16, 1, 0.3, 1) both;
}
.auth-page-bracket::before,
.auth-page-bracket::after {
  content: '';
  position: absolute;
  background: linear-gradient(90deg, rgba(56, 189, 248, 0.9), rgba(129, 140, 248, 0.35), transparent);
  border-radius: 999px;
  box-shadow: 0 0 18px rgba(56, 189, 248, 0.18);
}
.auth-page-bracket::before {
  top: 0;
  left: 0;
  width: 132px;
  height: 2px;
}
.auth-page-bracket::after {
  top: 0;
  left: 0;
  width: 2px;
  height: 132px;
  background: linear-gradient(180deg, rgba(56, 189, 248, 0.9), rgba(129, 140, 248, 0.35), transparent);
}
.auth-page-bracket span::before,
.auth-page-bracket span::after,
.auth-page-bracket i::before,
.auth-page-bracket i::after {
  content: '';
  position: absolute;
  border-radius: 999px;
}
.auth-page-bracket span::before {
  top: 12px;
  left: 0;
  width: 72px;
  height: 1px;
  background: linear-gradient(90deg, rgba(15, 23, 42, 0.28), rgba(56, 189, 248, 0.42), transparent);
}
.auth-page-bracket span::after {
  top: 0;
  left: 12px;
  width: 1px;
  height: 72px;
  background: linear-gradient(180deg, rgba(15, 23, 42, 0.28), rgba(56, 189, 248, 0.42), transparent);
}
.auth-page-bracket i {
  position: absolute;
  top: -4px;
  left: -4px;
  width: 18px;
  height: 18px;
  border-top: 2px solid rgba(15, 23, 42, 0.2);
  border-left: 2px solid rgba(15, 23, 42, 0.2);
  border-radius: 6px 0 0 0;
}
.auth-page-bracket i::before {
  top: 16px;
  left: 16px;
  width: 5px;
  height: 5px;
  background: #38bdf8;
  box-shadow: 0 0 14px rgba(56, 189, 248, 0.85);
}
.auth-page-bracket i::after {
  top: 25px;
  left: 25px;
  width: 46px;
  height: 1px;
  background: linear-gradient(90deg, rgba(129, 140, 248, 0.58), transparent);
  transform: rotate(45deg);
  transform-origin: left center;
}
.auth-pb-tl { top: 36px; left: 36px; }
.auth-pb-tr { top: 36px; right: 36px; transform: scaleX(-1); }
.auth-pb-bl { bottom: 36px; left: 36px; transform: scaleY(-1); }
.auth-pb-br { bottom: 36px; right: 36px; transform: scale(-1); }

.auth-hud-overlay {
  position: absolute;
  inset: 0;
  z-index: 3;
  pointer-events: none;
}
.auth-hud-visual {
  position: absolute;
  width: auto;
  height: 80px;
  opacity: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  animation: authHudDeploy 1s ease forwards;
}
.auth-visual-tl { top: 62px; left: 62px; animation-delay: 0.08s; }
.auth-visual-tr { top: 62px; right: 62px; align-items: flex-end; animation-delay: 0.14s; }
.auth-visual-bl { bottom: 62px; left: 62px; animation-delay: 0.2s; }
.auth-visual-br { bottom: 62px; right: 62px; align-items: flex-end; animation-delay: 0.26s; }

.auth-hud-ornament {
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
.auth-hud-ornament::before {
  content: '';
  position: absolute;
  inset: 8px;
  border: 1px solid rgba(56, 189, 248, 0.16);
  border-radius: 5px;
}
.auth-hud-ornament::after {
  content: '';
  position: absolute;
  left: 14px;
  right: 14px;
  top: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.72), transparent);
}

.auth-ornament-flow { display: flex; align-items: center; justify-content: center; }
.auth-hud-line {
  position: absolute;
  left: 18px;
  right: 18px;
  height: 2px;
  border-radius: 999px;
  background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.82), rgba(129, 140, 248, 0.42), transparent);
  overflow: hidden;
}
.auth-hud-line::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, #ffffff, transparent);
  transform: translateX(-100%);
  animation: authHudSweep 2.8s ease-in-out infinite;
}
.auth-line-a { top: 22px; }
.auth-line-b { top: 35px; opacity: 0.72; }
.auth-line-c { top: 48px; opacity: 0.48; }
.auth-hud-node {
  position: absolute;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #38bdf8;
  box-shadow: 0 0 14px rgba(56, 189, 248, 0.82);
}
.auth-node-a { left: 22px; top: 32px; }
.auth-node-b { right: 24px; top: 32px; background: #818cf8; }

.auth-ornament-orbit { display: flex; align-items: center; justify-content: center; }
.auth-orbit-ring {
  position: absolute;
  border-radius: 50%;
  border: 1px solid rgba(56, 189, 248, 0.32);
}
.auth-ring-a {
  width: 48px;
  height: 48px;
  border-top-color: rgba(129, 140, 248, 0.85);
  animation: authSpinCW 9s linear infinite;
}
.auth-ring-b {
  width: 32px;
  height: 32px;
  border-right-color: rgba(56, 189, 248, 0.85);
  animation: authSpinCCW 7s linear infinite;
}
.auth-orbit-core {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #ffffff;
  box-shadow: 0 0 18px rgba(56, 189, 248, 0.9), 0 0 32px rgba(129, 140, 248, 0.35);
}

.auth-ornament-matrix {
  display: grid;
  grid-template-columns: repeat(4, 8px);
  grid-auto-rows: 8px;
  place-content: center;
  gap: 7px;
}
.auth-ornament-matrix span {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: rgba(56, 189, 248, 0.45);
  animation: authMatrixSoft 2s ease-in-out infinite alternate;
}
.auth-ornament-matrix span:nth-child(3n) { background: rgba(129, 140, 248, 0.58); animation-delay: 0.35s; }
.auth-ornament-matrix span:nth-child(4n) { opacity: 0.34; animation-delay: 0.7s; }

.auth-ornament-wave {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.auth-ornament-wave span {
  width: 4px;
  height: 34px;
  border-radius: 999px;
  background: linear-gradient(to top, rgba(56, 189, 248, 0.16), rgba(129, 140, 248, 0.82), rgba(56, 189, 248, 0.16));
  animation: authWaveSoft 1.8s ease-in-out infinite alternate;
}
.auth-ornament-wave span:nth-child(2) { animation-delay: 0.15s; }
.auth-ornament-wave span:nth-child(3) { animation-delay: 0.3s; }
.auth-ornament-wave span:nth-child(4) { animation-delay: 0.45s; }
.auth-ornament-wave span:nth-child(5) { animation-delay: 0.6s; }
.auth-ornament-wave i {
  position: absolute;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  border: 1px solid rgba(56, 189, 248, 0.2);
  border-top-color: rgba(56, 189, 248, 0.68);
  animation: authSpinCW 8s linear infinite;
}

@keyframes authCornerDeploy {
  0% { opacity: 0; clip-path: inset(0 100% 100% 0); filter: blur(8px); }
  100% { opacity: 0.92; clip-path: inset(0 0 0 0); filter: blur(0); }
}
@keyframes authHudDeploy {
  0% { opacity: 0; transform: translateY(18px) scale(0.96); filter: blur(8px); }
  100% { opacity: 1; transform: translateY(0) scale(1); filter: blur(0); }
}
@keyframes authHudSweep { 45%, 100% { transform: translateX(100%); } }
@keyframes authMatrixSoft { from { opacity: 0.25; transform: scale(0.86); } to { opacity: 1; transform: scale(1.08); } }
@keyframes authWaveSoft { from { transform: scaleY(0.34); opacity: 0.42; } to { transform: scaleY(1); opacity: 1; } }
@keyframes authSpinCW { 100% { transform: rotate(360deg); } }
@keyframes authSpinCCW { 100% { transform: rotate(-360deg); } }

@media (prefers-reduced-motion: reduce) {
  .macro-orb,
  h1 {
    animation: none !important;
  }
}

/* ── Global toast ────────────────────────────────────────────────────────── */
.global-toast {
  position: fixed;
  bottom: 32px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 9999;
  padding: 10px 28px;
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.92);
  color: #f8fafc;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.04em;
  backdrop-filter: blur(12px);
  box-shadow: 0 8px 30px rgba(15, 23, 42, 0.28);
  pointer-events: none;
}

.toast-fade-enter-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.toast-fade-leave-active { transition: opacity 0.35s ease, transform 0.35s ease; }
.toast-fade-enter-from { opacity: 0; transform: translateX(-50%) translateY(12px); }
.toast-fade-leave-to   { opacity: 0; transform: translateX(-50%) translateY(-8px); }
</style>

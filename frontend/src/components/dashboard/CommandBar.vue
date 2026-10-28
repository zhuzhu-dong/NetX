<template>
  <header class="site-title">
    <h1>NetPulse</h1>
    <p>意图驱动 · 多智能体自治网络</p>
  </header>
</template>

<style scoped>
.site-title {
  min-height: 74px;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 8px;
  border: 1px solid rgba(226, 232, 240, 0.9);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.72);
  box-shadow: 0 24px 58px rgba(15, 23, 42, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.9);
  backdrop-filter: blur(16px);
}

h1,
p {
  margin: 0;
}

h1 {
  color: #0f172a;
  font-size: 2.05rem;
  line-height: 1;
  font-weight: 900;
  letter-spacing: 0;
  background: linear-gradient(110deg, #0f172a 0%, #0284c7 34%, #818cf8 64%, #0f172a 100%);
  background-size: 220% auto;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  animation: titleFlow 6s linear infinite;
}

p {
  color: #475569;
  font-size: 0.92rem;
  font-weight: 800;
  letter-spacing: 3px;
}

@keyframes titleFlow {
  to { background-position: 220% center; }
}
</style>

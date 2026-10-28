<template>
  <section class="intent-composer">
    <div class="section-title">
      <span></span>
      <h2>意图编排</h2>
    </div>

    <div class="composer-box">
      <textarea
        :value="modelValue"
        placeholder="输入自然语言网络意图..."
        @focus="$emit('focus-change', true)"
        @blur="$emit('focus-change', false)"
        @input="$emit('update:modelValue', $event.target.value)"
      />

      <div class="composer-actions">
        <div class="priority-group">
          <button
            v-for="item in priorities"
            :key="item"
            type="button"
            :class="{ active: item === priority }"
            @click="$emit('update:priority', item)"
          >
            {{ item }}
          </button>
        </div>
        <button type="button" class="submit-btn" :disabled="!modelValue.trim()" @click="$emit('submit')">
          提交意图
        </button>
      </div>
    </div>

    <div class="template-grid">
      <button v-for="item in templates" :key="item.name" type="button" @click="$emit('apply-template', item)">
        <span>{{ item.name }}</span>
        <small>{{ item.summary }}</small>
      </button>
    </div>
  </section>
</template>

<script setup>
defineProps({
  modelValue: { type: String, default: '' },
  priorities: { type: Array, default: () => [] },
  priority: { type: String, default: 'High' },
  templates: { type: Array, default: () => [] },
})

defineEmits(['update:modelValue', 'update:priority', 'submit', 'apply-template', 'focus-change'])
</script>

<style scoped>
.intent-composer {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}

.section-title span {
  width: 22px;
  height: 3px;
  border-radius: 999px;
  background: linear-gradient(90deg, #38bdf8, #818cf8);
}

h2 {
  margin: 0;
  color: #0f172a;
  font-size: 1rem;
  letter-spacing: 0;
}

.composer-box {
  border: 1px solid rgba(203, 213, 225, 0.8);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.72);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.88);
  overflow: hidden;
}

textarea {
  display: block;
  width: 100%;
  min-height: 126px;
  resize: vertical;
  box-sizing: border-box;
  padding: 18px 18px 12px;
  border: 0;
  outline: 0;
  color: #1e293b;
  background:
    linear-gradient(180deg, rgba(248, 250, 252, 0.9), rgba(255, 255, 255, 0.62)),
    radial-gradient(circle at 12% 10%, rgba(56, 189, 248, 0.12), transparent 32%);
  font-family: inherit;
  font-size: 0.98rem;
  font-weight: 650;
  line-height: 1.7;
  user-select: text;
}

textarea:focus {
  box-shadow: inset 0 0 0 1px rgba(56, 189, 248, 0.42);
}

.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 12px;
  border-top: 1px solid rgba(226, 232, 240, 0.9);
}

.priority-group {
  display: flex;
  gap: 7px;
}

button {
  height: 34px;
  border: 1px solid rgba(203, 213, 225, 0.88);
  border-radius: 999px;
  color: #475569;
  background: rgba(255, 255, 255, 0.8);
  font: inherit;
  font-size: 0.78rem;
  font-weight: 850;
  cursor: pointer;
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
}

button:hover {
  transform: translateY(-1px);
  border-color: rgba(56, 189, 248, 0.48);
}

.priority-group button {
  min-width: 68px;
}

.priority-group .active {
  color: #0f172a;
  border-color: rgba(56, 189, 248, 0.6);
  background: linear-gradient(135deg, rgba(224, 242, 254, 0.95), rgba(238, 242, 255, 0.95));
  box-shadow: 0 10px 22px rgba(14, 165, 233, 0.12);
}

.submit-btn {
  min-width: 118px;
  color: #ffffff;
  border: 0;
  background: linear-gradient(135deg, #0ea5e9, #818cf8);
  box-shadow: 0 16px 30px rgba(14, 165, 233, 0.22);
}

.submit-btn:disabled {
  cursor: not-allowed;
  opacity: 0.42;
  transform: none;
}

.template-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.template-grid button {
  height: 58px;
  border-radius: 12px;
  text-align: left;
  padding: 0 14px;
  color: #334155;
  background: rgba(248, 250, 252, 0.72);
}

.template-grid span,
.template-grid small {
  display: block;
}

.template-grid span {
  font-size: 0.8rem;
}

.template-grid small {
  margin-top: 4px;
  color: #64748b;
  font-size: 0.66rem;
  font-weight: 750;
}

@media (max-width: 760px) {
  .composer-actions,
  .priority-group {
    flex-wrap: wrap;
  }

  .template-grid {
    grid-template-columns: 1fr;
  }
}
</style>

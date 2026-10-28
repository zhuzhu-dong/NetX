/**
 * useStorage.js — 版本化 localStorage 读写 wrapper
 *
 * 封装 schema versioning：写入时自动追加 _v 字段，读取时检查版本号并调用 migrate。
 * 旧数据（无 _v 字段）视为 v0，migrate 回调负责升级。
 *
 * 用法:
 *   const storage = useStorage()
 *   storage.write(STORAGE_KEYS.SESSION, { email, name })
 *   const session = storage.read(STORAGE_KEYS.SESSION, null, (data, v) => data)
 */

const SCHEMA_VERSION = 1

export function useStorage() {
  function read(key, fallback = null, migrate = (data, _v) => data) {
    try {
      const raw = localStorage.getItem(key)
      if (raw === null) return fallback
      const envelope = JSON.parse(raw)
      // 如果已有版本号 → 迁移；否则 → 旧数据视为 v0
      if (envelope && typeof envelope === 'object' && '_v' in envelope) {
        return migrate(envelope.data, envelope._v)
      }
      // 旧格式 — 无 _v 字段
      return migrate(envelope, 0)
    } catch (_) {
      return fallback
    }
  }

  function write(key, data) {
    try {
      localStorage.setItem(key, JSON.stringify({ _v: SCHEMA_VERSION, data }))
    } catch (_) {
      // quota exceeded — silently fail
    }
  }

  function remove(key) {
    try {
      localStorage.removeItem(key)
    } catch (_) {
      /* ok */
    }
  }

  function has(key) {
    try {
      return localStorage.getItem(key) !== null
    } catch (_) {
      return false
    }
  }

  return { read, write, remove, has, SCHEMA_VERSION }
}

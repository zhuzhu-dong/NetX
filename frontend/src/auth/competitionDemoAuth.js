/**
 * Competition-only browser entry gate, modelled on origin/main's local
 * registration/login flow.  It deliberately grants UI access only; it never
 * supplies backend credentials or changes the real API client.
 *
 * The browser stores a derived verifier rather than a plaintext password.
 * This is not a security boundary and must not be used for an operator or
 * production identity system.
 */
export const DEMO_AUTH_STORAGE_KEYS = Object.freeze({
  ACCOUNT: 'netpulse-local-account',
  SESSION: 'netpulse-auth-session',
})

function normalizeEmail(value) {
  return String(value || '').trim().toLowerCase()
}

// A dependency-free demo verifier that works over the public HTTP entrypoint
// as well as secure contexts.  It avoids persisting a raw password; it is not
// presented as cryptographic authentication.
export function demoVerifier(email, password, salt) {
  const input = `${normalizeEmail(email)}\u0000${String(salt || '')}\u0000${String(password || '')}`
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index)
    first = Math.imul(first ^ code, 0x01000193) >>> 0
    second = Math.imul(second ^ (code + index), 0x85ebca6b) >>> 0
  }
  return `${first.toString(16).padStart(8, '0')}${second.toString(16).padStart(8, '0')}`
}

function parse(storage, key) {
  try { return JSON.parse(storage.getItem(key) || 'null') } catch (_) { return null }
}

function localSalt(email) {
  const entropy = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`
  return demoVerifier(email, entropy, 'netpulse-demo-salt')
}

export function createCompetitionDemoAuth(storage = globalThis.localStorage) {
  function account() {
    const value = parse(storage, DEMO_AUTH_STORAGE_KEYS.ACCOUNT)
    return value && typeof value.email === 'string' && typeof value.salt === 'string' && typeof value.verifier === 'string'
      ? value
      : null
  }

  function currentUser() {
    const session = parse(storage, DEMO_AUTH_STORAGE_KEYS.SESSION)
    if (!session || typeof session.email !== 'string' || !session.email) return null
    return {
      user_id: session.email,
      display_name: session.email,
      role: 'OPERATOR',
      auth_mode: 'competition_demo',
    }
  }

  function register({ email, password }) {
    const normalized = normalizeEmail(email)
    if (!normalized || !String(password || '')) throw new Error('DEMO_CREDENTIALS_REQUIRED')
    const salt = localSalt(normalized)
    storage.setItem(DEMO_AUTH_STORAGE_KEYS.ACCOUNT, JSON.stringify({
      email: normalized,
      salt,
      verifier: demoVerifier(normalized, password, salt),
      registered_at: new Date().toISOString(),
    }))
    return { user_id: normalized, display_name: normalized, role: 'OPERATOR', auth_mode: 'competition_demo' }
  }

  function login({ email, password }) {
    const stored = account()
    const normalized = normalizeEmail(email)
    if (!stored || stored.email !== normalized || stored.verifier !== demoVerifier(normalized, password, stored.salt)) {
      throw new Error('DEMO_CREDENTIALS_INVALID')
    }
    storage.setItem(DEMO_AUTH_STORAGE_KEYS.SESSION, JSON.stringify({
      email: normalized,
      role: 'OPERATOR',
      login_at: new Date().toISOString(),
    }))
    return currentUser()
  }

  function logout() {
    storage.removeItem(DEMO_AUTH_STORAGE_KEYS.SESSION)
  }

  return { account, currentUser, register, login, logout }
}

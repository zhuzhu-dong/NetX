import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createCompetitionDemoAuth, DEMO_AUTH_STORAGE_KEYS } from '../src/auth/competitionDemoAuth.js'

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
}

test('main-style local registration supports a correct demo credential without storing plaintext password', () => {
  const storage = memoryStorage()
  const auth = createCompetitionDemoAuth(storage)
  auth.register({ email: 'operator@example.test', password: 'example-passphrase' })

  const serializedAccount = storage.getItem(DEMO_AUTH_STORAGE_KEYS.ACCOUNT)
  assert.ok(serializedAccount)
  assert.doesNotMatch(serializedAccount, /example-passphrase/)
  assert.equal(auth.currentUser(), null)
  assert.equal(auth.login({ email: 'operator@example.test', password: 'example-passphrase' }).role, 'OPERATOR')
  assert.equal(auth.currentUser().user_id, 'operator@example.test')
})

test('invalid demo credential is rejected and logout clears only demo auth state', () => {
  const storage = memoryStorage()
  const auth = createCompetitionDemoAuth(storage)
  auth.register({ email: 'operator@example.test', password: 'example-passphrase' })
  assert.throws(() => auth.login({ email: 'operator@example.test', password: 'incorrect' }), /DEMO_CREDENTIALS_INVALID/)
  auth.login({ email: 'operator@example.test', password: 'example-passphrase' })
  auth.logout()
  assert.equal(storage.getItem(DEMO_AUTH_STORAGE_KEYS.SESSION), null)
  assert.equal(auth.currentUser(), null)
})

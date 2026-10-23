import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class MemoryStorage {
  private values = new Map<string, string>()

  getItem(key: string) {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string) {
    this.values.set(key, value)
  }

  removeItem(key: string) {
    this.values.delete(key)
  }
}

const installBrowserGlobals = () => {
  const storage = new MemoryStorage()
  const sessionStorage = new MemoryStorage()
  const cookies = new Map<string, string>()

  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      localStorage: storage,
      sessionStorage,
      location: { protocol: 'http:', origin: 'http://localhost:3000' },
    },
  })
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: {},
  })
  Object.defineProperty(globalThis.document, 'cookie', {
    configurable: true,
    get() {
      return Array.from(cookies.entries()).map(([key, value]) => `${key}=${value}`).join('; ')
    },
    set(value: string) {
      const [pair, ...attrs] = value.split(';').map((part) => part.trim())
      const [key, raw = ''] = pair.split('=')
      if (attrs.some((attr) => attr.toLowerCase() === 'max-age=0')) {
        cookies.delete(key)
        return
      }
      cookies.set(key, raw)
    },
  })

  return { storage, sessionStorage }
}

const importUserToken = async () => {
  vi.resetModules()
  return (await import('@/api/user')).userToken
}

const makeUserToken = (expiresAt: number) => {
  const payload = `user-id:alice:${expiresAt}:nonce`
  return `${btoa(payload).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')}.sig`
}

const importAdminToken = async () => {
  vi.resetModules()
  return (await import('@/api/admin')).adminToken
}

describe('userToken persistence', () => {
  beforeEach(() => {
    installBrowserGlobals()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete (globalThis as any).window
    delete (globalThis as any).document
  })

  it('writes user tokens to localStorage and a host cookie', async () => {
    const userToken = await importUserToken()

    userToken.set('token-for-test')

    expect(window.localStorage.getItem('raven_user_token')).toBe('token-for-test')
    expect(document.cookie).toContain('raven_user_token=token-for-test')
  })

  it('falls back to the host cookie and hydrates localStorage', async () => {
    const userToken = await importUserToken()
    document.cookie = 'raven_user_token=cookie-token; Path=/; Max-Age=3600; SameSite=Lax'

    expect(userToken.get()).toBe('cookie-token')
    expect(window.localStorage.getItem('raven_user_token')).toBe('cookie-token')
  })

  it('migrates an existing localStorage token into the host cookie', async () => {
    const userToken = await importUserToken()
    window.localStorage.setItem('raven_user_token', 'stored-token')

    expect(userToken.get()).toBe('stored-token')
    expect(document.cookie).toContain('raven_user_token=stored-token')
  })

  it('clears both localStorage and the host cookie', async () => {
    const userToken = await importUserToken()
    userToken.set('token-for-test')

    userToken.clear()

    expect(window.localStorage.getItem('raven_user_token')).toBeNull()
    expect(document.cookie).not.toContain('raven_user_token=')
  })

  it('detects expired Raven login tokens from their payload', async () => {
    const userToken = await importUserToken()

    expect(userToken.isExpired(makeUserToken(Math.floor(Date.now() / 1000) - 1))).toBe(true)
    expect(userToken.isExpired(makeUserToken(Math.floor(Date.now() / 1000) + 60))).toBe(false)
  })

  it('lets admin auth fall back to the persisted user token', async () => {
    const userToken = await importUserToken()
    userToken.set('admin-user-token')

    const adminToken = await importAdminToken()

    expect(window.sessionStorage.getItem('raven_admin_token')).toBeNull()
    expect(adminToken.get()).toBe('admin-user-token')
  })

  it('probes workbench admin access with the current user instead of a legacy admin token', async () => {
    vi.resetModules()
    const axios = (await import('axios')).default
    const previousAdapter = axios.defaults.adapter
    const adapter = vi.fn(async (config) => ({
      data: { success: true, data: { access_level: 'project_member' } },
      status: 200,
      statusText: 'OK',
      headers: {},
      config,
    }))
    axios.defaults.adapter = adapter
    try {
      const { userApi, userToken } = await import('@/api/user')
      userToken.set('current-member-token')
      window.sessionStorage.setItem('raven_admin_token', 'legacy-admin-token')

      await userApi.adminIdentity()

      expect(adapter).toHaveBeenCalledTimes(1)
      const config = adapter.mock.calls[0][0]
      expect(config.url).toBe('/admin/auth/me')
      expect(config.headers.get('Authorization')).toBe('Bearer current-member-token')
    } finally {
      axios.defaults.adapter = previousAdapter
    }
  })
})

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { userApi } from '@/api/user'
import { useUserStore } from './user'
import type { AdminIdentity, ApiResponse } from '@/types'

vi.mock('@/api/user', () => ({
  userApi: { adminIdentity: vi.fn() },
  userToken: { get: () => 'member-token', set: vi.fn(), clear: vi.fn() },
}))

const member: AdminIdentity = {
  username: 'member',
  access_level: 'project_member',
  allowed_nav_keys: ['project-repos'],
  allowed_project_ids: [1],
  allowed_project_codes: ['alpha'],
}
const probe = vi.mocked(userApi.adminIdentity)
const deferredIdentity = () => {
  let resolve!: (value: ApiResponse<AdminIdentity>) => void
  const promise = new Promise<ApiResponse<AdminIdentity>>((done) => { resolve = done })
  return { promise, resolve }
}

describe('workbench admin access', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    probe.mockReset()
  })

  it.each(['project_member', 'global_admin'] as const)(
    'allows the admin entry for a backend-authorized %s',
    async (accessLevel) => {
      const store = useUserStore()
      const identity = { ...member, access_level: accessLevel }
      probe.mockResolvedValue({ success: true, data: identity })

      expect(store.canAccessAdmin).toBe(false)
      await store.refreshAdminAccess()

      expect(store.canAccessAdmin).toBe(true)
      expect(store.adminIdentity).toEqual(identity)
    },
  )

  it('picks up a newly granted membership and its later removal without a new login', async () => {
    const store = useUserStore()
    probe.mockRejectedValueOnce({ response: { status: 403 } })
    probe.mockResolvedValueOnce({ success: true, data: member })
    probe.mockRejectedValueOnce({ response: { status: 403 } })

    await store.refreshAdminAccess()
    expect(store.canAccessAdmin).toBe(false)
    await store.refreshAdminAccess()
    expect(store.canAccessAdmin).toBe(true)
    await store.refreshAdminAccess()
    expect(store.canAccessAdmin).toBe(false)
    expect(store.token).toBe('member-token')
  })

  it('hides the entry if the identity probe fails', async () => {
    const store = useUserStore()
    probe.mockResolvedValueOnce({ success: true, data: member })
    await store.refreshAdminAccess()
    probe.mockRejectedValueOnce(new Error('network down'))

    await store.refreshAdminAccess()

    expect(store.canAccessAdmin).toBe(false)
  })

  it('does not probe or show admin access while logged out', async () => {
    const store = useUserStore()
    store.clear()

    await store.refreshAdminAccess()

    expect(probe).not.toHaveBeenCalled()
    expect(store.canAccessAdmin).toBe(false)
  })

  it('ignores an identity response that arrives after logout', async () => {
    const store = useUserStore()
    const response = deferredIdentity()
    probe.mockReturnValue(response.promise)

    const pending = store.refreshAdminAccess()
    store.clear()
    response.resolve({ success: true, data: member })
    await pending

    expect(store.adminIdentity).toBeNull()
    expect(store.canAccessAdmin).toBe(false)
  })

  it('clears access when switching accounts and ignores the previous account response', async () => {
    const store = useUserStore()
    probe.mockResolvedValueOnce({ success: true, data: member })
    await store.refreshAdminAccess()
    const response = deferredIdentity()
    probe.mockReturnValueOnce(response.promise)
    const pending = store.refreshAdminAccess()

    store.setToken('outsider-token')
    response.resolve({ success: true, data: member })
    await pending

    expect(store.canAccessAdmin).toBe(false)
    expect(store.adminIdentity).toBeNull()
  })

  it('ignores an older successful probe after a newer probe denies access', async () => {
    const store = useUserStore()
    const response = deferredIdentity()
    probe.mockReturnValueOnce(response.promise)
    const pending = store.refreshAdminAccess()
    probe.mockRejectedValueOnce({ response: { status: 403 } })

    await store.refreshAdminAccess()
    response.resolve({ success: true, data: member })
    await pending

    expect(store.canAccessAdmin).toBe(false)
  })
})

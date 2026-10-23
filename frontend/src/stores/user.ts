import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { userApi, userToken } from '@/api/user'
import type { AdminIdentity, UserProfile } from '@/types'

export const useUserStore = defineStore('user', () => {
  const token = ref(userToken.get())
  const profile = ref<UserProfile | null>(null)
  const adminIdentity = ref<AdminIdentity | null>(null)
  const isAuthenticated = computed(() => Boolean(token.value))
  const canAccessAdmin = computed(() => isAuthenticated.value && adminIdentity.value !== null)
  let adminAccessRequest = 0

  const setToken = (value: string) => {
    if (value !== token.value) {
      adminAccessRequest += 1
      adminIdentity.value = null
    }
    token.value = value
    if (value) {
      userToken.set(value)
    } else {
      userToken.clear()
    }
  }

  const setProfile = (value: UserProfile | null) => {
    profile.value = value
  }

  const clear = () => {
    setToken('')
    setProfile(null)
  }

  const refreshAdminAccess = async () => {
    const request = ++adminAccessRequest
    const requestToken = token.value
    adminIdentity.value = null
    if (!requestToken) return
    try {
      const resp = await userApi.adminIdentity()
      if (request === adminAccessRequest && token.value === requestToken) {
        adminIdentity.value = resp?.success && resp.data ? resp.data : null
      }
    } catch {
      // Ordinary users receive 403; a failed probe must not grant admin access.
    }
  }

  const bootstrap = async () => {
    const existing = userToken.get()
    if (!existing) return
    token.value = existing
    try {
      const resp = await userApi.me()
      if (resp?.success && resp.data) {
        profile.value = resp.data
      } else {
        clear()
      }
    } catch {
      clear()
    }
  }

  return {
    token,
    profile,
    adminIdentity,
    isAuthenticated,
    canAccessAdmin,
    refreshAdminAccess,
    setToken,
    setProfile,
    clear,
    bootstrap,
  }
})

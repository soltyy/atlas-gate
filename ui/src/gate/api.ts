// Клиент админских маршрутов гейта. Страница открывается только с машины роутера, но доверия к
// loopback у роутера с гейтом нет (#2): каждый запрос несёт X-Atlas-Token, который вводит
// администратор. Токен живёт в sessionStorage вкладки — не переживает закрытие браузера.

export const TOKEN_KEY = 'atlas.gate.adminToken'

export function adminToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setAdminToken(value: string): void {
  try {
    if (value.trim()) sessionStorage.setItem(TOKEN_KEY, value.trim())
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // sessionStorage недоступен — токен придётся вводить заново
  }
}

export interface Enrollment {
  user_code: string
  device_name: string
  platform: string
  app_version: string
  status: string
  created_at: number
  expires_at: number
  org?: string | null
  device_id?: string | null
}

export interface Device {
  device_id: string
  org: string
  user_id: string
  user_email: string
  user_name: string
  device_name: string
  platform: string
  app_version: string
  created_at: number
  revoked_at: number | null
}

export interface Reloaded {
  orgs: Record<string, number>
  errors: Record<string, string>
  warnings: Record<string, string[]>
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const token = adminToken()
  if (token) headers['X-Atlas-Token'] = token
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const message = data?.error?.message ?? `HTTP ${res.status}`
    throw new Error(message)
  }
  return data as T
}

export const gateApi = {
  enrollments: () => call<{ enrollments: Enrollment[]; orgs: string[] }>('GET', '/harness/admin/enrollments?status=pending'),
  devices: () => call<{ devices: Device[] }>('GET', '/harness/admin/devices'),
  approve: (user_code: string, org: string, user: { id: string; email: string; name: string }) =>
    call<{ ok: boolean }>('POST', '/harness/enroll/approve', { user_code, org, user }),
  deny: (user_code: string) => call<{ ok: boolean }>('POST', '/harness/enroll/deny', { user_code }),
  revoke: (device_id: string) => call<{ ok: boolean }>('POST', `/harness/devices/${encodeURIComponent(device_id)}/revoke`),
  reload: () => call<Reloaded>('POST', '/harness/admin/reload'),
}

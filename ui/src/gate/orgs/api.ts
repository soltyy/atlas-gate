// Клиент редактора организаций: /harness/admin/orgs*. Токен администратора — как у остальной админки.
import { adminToken } from '../api'
import type { FieldError, HistoryEntry, OrgConfig, OrgDetails, OrgSummary, Saved } from './types'

/** Отказ сервера с кодом и ошибками по полям (422 profile_invalid, 409 conflict). */
export class OrgApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly fields: FieldError[] = [],
  ) {
    super(message)
  }
}

async function call<T>(method: string, path: string, body?: unknown, extra: Record<string, string> = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...extra }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const token = adminToken()
  if (token) headers['X-Atlas-Token'] = token
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const err = data?.error ?? {}
    throw new OrgApiError(err.message ?? `HTTP ${res.status}`, res.status, err.code ?? 'error', err.fields ?? [])
  }
  return data as T
}

const base = (org: string) => `/harness/admin/orgs/${encodeURIComponent(org)}`

export const orgApi = {
  list: () => call<{ orgs: OrgSummary[]; errors: Record<string, string> }>('GET', '/harness/admin/orgs'),
  get: (org: string) => call<OrgDetails>('GET', base(org)),
  save: (org: string, config: OrgConfig, etag: string) => call<Saved>('PUT', base(org), config, { 'If-Match': etag }),
  history: (org: string) => call<{ org: string; current: number | null; versions: HistoryEntry[] }>('GET', `${base(org)}/history`),
  version: (org: string, version: number) => call<OrgConfig>('GET', `${base(org)}/history/${version}`),
  restore: (org: string, version: number) => call<Saved>('POST', `${base(org)}/history/${version}/restore`),
  /** source: 'template' | `copy_of:${org}` (#7). */
  create: (id: string, name: string, source: string) =>
    call<Saved & { org: string }>('POST', '/harness/admin/orgs', { id, name, source }),
  rename: (org: string, name: string) => call<Saved>('PATCH', base(org), { name }),
  archive: (org: string) => call<Saved & { archived: boolean }>('POST', `${base(org)}/archive`),
  unarchive: (org: string) => call<Saved & { archived: boolean }>('POST', `${base(org)}/unarchive`),
}

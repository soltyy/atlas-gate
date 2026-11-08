import { mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { flush, jsonResponse } from '../test-utils'
import UsageSection from './UsageSection.vue'
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear() })
const summary = { records: 2, legacyRecords: 1, incompleteRecords: 0, totalTokens: 103, totalTokensRecords: 1, cacheAdjustedTokens: 103, cacheAdjustedTokensRecords: 1, totalInputTokens: 100, totalInputTokensRecords: 1, uncachedInputTokens: null, uncachedInputTokensRecords: 0, cacheReadTokens: 0, cacheReadTokensRecords: 1, cacheWriteTokens: null, cacheWriteTokensRecords: 0, outputTokens: 3, outputTokensRecords: 2, cacheReadPercent: 0, cacheComparableRecords: 1 }
const user = { org: 'first', userId: 'user', userName: 'Алексей', userEmail: 'user@example.invalid' }
const row = { ...user, id: 2, ts: 1, deviceId: 'device', deviceName: 'EDT', model: 'gpt-6.1-sol', route: 'sub', source: 'router-sdk', status: 'success', totalInputTokens: 100, uncachedInputTokens: null, cacheReadTokens: 0, cacheWriteTokens: null, outputTokens: 3, durationMs: 1000, sessionId: 'session', turnId: 'turn', modelUsage: {} }
it('показывает ноль отдельно от неизвестного и сохраняет фильтры/период пагинации', async () => {
  sessionStorage.setItem('atlas.gate.adminToken', 'admin')
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({
    since: 1, until: 2, summary, groups: [{ ...summary, ...user, model: 'gpt-6.1-sol', kind: 'agent' }],
    rows: [row], trend: [], options: { users: [user], models: ['gpt-6.1-sol'], routes: ['sub'] }, nextBefore: 2,
  }))
  vi.stubGlobal('fetch', fetcher)
  const w = mount(UsageSection); await flush()
  expect(w.text()).toContain('нет данных')
  expect(w.text()).toContain('Без чтения кеша')
  expect(w.text()).toContain('0.0 %')
  expect(w.text()).toContain('Записей без детализации SDK: 1')
  expect(w.text()).toContain('Алексей')
  expect(w.text()).toContain('За вычетом чтения из кеша')
  const params = new URL(fetcher.mock.calls[0][0] as string, 'http://localhost').searchParams
  await w.findAll('button').find(b => b.text() === 'Показать ещё')!.trigger('click'); await flush()
  const second = new URL(fetcher.mock.calls[1][0] as string, 'http://localhost').searchParams
  expect(second.get('before')).toBe('2'); expect(second.get('until')).toBe(params.get('until'))
  expect((fetcher.mock.calls[0][1] as RequestInit).headers).toMatchObject({ 'X-Atlas-Token': 'admin' })
  await w.get('#usage-model').setValue('opus')
  await w.findAll('select').find(s => s.text().includes('Неделя с понедельника'))!.setValue('week')
  await w.findAll('select').find(s => s.text().includes('Алексей'))!.setValue(JSON.stringify([user.org, user.userId]))
  await w.find('form').trigger('submit'); await flush()
  const filtered = new URL(fetcher.mock.calls[2][0] as string, 'http://localhost').searchParams
  expect(filtered.get('model')).toBe('opus')
  expect(filtered.get('bucket')).toBe('week')
  expect(filtered.get('org')).toBe(user.org)
  expect(filtered.get('user')).toBe(user.userId)
})
it('отображает отказ API и позволяет повторить запрос', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ error: { message: 'Нет доступа' } }, 401)))
  const w = mount(UsageSection); await flush()
  expect(w.find('[role=alert]').text()).toBe('Нет доступа')
  expect(w.find('button').attributes('disabled')).toBeUndefined()
})

it('выгружает выбранные фильтры без курсора пагинации', async () => {
  sessionStorage.setItem('atlas.gate.adminToken', 'admin')
  const fetcher = vi.fn(async (url: string, _init?: RequestInit) => url.includes('usage.xlsx')
    ? new Response(new Blob(['xlsx']))
    : jsonResponse({ since: 1, until: 2, summary, groups: [], rows: [row], trend: [], nextBefore: 2 }))
  vi.stubGlobal('fetch', fetcher)
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL() { return 'blob:usage' }
    static revokeObjectURL() {}
  })
  const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  try {
    const w = mount(UsageSection); await flush()
    await w.get('#usage-model').setValue('opus')
    await w.findAll('button').find(b => b.text() === 'Скачать Excel')!.trigger('click')
    await flush()
    const call = fetcher.mock.calls.find(([url]) => url.includes('usage.xlsx'))!
    const query = new URL(call[0], 'http://localhost').searchParams
    expect(query.get('model')).toBe('opus')
    expect(query.get('group_by')).toBe('user_model')
    expect(query.has('before')).toBe(false)
    expect(call[1]?.headers).toMatchObject({ 'X-Atlas-Token': 'admin' })
    expect(clicked).toHaveBeenCalledOnce()
  } finally {
    clicked.mockRestore()
  }
})

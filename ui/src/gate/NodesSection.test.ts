import { mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { flush, mockFetch } from '../test-utils'
import NodesSection from './NodesSection.vue'
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear() })
it('показывает владельца беседы, нагрузку и отправляет drain с admin token', async () => {
  sessionStorage.setItem('atlas.gate.adminToken', 'admin')
  const drain = vi.fn(() => ({ draining: true }))
  const fetcher = mockFetch({
    'GET /harness/admin/nodes': { nodes: [{ node_id: 'machine-a', enabled: true, revoked: false, transport: 'connector', account_group: 'shared', account_turn_capacity: 4, cooldown_seconds: 0, credential: null, orgs: ['org'], routes: ['sub'], status: { backend: 'claude', ready: true, draining: false, sessions: 1, capacity: 4, reservedUnits: 2, turnCapacity: 4, activeSubagents: 1, quota: null }, bindings: [{ id: 'public-session', route: 'sub', status: 'ready', device_id: 'device' }] }] },
    'GET /harness/admin/node-enrollments': { enrollments: [] },
    'POST /harness/admin/nodes/machine-a/drain': drain,
  })
  const w = mount(NodesSection)
  await flush()
  expect(w.text()).toContain('machine-a')
  expect(w.text()).toContain('public-session')
  expect(w.text()).toContain('2/4')
  await w.findAll('button').find(b => b.text() === 'Остановить приём новых ходов')!.trigger('click')
  await flush()
  expect(drain).toHaveBeenCalledWith({ enabled: true })
  const sent = fetcher.mock.calls.find(c => c[0] === '/harness/admin/nodes/machine-a/drain')
  expect((sent![1] as RequestInit).headers).toMatchObject({ 'X-Atlas-Token': 'admin' })
})

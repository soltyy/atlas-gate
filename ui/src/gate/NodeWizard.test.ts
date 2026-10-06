import { mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { flush, mockFetch } from '../test-utils'
import NodeWizard from './NodeWizard.vue'
const options = { revision: 'rev', gate_url: 'https://gate.example.org', mtls_port: 0,
  orgs: [{ id: 'org', name: 'Компания', routes: [{ id: 'codex-sub', backends: ['codex'] }, { id: 'claude-sub', backends: ['claude'] }] }],
  account_groups: [{ id: 'existing', capacity: 0, backends: ['codex'] }] }
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear() })
async function ready(w: ReturnType<typeof mount>, placement = 'connector') {
  await flush()
  await w.get(`input[type="radio"][value="${placement}"]`).setValue()
  await w.get('input[placeholder="office-codex-02"]').setValue('office-2')
  await w.get('form').trigger('submit'); await flush()
  expect(w.text()).toContain('codex-sub')
  expect(w.text()).not.toContain('claude-sub')
  await w.get('input[type="checkbox"][value="codex-sub"]').setValue(true)
  await w.get('form').trigger('submit'); await flush()
  expect(w.text()).toContain('ATLAS_NODE_CAPACITY=0')
  expect(w.text()).toContain('ATLAS_NODE_TURN_CAPACITY=0')
  await w.get('input[type="checkbox"]').setValue(true)
}
it('проводит connector через доступ, отдельный аккаунт, приглашение и команды без передачи локального ключа', async () => {
  const add = vi.fn(() => ({ node_id: 'office-2', status: null }))
  const invite = vi.fn(() => ({ invitation: 'one-time', expiresIn: 900 }))
  mockFetch({ 'GET /harness/admin/node-options': options, 'POST /harness/admin/nodes': add, 'POST /harness/admin/nodes/office-2/invite': invite })
  const w = mount(NodeWizard, { props: { nodes: [] } })
  await ready(w)
  await w.get('form').trigger('submit'); await flush()
  expect(add).toHaveBeenCalledWith(expect.objectContaining({ transport: 'connector', token: '', url: '', share_account: false, account_turn_capacity: 0, routes: ['codex-sub'] }))
  expect(w.text()).toContain('uv run atlas-node enroll')
  expect(w.text()).toContain('Одобрите заявку')
  expect(w.text()).toContain('uv run atlas-node run')
  expect(w.text()).toContain('one-time')
  await w.findAll('button').find(b => b.text() === 'Скрыть приглашение')!.trigger('click')
  expect(w.text()).not.toContain('one-time')
  expect(w.emitted('refresh')).toHaveLength(1)
})
it('сохраняет поля при отказе direct и передаёт ключ только в защищённый admin запрос', async () => {
  sessionStorage.setItem('atlas.gate.adminToken', 'admin')
  const fetcher = mockFetch({ 'GET /harness/admin/node-options': options })
  const w = mount(NodeWizard, { props: { nodes: [] } })
  await ready(w, 'same')
  await w.get('input[type="password"]').setValue('private')
  await w.get('form').trigger('submit'); await flush()
  expect(w.find('[role="alert"]').exists()).toBe(true)
  expect((w.get('input[type="password"]').element as HTMLInputElement).value).toBe('private')
  const sent = fetcher.mock.calls.find(c => c[0] === '/harness/admin/nodes')!
  expect((sent[1] as RequestInit).headers).toMatchObject({ 'X-Atlas-Token': 'admin' })
  expect(JSON.parse((sent[1] as RequestInit).body as string)).toMatchObject({ transport: 'direct', token: 'private', account_turn_capacity: 0 })
})
it('не заменяет ключ автоматически при продолжении подключения', async () => {
  const fetcher = mockFetch({ 'GET /harness/admin/node-options': options })
  const w = mount(NodeWizard, { props: { nodes: [], resume: { node_id: 'a', transport: 'connector', backend: 'codex' } as any } })
  await flush()
  expect(w.text()).toContain('Создать новое приглашение')
  expect(fetcher.mock.calls).toHaveLength(1)
})

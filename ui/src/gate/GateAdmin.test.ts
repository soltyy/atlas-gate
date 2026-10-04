import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { flush, headersOfCall, mockFetch } from '../test-utils'
import GateAdmin from './GateAdmin.vue'

const pending = {
  enrollments: [{ user_code: 'ABCD2345', device_name: 'Ноутбук', platform: 'win32', app_version: '0.1.0', status: 'pending', created_at: 1, expires_at: 2 }],
  orgs: ['test-org'],
}
const devices = {
  devices: [{ device_id: 'd1', org: 'test-org', user_id: 'u1', user_email: 'u1@x', user_name: 'Иван', device_name: 'ПК', platform: 'win32', app_version: '0.1.0', created_at: 1, revoked_at: null }],
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('GateAdmin', () => {
  it('показывает ожидающие регистрации и устройства', async () => {
    mockFetch({ 'GET /harness/admin/enrollments?status=pending': pending, 'GET /harness/admin/devices': devices })
    const w = mount(GateAdmin)
    await flush()
    expect(w.text()).toContain('ABCD2345')
    expect(w.text()).toContain('Ноутбук')
    expect(w.text()).toContain('u1@x')
    expect(w.text()).toContain('активно')
  })

  it('«Одобрить» шлёт код, организацию и сотрудника', async () => {
    const approve = vi.fn(() => ({ ok: true }))
    mockFetch({
      'GET /harness/admin/enrollments?status=pending': pending,
      'GET /harness/admin/devices': devices,
      'POST /harness/enroll/approve': approve,
    })
    const w = mount(GateAdmin)
    await flush()
    const form = w.findAll('form')[1]
    const inputs = form.findAll('input')
    await inputs[0].setValue('u9')
    await inputs[1].setValue('u9@x')
    await form.trigger('submit')
    await flush()
    expect(approve).toHaveBeenCalledWith({ user_code: 'ABCD2345', org: 'test-org', user: { id: 'u9', email: 'u9@x', name: '' } })
  })

  it('токен администратора уходит заголовком X-Atlas-Token в каждом запросе', async () => {
    const fetchMock = mockFetch({ 'GET /harness/admin/enrollments?status=pending': pending, 'GET /harness/admin/devices': devices })
    const w = mount(GateAdmin)
    await flush()
    await w.find('#gate-token').setValue('adm-secret')
    await w.findAll('form')[0].trigger('submit')
    await flush()
    const last = fetchMock.mock.calls.length - 1
    expect(headersOfCall(fetchMock, last)['X-Atlas-Token']).toBe('adm-secret')
    expect(sessionStorage.getItem('atlas.gate.adminToken')).toBe('adm-secret')
  })
})

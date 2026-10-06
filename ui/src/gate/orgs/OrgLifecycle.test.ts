// #7: новая организация (шаблон / копия), переименование, архив и возврат из архива.
import { mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { flush, jsonResponse, mockFetch } from '../../test-utils'
import OrgEditor from './OrgEditor.vue'
import OrgsSection from './OrgsSection.vue'
import { config, details } from './fixtures'
import type { OrgSummary } from './types'

const summary = (org: string, name: string, archived = false): OrgSummary => ({
  org, name, archived, profile_version: 3, signed_at: 1, counts: { routes: 3, models: 2, agents: 1, devices: 2 }, warnings: [],
})
const LIST = { orgs: [summary('acme', 'ACME'), summary('old-co', 'Старая', true)], errors: {} }

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

async function section(routes: Record<string, unknown>): Promise<VueWrapper> {
  mockFetch({ 'GET /harness/admin/orgs': LIST, ...routes })
  const w = mount(OrgsSection, { attachTo: document.body })
  await flush()
  return w
}

describe('список организаций', () => {
  it('архивная — серым с бейджем «архив»', async () => {
    const w = await section({})
    expect(w.find('[data-org="old-co"]').classes()).toContain('archived')
    expect(w.find('[data-org="old-co"] [data-archived]').text()).toBe('архив')
    expect(w.find('[data-org="acme"] [data-archived]').exists()).toBe(false)
  })
})

describe('«Новая организация»', () => {
  it('из шаблона: POST с id, названием и источником, затем открывается редактор новой', async () => {
    const created = vi.fn(() => ({ org: 'new-co', profile_version: 1, warnings: [], etag: '"e1"' }))
    const fresh = config()
    fresh.id = 'new-co'
    fresh.name = 'Новая Ко'
    const w = await section({ 'POST /harness/admin/orgs': created, 'GET /harness/admin/orgs/new-co': details(fresh, 1) })
    await w.find('[data-act="new-org"]').trigger('click')
    const options = w.findAll('[data-f="new.source"] option').map((o) => o.attributes('value'))
    expect(options).toEqual(['template', 'copy_of:acme', 'copy_of:old-co'])
    await w.find('[data-f="new.id"]').setValue('new-co')
    await w.find('[data-f="new.name"]').setValue('Новая Ко')
    await w.find('[data-new-org]').trigger('submit')
    await flush()
    expect(created).toHaveBeenCalledWith({ id: 'new-co', name: 'Новая Ко', source: 'template' })
    expect(w.text()).toContain('Новая Ко')
    expect(w.text()).toContain('версия 1')
  })

  it('копией: источник copy_of:<org>', async () => {
    const created = vi.fn(() => ({ org: 'acme-2', profile_version: 1, warnings: [], etag: '"e"' }))
    const w = await section({ 'POST /harness/admin/orgs': created, 'GET /harness/admin/orgs/acme-2': details(config(), 1) })
    await w.find('[data-act="new-org"]').trigger('click')
    await w.find('[data-f="new.id"]').setValue('acme-2')
    await w.find('[data-f="new.name"]').setValue('ACME 2')
    await w.find('[data-f="new.source"]').setValue('copy_of:acme')
    await w.find('[data-new-org]').trigger('submit')
    await flush()
    expect(created).toHaveBeenCalledWith({ id: 'acme-2', name: 'ACME 2', source: 'copy_of:acme' })
  })

  it('неверный id ловится до запроса; занятый (409) — ошибка у поля id', async () => {
    const fetchMock = mockFetch({
      'GET /harness/admin/orgs': LIST,
      'POST /harness/admin/orgs': jsonResponse({ error: { code: 'conflict', message: "организация с id 'acme' уже есть" } }, 409),
    })
    const w = mount(OrgsSection, { attachTo: document.body })
    await flush()
    await w.find('[data-act="new-org"]').trigger('click')
    await w.find('[data-f="new.id"]').setValue('Плохой id')
    await w.find('[data-f="new.name"]').setValue('X')
    expect(w.find('[data-new-org] .field-error').text()).toContain('латиница')
    await w.find('[data-new-org]').trigger('submit')
    await flush()
    expect(fetchMock.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'POST')).toHaveLength(0)
    await w.find('[data-f="new.id"]').setValue('acme')
    await w.find('[data-new-org]').trigger('submit')
    await flush()
    expect(w.find('[data-new-org] .field-error').text()).toContain('уже есть')
    expect(w.find('[data-new-org]').exists()).toBe(true)
  })

  it('422 по полю name — у поля', async () => {
    const w = await section({
      'POST /harness/admin/orgs': jsonResponse({ error: { code: 'profile_invalid', message: 'x', fields: [{ loc: 'name', msg: 'название не может быть пустым' }] } }, 422),
    })
    await w.find('[data-act="new-org"]').trigger('click')
    await w.find('[data-f="new.id"]').setValue('ok')
    await w.find('[data-f="new.name"]').setValue('x')
    await w.find('[data-new-org]').trigger('submit')
    await flush()
    expect(w.text()).toContain('название не может быть пустым')
  })
})

describe('редактор: переименовать и архив', () => {
  const ORG = '/harness/admin/orgs/acme'

  function editorServer() {
    let current = details(config(), 5, false)
    const rename = vi.fn((body: unknown) => {
      const org = { ...current.org, name: (body as { name: string }).name }
      current = details(org, (current.profile_version ?? 0) + 1, current.archived)
      return { profile_version: current.profile_version, warnings: [], etag: current.etag }
    })
    const flip = (archived: boolean) => vi.fn(() => {
      current = details({ ...current.org, archived: archived || undefined }, (current.profile_version ?? 0) + 1, archived)
      return { archived, profile_version: current.profile_version, warnings: [], etag: current.etag }
    })
    const archive = flip(true)
    const unarchive = flip(false)
    mockFetch({
      [`GET ${ORG}`]: () => current,
      [`PATCH ${ORG}`]: rename,
      [`POST ${ORG}/archive`]: archive,
      [`POST ${ORG}/unarchive`]: unarchive,
    })
    return { rename, archive, unarchive }
  }

  async function editor(): Promise<VueWrapper> {
    const w = mount(OrgEditor, { props: { orgId: 'acme' }, attachTo: document.body })
    await flush()
    return w
  }

  it('«Переименовать»: PATCH с новым названием, id прежний', async () => {
    const { rename } = editorServer()
    vi.spyOn(window, 'prompt').mockReturnValue('ACME Holding')
    const w = await editor()
    await w.find('[data-act="rename"]').trigger('click')
    await flush()
    expect(rename).toHaveBeenCalledWith({ name: 'ACME Holding' })
    expect(w.find('.ed-title h2').text()).toBe('ACME Holding')
    expect(w.find('.ed-title code').text()).toBe('acme')
    expect(w.text()).toContain('Переименовано: профиль версии 6')
  })

  it('«Архивировать» с подтверждением → бейдж «в архиве» → «Разархивировать»', async () => {
    const { archive, unarchive } = editorServer()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const w = await editor()
    await w.find('[data-act="archive"]').trigger('click')
    await flush()
    expect(archive).not.toHaveBeenCalled()
    confirm.mockReturnValue(true)
    await w.find('[data-act="archive"]').trigger('click')
    await flush()
    expect(archive).toHaveBeenCalled()
    expect(w.find('.ed-title [data-archived]').text()).toBe('в архиве')
    expect(w.text()).toContain('Организация в архиве')
    await w.find('[data-act="unarchive"]').trigger('click')
    await flush()
    expect(unarchive).toHaveBeenCalled()
    expect(w.find('.ed-title [data-archived]').exists()).toBe(false)
    expect(w.find('[data-act="archive"]').exists()).toBe(true)
  })

  it('при несохранённых правках переименование и архив выключены', async () => {
    editorServer()
    const w = await editor()
    await w.find('[data-tab="general"]').trigger('click')
    await w.find('[data-f="name"]').setValue('черновик')
    expect(w.find('[data-act="rename"]').attributes('disabled')).toBeDefined()
    expect(w.find('[data-act="archive"]').attributes('disabled')).toBeDefined()
  })
})

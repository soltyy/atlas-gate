import { mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { flush, headersOfCall, jsonResponse, mockFetch, type FetchMock } from '../../test-utils'
import GateAdmin from '../GateAdmin.vue'
import OrgEditor from './OrgEditor.vue'
import { config, details } from './fixtures'
import type { OrgConfig } from './types'
import { diffLines } from './util'

const ORG = '/harness/admin/orgs/acme'

/** Сервер-двойник: PUT принимает конфигурацию, следующий GET отдаёт её с версией +1. */
function server(extra: Record<string, unknown> = {}) {
  let current = details()
  const put = vi.fn((body: unknown) => {
    current = details(body as OrgConfig, (current.profile_version ?? 0) + 1)
    return { profile_version: current.profile_version, warnings: ['предупреждение после сохранения'], etag: current.etag }
  })
  const fetchMock = mockFetch({
    [`GET ${ORG}`]: () => current,
    [`PUT ${ORG}`]: put,
    ...extra,
  })
  return { put, fetchMock, current: () => current }
}

async function editor(): Promise<VueWrapper> {
  const w = mount(OrgEditor, { props: { orgId: 'acme' }, attachTo: document.body })
  await flush()
  return w
}

async function openTab(w: VueWrapper, tab: string) {
  await w.find(`[data-tab="${tab}"]`).trigger('click')
}

async function save(w: VueWrapper) {
  await w.find('[data-act="save"]').trigger('click')
  await flush()
}

function lastPutHeaders(fetchMock: FetchMock): Record<string, string> {
  const i = fetchMock.mock.calls.findIndex((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
  return headersOfCall(fetchMock, i)
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  sessionStorage.clear()
  document.body.innerHTML = ''
})

describe('раздел «Организации» в админке', () => {
  it('список → редактор выбранной организации', async () => {
    mockFetch({
      'GET /harness/admin/enrollments?status=pending': { enrollments: [], orgs: ['acme'] },
      'GET /harness/admin/devices': { devices: [] },
      'GET /harness/admin/orgs': { orgs: [{ org: 'acme', name: 'ACME', profile_version: 5, signed_at: 1, counts: { routes: 3, models: 2, agents: 1, devices: 4 }, warnings: ['нет ключа'] }], errors: { broken: 'id не совпадает' } },
      [`GET ${ORG}`]: details(),
    })
    const w = mount(GateAdmin)
    await flush()
    await w.find('[data-section="orgs"]').trigger('click')
    await flush()
    expect(w.find('[data-org="acme"]').text()).toContain('3 / 2 / 1')
    expect(w.text()).toContain('broken: файл отвергнут')
    await w.find('[data-open="acme"]').trigger('click')
    await flush()
    expect(w.text()).toContain('версия 5')
    expect(w.findAll('[data-tab]')).toHaveLength(7)
  })
})

describe('вкладка «Маршруты»', () => {
  it('показывает присутствие ключа и состояние апстрима, прячет апстрим у router-agent', async () => {
    server()
    const w = await editor()
    expect(w.find('[data-f="routes.0.key_env"]').element).toBeTruthy()
    const rows = w.findAll('tbody tr')
    expect(rows[0].text()).toContain('задан')
    expect(rows[1].text()).toContain('нет')
    expect(rows[1].text()).toContain('down')
    expect(w.find('[data-f="routes.2.upstream_url"]').exists()).toBe(false)
    expect(rows[2].text()).toContain('без апстрима и ключа')
  })

  it('выключение и таймаут уходят в PUT с If-Match; после сохранения — новая версия и предупреждения', async () => {
    const { put, fetchMock } = server()
    const w = await editor()
    await w.find('[data-f="routes.1.enabled"]').setValue(false)
    await w.find('[data-f="routes.0.timeout_s"]').setValue('90')
    await save(w)
    const body = put.mock.calls[0][0] as OrgConfig
    expect(body.routes[1].enabled).toBe(false)
    expect(body.routes[0].timeout_s).toBe(90)
    expect(lastPutHeaders(fetchMock)['If-Match']).toBe('"etag-v5"')
    expect(w.text()).toContain('Сохранено: профиль версии 6')
    expect(w.text()).toContain('предупреждение после сохранения')
    // Правок больше нет — панель сохранения скрыта (#12: видна только при несохранённых изменениях).
    expect(w.find('[data-savebar]').exists()).toBe(false)
  })

  it('новый маршрут router-agent: протокол atlas-agent, без апстрима и ключа', async () => {
    const { put } = server()
    const w = await editor()
    await w.find('[data-act="add-route"]').trigger('click')
    await w.find('[data-f="routes.3.id"]').setValue('claude-2')
    await w.find('[data-f="routes.3.kind"]').setValue('router-agent')
    await save(w)
    const r = (put.mock.calls[0][0] as OrgConfig).routes[3]
    expect(r).toMatchObject({ id: 'claude-2', kind: 'router-agent', protocol: 'atlas-agent', upstream_url: '', key_env: '' })
  })

  it('ошибка по полю — у поля, счётчик на вкладке; общая — над вкладками; файл не сохранён', async () => {
    server({
      [`PUT ${ORG}`]: jsonResponse({ error: { code: 'profile_invalid', message: 'x', fields: [
        { loc: 'routes.1.upstream_url', msg: 'у gateway-маршрута обязательны upstream_url и key_env' },
        { loc: '', msg: 'повтор id маршрута' },
      ] } }, 422),
    })
    const w = await editor()
    await openTab(w, 'general')
    await w.find('[data-f="name"]').setValue('ACME 2')
    await save(w)
    expect(w.find('[data-tab="routes"]').classes()).toContain('active')
    expect(w.find('[data-tab="routes"] .badge').text()).toBe('1')
    expect(w.text()).toContain('обязательны upstream_url и key_env')
    expect(w.find('[data-general-error]').text()).toBe('повтор id маршрута')
    expect(w.text()).toContain('Профиль не сохранён')
  })
})

describe('удаление маршрута (GW-ADMIN-03)', () => {
  it('кнопка «Удалить» только у маршрута без вызовов и устройств; у остальных — причина', async () => {
    server()
    const w = await editor()
    expect(w.find('[data-act="delete-route-openai"]').exists()).toBe(true)
    expect(w.find('[data-act="delete-route-deepseek"]').exists()).toBe(false)
    expect(w.find('[data-keep="deepseek"]').text()).toContain('только выключить: вызовов в учёте: 5')
    expect(w.find('[data-keep="claude-sub"]').exists()).toBe(true)
  })

  it('удаление с подтверждением убирает маршрут вместе с моделями, ценами и ссылками', async () => {
    const org = config()
    org.models.push({ route_id: 'openai', model: 'gpt-6-sol', display_name: '', context_window: 0, max_output: 0, enabled: true })
    org.pricing.push({ route_id: 'openai', model: 'gpt-6-sol', currency: 'USD', input_uncached: 1, input_cached: 0, output: 2, pricing_version: '1', subscription: false })
    org.agents[0].allowed_routes = ['deepseek', 'openai']
    const put = vi.fn((_body: unknown) => ({ profile_version: 6, warnings: [], etag: '"e6"' }))
    mockFetch({ [`GET ${ORG}`]: details(org), [`PUT ${ORG}`]: put })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const w = await editor()
    await w.find('[data-act="delete-route-openai"]').trigger('click')
    expect(w.find('[data-f="routes.1.id"]').element).toBeTruthy()
    expect((w.find('[data-f="routes.1.id"]').element as HTMLInputElement).value).toBe('openai')
    confirm.mockReturnValue(true)
    await w.find('[data-act="delete-route-openai"]').trigger('click')
    await save(w)
    const body = put.mock.calls[0][0] as OrgConfig
    expect(body.routes.map((r) => r.id)).toEqual(['deepseek', 'claude-sub'])
    expect(body.models.some((m) => m.route_id === 'openai')).toBe(false)
    expect(body.pricing.some((p) => p.route_id === 'openai')).toBe(false)
    expect(body.policy.routes_by_class.public).toEqual(['deepseek', 'claude-sub'])
    expect(body.agents[0].allowed_routes).toEqual(['deepseek'])
  })
})

describe('вкладка «Модели»', () => {
  it('модели подписки — доступны к добавлению, а не отмечены заранее', async () => {
    const org = config()
    org.models = org.models.filter((m) => m.route_id !== 'claude-sub')
    org.pricing = org.pricing.filter((p) => p.route_id !== 'claude-sub')
    mockFetch({ [`GET ${ORG}`]: details(org) })
    const w = await editor()
    await openTab(w, 'models')
    const boxes = w.findAll('[data-route="claude-sub"] [data-sub]')
    expect(boxes.map((b) => b.attributes('data-sub'))).toEqual(['claude-opus-5-5', 'claude-sonnet-5'])
    expect(boxes.every((b) => !(b.element as HTMLInputElement).checked)).toBe(true)
    expect(w.find('[data-route="claude-sub"]').text()).toContain('не в профиле')
  })

  it('router-agent: галочка из списка подписки добавляет модель; у gateway правится окно', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'models')
    await w.find('[data-sub="claude-sonnet-5"]').setValue(true)
    await w.find('[data-f="models.0.context_window"]').setValue('64000')
    await save(w)
    const models = (put.mock.calls[0][0] as OrgConfig).models
    expect(models.find((m) => m.model === 'claude-sonnet-5')).toMatchObject({ route_id: 'claude-sub', enabled: true })
    expect(models[0].context_window).toBe(64000)
  })

  it('ошибка модели показывается у поля', async () => {
    server({
      [`PUT ${ORG}`]: jsonResponse({ error: { code: 'profile_invalid', message: 'x', fields: [{ loc: 'models.0.context_window', msg: 'Input should be a valid integer' }] } }, 422),
    })
    const w = await editor()
    await openTab(w, 'models')
    await w.find('[data-f="models.0.display_name"]').setValue('DS')
    await save(w)
    expect(w.find('[data-tab="models"]').classes()).toContain('active')
    expect(w.text()).toContain('Input should be a valid integer')
  })
})

describe('вкладка «Цены»', () => {
  it('цена за 1M правится; подписка — только чтение', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'pricing')
    expect(w.find('[data-f="pricing.1.output"]').exists()).toBe(false)
    expect(w.find('[data-sub-prices]').text()).toContain('Подписка: claude-opus-5-5 — 0 за токены')
    await w.find('[data-f="pricing.0.output"]').setValue('0.84')
    await save(w)
    expect((put.mock.calls[0][0] as OrgConfig).pricing[0].output).toBe(0.84)
  })
})

describe('вкладка «Политика»', () => {
  it('матрица классы × маршруты и режим инструмента уходят в PUT', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'policy')
    await w.find('[data-cell="internal|openai"]').setValue(true)
    await w.find('[data-cell="public|claude-sub"]').setValue(false)
    await w.find('[data-f="policy.tool_policies.0.mode"]').setValue('deny')
    await w.find('[data-f="builtin_tools.shell"]').setValue('allow')
    await save(w)
    const p = (put.mock.calls[0][0] as OrgConfig).policy
    expect(p.routes_by_class.internal).toEqual(['deepseek', 'openai'])
    expect(p.routes_by_class.public).toEqual(['deepseek', 'openai'])
    expect(p.tool_policies[0].mode).toBe('deny')
    expect((put.mock.calls[0][0] as OrgConfig).builtin_tools.shell).toBe('allow')
  })
})

describe('вкладка «Агенты» (#12)', () => {
  it('пустое название блокирует сохранение и показывает ошибку у поля', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'agents')
    await w.find('[data-f="agent.name"]').setValue('   ')
    await save(w)
    expect(put).not.toHaveBeenCalled()
    expect(w.text()).toContain('имя агента не должно быть пустым')
    await w.find('[data-f="agent.name"]').setValue('Помощник')
    await save(w)
    expect(put).toHaveBeenCalledTimes(1)
  })
  it('промпт сохранённого агента правится прямо в карточке — при сохранении это новая версия', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'agents')
    await w.find('[data-f="agent.system_prompt"]').setValue('Помогай кратко.')
    await save(w)
    const agents = (put.mock.calls[0][0] as OrgConfig).agents
    expect(agents).toHaveLength(2)
    expect(agents[0]).toMatchObject({ version: '1', system_prompt: 'Помогай.', default: false })
    expect(agents[1]).toMatchObject({ version: '2', system_prompt: 'Помогай кратко.', default: true })
  })

  it('«Все инструменты» в один клик: пишется «*» и полный список поимённо', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'agents')
    expect((w.find('[data-mode="selected"]').element as HTMLInputElement).checked).toBe(true)
    await w.find('[data-mode="all"]').setValue(true)
    await save(w)
    const tools = (put.mock.calls[0][0] as OrgConfig).agents.slice(-1)[0].tools_allowlist
    expect(tools[0]).toBe('*')
    expect(tools).toEqual(expect.arrayContaining(['read', 'write', 'edit', 'shell', 'run_console']))
  })

  it('«Только выбранные»: «Выбрать все», «Снять все», галочка и своё имя', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'agents')
    await w.find('[data-act="tools-all"]').trigger('click')
    await w.find('[data-tool="shell"]').setValue(false)
    await w.find('[data-f="tool-custom"]').setValue('grep_repo')
    await w.find('[data-act="tool-add"]').trigger('click')
    await save(w)
    const tools = (put.mock.calls[0][0] as OrgConfig).agents.slice(-1)[0].tools_allowlist
    expect(tools).not.toContain('*')
    expect(tools).not.toContain('shell')
    expect(tools).toEqual(expect.arrayContaining(['read', 'write', 'edit', 'run_console', 'grep_repo']))
  })

  it('новый агент — «Все инструменты» по умолчанию, id заполнен, модель — со своим маршрутом', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'agents')
    await w.find('[data-act="add-agent"]').trigger('click')
    expect((w.find('[data-mode="all"]').element as HTMLInputElement).checked).toBe(true)
    await w.find('[data-f="agent.name"]').setValue('Юрист')
    await w.find('[data-f="agent.model"]').setValue('claude-opus-5-5')
    await save(w)
    const added = (put.mock.calls[0][0] as OrgConfig).agents.slice(-1)[0]
    expect(added).toMatchObject({ id: 'agent-2', version: '1', name: 'Юрист', model: 'claude-opus-5-5', default: false })
    expect(added.allowed_routes).toContain('claude-sub')
    expect(added.tools_allowlist[0]).toBe('*')
  })

  it('модель не с разрешённого маршрута — предупреждение у поля', async () => {
    const org = config()
    org.agents[0].allowed_routes = ['claude-sub']
    mockFetch({ [`GET ${ORG}`]: details(org) })
    const w = await editor()
    await openTab(w, 'agents')
    expect(w.find('[data-model-warning]').text()).toContain('маршруту deepseek')
  })

  it('«Удалить агента» с подтверждением убирает все его версии', async () => {
    const org = config()
    org.agents.push({ ...org.agents[0], id: 'lawyer', name: 'Юрист', default: false })
    const put = vi.fn((_body: unknown) => ({ profile_version: 6, warnings: [], etag: '"e6"' }))
    mockFetch({ [`GET ${ORG}`]: details(org), [`PUT ${ORG}`]: put })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const w = await editor()
    await openTab(w, 'agents')
    await w.find('[data-agent="lawyer"]').trigger('click')
    await w.find('[data-act="delete-agent"]').trigger('click')
    await save(w)
    expect((put.mock.calls[0][0] as OrgConfig).agents.map((a) => a.id)).toEqual(['assistant'])
  })

  it('агент без флага default отмечается при открытии — ровно один', async () => {
    const org = config()
    org.agents[0].default = false
    mockFetch({ [`GET ${ORG}`]: details(org) })
    const w = await editor()
    expect(w.text()).toContain('Агент по умолчанию не был отмечен')
    await openTab(w, 'agents')
    expect((w.find('[data-f="agent.default"]').element as HTMLInputElement).checked).toBe(true)
  })

  it('ошибка id агента с сервера — открывает этого агента и показывается у него', async () => {
    const org = config()
    org.agents.push({ ...org.agents[0], id: 'bad', name: 'Плохой', default: false })
    server({
      [`GET ${ORG}`]: details(org),
      [`PUT ${ORG}`]: jsonResponse({ error: { code: 'profile_invalid', message: 'x', fields: [{ loc: 'agents.1.id', msg: 'id агента: латиница, цифры' }] } }, 422),
    })
    const w = await editor()
    await openTab(w, 'general')
    await w.find('[data-f="name"]').setValue('X')
    await save(w)
    expect(w.find('[data-tab="agents"]').classes()).toContain('active')
    expect(w.find('[data-card="bad"]').text()).toContain('id агента: латиница, цифры')
    expect(w.find('[data-error-summary]').text()).toContain('Агенты: id агента')
  })
})

describe('вкладка «MCP»', () => {
  it('HTTP → stdio: видимое пустое args сохраняется как []', async () => {
    const org = config()
    org.mcp = [{ id: 'old', name: 'Old', transport: 'http', url: 'https://mcp.example',
      args: null, headers_ref: null, required: false, tools_allowlist: [], ref_fields: {}, display_fields: {} }]
    const { put } = server({ [`GET ${ORG}`]: details(org) })
    const w = await editor()
    await openTab(w, 'mcp')
    await w.find('[data-f="mcp.0.transport"]').setValue('stdio')
    await w.find('[data-f="mcp.0.command"]').setValue('node')
    expect((w.find('[data-f="mcp.0.args"]').element as HTMLInputElement).value).toBe('[]')
    await save(w)
    expect((put.mock.calls[0][0] as OrgConfig).mcp[0].args).toEqual([])
  })

  it('удаление MCP сохраняет args следующей строки без чужого черновика', async () => {
    server()
    const w = await editor()
    await openTab(w, 'mcp')
    for (const i of [0, 1]) {
      await w.find('[data-act="add-mcp"]').trigger('click')
      await w.find(`[data-f="mcp.${i}.transport"]`).setValue('stdio')
      await w.find(`[data-f="mcp.${i}.args"]`).setValue(JSON.stringify([`server-${i}.cjs`]))
    }
    await w.findAll('section.sub')[0].findAll('button').find(b => b.text() === 'Убрать')!.trigger('click')
    expect((w.find('[data-f="mcp.0.args"]').element as HTMLInputElement).value).toBe('["server-1.cjs"]')
  })

  it('stdio: переключение транспорта и сохранение локального контракта', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'mcp')
    await w.find('[data-act="add-mcp"]').trigger('click')
    await w.find('[data-f="mcp.0.transport"]').setValue('stdio')
    expect(w.find('[data-f="mcp.0.url"]').exists()).toBe(false)
    await w.find('[data-f="mcp.0.id"]').setValue('browser')
    await w.find('[data-f="mcp.0.command"]').setValue('node')
    await w.find('[data-f="mcp.0.args"]').setValue('["browser.cjs", "--headless"]')
    await w.find('[data-f="mcp.0.cwd"]').setValue('/workspace')
    await w.find('[data-f="mcp.0.env_ref"]').setValue('TOKEN, BROWSER_KEY')
    await w.find('[data-f="mcp.0.lifecycle"]').setValue('session')
    await save(w)
    expect((put.mock.calls[0][0] as OrgConfig).mcp[0]).toMatchObject({
      id: 'browser', transport: 'stdio', command: 'node', args: ['browser.cjs', '--headless'],
      cwd: '/workspace', env_ref: ['TOKEN', 'BROWSER_KEY'], lifecycle: 'session',
    })
  })

  it('новый сервер: имя переменной заголовков с признаком, поля по инструменту', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'mcp')
    await w.find('[data-act="add-mcp"]').trigger('click')
    await w.find('[data-f="mcp.0.id"]').setValue('kb')
    await w.find('[data-f="mcp.0.url"]').setValue('https://kb.example/mcp')
    await w.find('[data-f="mcp.0.headers_ref"]').setValue('MCP_HEADERS')
    await w.find('[data-f="mcp.0.tools_allowlist"]').setValue('search, get')
    await w.find('[data-f="mcp.0.ref_fields"]').setValue('search: id, url\nget: id')
    expect(w.text()).toContain('задан')
    await save(w)
    expect((put.mock.calls[0][0] as OrgConfig).mcp[0]).toMatchObject({
      id: 'kb', url: 'https://kb.example/mcp', headers_ref: 'MCP_HEADERS', tools_allowlist: ['search', 'get'],
      ref_fields: { search: ['id', 'url'], get: ['id'] },
    })
  })
})

describe('вкладка «Общее»', () => {
  it('квота: число и пусто = без лимита', async () => {
    const { put } = server()
    const w = await editor()
    await openTab(w, 'general')
    await w.find('[data-f="quota.limit"]').setValue('77')
    await w.find('[data-f="quota.agent_turns_per_day"]').setValue('200')
    await w.find('[data-f="overrides.allow_local_mcp"]').setValue(true)
    await save(w)
    const q = (put.mock.calls[0][0] as OrgConfig).quota
    expect(q).toMatchObject({ limit: 77, agent_turns_per_day: 200 })
    expect((put.mock.calls[0][0] as OrgConfig).overrides.allow_local_mcp).toBe(true)
    expect(w.text()).toContain('версии 6')
    await openTab(w, 'general')
    await w.find('[data-f="quota.limit"]').setValue('')
    await save(w)
    expect((put.mock.calls[1][0] as OrgConfig).quota.limit).toBeNull()
  })

  it('409: файл изменён после открытия — сообщение и «Перечитать»', async () => {
    server({ [`PUT ${ORG}`]: jsonResponse({ error: { code: 'conflict', message: 'изменился' } }, 409) })
    const w = await editor()
    await openTab(w, 'general')
    await w.find('[data-f="name"]').setValue('X')
    await save(w)
    expect(w.text()).toContain('Файл организации изменён')
    await w.find('[data-act="reread"]').trigger('click')
    await flush()
    expect((w.find('[data-f="name"]').element as HTMLInputElement).value).toBe('ACME')
  })
})

describe('кнопки редактора', () => {
  it('«Отменить» возвращает прочитанное, «Показать JSON» — черновик', async () => {
    server()
    const w = await editor()
    await openTab(w, 'general')
    await w.find('[data-f="name"]').setValue('Черновик')
    expect(w.find('[data-savebar]').exists()).toBe(true)
    await w.find('[data-act="json"]').trigger('click')
    expect(w.find('[data-json]').text()).toContain('"name": "Черновик"')
    await w.find('[data-act="cancel"]').trigger('click')
    expect(w.find('[data-json]').text()).toContain('"name": "ACME"')
    expect(w.find('[data-savebar]').exists()).toBe(false)
  })

  it('история: список версий, дифф в две колонки, откат', async () => {
    const old = config()
    old.quota.limit = 10
    const restore = vi.fn(() => ({ profile_version: 6, warnings: [], etag: '"e6"' }))
    server({
      [`GET ${ORG}/history`]: { org: 'acme', current: 5, versions: [{ version: 5, saved_at: 2, size: 1 }, { version: 4, saved_at: 1, size: 1 }] },
      [`GET ${ORG}/history/4`]: old,
      [`POST ${ORG}/history/4/restore`]: restore,
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const w = await editor()
    await w.find('[data-act="history"]').trigger('click')
    await flush()
    expect(w.findAll('[data-version]')).toHaveLength(2)
    await w.find('[data-version="4"]').trigger('click')
    await flush()
    const changed = w.findAll('table.diff tbody tr')
    expect(changed).toHaveLength(1)
    expect(changed[0].classes()).toContain('mod')
    expect(changed[0].findAll('td')[0].text()).toContain('"limit": 10')
    expect(changed[0].findAll('td')[1].text()).toContain('"limit": 50')
    await w.find('[data-act="rollback"]').trigger('click')
    await flush()
    expect(restore).toHaveBeenCalled()
    expect(w.text()).toContain('Откат выполнен: профиль версии 6')
  })
})

describe('diffLines', () => {
  it('совпадающие строки, изменённая, добавленная и удалённая', () => {
    const rows = diffLines('a\nb\nc\nd', 'a\nB\nc\ne\nf')
    expect(rows.map((r) => r.kind)).toEqual(['same', 'mod', 'same', 'mod', 'add'])
    expect(diffLines('a\nx\nb', 'a\nb').map((r) => r.kind)).toEqual(['same', 'del', 'same'])
  })
})

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AgentsTab from './AgentsTab.vue'
import type { OrgConfig, EditorContext } from './types'

describe('единые показатели в карточке агента', () => {
  it('выбранная модель читает контракт своего маршрута; черновик меняет окно без новой версии агента', async () => {
    const org = { routes: [{ id: 'a', enabled: true }, { id: 'b', enabled: true }],
      models: [{ route_id: 'a', model: 'same', display_name: 'Model A', context_window: 258400, max_output: 0, enabled: true },
        { route_id: 'b', model: 'same', display_name: 'Model B', context_window: 1000000, max_output: 8192, enabled: true }],
      agents: [{ id: 'assistant', version: '1', model: 'same', allowed_routes: ['a'], name: 'Assistant', tools_allowlist: [], mcp_ids: [], settings: {} }],
      mcp: [], policy: { tool_policies: [] } } as unknown as OrgConfig
    const ctx = { model_contracts: { a: { same: { model: 'same', display_name: 'A', context_window: 258400, max_output: null,
      limits_source: 'configured', attachments: { source: 'router-reported', maxFiles: 8, maxRawBytes: 15728640,
        maxTotalBytes: 20971520, maxTextChars: 1000000, maxPdfPages: 500, maxPagePixels: 4000000, image: true, pdfNative: false,
        documentPages: true, agentDocumentTools: true } } } } } as unknown as EditorContext
    const w = mount(AgentsTab, { props: { org, ctx, errors: {}, saved: new Set(['assistant@1']) } })
    const metrics = w.find('[data-model-metrics="a/same"]')
    expect(w.find('[data-model-metrics="b/same"]').exists()).toBe(false)
    for (const label of ['Окно, токенов', 'Макс. ответ, токенов', 'Файлов за ход', 'Байт на файл', 'Байт суммарно',
      'Символов текста', 'Страниц PDF', 'Пикселей страницы', 'router-reported', 'неизвестно']) expect(metrics.text()).toContain(label)
    expect(metrics.findAll('dd').map(x => x.text().replace(/\u00a0/g, ' ')).slice(0, 8)).toEqual(['258 400', 'неизвестно', '8', '15 728 640', '20 971 520', '1 000 000', '500', '4 000 000'])
    org.models[0].context_window = 900000
    await w.setProps({ org: { ...org, models: org.models.map(m => ({ ...m })) } })
    expect(w.find('[data-model-metrics="a/same"]').text().replace(/\u00a0/g, ' ')).toContain('900 000')
    expect(org.agents).toHaveLength(1)
  })
})

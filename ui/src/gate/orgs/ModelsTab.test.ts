import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ModelsTab from './ModelsTab.vue'
import type { OrgConfig } from './types'

describe('каталоги отдельных подписочных маршрутов', () => {
  it('выбрать все не смешивает поставщиков; отсутствующая модель остаётся с предупреждением', async () => {
    const org = { routes: [{ id: 'claude', kind: 'router-agent', enabled: true }, { id: 'codex', kind: 'router-agent', enabled: true }],
      models: [{ route_id: 'codex', model: 'old-gpt', display_name: '', context_window: 0, max_output: 0, enabled: true }] } as OrgConfig
    const w = mount(ModelsTab, { props: { org, errors: {}, ctx: { secrets: {}, upstreams: {}, route_usage: {},
      subscription_models: ['sonnet', 'gpt'], subscription_models_by_route: {
        claude: [{ value: 'sonnet', name: 'Sonnet' }], codex: [{ value: 'gpt', name: 'GPT' }],
      } } } })
    expect(w.find('[data-route="claude"]').findAll('[data-sub]').map(x => x.attributes('data-sub'))).toEqual(['sonnet'])
    expect(w.find('[data-route="codex"]').findAll('[data-sub]').map(x => x.attributes('data-sub'))).toEqual(['gpt', 'old-gpt'])
    expect(w.find('[data-route="codex"]').text()).toContain('нет у роутера этого маршрута')
    await w.find('[data-act="sub-all-claude"]').trigger('click')
    await w.find('[data-act="sub-all-codex"]').trigger('click')
    expect(org.models.filter(x => x.route_id === 'claude').map(x => x.model)).toEqual(['sonnet'])
    expect(org.models.find(x => x.model === 'gpt')?.display_name).toBe('GPT')
    const limits = w.find('[data-route="claude"]').findAll('input[type="number"]')
    expect((limits[0].element as HTMLInputElement).value).toBe('')
    expect(limits[0].attributes('placeholder')).toBe('неизвестно')
    await limits[0].setValue('64000')
    expect(org.models.find(x => x.model === 'sonnet')?.context_window).toBe(64000)
    await limits[0].setValue('')
    expect(org.models.find(x => x.model === 'sonnet')?.context_window).toBe(0)
  })
})

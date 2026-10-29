import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PricingTab from './PricingTab.vue'
import { config } from './fixtures'

describe('Тариф по времени', () => {
  it('показывает сроки/оценку и интервалы, не предлагает дублирующую статическую цену', () => {
    const org = config()
    org.routes.push({ id: 'deepseek', kind: 'gateway', protocol: 'openai', enabled: true } as typeof org.routes[number])
    org.models.push({ route_id: 'deepseek', model: 'flash' } as typeof org.models[number])
    org.pricing_v1 = [{ route_id: 'deepseek', model: 'flash', currency: 'USD', pricing_version: 'reviewed',
      valid_from: '2026-10-29T00:00:00Z', valid_until: '2026-10-30T00:00:00Z', source_url: 'https://example.invalid',
      periods: [{ starts_at: '2026-10-29T00:00:00Z', ends_at: '2026-10-30T00:00:00Z', tariff_id: 'offpeak',
        rates: { input_uncached: .15, input_cached: .003, output: .6 } }] }]
    const before = JSON.stringify(org.pricing_v1)
    const w = mount(PricingTab, { props: { org, errors: {} } })
    expect(w.find('[data-dynamic-price]').text()).toContain('Расчётная стоимость')
    expect(w.find('[data-dynamic-price]').text()).toContain('reviewed')
    expect(w.find('[data-dynamic-price]').text()).toContain('2026-10-30')
    expect(w.findAll('button').some(b => b.text().includes('+ deepseek / flash'))).toBe(false)
    expect(JSON.stringify(org.pricing_v1)).toBe(before)
  })
})

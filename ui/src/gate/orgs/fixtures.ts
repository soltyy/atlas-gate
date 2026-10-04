// Общие данные тестов редактора организаций: конфигурация acme и ответ GET /harness/admin/orgs/acme.
import type { OrgConfig, OrgDetails } from './types'

export function config(): OrgConfig {
  return {
    id: 'acme',
    name: 'ACME',
    routes: [
      { id: 'deepseek', kind: 'gateway', protocol: 'openai', upstream_url: 'https://api.deepseek.com/v1', key_env: 'DEEPSEEK_API_KEY', timeout_s: 120, enabled: true, region: '', zdr: false, max_data_class: '' },
      { id: 'openai', kind: 'gateway', protocol: 'openai', upstream_url: 'https://api.openai.com/v1', key_env: 'OPENAI_API_KEY', timeout_s: 60, enabled: true, region: '', zdr: false, max_data_class: '' },
      { id: 'claude-sub', kind: 'router-agent', protocol: 'atlas-agent', upstream_url: '', key_env: '', timeout_s: 120, enabled: true, region: '', zdr: false, max_data_class: '' },
    ],
    models: [
      { route_id: 'deepseek', model: 'deepseek-chat', display_name: 'DeepSeek', context_window: 128000, max_output: 8192, enabled: true },
      { route_id: 'claude-sub', model: 'claude-opus-5-5', display_name: 'Opus', context_window: 1000000, max_output: 64000, enabled: true },
    ],
    pricing: [
      { route_id: 'deepseek', model: 'deepseek-chat', currency: 'USD', input_uncached: 0.28, input_cached: 0.028, output: 0.42, pricing_version: '1', subscription: false },
      { route_id: 'claude-sub', model: 'claude-opus-5-5', currency: 'USD', input_uncached: 0, input_cached: 0, output: 0, pricing_version: '1', subscription: true },
    ],
    policy: {
      data_classes: ['public', 'internal', 'personal'],
      routes_by_class: { public: ['deepseek', 'openai', 'claude-sub'], internal: ['deepseek'] },
      tool_policies: [{ tool: 'run_console', mode: 'ask', max_data_class: 'internal', escalatable: false }],
      default_data_class: 'internal',
    },
    agents: [
      { id: 'assistant', version: '1', name: 'Ассистент', system_prompt: 'Помогай.', model: 'deepseek-chat', allowed_routes: ['deepseek'], tools_allowlist: ['read'], mcp_ids: [], settings: {}, default: true },
    ],
    mcp: [],
    builtin_tools: { read: 'allow', write: 'deny', edit: 'deny', shell: 'deny' },
    overrides: { allow_personal_agents: false, allow_local_mcp: false, allow_local_routes: false, allow_data_class_raise: false, allow_workspace_root: true },
    feature_flags: {},
    min_app_version: '0.0.0',
    recommended_app_version: '',
    update_channel_url: '',
    update_manifest: null,
    quota: { limit: 50, currency: 'USD', agent_turns_per_day: null },
    audit: { include_messages: false },
  }
}

export function details(org = config(), version = 5, archived = false): OrgDetails {
  return {
    org, archived, etag: `"etag-v${version}"`, profile_version: version, signed_at: 1_780_000_000,
    secrets: { DEEPSEEK_API_KEY: true, OPENAI_API_KEY: false, MCP_HEADERS: true },
    subscription_models: ['claude-opus-5-5', 'claude-sonnet-5'],
    upstreams: { deepseek: 'ok', openai: 'down' }, warnings: ['модель gpt (openai) не отдана: нет ключа OPENAI_API_KEY'],
    route_usage: {
      deepseek: { calls: 5, devices: 1, deletable: false, reason: 'вызовов в учёте: 5, устройств с его моделями: 1' },
      openai: { calls: 0, devices: 0, deletable: true, reason: '' },
      'claude-sub': { calls: 3, devices: 1, deletable: false, reason: 'вызовов в учёте: 3, устройств с его моделями: 1' },
    },
  }
}

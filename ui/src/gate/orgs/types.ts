// Конфигурация организации — зеркало pydantic-модели atlas_router/gate/orgs.py (источник истины —
// файл gate/orgs/<org>.json; редактор правит его целиком через PUT /harness/admin/orgs/{org}).

export type RouteKind = 'gateway' | 'router-agent'
export type Protocol = 'openai' | 'anthropic' | 'atlas-agent'

export interface Route {
  id: string
  kind: RouteKind
  protocol: Protocol
  upstream_url: string
  key_env: string
  timeout_s: number
  enabled: boolean
  region: string
  zdr: boolean
  max_data_class: string
}

export interface ModelEntry {
  route_id: string
  model: string
  display_name: string
  context_window: number
  max_output: number
  enabled: boolean
}

export interface ModelContract {
  model: string
  display_name: string
  context_window: number | null
  max_output: number | null
  limits_source: string
  attachments: Record<string, number | boolean | string> | null
}

export interface Pricing {
  route_id: string
  model: string
  currency: string
  input_uncached: number
  input_cached: number
  output: number
  pricing_version: string
  subscription: boolean
}

export type ToolMode = 'auto' | 'ask' | 'deny'

export interface ToolPolicy {
  tool: string
  mode: ToolMode
  max_data_class: string
  escalatable: boolean
}

export interface Policy {
  data_classes: string[]
  routes_by_class: Record<string, string[]>
  tool_policies: ToolPolicy[]
  default_data_class: string
}

export interface Agent {
  id: string
  version: string
  name: string
  system_prompt: string
  model: string
  allowed_routes: string[]
  tools_allowlist: string[]
  mcp_ids: string[]
  settings: Record<string, unknown>
  default: boolean
  [extra: string]: unknown
}

export interface Mcp {
  id: string
  name: string
  transport: 'http' | 'stdio'
  url: string | null
  command?: string | null
  args?: string[] | null
  cwd?: string | null
  env_ref?: string[]
  lifecycle?: 'run' | 'session'
  headers_ref: string | null
  required: boolean
  tools_allowlist: string[]
  ref_fields: Record<string, string[]>
  display_fields: Record<string, string[]>
}

export type Allow = 'allow' | 'deny'

export interface OrgConfig {
  id: string
  name: string
  routes: Route[]
  models: ModelEntry[]
  pricing: Pricing[]
  policy: Policy
  agents: Agent[]
  mcp: Mcp[]
  builtin_tools: { read: Allow; write: Allow; edit: Allow; shell: Allow }
  overrides: {
    allow_personal_agents: boolean
    allow_local_mcp: boolean
    allow_local_routes: boolean
    allow_data_class_raise: boolean
    allow_workspace_root: boolean
  }
  feature_flags: Record<string, unknown>
  min_app_version: string
  recommended_app_version: string
  update_channel_url: string
  update_manifest: unknown
  quota: { limit: number | null; currency: string; agent_turns_per_day: number | null }
  audit: { include_messages: boolean }
  /** Только у архивной: неархивная пишется без поля (#7). */
  archived?: boolean
}

export interface OrgSummary {
  org: string
  name: string
  archived: boolean
  profile_version: number | null
  signed_at: number | null
  counts: { routes: number; models: number; agents: number; devices: number }
  warnings: string[]
}

export interface OrgDetails {
  org: OrgConfig
  etag: string
  archived: boolean
  profile_version: number | null
  signed_at: number | null
  /** Имя переменной с секретом → задана ли она (значения гейт не отдаёт никогда). */
  secrets: Record<string, boolean>
  subscription_models: string[]
  subscription_models_by_route?: Record<string, { value: string; name: string }[]>
  model_contracts?: Record<string, Record<string, ModelContract>>
  upstreams: Record<string, string>
  /** Можно ли удалить сохранённый маршрут: нет вызовов и устройств с его моделями (GW-ADMIN-03). */
  route_usage?: Record<string, RouteUsage>
  warnings: string[]
}

export interface RouteUsage {
  calls: number
  devices: number
  deletable: boolean
  reason: string
}

export interface Saved {
  profile_version: number
  warnings: string[]
  etag: string
}

export interface HistoryEntry {
  version: number
  saved_at: number
  size: number
}

export interface FieldError {
  loc: string
  msg: string
}

/** Контекст вкладок: то, что пришло с сервера рядом с конфигурацией. */
export interface EditorContext {
  secrets: Record<string, boolean>
  upstreams: Record<string, string>
  subscription_models: string[]
  subscription_models_by_route?: Record<string, { value: string; name: string }[]>
  model_contracts?: Record<string, Record<string, ModelContract>>
  route_usage: Record<string, RouteUsage>
}

// Помощники редактора: списки через запятую, карта «инструмент: поля», JSON-поля, построчный дифф.

/** Встроенные инструменты клиента (builtin_tools профиля). */
export const BUILTIN_TOOLS = ['read', 'write', 'edit', 'shell'] as const

/** «Все инструменты, включая добавленные позже» (#12). */
export const ALL_TOOLS = '*'

/** Каталог известных имён инструментов: встроенные, из политик, уже назначенные агентам. */
export function toolCatalog(org: {
  policy: { tool_policies: { tool: string }[] }
  agents: { tools_allowlist: string[] }[]
}): string[] {
  const names = new Set<string>(BUILTIN_TOOLS)
  for (const t of org.policy.tool_policies) if (t.tool.trim()) names.add(t.tool.trim())
  for (const a of org.agents) for (const t of a.tools_allowlist) if (t && t !== ALL_TOOLS) names.add(t)
  return [...names]
}

/** Запись режима «все»: «*» и явный список — клиент без поддержки «*» получит инструменты поимённо. */
export function allTools(catalog: string[]): string[] {
  return [ALL_TOOLS, ...catalog.filter((t) => t !== ALL_TOOLS)]
}

/** 1000000 → «1 000 000». */
export function fmtNum(n: number | null | undefined): string {
  return typeof n === 'number' && Number.isFinite(n) ? n.toLocaleString('ru-RU') : ''
}

export function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T
}

export function splitList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

export function joinList(list: string[] | undefined): string {
  return (list ?? []).join(', ')
}

/** «tool: a, b» по строке → {tool: [a, b]}. */
export function parseFieldMap(text: string): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const line of text.split('\n')) {
    const i = line.indexOf(':')
    if (i <= 0) continue
    const tool = line.slice(0, i).trim()
    if (tool) out[tool] = splitList(line.slice(i + 1))
  }
  return out
}

export function formatFieldMap(map: Record<string, string[]> | undefined): string {
  return Object.entries(map ?? {})
    .map(([tool, fields]) => `${tool}: ${fields.join(', ')}`)
    .join('\n')
}

/** Число или null из поля ввода (пусто — null: «без лимита»). */
export function numberOrNull(text: string): number | null {
  const t = text.trim().replace(',', '.')
  if (!t) return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

export interface DiffRow {
  left: string | null
  right: string | null
  kind: 'same' | 'del' | 'add' | 'mod'
}

/** Построчный дифф (LCS) для двухколоночного показа: слева — версия из истории, справа — текущая. */
export function diffLines(a: string, b: string): DiffRow[] {
  const x = a.split('\n')
  const y = b.split('\n')
  const n = x.length
  const m = y.length
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--) lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
  const raw: DiffRow[] = []
  let i = 0
  let j = 0
  while (i < n || j < m) {
    if (i < n && j < m && x[i] === y[j]) {
      raw.push({ left: x[i++], right: y[j++], kind: 'same' })
    } else if (i < n && (j >= m || lcs[i + 1][j] >= lcs[i][j + 1])) {
      raw.push({ left: x[i++], right: null, kind: 'del' })
    } else {
      raw.push({ left: null, right: y[j++], kind: 'add' })
    }
  }
  // Серия «удалено» и следующая за ней серия «добавлено» сводятся построчно в изменённые строки.
  const out: DiffRow[] = []
  let k = 0
  while (k < raw.length) {
    if (raw[k].kind !== 'del') {
      out.push(raw[k++])
      continue
    }
    const dels: DiffRow[] = []
    while (k < raw.length && raw[k].kind === 'del') dels.push(raw[k++])
    const adds: DiffRow[] = []
    while (k < raw.length && raw[k].kind === 'add') adds.push(raw[k++])
    for (let t = 0; t < Math.max(dels.length, adds.length); t++) {
      const d = dels[t]
      const a = adds[t]
      if (d && a) out.push({ left: d.left, right: a.right, kind: 'mod' })
      else out.push(d ?? a)
    }
  }
  return out
}

/** Вкладка, к которой относится ошибка по полю (loc «routes.0.kind» → routes). */
export function tabOf(loc: string): string {
  const head = loc.split('.')[0]
  if (['routes', 'models', 'pricing', 'policy', 'agents', 'mcp'].includes(head)) return head
  if (head === 'builtin_tools') return 'policy'
  return head ? 'general' : ''
}

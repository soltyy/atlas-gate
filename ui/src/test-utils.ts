// Общие помощники для тестов: подмена fetch и ответ в формате Response-подобного объекта.
import { vi } from 'vitest'

export interface FakeResponseInit {
  status?: number
  body?: unknown
}

/** Минимальный объект в духе Response — ровно то, что читает src/api.ts. */
export function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

export type FetchMock = ReturnType<typeof vi.fn>

/**
 * Подменяет globalThis.fetch. `routes` сопоставляет «METHOD /path» → ответ
 * (или функцию от тела запроса). Незнакомый путь отдаёт 404.
 */
export function mockFetch(routes: Record<string, unknown | ((body: unknown) => unknown)>): FetchMock {
  const fn = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const method = (init?.method ?? 'GET').toUpperCase()
    const key = `${method} ${url}`
    if (!(key in routes)) return jsonResponse({ detail: 'Not Found' }, 404)
    const route = routes[key]
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    const result = typeof route === 'function' ? (route as (b: unknown) => unknown)(body) : route
    // Готовый ответ (с полем ok) отдаём как есть, иначе оборачиваем в 200.
    if (result && typeof result === 'object' && 'ok' in (result as object) && 'json' in (result as object)) {
      return result as ReturnType<typeof jsonResponse>
    }
    return jsonResponse(result)
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

/** Даёт отработать всем ожидающим промисам (после await-цепочек в компонентах). */
export async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve()
  await new Promise((r) => setTimeout(r, 0))
}

/** Заголовки конкретного вызова fetch по индексу. */
export function headersOfCall(fn: FetchMock, index = 0): Record<string, string> {
  const init = fn.mock.calls[index]?.[1] as RequestInit | undefined
  return (init?.headers as Record<string, string> | undefined) ?? {}
}

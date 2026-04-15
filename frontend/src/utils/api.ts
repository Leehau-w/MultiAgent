import { useToastStore } from '../stores/toastStore'

/**
 * Wrapper around fetch that shows toast on error.
 */
export async function apiFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    const res = await fetch(url, init)
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }))
      const msg = body.detail || body.message || `${res.status} ${res.statusText}`
      useToastStore.getState().add('error', msg)
    }
    return res
  } catch (err) {
    const msg = err instanceof Error ? err.message : 'Network error'
    useToastStore.getState().add('error', msg)
    throw err
  }
}

export async function apiPost(url: string, body: unknown): Promise<Response> {
  return apiFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function apiPut(url: string, body: unknown): Promise<Response> {
  return apiFetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function apiDelete(url: string): Promise<Response> {
  return apiFetch(url, { method: 'DELETE' })
}

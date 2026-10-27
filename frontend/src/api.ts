import type { ApprovalAction, HealthOut, RunIn, RunStateOut, StartRunOut, WorkflowSpec } from './types'

interface ErrorBody {
  error?: string
  detail?: string | { msg?: string }[]
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json', ...init.headers } : init?.headers,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as ErrorBody
    const validation = Array.isArray(body.detail) ? body.detail.map((item) => item.msg).filter(Boolean).join('；') : body.detail
    throw new Error(body.error || validation || `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<HealthOut>('/api/health'),
  workflows: () => request<{ workflows: WorkflowSpec[] }>('/api/workflows'),
  runs: () => request<{ runs: RunStateOut[] }>('/api/runs'),
  run: (threadId: string) => request<RunStateOut>(`/api/runs/${encodeURIComponent(threadId)}`),
  startRun: (input: RunIn) => request<StartRunOut>('/api/runs', { method: 'POST', body: JSON.stringify(input) }),
  resume: (threadId: string, action: ApprovalAction, feedback: string) => request<{ ok: boolean; thread_id: string }>(`/api/runs/${encodeURIComponent(threadId)}/resume`, {
    method: 'POST',
    body: JSON.stringify({ action, feedback }),
  }),
}

import type {
  AgentListOut, AgentProfileOut, AgentProfileSpec, CancelRunOut, CopilotRequest,
  CopilotResponse, CreateWorkflowRequest, DebugThreadsOut, DeleteAgentOut, DeleteWorkflowOut, HealthOut,
  RenameNodeRequest, ResumeIn, ResumeOut, RetryRunIn, RetryRunOut, RunIn, RunListOut, RunLogQuery,
  RunLogsOut, RunStateOut, StartRunOut, ValidateWorkflowOut, WorkerListOut,
  WorkflowListOut, WorkflowSpec,
} from './types'

interface ValidationIssue {
  msg?: string
  loc?: (string | number)[]
  type?: string
  input?: unknown
  ctx?: Record<string, unknown>
}

interface ErrorBody {
  error?: string
  detail?: string | ValidationIssue[]
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly issues: ValidationIssue[] = []) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json', ...init.headers } : init?.headers,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as ErrorBody
    const issues = Array.isArray(body.detail) ? body.detail : []
    const validation = issues.map((item) => {
      const location = item.loc?.filter((part) => part !== 'body').join('.')
      return `${location ? `${location}: ` : ''}${item.msg || '参数无效'}`
    }).join('；')
    const detail = typeof body.detail === 'string' ? body.detail : validation
    throw new ApiError(body.error || detail || `请求失败（${response.status}）`, response.status, issues)
  }
  return response.json() as Promise<T>
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })
const segment = (value: string) => encodeURIComponent(value)

export function runStreamUrl(threadId: string, afterSeq = 0): string {
  return `/api/runs/${segment(threadId)}/stream?after_seq=${Math.max(0, Math.floor(afterSeq))}`
}

export const api = {
  health: () => request<HealthOut>('/api/health'),
  agents: () => request<AgentListOut>('/api/agents'),
  saveAgent: (input: AgentProfileSpec) => request<AgentProfileOut>('/api/agents', json('POST', input)),
  deleteAgent: (agentId: string) => request<DeleteAgentOut>(`/api/agents/${segment(agentId)}`, { method: 'DELETE' }),
  workers: () => request<WorkerListOut>('/api/workers'),

  workflows: () => request<WorkflowListOut>('/api/workflows'),
  workflow: (workflowId: string) => request<WorkflowSpec>(`/api/workflows/${segment(workflowId)}`),
  saveWorkflow: (input: WorkflowSpec) => request<WorkflowSpec>('/api/workflows', json('POST', input)),
  updateWorkflow: (workflowId: string, input: WorkflowSpec) => request<WorkflowSpec>(`/api/workflows/${segment(workflowId)}`, json('PUT', input)),
  deleteWorkflow: (workflowId: string) => request<DeleteWorkflowOut>(`/api/workflows/${segment(workflowId)}`, { method: 'DELETE' }),
  validateWorkflow: (input: WorkflowSpec) => request<ValidateWorkflowOut>('/api/workflows/validate', json('POST', input)),
  createWorkflow: (input: CreateWorkflowRequest) => request<WorkflowSpec>('/api/workflows/create', json('POST', input)),
  renameNode: (input: RenameNodeRequest) => request<WorkflowSpec>('/api/workflows/rename-node', json('POST', input)),
  workflowCopilot: (input: CopilotRequest) => request<CopilotResponse>('/api/workflows/copilot', json('POST', input)),

  runs: () => request<RunListOut>('/api/runs'),
  run: (threadId: string) => request<RunStateOut>(`/api/runs/${segment(threadId)}`),
  startRun: (input: RunIn) => request<StartRunOut>('/api/runs', json('POST', input)),
  runLogs: (threadId: string, query: RunLogQuery = {}) => {
    const params = new URLSearchParams()
    if (query.node_id) params.set('node_id', query.node_id)
    params.set('after_seq', String(Math.max(0, Math.floor(query.after_seq ?? 0))))
    params.set('limit', String(Math.min(5000, Math.max(1, Math.floor(query.limit ?? 1000)))))
    return request<RunLogsOut>(`/api/runs/${segment(threadId)}/logs?${params}`)
  },
  resume: (threadId: string, input: ResumeIn) => request<ResumeOut>(`/api/runs/${segment(threadId)}/resume`, json('POST', input)),
  cancel: (threadId: string) => request<CancelRunOut>(`/api/runs/${segment(threadId)}/cancel`, { method: 'POST' }),
  retry: (threadId: string, input: RetryRunIn) => request<RetryRunOut>(`/api/runs/${segment(threadId)}/retry`, json('POST', input)),

  debugThreads: () => request<DebugThreadsOut>('/api/debug/threads'),
}

export type RunStatus = 'waiting' | 'running' | 'done' | 'error' | 'interrupted' | 'unknown'
export type ApprovalAction = 'approve' | 'revise' | 'abort'

export interface HealthOut {
  ok: boolean
  agent: string
}

export interface NodeSpec {
  id: string
  label: string
  kind: 'agent' | 'gate' | 'shell' | 'summary'
  description?: string | null
  position?: { x: number; y: number } | null
  stage?: string
  options?: string[]
}

export interface RouteRule {
  field: string
  op?: '==' | '!=' | '>=' | '<=' | '>' | '<' | 'truthy' | 'falsy'
  value?: unknown
  value_ref?: string | null
  target: string
  label?: string | null
}

export interface EdgeSpec {
  source: string
  target?: string | null
  conditions?: RouteRule[]
  default_target?: string | null
  label?: string | null
}

export interface WorkflowSpec {
  id: string
  name: string
  description?: string
  builtin?: boolean
  entry_point: string
  nodes: NodeSpec[]
  edges: EdgeSpec[]
  updated_at?: string | null
}

export interface RunValues {
  task?: string | null
  repo?: string | null
  test_cmd?: string | null
  max_attempts?: number | null
  plan?: string | null
  feedback?: string | null
  action?: string | null
  approved?: boolean | null
  attempt?: number | null
  patch?: string | null
  test_output?: string | null
  passed?: boolean | null
  review?: string | null
  result?: string | null
  events?: string[]
  [key: string]: unknown
}

export interface InterruptPayload {
  stage?: string | null
  task?: string | null
  plan?: string | null
  patch?: string | null
  test_output?: string | null
  review?: string | null
  options?: string[]
  [key: string]: unknown
}

export interface RunStateOut {
  thread_id: string
  workflow_id?: string
  status: RunStatus
  next?: string[]
  values?: RunValues
  interrupts?: InterruptPayload[]
  created_at?: string | null
  error?: string | null
  source?: string | null
}

export interface RunIn {
  task: string
  workflow_id: string
  repo?: string | null
  test_cmd?: string | null
  max_attempts: number
}

export interface StartRunOut {
  thread_id: string
  workflow_id: string
  warnings?: string[]
}

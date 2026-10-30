export type RunStatus = 'waiting' | 'running' | 'done' | 'error' | 'cancelled' | 'interrupted' | 'unknown'
export type ApprovalAction = 'approve' | 'revise' | 'abort'
export type AgentDriver = 'codex' | 'claude' | 'kimi' | 'gemini' | 'custom'
export type NodeKind = 'agent' | 'shell' | 'gate' | 'condition' | 'summary'
export type JoinMode = 'auto' | 'all' | 'any'
export type SandboxMode = 'read-only' | 'workspace-write'
export type RouteOperator = '==' | '!=' | '>=' | '<=' | '>' | '<' | 'contains' | 'not_contains' | 'regex' | 'truthy' | 'falsy'
export type WorkflowInputKind = 'textarea' | 'text' | 'path' | 'number' | 'boolean' | 'select'
export type RunLogStream = 'stdout' | 'stderr' | 'system'

export interface HealthOut {
  ok: boolean
  agent: string
  online_agents?: string[]
  workers?: string[]
  default_repo: string
}

export interface AgentProfileSpec {
  id: string
  name: string
  driver?: AgentDriver
  binary: string
  description?: string
  model?: string | null
  args_template?: string[]
  command_template?: string | null
  timeout_sec?: number
  env?: Record<string, string>
  builtin?: boolean
}

export interface AgentProfileOut extends AgentProfileSpec {
  available?: boolean
  resolved_path?: string | null
}

export interface AgentListOut { agents: AgentProfileOut[] }
export interface DeleteAgentOut { ok: boolean; agent_id: string }

export interface WorkerInfoOut {
  id: string
  name: string
  hostname: string
  platform: string
  online?: boolean
  is_local?: boolean
  default_repo: string
  online_agents?: string[]
}

export interface WorkerListOut { workers: WorkerInfoOut[] }

export interface NodePosition { x?: number; y?: number }

export interface NodeSpec {
  id: string
  label: string
  kind: NodeKind
  description?: string | null
  position?: NodePosition | null
  join_mode?: JoinMode
  worker_id?: string
  cwd?: string | null
  use_worktree?: boolean
  max_runs?: number
  timeout_sec?: number
  agent_id?: string
  prompt_template?: string | null
  sandbox?: SandboxMode
  capture_git_diff?: boolean
  command?: string | null
  continue_on_error?: boolean
  stage?: string
  instruction?: string | null
  review_nodes?: string[]
  allow_edit_output?: boolean
  rewind_targets?: string[]
  result_template?: string | null
  abort_result_template?: string | null
  output_key?: string | null
  cmd_key?: string | null
  default_cmd?: string | null
  expose_keys?: string[]
  options?: string[]
}

export interface EdgeCondition {
  field: string
  op?: RouteOperator
  value?: unknown
  value_ref?: string | null
}

export interface RouteRule {
  field: string
  op?: RouteOperator
  value?: unknown
  value_ref?: string | null
  target: string
  label?: string | null
}

export interface EdgeSpec {
  id?: string | null
  source: string
  target?: string | null
  label?: string | null
  condition?: EdgeCondition | null
  is_default?: boolean
  conditions?: RouteRule[]
  default_target?: string | null
}

export interface WorkflowInputSpec {
  key: string
  label: string
  kind?: WorkflowInputKind
  required?: boolean
  default?: unknown
  placeholder?: string | null
  description?: string | null
  options?: string[]
}

export interface WorkflowSpec {
  id: string
  name: string
  description?: string
  builtin?: boolean
  inputs?: WorkflowInputSpec[]
  entry_point: string
  nodes: NodeSpec[]
  edges?: EdgeSpec[]
  updated_at?: string | null
}

export interface WorkflowListOut { workflows: WorkflowSpec[] }
export interface DeleteWorkflowOut { ok: boolean; workflow_id: string }
export interface ValidateWorkflowOut {
  valid: boolean
  error?: string | null
  upstream_map?: Record<string, string[]>
}

export interface CopilotRequest {
  instruction: string
  current_workflow?: WorkflowSpec | null
  agent_id?: string
  auto_save?: boolean
  preferred_id?: string | null
  preferred_name?: string | null
  preferred_description?: string | null
}

export type CreateWorkflowMode = 'blank' | 'template' | 'ai'

export interface CreateWorkflowRequest {
  name: string
  id?: string | null
  description?: string
  mode?: CreateWorkflowMode
  template_id?: string | null
  ai_instruction?: string | null
  agent_id?: string
}

export interface RenameNodeRequest {
  workflow: WorkflowSpec
  old_node_id: string
  new_node_id: string
}

export interface CopilotResponse {
  workflow: WorkflowSpec
  summary: string
  agent_used: string
  saved?: boolean
}

export interface NodeExecutionOut {
  node_id: string
  label?: string | null
  kind?: string | null
  status?: string | null
  ok?: boolean | null
  exit_code?: number | null
  output?: string | null
  stdout?: string | null
  diff?: string | null
  runs?: number
  worker_id?: string | null
  agent_id?: string | null
  cwd?: string | null
  duration_ms?: number | null
  started_at?: string | null
  finished_at?: string | null
  edited_by_human?: boolean
  [key: string]: unknown
}

export interface RunValues {
  inputs?: Record<string, unknown>
  task?: string | null
  repo?: string | null
  feedback?: string | null
  action?: string | null
  result?: string | null
  nodes?: Record<string, NodeExecutionOut>
  events?: string[]
  [key: string]: unknown
}

export interface UpstreamSnapshotOut {
  node_id: string
  label: string
  kind?: string
  ok?: boolean
  exit_code?: number | null
  output?: string
  diff?: string
  stdout?: string
  edited_by_human?: boolean
}

export interface RewindOptionOut { id: string; label: string }

export interface InterruptPayload {
  node_id?: string | null
  label?: string | null
  stage?: string | null
  instruction?: string | null
  allow_edit_output?: boolean
  upstream_outputs?: Record<string, UpstreamSnapshotOut>
  rewind_options?: RewindOptionOut[]
  options?: string[]
  [key: string]: unknown
}

export interface RunStateOut {
  thread_id: string
  workflow_id: string
  status: RunStatus
  next?: string[]
  running_nodes?: string[]
  values?: RunValues
  interrupts?: InterruptPayload[]
  created_at?: string | null
  error?: string | null
  source?: string | null
}

export interface RunListOut { runs: RunStateOut[] }

export interface RunIn {
  workflow_id: string
  task?: string
  inputs?: Record<string, unknown>
  repo?: string | null
  test_cmd?: string | null
  max_attempts?: number
}

export interface StartRunOut {
  thread_id: string
  workflow_id: string
  warnings?: string[]
}

export interface ResumeIn {
  action?: ApprovalAction
  feedback?: string
  edited_outputs?: Record<string, string>
  target_node_id?: string | null
}

export interface ResumeOut { ok: boolean; thread_id: string }
export interface CancelRunOut { ok: boolean; thread_id: string; killed_processes?: number }

export interface RetryRunIn {
  node_id?: string | null
  prompt_template?: string | null
  command?: string | null
  agent_id?: string | null
  cwd?: string | null
}

export interface RetryRunOut { ok: boolean; thread_id: string; retried_node_id: string }

export interface RunLogEntry {
  seq: number
  ts: string
  node_id: string
  stream?: RunLogStream
  text: string
}

export interface RunLogsOut { thread_id: string; logs: RunLogEntry[]; next_seq: number }
export interface RunLogQuery { node_id?: string; after_seq?: number; limit?: number }
export interface DebugThreadsOut { threads: string[] }

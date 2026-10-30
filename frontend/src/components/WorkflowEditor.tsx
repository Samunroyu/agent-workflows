import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DragEvent as ReactDragEvent, FormEvent } from 'react'
import {
  Bot, Check, GitBranch, LoaderCircle, Play, Plus, Save, Settings2,
  ShieldCheck, Sparkles, Terminal, Trash2, Undo2, Workflow,
} from 'lucide-react'
import { api } from '../api'
import type {
  AgentProfileOut, EdgeSpec, NodeKind, NodeSpec, ValidateWorkflowOut,
  WorkerInfoOut, WorkflowSpec,
} from '../types'
import FlowCanvas from './FlowCanvas'
import type { CanvasSelection } from './FlowCanvas'
import NodeEditorDialog from './NodeEditorDialog'
import EdgeEditorDialog from './EdgeEditorDialog'
import { ConfirmDialog, CreateWorkflowDialog, WorkflowSettingsDialog } from './Dialogs'

interface WorkflowEditorProps {
  workflows: WorkflowSpec[]
  selectedWorkflowId: string
  agents: AgentProfileOut[]
  workers: WorkerInfoOut[]
  onSelectWorkflow: (workflowId: string) => void
  onWorkflowsChanged: () => Promise<void>
  onOpenRun: () => void
  onToast: (message: string) => void
}

const nodeTools: Array<{ kind: NodeKind; label: string }> = [
  { kind: 'agent', label: 'Agent 节点' },
  { kind: 'shell', label: 'Shell 命令' },
  { kind: 'gate', label: '人工卡点' },
  { kind: 'condition', label: '条件判断' },
]

function clone<T>(value: T): T {
  return structuredClone(value)
}

function edgeId(): string {
  return `edge_${crypto.randomUUID()}`
}

function normalizeWorkflow(input: WorkflowSpec): WorkflowSpec {
  const workflow = clone(input)
  const edges: EdgeSpec[] = []
  ;(workflow.edges ?? []).forEach((edge, index) => {
    if (edge.target) {
      edges.push({ ...edge, id: edge.id ?? `edge_${index}_${edge.source}_${edge.target}` })
      return
    }
    ;(edge.conditions ?? []).forEach((rule, branchIndex) => {
      edges.push({
        id: `${edge.id ?? `edge_${index}`}_condition_${branchIndex}`,
        source: edge.source,
        target: rule.target,
        label: rule.label,
        condition: { field: rule.field, op: rule.op, value: rule.value, value_ref: rule.value_ref },
        is_default: false,
      })
    })
    if (edge.default_target) {
      edges.push({
        id: `${edge.id ?? `edge_${index}`}_default`,
        source: edge.source,
        target: edge.default_target,
        label: edge.label ?? '默认',
        condition: null,
        is_default: true,
      })
    }
  })
  return { ...workflow, inputs: workflow.inputs ?? [], edges }
}

function newNode(kind: NodeKind, sequence: number, position = { x: 120, y: 120 }): NodeSpec {
  const base = {
    id: `${kind}_${sequence}`,
    position,
    join_mode: 'auto' as const,
    max_runs: 8,
  }
  if (kind === 'shell') return { ...base, label: 'Shell 命令', kind, worker_id: 'local', command: '', timeout_sec: 300, continue_on_error: false }
  if (kind === 'gate') return { ...base, label: '人工审批', kind, instruction: '请审阅上游产出后决定继续或打回。', allow_edit_output: true, review_nodes: [], rewind_targets: [] }
  if (kind === 'condition') return { ...base, label: '条件判断', kind }
  return {
    ...base,
    label: 'Agent 执行',
    kind: 'agent',
    worker_id: 'local',
    agent_id: 'auto',
    sandbox: 'workspace-write' as const,
    capture_git_diff: true,
    prompt_template: '{task}',
  }
}

function toolIcon(kind: NodeKind) {
  if (kind === 'shell') return <Terminal size={14} aria-hidden="true" />
  if (kind === 'gate') return <ShieldCheck size={14} aria-hidden="true" />
  if (kind === 'condition') return <GitBranch size={14} aria-hidden="true" />
  return <Bot size={14} aria-hidden="true" />
}

export default function WorkflowEditor({
  workflows,
  selectedWorkflowId,
  agents,
  workers,
  onSelectWorkflow,
  onWorkflowsChanged,
  onOpenRun,
  onToast,
}: WorkflowEditorProps) {
  const selected = workflows.find((item) => item.id === selectedWorkflowId) ?? workflows[0] ?? null
  const [draft, setDraft] = useState<WorkflowSpec | null>(selected ? normalizeWorkflow(selected) : null)
  const [baseline, setBaseline] = useState(() => draft ? JSON.stringify(draft) : '')
  const [selection, setSelection] = useState<CanvasSelection>(null)
  const [upstreamMap, setUpstreamMap] = useState<Record<string, string[]>>({})
  const [validation, setValidation] = useState<ValidateWorkflowOut | null>(null)
  const [validating, setValidating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [nodeDialogId, setNodeDialogId] = useState<string | null>(null)
  const [edgeDialogId, setEdgeDialogId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [instruction, setInstruction] = useState('')
  const [copilotAgent, setCopilotAgent] = useState('auto')
  const [copilotPending, setCopilotPending] = useState(false)
  const [aiUndo, setAiUndo] = useState<WorkflowSpec | null>(null)
  const [leaveAction, setLeaveAction] = useState<(() => void) | null>(null)
  const validationSequence = useRef(0)
  const dirty = Boolean(draft && JSON.stringify(draft) !== baseline)

  useEffect(() => {
    if (draft?.id === selectedWorkflowId) return
    const next = selected ? normalizeWorkflow(selected) : null
    setDraft(next)
    setBaseline(next ? JSON.stringify(next) : '')
    setSelection(null)
    setNodeDialogId(null)
    setEdgeDialogId(null)
    setValidation(null)
    setUpstreamMap({})
    setAiUndo(null)
    setError('')
  }, [selectedWorkflowId, workflows]) // 保存刷新不覆盖当前草稿，显式切换才重建

  const requestLeave = (action: () => void) => {
    if (dirty) setLeaveAction(() => action)
    else action()
  }

  useEffect(() => {
    if (!draft) return
    const sequence = ++validationSequence.current
    const timer = window.setTimeout(async () => {
      setValidating(true)
      try {
        const result = await api.validateWorkflow(draft)
        if (validationSequence.current !== sequence) return
        setValidation(result)
        setUpstreamMap(result.upstream_map ?? {})
      } catch (requestError) {
        if (validationSequence.current !== sequence) return
        setValidation({ valid: false, error: requestError instanceof Error ? requestError.message : '工作流校验失败' })
      } finally {
        if (validationSequence.current === sequence) setValidating(false)
      }
    }, 450)
    return () => window.clearTimeout(timer)
  }, [draft])

  const updateDraft = useCallback((updater: (current: WorkflowSpec) => WorkflowSpec) => {
    setDraft((current) => current ? updater(current) : current)
    setError('')
  }, [])

  const addNode = (kind: NodeKind, position?: { x: number; y: number }) => {
    if (!['agent', 'shell', 'gate', 'condition'].includes(kind)) return
    updateDraft((current) => {
      let sequence = current.nodes.length + 1
      while (current.nodes.some((node) => node.id === `${kind}_${sequence}`)) sequence += 1
      const node = newNode(kind, sequence, position ?? { x: 100 + (current.nodes.length % 3) * 230, y: 100 + Math.floor(current.nodes.length / 3) * 180 })
      setSelection({ type: 'node', id: node.id })
      return { ...current, nodes: [...current.nodes, node] }
    })
  }

  const deleteNode = (nodeId: string) => {
    updateDraft((current) => {
      if (current.nodes.length <= 1) {
        onToast('工作流至少需要保留一个节点')
        return current
      }
      const nodes = current.nodes.filter((node) => node.id !== nodeId)
      return {
        ...current,
        nodes,
        entry_point: current.entry_point === nodeId ? nodes[0].id : current.entry_point,
        edges: (current.edges ?? []).filter((edge) => edge.source !== nodeId && edge.target !== nodeId),
      }
    })
    setSelection(null)
  }

  const addEdge = (source: string, target: string) => {
    if (!source || !target || source === target || source === 'END' || target === 'START') return
    updateDraft((current) => {
      if ((current.edges ?? []).some((edge) => edge.source === source && edge.target === target)) {
        onToast('相同来源与目标的连线已存在')
        return current
      }
      const sourceNode = current.nodes.find((node) => node.id === source)
      const next: EdgeSpec = {
        id: edgeId(),
        source,
        target,
        label: sourceNode?.kind === 'condition' ? '条件分支' : '',
        condition: null,
        is_default: false,
      }
      setSelection({ type: 'edge', id: next.id! })
      return { ...current, edges: [...(current.edges ?? []), next] }
    })
  }

  const deleteEdge = (id: string) => {
    updateDraft((current) => ({
      ...current,
      edges: (current.edges ?? []).filter((edge, index) => (edge.id ?? `edge_${index}_${edge.source}_${edge.target}`) !== id),
    }))
    setSelection(null)
  }

  const changeEdge = (id: string, patch: Partial<EdgeSpec>) => {
    updateDraft((current) => ({
      ...current,
      edges: (current.edges ?? []).map((edge, index) => (edge.id ?? `edge_${index}_${edge.source}_${edge.target}`) === id ? { ...edge, ...patch } : edge),
    }))
  }

  const draftRef = useRef(draft)
  draftRef.current = draft

  const save = async () => {
    if (!draft || saving || copilotPending) return
    const submitted = draft
    const submittedJson = JSON.stringify(submitted)
    setSaving(true)
    setError('')
    try {
      const check = await api.validateWorkflow(submitted)
      setValidation(check)
      setUpstreamMap(check.upstream_map ?? {})
      if (!check.valid) throw new Error(check.error || '工作流拓扑校验未通过')
      const saved = await api.updateWorkflow(submitted.id, submitted)
      const normalized = normalizeWorkflow(saved)
      setDraft((current) => current && JSON.stringify(current) === submittedJson ? normalized : current)
      setBaseline(JSON.stringify(normalized))
      await onWorkflowsChanged()
      onSelectWorkflow(saved.id)
      onToast(`工作流「${saved.name}」已保存${draftRef.current && JSON.stringify(draftRef.current) !== submittedJson ? '，期间的新修改仍保留为未保存草稿' : ''}`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '保存工作流失败')
    } finally {
      setSaving(false)
    }
  }

  const saveSettings = async (incoming: WorkflowSpec) => {
    const current = draftRef.current
    if (!current) throw new Error('没有可保存的工作流')
    const merged: WorkflowSpec = {
      ...current,
      name: incoming.name,
      description: incoming.description,
      inputs: incoming.inputs,
    }
    const check = await api.validateWorkflow(merged)
    setValidation(check)
    setUpstreamMap(check.upstream_map ?? {})
    if (!check.valid) throw new Error(check.error || '工作流拓扑校验未通过')
    const saved = await api.updateWorkflow(current.id, merged)
    const normalized = normalizeWorkflow(saved)
    setDraft(normalized)
    setBaseline(JSON.stringify(normalized))
    await onWorkflowsChanged()
    onToast(`工作流「${saved.name}」设置已同步落盘保存`)
  }

  const renameNode = async (oldNodeId: string, newNodeId: string) => {
    const current = draftRef.current
    if (!current) throw new Error('没有正在编辑的工作流')
    const renamed = await api.renameNode({ workflow: current, old_node_id: oldNodeId, new_node_id: newNodeId })
    const normalized = normalizeWorkflow(renamed)
    setDraft(normalized)
    setSelection({ type: 'node', id: newNodeId })
    setNodeDialogId(newNodeId)
    onToast(`节点 ID 已联动更新为 ${newNodeId}，请记得保存画布落盘`)
  }

  const remove = async () => {
    if (!draft || deleting) return
    setDeleting(true)
    setError('')
    try {
      await api.deleteWorkflow(draft.id)
      await onWorkflowsChanged()
      setDeleteOpen(false)
      onToast(`工作流「${draft.name}」已删除`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '删除工作流失败')
      setDeleteOpen(false)
    } finally {
      setDeleting(false)
    }
  }

  const runCopilot = async (event: FormEvent) => {
    event.preventDefault()
    if (!draft || copilotPending || saving) return
    const normalizedInstruction = instruction.trim()
    if (normalizedInstruction.length < 2) {
      setError('AI 改图指令去除首尾空格后至少需要 2 个字符')
      return
    }
    const submitted = draft
    const submittedJson = JSON.stringify(submitted)
    setCopilotPending(true)
    setError('')
    try {
      const result = await api.workflowCopilot({
        instruction: normalizedInstruction,
        current_workflow: submitted,
        agent_id: copilotAgent,
        auto_save: false,
      })
      const current = draftRef.current
      if (!current || JSON.stringify(current) !== submittedJson) {
        setError('构图期间画布已有新修改，本次 AI 结果未应用，请基于当前草稿重新提交')
        return
      }
      setAiUndo(clone(current))
      setDraft(normalizeWorkflow(result.workflow))
      setSelection(null)
      setInstruction('')
      onToast(`${result.summary}（由 ${result.agent_used} 构图，尚未保存）`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'AI 改图失败')
    } finally {
      setCopilotPending(false)
    }
  }

  const handleCreated = (created: WorkflowSpec) => {
    requestLeave(() => {
      void onWorkflowsChanged().then(() => onSelectWorkflow(created.id))
      onToast(`工作流「${created.name}」已创建并落盘保存，立即可发起运行`)
    })
  }

  const stats = useMemo(() => ({ nodes: draft?.nodes.length ?? 0, edges: draft?.edges?.length ?? 0 }), [draft])

  if (!draft) {
    return (
      <section className="editor-empty">
        <Workflow size={28} aria-hidden="true" />
        <h2>从第一张工作流画布开始</h2>
        <p>一步创建并落盘保存：空白画布、一句话 AI 生成，或复制现有模板。</p>
        <button type="button" className="modal-submit" onClick={() => setCreateOpen(true)}>
          <Plus size={15} aria-hidden="true" />新建工作流
        </button>
        <CreateWorkflowDialog open={createOpen} workflows={workflows} agents={agents} onClose={() => setCreateOpen(false)} onCreated={handleCreated} />
      </section>
    )
  }

  return (
    <section className="b-canvas-card workflow-editor" id="workflow-panel">
      <div className="workflow-management-bar">
        <div className="workflow-switch-group">
          <Workflow size={18} aria-hidden="true" />
          <label htmlFor="workflow-switcher">工作流</label>
          <select id="workflow-switcher" name="workflow_id" value={draft.id} onChange={(event) => requestLeave(() => onSelectWorkflow(event.target.value))}>
            {workflows.map((item) => <option key={item.id} value={item.id}>{item.name}（{item.id}）</option>)}
          </select>
          <button type="button" className="create-workflow-button" onClick={() => setCreateOpen(true)}>
            <Plus size={14} aria-hidden="true" />新建工作流
          </button>
        </div>
        <div className="workflow-actions">
          <button type="button" className="soft-button" onClick={() => setSettingsOpen(true)}><Settings2 size={14} aria-hidden="true" />设置</button>
          <button type="button" className="soft-button danger-button" onClick={() => setDeleteOpen(true)}><Trash2 size={14} aria-hidden="true" />删除</button>
          <button type="button" className="soft-button" onClick={onOpenRun} disabled={!workflows.length}><Play size={14} aria-hidden="true" />试运行 / 发起运行</button>
          <button type="button" className="save-button" onClick={() => void save()} disabled={saving || copilotPending}>{saving ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <Save size={14} aria-hidden="true" />}{saving ? '保存中…' : '保存'}</button>
        </div>
      </div>

      <form className="copilot-bar" onSubmit={(event) => void runCopilot(event)}>
        <Sparkles size={17} aria-hidden="true" />
        <label htmlFor="workflow-copilot-instruction">一句话 AI 改图</label>
        <input id="workflow-copilot-instruction" name="instruction" required minLength={2} value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="如：在测试后加一个 Claude 审查节点，或改成前后端并行开发…" />
        <label className="visually-hidden" htmlFor="workflow-copilot-agent">构图 Agent</label>
        <select id="workflow-copilot-agent" name="agent_id" value={copilotAgent} onChange={(event) => setCopilotAgent(event.target.value)}>
          <option value="auto">auto</option>
          {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.id}{agent.available ? '' : '（未安装）'}</option>)}
        </select>
        <button type="submit" className="copilot-submit" disabled={copilotPending || saving}>{copilotPending ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <Sparkles size={14} aria-hidden="true" />}{copilotPending ? '构图中…' : 'AI 构图'}</button>
        {aiUndo && <button type="button" className="soft-button" onClick={() => { setDraft(aiUndo); setAiUndo(null); setSelection(null); onToast('已撤销本次 AI 改图') }}><Undo2 size={14} aria-hidden="true" />撤销改图</button>}
      </form>

      <div className="node-toolbar" aria-label="节点工具栏">
        <span>拖入或点击添加</span>
        {nodeTools.map((tool) => <button key={tool.kind} type="button" draggable onDragStart={(event: ReactDragEvent<HTMLButtonElement>) => { event.dataTransfer.setData('application/x-workflow-node', tool.kind); event.dataTransfer.effectAllowed = 'copy' }} onClick={() => addNode(tool.kind)}>{toolIcon(tool.kind)}{tool.label}</button>)}
        <span className={`validation-state ${validation?.valid ? 'is-valid' : validation ? 'is-invalid' : ''}`} role="status">{validating ? '正在校验…' : validation?.valid ? <><Check size={13} aria-hidden="true" />拓扑有效</> : validation?.error || '等待校验'}</span>
      </div>

      {error && <div className="inline-error editor-error" role="alert">{error}</div>}
      <FlowCanvas
        workflow={draft}
        mode="edit"
        selected={selection}
        onSelect={setSelection}
        onNodePositionChange={(nodeId, position) => updateDraft((current) => ({ ...current, nodes: current.nodes.map((node) => node.id === nodeId ? { ...node, position } : node) }))}
        onConnect={addEdge}
        onDeleteNode={deleteNode}
        onDeleteEdge={deleteEdge}
        onDropNode={addNode}
        onOpenNode={(id) => setNodeDialogId(id)}
        onOpenEdge={(id) => setEdgeDialogId(id)}
        nodeActionLabel={() => '编辑'}
      />
      <div className="b-canvas-footer"><span><i aria-hidden="true" />{dirty ? '未保存更改' : '所有更改已保存'}</span><span>{stats.nodes} 个节点 · {stats.edges} 条连线 · 多出口普通边即并行</span></div>

      <NodeEditorDialog
        nodeId={nodeDialogId}
        workflow={draft}
        agents={agents}
        workers={workers}
        upstreamMap={upstreamMap}
        onChangeNode={(nodeId, patch) => updateDraft((current) => ({ ...current, nodes: current.nodes.map((node) => node.id === nodeId ? { ...node, ...patch } : node) }))}
        onChangeEdge={changeEdge}
        onAddEdge={addEdge}
        onDeleteEdge={deleteEdge}
        onSetEntry={(entry_point) => updateDraft((current) => ({ ...current, entry_point }))}
        onDeleteNode={deleteNode}
        onRenameNode={renameNode}
        onClose={() => setNodeDialogId(null)}
      />
      <EdgeEditorDialog
        edgeId={edgeDialogId}
        workflow={draft}
        upstreamMap={upstreamMap}
        onChangeEdge={changeEdge}
        onDeleteEdge={deleteEdge}
        onClose={() => setEdgeDialogId(null)}
      />
      <CreateWorkflowDialog open={createOpen} workflows={workflows} agents={agents} onClose={() => setCreateOpen(false)} onCreated={handleCreated} />
      <WorkflowSettingsDialog open={settingsOpen} workflow={draft} onClose={() => setSettingsOpen(false)} onSave={saveSettings} />
      <ConfirmDialog open={deleteOpen} title="删除工作流" description={`确定删除「${draft.name}」（${draft.id}）吗？此操作不可撤销。`} confirmLabel="确认删除" pending={deleting} onClose={() => setDeleteOpen(false)} onConfirm={() => void remove()} />
      <ConfirmDialog open={leaveAction !== null} title="放弃未保存更改" description="当前画布有未保存修改，继续操作将放弃这些更改。你也可以先取消并保存工作流。" confirmLabel="放弃并继续" onClose={() => setLeaveAction(null)} onConfirm={() => { const action = leaveAction; setLeaveAction(null); action?.() }} />
    </section>
  )
}

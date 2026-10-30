import { useEffect, useId, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import {
  Bot, CircleCheck, GitBranch, LoaderCircle, Maximize2, Minimize2,
  ShieldCheck, Terminal, Trash2, X,
} from 'lucide-react'
import type {
  AgentProfileOut,
  EdgeCondition,
  EdgeSpec,
  JoinMode,
  NodeKind,
  NodeSpec,
  RouteOperator,
  SandboxMode,
  WorkerInfoOut,
  WorkflowSpec,
} from '../types'
import { NativeDialog } from './Dialogs'

const JOIN_MODES: Array<{ value: JoinMode; label: string }> = [
  { value: 'auto', label: 'auto · 自动识别' },
  { value: 'all', label: 'all · 等待全部分支' },
  { value: 'any', label: 'any · 任一分支到达' },
]

const ROUTE_OPERATORS: RouteOperator[] = [
  '==', '!=', '<', '<=', '>', '>=',
  'contains', 'not_contains', 'regex', 'truthy', 'falsy',
]

const CONDITION_OUTPUTS = ['ok', 'exit_code', 'runs', 'output'] as const

const kindLabels: Record<NodeKind, string> = {
  agent: 'Agent',
  shell: 'Shell',
  gate: '人工卡点',
  condition: '条件判断',
  summary: '汇总',
}

const NODE_ID_PATTERN = '^[a-zA-Z0-9_-]+$'

function kindIcon(kind: NodeKind) {
  if (kind === 'gate') return <ShieldCheck size={16} aria-hidden="true" />
  if (kind === 'shell') return <Terminal size={16} aria-hidden="true" />
  if (kind === 'condition') return <GitBranch size={16} aria-hidden="true" />
  if (kind === 'summary') return <CircleCheck size={16} aria-hidden="true" />
  return <Bot size={16} aria-hidden="true" />
}

function nodeOptions(workflow: WorkflowSpec, excludeId: string) {
  return [
    ...workflow.nodes.filter((node) => node.id !== excludeId).map((node) => ({ id: node.id, label: node.label })),
    { id: 'END', label: 'END · 结束' },
  ]
}

interface TemplateEditorProps {
  controlId: string
  name: string
  label: string
  value: string
  variables: string[]
  maximized: boolean
  onChange: (value: string) => void
}

function TemplateEditor({ controlId, name, label, value, variables, maximized, onChange }: TemplateEditorProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  function preserveSelection(event: ReactMouseEvent<HTMLButtonElement>) {
    event.preventDefault()
  }

  function insertVariable(variable: string) {
    const textarea = textareaRef.current
    if (!textarea) return
    const start = textarea.selectionStart
    const end = textarea.selectionEnd
    onChange(`${value.slice(0, start)}${variable}${value.slice(end)}`)
    const nextCursor = start + variable.length
    window.requestAnimationFrame(() => {
      textarea.focus()
      textarea.setSelectionRange(nextCursor, nextCursor)
    })
  }

  return (
    <section className="workbench-editor" aria-label={label}>
      <div className="variable-chips" aria-label={`${label}变量快捷插入`}>
        {variables.map((variable) => (
          <button
            className="variable-chip"
            type="button"
            key={variable}
            title={`插入 ${variable}`}
            onMouseDown={preserveSelection}
            onClick={() => insertVariable(variable)}
          >
            {variable}
          </button>
        ))}
      </div>
      <label className="visually-hidden" htmlFor={controlId}>{label}</label>
      <textarea
        ref={textareaRef}
        id={controlId}
        name={name}
        className={`template-textarea ${maximized ? 'template-textarea--max' : ''}`}
        value={value}
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
      />
    </section>
  )
}

interface NodeChecklistProps {
  legend: string
  values: string[]
  options: Array<{ id: string; label: string }>
  name: string
  onChange: (values: string[]) => void
}

function NodeChecklist({ legend, values, options, name, onChange }: NodeChecklistProps) {
  return (
    <fieldset className="node-checklist">
      <legend>{legend}</legend>
      {options.length === 0 && <p className="field-hint">暂无上游节点可选。</p>}
      <div className="node-checklist__options">
        {options.map((option) => (
          <label key={option.id}>
            <input
              type="checkbox"
              name={name}
              value={option.id}
              checked={values.includes(option.id)}
              onChange={(event) => onChange(
                event.target.checked ? [...values, option.id] : values.filter((id) => id !== option.id),
              )}
            />
            <span>{option.label}（{option.id}）</span>
          </label>
        ))}
      </div>
    </fieldset>
  )
}

interface ConditionBranchListProps {
  workflow: WorkflowSpec
  node: NodeSpec
  upstreamMap: Record<string, string[]>
  onChangeEdge: (edgeId: string, patch: Partial<EdgeSpec>) => void
  onAddEdge: (source: string, target: string) => void
  onDeleteEdge: (edgeId: string) => void
}

function ConditionBranchList({ workflow, node, upstreamMap, onChangeEdge, onAddEdge, onDeleteEdge }: ConditionBranchListProps) {
  const [pendingTarget, setPendingTarget] = useState('')
  const controlId = useId()
  const upstreamIds = upstreamMap[node.id] ?? []
  const outgoing = (workflow.edges ?? [])
    .map((edge, index) => ({ edge, id: edge.id ?? `edge_${index}_${edge.source}_${edge.target}` }))
    .filter(({ edge }) => edge.source === node.id && Boolean(edge.target))

  useEffect(() => setPendingTarget(''), [node.id])

  return (
    <section className="workbench-branches" aria-label="条件分支">
      <h3>出边条件分支（双击画布连线可放大编辑单条）</h3>
      {outgoing.length === 0 && <p className="field-hint">尚无出边，请在下方选择目标节点新增分支。</p>}
      {outgoing.map(({ edge, id }) => {
        const condition = edge.condition ?? { field: '', op: '==' as RouteOperator, value: '' }
        const prefix = `${controlId}-${id}`
        const changeCondition = (patch: Partial<EdgeCondition>) => onChangeEdge(id, { condition: { ...condition, ...patch } })
        return (
          <div className="branch-row" key={id}>
            <div className="field">
              <label htmlFor={`${prefix}-target`}>目标节点</label>
              <select
                id={`${prefix}-target`}
                name="target"
                value={edge.target ?? ''}
                onChange={(event) => onChangeEdge(id, { target: event.target.value })}
              >
                {nodeOptions(workflow, node.id).map((option) => (
                  <option key={option.id} value={option.id}>{option.label}（{option.id}）</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor={`${prefix}-default`}>兜底分支</label>
              <input
                id={`${prefix}-default`}
                name="is_default"
                type="checkbox"
                checked={edge.is_default ?? false}
                onChange={(event) => onChangeEdge(id, event.target.checked
                  ? { is_default: true, condition: null }
                  : { is_default: false, condition: { field: '', op: '==', value: '' } })}
              />
            </div>
            {!edge.is_default && (
              <>
                <div className="field">
                  <label htmlFor={`${prefix}-field`}>判断字段</label>
                  <select
                    id={`${prefix}-field`}
                    name="condition_field"
                    value={condition.field}
                    onChange={(event) => changeCondition({ field: event.target.value })}
                  >
                    <option value="">请选择判断字段</option>
                    <optgroup label="上游节点结果">
                      {upstreamIds.flatMap((nodeId) => CONDITION_OUTPUTS.map((output) => {
                        const field = `${nodeId}.${output}`
                        return <option key={field} value={field}>{field}</option>
                      }))}
                    </optgroup>
                    <optgroup label="工作流输入">
                      {(workflow.inputs ?? []).map((input) => (
                        <option key={input.key} value={`inputs.${input.key}`}>inputs.{input.key}</option>
                      ))}
                    </optgroup>
                  </select>
                </div>
                <div className="field">
                  <label htmlFor={`${prefix}-op`}>运算符</label>
                  <select
                    id={`${prefix}-op`}
                    name="condition_op"
                    value={condition.op ?? '=='}
                    onChange={(event) => changeCondition({ op: event.target.value as RouteOperator })}
                  >
                    {ROUTE_OPERATORS.map((operator) => <option key={operator} value={operator}>{operator}</option>)}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor={`${prefix}-value`}>比较值</label>
                  <input
                    id={`${prefix}-value`}
                    name="condition_value"
                    autoCorrect="off"
                    autoCapitalize="off"
                    spellCheck={false}
                    value={condition.value === undefined || condition.value === null ? '' : String(condition.value)}
                    onChange={(event) => changeCondition({ value: event.target.value })}
                  />
                </div>
              </>
            )}
            <button
              type="button"
              className="inspector-button inspector-button--danger branch-row__delete"
              aria-label={`删除到 ${edge.target} 的分支`}
              onClick={() => onDeleteEdge(id)}
            >
              <Trash2 size={13} aria-hidden="true" />
            </button>
          </div>
        )
      })}
      <div className="field field--inline">
        <label htmlFor={`${controlId}-add-target`}>新增分支目标</label>
        <select
          id={`${controlId}-add-target`}
          name="new_branch_target"
          value={pendingTarget}
          onChange={(event) => setPendingTarget(event.target.value)}
        >
          <option value="">请选择目标节点</option>
          {nodeOptions(workflow, node.id).map((option) => (
            <option key={option.id} value={option.id}>{option.label}（{option.id}）</option>
          ))}
        </select>
        <button
          type="button"
          className="inspector-button inspector-button--secondary"
          disabled={!pendingTarget}
          onClick={() => {
            if (!pendingTarget) return
            onAddEdge(node.id, pendingTarget)
            setPendingTarget('')
          }}
        >
          新增分支
        </button>
      </div>
    </section>
  )
}

export interface NodeEditorDialogProps {
  nodeId: string | null
  workflow: WorkflowSpec
  agents: AgentProfileOut[]
  workers: WorkerInfoOut[]
  upstreamMap: Record<string, string[]>
  onChangeNode: (nodeId: string, patch: Partial<NodeSpec>) => void
  onChangeEdge: (edgeId: string, patch: Partial<EdgeSpec>) => void
  onAddEdge: (source: string, target: string) => void
  onDeleteEdge: (edgeId: string) => void
  onSetEntry: (nodeId: string) => void
  onDeleteNode: (nodeId: string) => void
  onRenameNode: (oldNodeId: string, newNodeId: string) => Promise<void>
  onClose: () => void
}

export default function NodeEditorDialog({
  nodeId,
  workflow,
  agents,
  workers,
  upstreamMap,
  onChangeNode,
  onChangeEdge,
  onAddEdge,
  onDeleteEdge,
  onSetEntry,
  onDeleteNode,
  onRenameNode,
  onClose,
}: NodeEditorDialogProps) {
  const [maximized, setMaximized] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [newNodeId, setNewNodeId] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [renameError, setRenameError] = useState('')
  const dialogTitleId = useId()
  const node = workflow.nodes.find((candidate) => candidate.id === nodeId) ?? null

  useEffect(() => {
    setConfirmingDelete(false)
    setNewNodeId('')
    setRenaming(false)
    setRenameError('')
  }, [nodeId])

  useEffect(() => {
    if (nodeId && !node) onClose()
  }, [nodeId, node, onClose])

  if (!node) {
    return <NativeDialog open={false} onClose={onClose} labelledBy={dialogTitleId}><span className="visually-hidden">节点编辑</span></NativeDialog>
  }

  const prefix = `node-editor-${node.id}`
  const upstreamIds = upstreamMap[node.id] ?? []
  const upstreamNodes = upstreamIds.map((id) => ({
    id,
    label: workflow.nodes.find((candidate) => candidate.id === id)?.label ?? id,
  }))
  const variables = [
    '{task}',
    '{feedback}',
    ...(workflow.inputs ?? []).map((input) => `{inputs.${input.key}}`),
    ...upstreamIds.flatMap((id) => [`{${id}.output}`, `{${id}.diff}`, `{${id}.stdout}`]),
  ]

  const mainEditor = node.kind === 'agent'
    ? { name: 'prompt_template', label: '提示词模板（prompt_template）', value: node.prompt_template ?? '', apply: (value: string) => onChangeNode(node.id, { prompt_template: value }) }
    : node.kind === 'shell'
      ? { name: 'command', label: 'Shell 脚本命令（command）', value: node.command ?? '', apply: (value: string) => onChangeNode(node.id, { command: value }) }
      : node.kind === 'gate'
        ? { name: 'instruction', label: '审批指引说明（instruction）', value: node.instruction ?? '', apply: (value: string) => onChangeNode(node.id, { instruction: value }) }
        : null

  const applyRename = async () => {
    const nextId = newNodeId.trim()
    if (!nextId || renaming) return
    if (!new RegExp(NODE_ID_PATTERN).test(nextId)) {
      setRenameError('节点 ID 仅限字母、数字、连字符 - 或下划线 _')
      return
    }
    if (nextId === node.id) {
      setRenameError('新 ID 与当前节点 ID 相同')
      return
    }
    setRenaming(true)
    setRenameError('')
    try {
      await onRenameNode(node.id, nextId)
      setNewNodeId('')
    } catch (error) {
      setRenameError(error instanceof Error ? error.message : '重命名节点失败')
    } finally {
      setRenaming(false)
    }
  }

  return (
    <NativeDialog
      open
      onClose={onClose}
      labelledBy={dialogTitleId}
      className={`workbench-dialog ${maximized ? 'workbench-dialog--max' : ''}`}
    >
      <header className="workbench-header">
        <span className={`workbench-kind flow-node-icon--${node.kind}`}>{kindIcon(node.kind)}{kindLabels[node.kind]}</span>
        <h2 id={dialogTitleId}>{node.label}</h2>
        <code className="workbench-node-id">{node.id}</code>
        <div className="workbench-actions">
          <button
            type="button"
            className="soft-button"
            disabled={workflow.entry_point === node.id}
            onClick={() => onSetEntry(node.id)}
          >
            {workflow.entry_point === node.id ? '当前入口节点 START' : '设为入口节点 START'}
          </button>
          <button
            type="button"
            className={`soft-button danger-button ${confirmingDelete ? 'is-confirming' : ''}`}
            onClick={() => {
              if (!confirmingDelete) {
                setConfirmingDelete(true)
                return
              }
              onDeleteNode(node.id)
              onClose()
            }}
          >
            <Trash2 size={13} aria-hidden="true" />{confirmingDelete ? '再次点击确认删除' : '删除节点'}
          </button>
          <button
            type="button"
            className="icon-button"
            aria-label={maximized ? '还原窗口' : '最大化窗口'}
            title={maximized ? '还原窗口' : '最大化窗口'}
            onClick={() => setMaximized((value) => !value)}
          >
            {maximized ? <Minimize2 size={16} aria-hidden="true" /> : <Maximize2 size={16} aria-hidden="true" />}
          </button>
          <button type="button" className="icon-button" aria-label="完成并关闭节点编辑" title="完成关闭" onClick={onClose}>
            <X size={17} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="workbench-body">
        <section className="workbench-core" aria-label="核心属性">
          <div className="field">
            <label htmlFor={`${prefix}-label`}>节点名称</label>
            <input
              id={`${prefix}-label`}
              name="label"
              value={node.label}
              onChange={(event) => onChangeNode(node.id, { label: event.target.value })}
            />
          </div>
          {node.kind === 'agent' && (
            <>
              <div className="field">
                <label htmlFor={`${prefix}-agent`}>CLI Agent</label>
                <select
                  id={`${prefix}-agent`}
                  name="agent_id"
                  value={node.agent_id ?? 'auto'}
                  onChange={(event) => onChangeNode(node.id, { agent_id: event.target.value })}
                >
                  <option value="auto">auto · 自动选择</option>
                  {agents.map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}（{agent.id}）· {agent.available ? '在线' : '未安装 / 离线'}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor={`${prefix}-cwd`}>工作目录（cwd）</label>
                <input
                  id={`${prefix}-cwd`}
                  name="cwd"
                  placeholder="frontend、绝对路径或 {inputs.repo}"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  value={node.cwd ?? ''}
                  onChange={(event) => onChangeNode(node.id, { cwd: event.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor={`${prefix}-sandbox`}>沙箱权限（sandbox）</label>
                <select
                  id={`${prefix}-sandbox`}
                  name="sandbox"
                  value={node.sandbox ?? 'workspace-write'}
                  onChange={(event) => onChangeNode(node.id, { sandbox: event.target.value as SandboxMode })}
                >
                  <option value="read-only">read-only · 只读分析</option>
                  <option value="workspace-write">workspace-write · 允许读写代码</option>
                </select>
              </div>
            </>
          )}
          {node.kind === 'shell' && (
            <>
              <div className="field">
                <label htmlFor={`${prefix}-cwd`}>工作目录（cwd）</label>
                <input
                  id={`${prefix}-cwd`}
                  name="cwd"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  value={node.cwd ?? ''}
                  onChange={(event) => onChangeNode(node.id, { cwd: event.target.value })}
                />
              </div>
              <div className="field field--checkbox">
                <input
                  id={`${prefix}-continue-on-error`}
                  name="continue_on_error"
                  type="checkbox"
                  checked={node.continue_on_error ?? false}
                  onChange={(event) => onChangeNode(node.id, { continue_on_error: event.target.checked })}
                />
                <label htmlFor={`${prefix}-continue-on-error`}>出错后继续（continue_on_error）</label>
              </div>
            </>
          )}
          {node.kind === 'gate' && (
            <>
              <div className="field field--checkbox">
                <input
                  id={`${prefix}-allow-edit`}
                  name="allow_edit_output"
                  type="checkbox"
                  checked={node.allow_edit_output ?? true}
                  onChange={(event) => onChangeNode(node.id, { allow_edit_output: event.target.checked })}
                />
                <label htmlFor={`${prefix}-allow-edit`}>允许审批人在线修改产出</label>
              </div>
              <NodeChecklist
                legend="重点审阅的上游节点（review_nodes）"
                name="review_nodes"
                values={node.review_nodes ?? []}
                options={upstreamNodes}
                onChange={(review_nodes) => onChangeNode(node.id, { review_nodes })}
              />
              <NodeChecklist
                legend="允许打回的上游节点（rewind_targets）"
                name="rewind_targets"
                values={node.rewind_targets ?? []}
                options={upstreamNodes}
                onChange={(rewind_targets) => onChangeNode(node.id, { rewind_targets })}
              />
            </>
          )}
        </section>

        {node.kind === 'condition' && (
          <ConditionBranchList
            workflow={workflow}
            node={node}
            upstreamMap={upstreamMap}
            onChangeEdge={onChangeEdge}
            onAddEdge={onAddEdge}
            onDeleteEdge={onDeleteEdge}
          />
        )}

        {mainEditor && (
          <TemplateEditor
            controlId={`${prefix}-template`}
            name={mainEditor.name}
            label={mainEditor.label}
            value={mainEditor.value}
            variables={variables}
            maximized={maximized}
            onChange={mainEditor.apply}
          />
        )}

        <details className="workbench-advanced">
          <summary>高级设置</summary>
          <div className="workbench-advanced__grid">
            <div className="field">
              <label htmlFor={`${prefix}-rename`}>重命名节点 ID</label>
              <div className="field field--inline">
                <input
                  id={`${prefix}-rename`}
                  name="new_node_id"
                  placeholder={node.id}
                  pattern="[A-Za-z0-9_-]+"
                  title="仅限字母、数字、连字符 - 或下划线 _"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  value={newNodeId}
                  onChange={(event) => setNewNodeId(event.target.value)}
                />
                <button
                  type="button"
                  className="inspector-button inspector-button--secondary"
                  disabled={renaming || !newNodeId.trim()}
                  onClick={() => void applyRename()}
                >
                  {renaming ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : null}
                  应用新 ID
                </button>
              </div>
              {renameError && <p className="field-error" role="alert">{renameError}</p>}
              <p className="field-hint">应用后自动联动更新入口、连线、审批列表与全部 {'{节点ID.xxx}'} 模板引用。</p>
            </div>
            <div className="field">
              <label htmlFor={`${prefix}-worker`}>执行主机（worker_id）</label>
              <select
                id={`${prefix}-worker`}
                name="worker_id"
                value={node.worker_id ?? 'local'}
                onChange={(event) => onChangeNode(node.id, { worker_id: event.target.value })}
              >
                {workers.map((worker) => (
                  <option key={worker.id} value={worker.id}>
                    {worker.name}（{worker.id}）{worker.online === false ? ' · 离线' : ' · 在线'}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor={`${prefix}-join-mode`}>多入边汇聚策略（join_mode）</label>
              <select
                id={`${prefix}-join-mode`}
                name="join_mode"
                value={node.join_mode ?? 'auto'}
                onChange={(event) => onChangeNode(node.id, { join_mode: event.target.value as JoinMode })}
              >
                {JOIN_MODES.map((mode) => <option key={mode.value} value={mode.value}>{mode.label}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor={`${prefix}-max-runs`}>自动循环上限（max_runs）</label>
              <input
                id={`${prefix}-max-runs`}
                name="max_runs"
                type="number"
                min={1}
                max={50}
                step={1}
                value={node.max_runs ?? 8}
                onChange={(event) => onChangeNode(node.id, { max_runs: Number(event.target.value) })}
              />
            </div>
            <div className="field">
              <label htmlFor={`${prefix}-timeout`}>超时秒数（timeout_sec）</label>
              <input
                id={`${prefix}-timeout`}
                name="timeout_sec"
                type="number"
                min={5}
                max={14400}
                step={1}
                value={node.timeout_sec ?? 1800}
                onChange={(event) => onChangeNode(node.id, { timeout_sec: Number(event.target.value) })}
              />
            </div>
            {(node.kind === 'agent' || node.kind === 'shell') && (
              <div className="field field--checkbox">
                <input
                  id={`${prefix}-capture-diff`}
                  name="capture_git_diff"
                  type="checkbox"
                  checked={node.capture_git_diff ?? true}
                  onChange={(event) => onChangeNode(node.id, { capture_git_diff: event.target.checked })}
                />
                <label htmlFor={`${prefix}-capture-diff`}>抓取 Git Diff（capture_git_diff）</label>
              </div>
            )}
            <div className="field">
              <label htmlFor={`${prefix}-description`}>节点职责描述</label>
              <textarea
                id={`${prefix}-description`}
                name="description"
                rows={3}
                value={node.description ?? ''}
                onChange={(event) => onChangeNode(node.id, { description: event.target.value })}
              />
            </div>
          </div>
        </details>
      </div>
    </NativeDialog>
  )
}

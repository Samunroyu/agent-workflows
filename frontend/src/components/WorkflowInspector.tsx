import { useEffect, useId, useRef, useState } from 'react'
import type { ChangeEvent, MouseEvent as ReactMouseEvent } from 'react'
import type { CanvasSelection } from './FlowCanvas'
import type {
  AgentProfileOut,
  EdgeCondition,
  EdgeSpec,
  JoinMode,
  NodeSpec,
  RouteOperator,
  SandboxMode,
  WorkerInfoOut,
  WorkflowSpec,
} from '../types'

export interface WorkflowInspectorProps {
  workflow: WorkflowSpec
  selection: CanvasSelection
  agents: AgentProfileOut[]
  workers: WorkerInfoOut[]
  upstreamMap: Record<string, string[]>
  onChangeNode: (nodeId: string, patch: Partial<NodeSpec>) => void
  onChangeEdge: (edgeId: string, patch: Partial<EdgeSpec>) => void
  onSetEntry: (nodeId: string) => void
  onDeleteNode: (nodeId: string) => void
  onDeleteEdge: (edgeId: string) => void
  onAddEdge: (source: string, target: string) => void
  onSelect: (selection: CanvasSelection) => void
}

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

function directEdgeId(edge: EdgeSpec, index: number) {
  return edge.id ?? `edge-${index}-${edge.source}-${edge.target ?? 'unlinked'}`
}

function nodeOptions(workflow: WorkflowSpec, includeEnd = false) {
  return [
    ...workflow.nodes.map((node) => ({ id: node.id, label: node.label })),
    ...(includeEnd ? [{ id: 'END', label: 'END · 结束' }] : []),
  ]
}

interface AddEdgeControlProps {
  source: string
  workflow: WorkflowSpec
  onAddEdge: (source: string, target: string) => void
}

function AddEdgeControl({ source, workflow, onAddEdge }: AddEdgeControlProps) {
  const [target, setTarget] = useState('')
  const controlId = useId()

  useEffect(() => setTarget(''), [source])

  return (
    <section className="inspector-section inspector-add-edge" aria-labelledby={`${controlId}-title`}>
      <h3 id={`${controlId}-title`}>新增连线目标</h3>
      <p className="inspector-help">通过键盘选择目标，无需在画布上拖线。</p>
      <div className="field field--inline">
        <label htmlFor={controlId}>目标节点</label>
        <select
          id={controlId}
          name="newEdgeTarget"
          value={target}
          onChange={(event) => setTarget(event.target.value)}
        >
          <option value="">请选择目标节点</option>
          {nodeOptions(workflow, true)
            .filter((node) => node.id !== source)
            .map((node) => <option key={node.id} value={node.id}>{node.label}（{node.id}）</option>)}
        </select>
        <button
          className="inspector-button inspector-button--secondary"
          type="button"
          disabled={!target}
          onClick={() => {
            if (!target) return
            onAddEdge(source, target)
            setTarget('')
          }}
        >
          新增连线
        </button>
      </div>
    </section>
  )
}

interface VariableTemplateEditorProps {
  controlId: string
  name: string
  label: string
  value: string
  onChange: (value: string) => void
  variables: string[]
  rows?: number
}

function VariableTemplateEditor({
  controlId,
  name,
  label,
  value,
  onChange,
  variables,
  rows = 8,
}: VariableTemplateEditorProps) {
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
    <div className="field field--template">
      <label htmlFor={controlId}>{label}</label>
      <textarea
        ref={textareaRef}
        id={controlId}
        name={name}
        rows={rows}
        value={value}
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
      />
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
    </div>
  )
}

interface NodeMultiSelectProps {
  controlId: string
  name: string
  label: string
  values: string[]
  options: Array<{ id: string; label: string }>
  onChange: (values: string[]) => void
}

function NodeMultiSelect({ controlId, name, label, values, options, onChange }: NodeMultiSelectProps) {
  function handleChange(event: ChangeEvent<HTMLSelectElement>) {
    onChange(Array.from(event.target.selectedOptions, (option) => option.value))
  }

  return (
    <div className="field">
      <label htmlFor={controlId}>{label}</label>
      <select id={controlId} name={name} multiple value={values} onChange={handleChange}>
        {options.map((option) => (
          <option key={option.id} value={option.id}>{option.label}（{option.id}）</option>
        ))}
      </select>
      <span className="field-hint">可按 Ctrl / Command 多选。</span>
    </div>
  )
}

interface NodeInspectorProps extends Omit<WorkflowInspectorProps, 'selection' | 'onChangeEdge' | 'onDeleteEdge'> {
  node: NodeSpec
}

function NodeInspector({
  node,
  workflow,
  agents,
  workers,
  upstreamMap,
  onChangeNode,
  onSetEntry,
  onDeleteNode,
  onAddEdge,
  onSelect,
}: NodeInspectorProps) {
  const prefix = `inspector-node-${node.id}`
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
  const outgoingEdges = (workflow.edges ?? [])
    .map((edge, index) => ({ edge, id: directEdgeId(edge, index) }))
    .filter(({ edge }) => edge.source === node.id && Boolean(edge.target))

  return (
    <>
      <header className="inspector-header">
        <span className="inspector-eyebrow">节点属性 · {node.kind}</span>
        <h2>{node.label}</h2>
      </header>

      <section className="inspector-section" aria-label="通用节点设置">
        <div className="field">
          <label htmlFor={`${prefix}-id`}>节点 ID</label>
          <input
            id={`${prefix}-id`}
            name="nodeId"
            value={node.id}
            readOnly
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
          />
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-label`}>显示名称</label>
          <input
            id={`${prefix}-label`}
            name="label"
            value={node.label}
            onChange={(event) => onChangeNode(node.id, { label: event.target.value })}
          />
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-description`}>职责描述</label>
          <textarea
            id={`${prefix}-description`}
            name="description"
            rows={3}
            value={node.description ?? ''}
            onChange={(event) => onChangeNode(node.id, { description: event.target.value })}
          />
        </div>
        <button
          className="inspector-button inspector-button--secondary"
          type="button"
          disabled={workflow.entry_point === node.id}
          onClick={() => onSetEntry(node.id)}
        >
          {workflow.entry_point === node.id ? '当前入口节点' : '设为入口节点'}
        </button>
        <div className="field">
          <label htmlFor={`${prefix}-join-mode`}>多入边汇聚策略（join_mode）</label>
          <select
            id={`${prefix}-join-mode`}
            name="joinMode"
            value={node.join_mode ?? 'auto'}
            onChange={(event) => onChangeNode(node.id, { join_mode: event.target.value as JoinMode })}
          >
            {JOIN_MODES.map((mode) => <option key={mode.value} value={mode.value}>{mode.label}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-max-runs`}>循环上限（max_runs）</label>
          <input
            id={`${prefix}-max-runs`}
            name="maxRuns"
            type="number"
            min={1}
            max={50}
            step={1}
            value={node.max_runs ?? 8}
            onChange={(event) => onChangeNode(node.id, { max_runs: Number(event.target.value) })}
          />
        </div>
      </section>

      {node.kind === 'agent' && (
        <section className="inspector-section inspector-agent" aria-label="Agent 节点设置">
          <h3>Agent 执行设置</h3>
          <div className="field">
            <label htmlFor={`${prefix}-worker`}>执行主机（worker_id）</label>
            <select
              id={`${prefix}-worker`}
              name="workerId"
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
            <label htmlFor={`${prefix}-agent`}>CLI Agent（agent_id）</label>
            <select
              id={`${prefix}-agent`}
              name="agentId"
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
              value={node.cwd ?? ''}
              placeholder="frontend、绝对路径或 {inputs.repo}"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
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
          <div className="field field--checkbox">
            <input
              id={`${prefix}-capture-diff`}
              name="captureGitDiff"
              type="checkbox"
              checked={node.capture_git_diff ?? true}
              onChange={(event) => onChangeNode(node.id, { capture_git_diff: event.target.checked })}
            />
            <label htmlFor={`${prefix}-capture-diff`}>捕获 Git Diff（capture_git_diff）</label>
          </div>
          <VariableTemplateEditor
            controlId={`${prefix}-prompt`}
            name="promptTemplate"
            label="Prompt 模板（prompt_template）"
            value={node.prompt_template ?? ''}
            variables={variables}
            onChange={(prompt_template) => onChangeNode(node.id, { prompt_template })}
          />
        </section>
      )}

      {node.kind === 'shell' && (
        <section className="inspector-section inspector-shell" aria-label="Shell 节点设置">
          <h3>Shell 执行设置</h3>
          <div className="field">
            <label htmlFor={`${prefix}-worker`}>执行主机（worker_id）</label>
            <select
              id={`${prefix}-worker`}
              name="workerId"
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
            <label htmlFor={`${prefix}-cwd`}>工作目录（cwd）</label>
            <input
              id={`${prefix}-cwd`}
              name="cwd"
              value={node.cwd ?? ''}
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              onChange={(event) => onChangeNode(node.id, { cwd: event.target.value })}
            />
          </div>
          <VariableTemplateEditor
            controlId={`${prefix}-command`}
            name="command"
            label="命令模板（command）"
            value={node.command ?? ''}
            variables={variables}
            rows={5}
            onChange={(command) => onChangeNode(node.id, { command })}
          />
          <div className="field field--checkbox">
            <input
              id={`${prefix}-continue-on-error`}
              name="continueOnError"
              type="checkbox"
              checked={node.continue_on_error ?? false}
              onChange={(event) => onChangeNode(node.id, { continue_on_error: event.target.checked })}
            />
            <label htmlFor={`${prefix}-continue-on-error`}>出错后继续（continue_on_error）</label>
          </div>
          <div className="field">
            <label htmlFor={`${prefix}-timeout`}>超时秒数（timeout_sec）</label>
            <input
              id={`${prefix}-timeout`}
              name="timeoutSec"
              type="number"
              min={5}
              max={14400}
              step={1}
              value={node.timeout_sec ?? 1800}
              onChange={(event) => onChangeNode(node.id, { timeout_sec: Number(event.target.value) })}
            />
          </div>
        </section>
      )}

      {node.kind === 'gate' && (
        <section className="inspector-section inspector-gate" aria-label="人工卡点设置">
          <h3>人工审批设置</h3>
          <div className="field">
            <label htmlFor={`${prefix}-instruction`}>审批提示（instruction）</label>
            <textarea
              id={`${prefix}-instruction`}
              name="instruction"
              rows={5}
              value={node.instruction ?? ''}
              onChange={(event) => onChangeNode(node.id, { instruction: event.target.value })}
            />
          </div>
          <NodeMultiSelect
            controlId={`${prefix}-review-nodes`}
            name="reviewNodes"
            label="重点审阅的上游节点（review_nodes）"
            values={node.review_nodes ?? []}
            options={upstreamNodes}
            onChange={(review_nodes) => onChangeNode(node.id, { review_nodes })}
          />
          <div className="field field--checkbox">
            <input
              id={`${prefix}-allow-edit`}
              name="allowEditOutput"
              type="checkbox"
              checked={node.allow_edit_output ?? true}
              onChange={(event) => onChangeNode(node.id, { allow_edit_output: event.target.checked })}
            />
            <label htmlFor={`${prefix}-allow-edit`}>允许审批人在线修改产出（allow_edit_output）</label>
          </div>
          <NodeMultiSelect
            controlId={`${prefix}-rewind-targets`}
            name="rewindTargets"
            label="允许打回的上游节点（rewind_targets）"
            values={node.rewind_targets ?? []}
            options={upstreamNodes}
            onChange={(rewind_targets) => onChangeNode(node.id, { rewind_targets })}
          />
        </section>
      )}

      {node.kind === 'condition' && (
        <section className="inspector-section inspector-condition" aria-label="条件分支设置">
          <h3>出边与条件分支</h3>
          {outgoingEdges.length === 0 ? (
            <p className="inspector-empty">尚无出边，请在下方新增连线目标。</p>
          ) : (
            <ul className="inspector-edge-list">
              {outgoingEdges.map(({ edge, id }) => (
                <li key={id}>
                  <button
                    className="inspector-edge-link"
                    type="button"
                    onClick={() => onSelect({ type: 'edge', id })}
                  >
                    <span>{edge.label || (edge.is_default ? '默认分支' : '条件分支')}</span>
                    <span>→ {edge.target}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <AddEdgeControl source={node.id} workflow={workflow} onAddEdge={onAddEdge} />

      <section className="inspector-section inspector-danger" aria-label="危险操作">
        <button
          className="inspector-button inspector-button--danger"
          type="button"
          onClick={() => onDeleteNode(node.id)}
        >
          删除节点
        </button>
      </section>
    </>
  )
}

interface EdgeInspectorProps {
  edge: EdgeSpec
  edgeId: string
  workflow: WorkflowSpec
  upstreamMap: Record<string, string[]>
  onChangeEdge: (edgeId: string, patch: Partial<EdgeSpec>) => void
  onDeleteEdge: (edgeId: string) => void
}

function EdgeInspector({
  edge,
  edgeId,
  workflow,
  upstreamMap,
  onChangeEdge,
  onDeleteEdge,
}: EdgeInspectorProps) {
  const prefix = `inspector-edge-${edgeId}`
  const upstreamIds = upstreamMap[edge.source] ?? []
  const condition = edge.condition ?? { field: '', op: '==' as RouteOperator }

  function changeCondition(patch: Partial<EdgeCondition>) {
    onChangeEdge(edgeId, { condition: { ...condition, ...patch } })
  }

  return (
    <>
      <header className="inspector-header">
        <span className="inspector-eyebrow">连线属性</span>
        <h2>{edge.source} → {edge.target ?? '未连接'}</h2>
      </header>
      <section className="inspector-section" aria-label="连线设置">
        <div className="field">
          <label htmlFor={`${prefix}-source`}>来源节点（source）</label>
          <input
            id={`${prefix}-source`}
            name="source"
            value={edge.source}
            readOnly
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
          />
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-target`}>目标节点（target）</label>
          <select
            id={`${prefix}-target`}
            name="target"
            value={edge.target ?? ''}
            onChange={(event) => onChangeEdge(edgeId, { target: event.target.value })}
          >
            <option value="">请选择目标节点</option>
            {nodeOptions(workflow, true)
              .filter((node) => node.id !== edge.source)
              .map((node) => <option key={node.id} value={node.id}>{node.label}（{node.id}）</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-label`}>连线标签（label）</label>
          <input
            id={`${prefix}-label`}
            name="label"
            value={edge.label ?? ''}
            onChange={(event) => onChangeEdge(edgeId, { label: event.target.value })}
          />
        </div>
        <div className="field field--checkbox">
          <input
            id={`${prefix}-default`}
            name="isDefault"
            type="checkbox"
            checked={edge.is_default ?? false}
            onChange={(event) => onChangeEdge(edgeId, { is_default: event.target.checked })}
          />
          <label htmlFor={`${prefix}-default`}>兜底分支（is_default）</label>
        </div>
      </section>

      <section className="inspector-section inspector-condition-fields" aria-label="条件表达式">
        <h3>条件表达式（condition）</h3>
        <div className="field">
          <label htmlFor={`${prefix}-condition-field`}>判断字段（condition.field）</label>
          <select
            id={`${prefix}-condition-field`}
            name="conditionField"
            value={condition.field}
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
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
          <label htmlFor={`${prefix}-condition-op`}>操作符（condition.op）</label>
          <select
            id={`${prefix}-condition-op`}
            name="conditionOp"
            value={condition.op ?? '=='}
            onChange={(event) => changeCondition({ op: event.target.value as RouteOperator })}
          >
            {ROUTE_OPERATORS.map((operator) => <option key={operator} value={operator}>{operator}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-condition-value`}>常量值（condition.value）</label>
          <input
            id={`${prefix}-condition-value`}
            name="conditionValue"
            value={condition.value === undefined || condition.value === null ? '' : String(condition.value)}
            onChange={(event) => changeCondition({ value: event.target.value })}
          />
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-condition-value-ref`}>引用值（condition.value_ref）</label>
          <input
            id={`${prefix}-condition-value-ref`}
            name="conditionValueRef"
            value={condition.value_ref ?? ''}
            placeholder="例如 inputs.expected_status"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            onChange={(event) => changeCondition({ value_ref: event.target.value })}
          />
        </div>
      </section>

      <section className="inspector-section inspector-danger" aria-label="危险操作">
        <button
          className="inspector-button inspector-button--danger"
          type="button"
          onClick={() => onDeleteEdge(edgeId)}
        >
          删除连线
        </button>
      </section>
    </>
  )
}

export default function WorkflowInspector(props: WorkflowInspectorProps) {
  const {
    workflow,
    selection,
    agents,
    workers,
    upstreamMap,
    onChangeNode,
    onChangeEdge,
    onSetEntry,
    onDeleteNode,
    onDeleteEdge,
    onAddEdge,
    onSelect,
  } = props

  let content
  if (!selection) {
    content = (
      <div className="inspector-empty-state">
        <h2>工作流属性</h2>
        <p>选择画布中的节点或连线，在右侧编辑属性。</p>
        <ul className="inspector-instructions">
          <li>拖拽节点可调整位置，拖动手柄可创建连线。</li>
          <li>选中节点后可设置入口、执行参数与变量模板。</li>
          <li>选中条件节点的出边后可配置路由表达式。</li>
          <li>按 Delete / Backspace 可删除当前选中项。</li>
        </ul>
      </div>
    )
  } else if (selection.type === 'node') {
    const node = workflow.nodes.find((candidate) => candidate.id === selection.id)
    content = node ? (
      <NodeInspector
        node={node}
        workflow={workflow}
        agents={agents}
        workers={workers}
        upstreamMap={upstreamMap}
        onChangeNode={onChangeNode}
        onSetEntry={onSetEntry}
        onDeleteNode={onDeleteNode}
        onAddEdge={onAddEdge}
        onSelect={onSelect}
      />
    ) : (
      <div className="inspector-empty-state">
        <h2>节点不存在</h2>
        <p>所选节点可能已被删除。</p>
        <button className="inspector-button" type="button" onClick={() => onSelect(null)}>清除选择</button>
      </div>
    )
  } else {
    const selectedEdge = (workflow.edges ?? [])
      .map((edge, index) => ({ edge, id: directEdgeId(edge, index) }))
      .find(({ id }) => id === selection.id)
    content = selectedEdge ? (
      <EdgeInspector
        edge={selectedEdge.edge}
        edgeId={selectedEdge.id}
        workflow={workflow}
        upstreamMap={upstreamMap}
        onChangeEdge={onChangeEdge}
        onDeleteEdge={onDeleteEdge}
      />
    ) : (
      <div className="inspector-empty-state">
        <h2>连线不存在</h2>
        <p>所选连线可能已被删除。</p>
        <button className="inspector-button" type="button" onClick={() => onSelect(null)}>清除选择</button>
      </div>
    )
  }

  return <aside className="workflow-inspector inspector-panel" aria-label="工作流属性面板">{content}</aside>
}

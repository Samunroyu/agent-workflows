import { useEffect, useId, useRef, useState } from 'react'
import type {
  CSSProperties,
  FormEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from 'react'
import { ChevronDown, LoaderCircle, Pencil, Plus, Sparkles, Trash2, X } from 'lucide-react'
import { api } from '../api'
import type {
  AgentDriver,
  AgentProfileOut,
  AgentProfileSpec,
  CreateWorkflowMode,
  CreateWorkflowRequest,
  StartRunOut,
  WorkflowInputKind,
  WorkflowInputSpec,
  WorkflowSpec,
} from '../types'

const dialogBehavior = { closedby: 'any' } as const
const inputKeyPattern = '[A-Za-z_][A-Za-z0-9_]*'
const idPattern = '[A-Za-z0-9_-]+'

const fieldGridStyle: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
  gap: 13,
}

const listStyle: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 10,
  margin: 0,
  padding: 0,
  listStyle: 'none',
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请稍后重试'
}

function isOutsideDialog(event: ReactPointerEvent<HTMLDialogElement>): boolean {
  const rect = event.currentTarget.getBoundingClientRect()
  return event.clientX < rect.left || event.clientX > rect.right
    || event.clientY < rect.top || event.clientY > rect.bottom
}

export function NativeDialog({
  open,
  onClose,
  labelledBy,
  className,
  children,
  preventClose = false,
}: {
  open: boolean
  onClose: () => void
  labelledBy: string
  className?: string
  children: ReactNode
  preventClose?: boolean
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const pointerDownRef = useRef<number | null>(null)
  const openRef = useRef(open)
  const onCloseRef = useRef(onClose)
  const preventCloseRef = useRef(preventClose)
  openRef.current = open
  onCloseRef.current = onClose
  preventCloseRef.current = preventClose

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  const handleNativeClose = () => {
    const dialog = dialogRef.current
    if (!openRef.current) return
    if (preventCloseRef.current) {
      if (dialog && !dialog.open) dialog.showModal()
      return
    }
    onCloseRef.current()
  }

  const handlePointerDown = (event: ReactPointerEvent<HTMLDialogElement>) => {
    pointerDownRef.current = event.target === event.currentTarget && isOutsideDialog(event)
      ? event.pointerId
      : null
  }

  const handlePointerUp = (event: ReactPointerEvent<HTMLDialogElement>) => {
    if (pointerDownRef.current !== event.pointerId) return
    pointerDownRef.current = null
    if (event.target !== event.currentTarget || !isOutsideDialog(event) || preventClose) return
    onClose()
  }

  return (
    <dialog
      {...dialogBehavior}
      ref={dialogRef}
      className={className}
      aria-labelledby={labelledBy}
      onCancel={(event) => {
        if (preventClose) event.preventDefault()
      }}
      onClose={handleNativeClose}
      onPointerDown={handlePointerDown}
      onPointerUp={handlePointerUp}
    >
      {children}
    </dialog>
  )
}

type RunInputValue = string | number | boolean

function defaultRunValue(spec: WorkflowInputSpec): RunInputValue {
  const kind = spec.kind ?? 'text'
  if (spec.default !== undefined && spec.default !== null) {
    if (kind === 'boolean') {
      if (typeof spec.default === 'boolean') return spec.default
      return String(spec.default).toLowerCase() === 'true'
    }
    if (kind === 'number' && typeof spec.default === 'number') return spec.default
    return String(spec.default)
  }
  return kind === 'boolean' ? false : ''
}

function defaultRunValues(specs: WorkflowInputSpec[]): Record<string, RunInputValue> {
  return Object.fromEntries(specs.map((spec) => [spec.key, defaultRunValue(spec)]))
}

export function NewRunDialog({
  open,
  workflows,
  initialWorkflowId,
  onClose,
  onStarted,
}: {
  open: boolean
  workflows: WorkflowSpec[]
  initialWorkflowId: string
  onClose: () => void
  onStarted: (result: StartRunOut) => void
}) {
  const [workflowId, setWorkflowId] = useState(initialWorkflowId)
  const [values, setValues] = useState<Record<string, RunInputValue>>({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [warnings, setWarnings] = useState<string[]>([])
  const workflow = workflows.find((item) => item.id === workflowId) ?? null
  const inputSpecs = workflow?.inputs?.length
    ? workflow.inputs
    : [{ key: 'task', label: '任务描述', kind: 'textarea' as const, required: true }]

  useEffect(() => {
    if (!open) return
    const nextWorkflow = workflows.find((item) => item.id === initialWorkflowId) ?? workflows[0] ?? null
    const nextId = nextWorkflow?.id ?? ''
    setWorkflowId(nextId)
    setValues(defaultRunValues(nextWorkflow?.inputs?.length
      ? nextWorkflow.inputs
      : [{ key: 'task', label: '任务描述', kind: 'textarea', required: true }]))
    setError('')
    setWarnings([])
    setSubmitting(false)
  }, [open, initialWorkflowId, workflows])

  const requestClose = () => {
    if (!submitting) onClose()
  }

  const changeWorkflow = (nextId: string) => {
    const nextWorkflow = workflows.find((item) => item.id === nextId) ?? null
    setWorkflowId(nextId)
    setValues(defaultRunValues(nextWorkflow?.inputs?.length
      ? nextWorkflow.inputs
      : [{ key: 'task', label: '任务描述', kind: 'textarea', required: true }]))
    setError('')
    setWarnings([])
  }

  const updateValue = (key: string, value: RunInputValue) => {
    setValues((current) => ({ ...current, [key]: value }))
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (submitting) return
    if (!workflow) {
      setError('请先选择工作流')
      return
    }
    setSubmitting(true)
    setError('')
    setWarnings([])
    const task = String(values.task ?? '')
    const inputs: Record<string, unknown> = {}
    inputSpecs.forEach((spec) => {
      const value = values[spec.key]
      if ((spec.kind ?? 'text') === 'number') {
        if (value !== '') inputs[spec.key] = Number(value)
        return
      }
      inputs[spec.key] = value
    })
    inputs.task = task
    try {
      const result = await api.startRun({ workflow_id: workflow.id, task, inputs })
      setWarnings(result.warnings ?? [])
      onStarted(result)
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <NativeDialog
      open={open}
      onClose={requestClose}
      labelledBy="new-run-dialog-title"
      className="run-modal"
      preventClose={submitting}
    >
      <div className="modal-heading">
        <div>
          <div>
            <span className="eyebrow">NEW RUN</span>
            <h2 id="new-run-dialog-title">发起新运行</h2>
          </div>
        </div>
        <button type="button" className="icon-button" aria-label="关闭新运行弹窗" onClick={requestClose} disabled={submitting}>
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      {warnings.length > 0 && (
        <div className="warning-box" role="status" aria-live="polite">
          <div>
            <strong>运行已创建，请注意以下警告</strong>
            <ul>
              {warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
            </ul>
          </div>
        </div>
      )}
      {error && <div className="inline-error" role="alert">{error}</div>}
      <form onSubmit={handleSubmit}>
        <div className="form-field">
          <label htmlFor="new-run-workflow">工作流<em>必填</em></label>
          <div className="form-select">
            <select
              id="new-run-workflow"
              name="workflow_id"
              required
              value={workflowId}
              onChange={(event) => changeWorkflow(event.target.value)}
            >
              {workflows.length === 0 && <option value="">暂无可用工作流</option>}
              {workflows.map((item) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
            </select>
            <ChevronDown size={15} aria-hidden="true" />
          </div>
        </div>
        {inputSpecs.map((spec, index) => {
          const kind = spec.kind ?? 'text'
          const id = `new-run-input-${index}`
          const descriptionId = `${id}-description`
          const describedBy = spec.description ? descriptionId : undefined
          const value = values[spec.key] ?? defaultRunValue(spec)
          return (
            <div className="form-field" key={`${spec.key}-${index}`}>
              <label htmlFor={id}>
                {spec.label || spec.key}
                {spec.required && <em>必填</em>}
              </label>
              {kind === 'textarea' ? (
                <textarea
                  id={id}
                  name={spec.key}
                  required={spec.required}
                  placeholder={spec.placeholder ?? undefined}
                  aria-describedby={describedBy}
                  value={String(value)}
                  onChange={(event) => updateValue(spec.key, event.target.value)}
                />
              ) : kind === 'boolean' ? (
                <input
                  id={id}
                  name={spec.key}
                  type="checkbox"
                  aria-required={spec.required}
                  aria-describedby={describedBy}
                  checked={value === true}
                  onChange={(event) => updateValue(spec.key, event.target.checked)}
                />
              ) : kind === 'select' ? (
                <div className="form-select">
                  <select
                    id={id}
                    name={spec.key}
                    required={spec.required}
                    aria-describedby={describedBy}
                    value={String(value)}
                    onChange={(event) => updateValue(spec.key, event.target.value)}
                  >
                    <option value="">请选择…</option>
                    {(spec.options ?? []).map((option, optionIndex) => (
                      <option key={`${option}-${optionIndex}`} value={option}>{option}</option>
                    ))}
                  </select>
                  <ChevronDown size={15} aria-hidden="true" />
                </div>
              ) : (
                <input
                  id={id}
                  name={spec.key}
                  type={kind === 'number' ? 'number' : 'text'}
                  inputMode={kind === 'path' ? 'url' : undefined}
                  required={spec.required}
                  placeholder={spec.placeholder ?? undefined}
                  aria-describedby={describedBy}
                  autoCorrect={kind === 'path' ? 'off' : undefined}
                  autoCapitalize={kind === 'path' ? 'off' : undefined}
                  spellCheck={kind === 'path' ? false : undefined}
                  value={String(value)}
                  onChange={(event) => updateValue(spec.key, event.target.value)}
                />
              )}
              {spec.description && <small id={descriptionId}>{spec.description}</small>}
            </div>
          )
        })}
        {submitting && <span role="status" aria-live="polite">正在创建运行…</span>}
        <div className="modal-actions">
          <button type="button" className="modal-cancel" onClick={requestClose} disabled={submitting}>取消</button>
          <button type="submit" className="modal-submit" disabled={submitting}>
            {submitting && <LoaderCircle size={15} className="animate-spin" aria-hidden="true" />}
            {submitting ? '创建中…' : '创建并运行'}
          </button>
        </div>
      </form>
    </NativeDialog>
  )
}

function inputDefaultText(spec: WorkflowInputSpec): string {
  return spec.default === undefined || spec.default === null ? '' : String(spec.default)
}

export function WorkflowSettingsDialog({
  open,
  workflow,
  onClose,
  onSave,
}: {
  open: boolean
  workflow: WorkflowSpec | null
  onClose: () => void
  onSave: (workflow: WorkflowSpec) => Promise<void>
}) {
  const [draft, setDraft] = useState<WorkflowSpec | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) return
    setDraft(workflow ? {
      ...workflow,
      inputs: workflow.inputs?.map((spec) => ({
        ...spec,
        options: spec.options ? [...spec.options] : undefined,
      })),
    } : null)
    setError('')
    setSubmitting(false)
  }, [open, workflow])

  const requestClose = () => {
    if (!submitting) onClose()
  }

  const updateInput = (index: number, patch: Partial<WorkflowInputSpec>) => {
    setError('')
    setDraft((current) => current ? {
      ...current,
      inputs: (current.inputs ?? []).map((spec, itemIndex) => itemIndex === index ? { ...spec, ...patch } : spec),
    } : current)
  }

  const changeInputKind = (index: number, kind: WorkflowInputKind) => {
    const current = draft?.inputs?.[index]
    if (!current) return
    let value: unknown = current.default
    if (kind === 'boolean') value = current.default === true || current.default === 'true'
    if (kind !== 'boolean' && typeof value === 'boolean') value = String(value)
    updateInput(index, { kind, default: value })
  }

  const addInput = () => {
    setDraft((current) => {
      if (!current) return current
      const inputs = current.inputs ?? []
      let suffix = inputs.length + 1
      while (inputs.some((spec) => spec.key === `field_${suffix}`)) suffix += 1
      return {
        ...current,
        inputs: [...inputs, {
          key: `field_${suffix}`,
          label: '新字段',
          kind: 'text',
          required: false,
          default: '',
          placeholder: '',
          options: [],
        }],
      }
    })
  }

  const removeInput = (index: number) => {
    setDraft((current) => current ? {
      ...current,
      inputs: (current.inputs ?? []).filter((_, itemIndex) => itemIndex !== index),
    } : current)
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!draft || submitting) return
    const inputs = (draft.inputs ?? []).map((spec) => ({
      ...spec,
      key: spec.key.trim(),
      label: spec.label.trim(),
      default: (spec.kind ?? 'text') === 'number' && spec.default !== '' && spec.default !== undefined
        ? Number(spec.default)
        : spec.default,
      placeholder: spec.placeholder?.trim() || null,
      options: spec.options?.map((option) => option.trim()).filter(Boolean),
    }))
    const seenKeys = new Set<string>()
    for (const [index, spec] of inputs.entries()) {
      if (!spec.key) {
        setError(`字段 ${index + 1} 的 key 不能为空`)
        return
      }
      if (seenKeys.has(spec.key)) {
        setError(`字段 key「${spec.key}」重复，请确保每个 key 唯一`)
        return
      }
      seenKeys.add(spec.key)
    }
    setSubmitting(true)
    setError('')
    try {
      await onSave({
        ...draft,
        id: draft.id.trim(),
        name: draft.name.trim(),
        description: draft.description?.trim() ?? '',
        inputs,
      })
      onClose()
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <NativeDialog open={open} onClose={requestClose} labelledBy="workflow-settings-title" className="run-modal" preventClose={submitting}>
      <div className="modal-heading">
        <div>
          <div>
            <span className="eyebrow">WORKFLOW</span>
            <h2 id="workflow-settings-title">工作流设置</h2>
          </div>
        </div>
        <button type="button" className="icon-button" aria-label="关闭工作流设置" onClick={requestClose} disabled={submitting}>
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      {draft && (
        <form onSubmit={(event) => void handleSubmit(event)}>
          <div style={fieldGridStyle}>
            <div className="form-field">
              <label htmlFor="workflow-settings-id">工作流 ID</label>
              <input
                id="workflow-settings-id"
                name="workflow_id"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                readOnly
                value={draft.id}
              />
            </div>
            <div className="form-field">
              <label htmlFor="workflow-settings-name">名称<em>必填</em></label>
              <input
                id="workflow-settings-name"
                name="workflow_name"
                required
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              />
            </div>
          </div>
          <div className="form-field">
            <label htmlFor="workflow-settings-description">描述</label>
            <textarea
              id="workflow-settings-description"
              name="workflow_description"
              value={draft.description ?? ''}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
            />
          </div>
          <fieldset>
            <legend>启动入参</legend>
            {(draft.inputs ?? []).map((spec, index) => {
              const kind = spec.kind ?? 'text'
              const prefix = `workflow-input-${index}`
              return (
                <fieldset key={index}>
                  <legend>字段 {index + 1}</legend>
                  <div style={fieldGridStyle}>
                    <div className="form-field">
                      <label htmlFor={`${prefix}-key`}>字段 key<em>必填</em></label>
                      <input
                        id={`${prefix}-key`}
                        name={`inputs.${index}.key`}
                        required
                        pattern={inputKeyPattern}
                        title="以字母或下划线开头，仅使用字母、数字和下划线"
                        autoCorrect="off"
                        autoCapitalize="off"
                        spellCheck={false}
                        value={spec.key}
                        onChange={(event) => updateInput(index, { key: event.target.value })}
                      />
                    </div>
                    <div className="form-field">
                      <label htmlFor={`${prefix}-label`}>显示名称<em>必填</em></label>
                      <input
                        id={`${prefix}-label`}
                        name={`inputs.${index}.label`}
                        required
                        value={spec.label}
                        onChange={(event) => updateInput(index, { label: event.target.value })}
                      />
                    </div>
                    <div className="form-field">
                      <label htmlFor={`${prefix}-kind`}>字段类型</label>
                      <div className="form-select">
                        <select
                          id={`${prefix}-kind`}
                          name={`inputs.${index}.kind`}
                          value={kind}
                          onChange={(event) => changeInputKind(index, event.target.value as WorkflowInputKind)}
                        >
                          <option value="textarea">多行文本</option>
                          <option value="text">单行文本</option>
                          <option value="path">路径</option>
                          <option value="number">数字</option>
                          <option value="boolean">布尔值</option>
                          <option value="select">单选</option>
                        </select>
                        <ChevronDown size={15} aria-hidden="true" />
                      </div>
                    </div>
                    <div className="form-field">
                      <label htmlFor={`${prefix}-required`}>必填</label>
                      <input
                        id={`${prefix}-required`}
                        name={`inputs.${index}.required`}
                        type="checkbox"
                        checked={spec.required ?? false}
                        onChange={(event) => updateInput(index, { required: event.target.checked })}
                      />
                    </div>
                    <div className="form-field">
                      <label htmlFor={`${prefix}-default`}>默认值</label>
                      {kind === 'boolean' ? (
                        <input
                          id={`${prefix}-default`}
                          name={`inputs.${index}.default`}
                          type="checkbox"
                          checked={spec.default === true}
                          onChange={(event) => updateInput(index, { default: event.target.checked })}
                        />
                      ) : (
                        <input
                          id={`${prefix}-default`}
                          name={`inputs.${index}.default`}
                          type={kind === 'number' ? 'number' : 'text'}
                          autoCorrect={kind === 'path' ? 'off' : undefined}
                          autoCapitalize={kind === 'path' ? 'off' : undefined}
                          spellCheck={kind === 'path' ? false : undefined}
                          value={inputDefaultText(spec)}
                          onChange={(event) => updateInput(index, { default: event.target.value })}
                        />
                      )}
                    </div>
                    <div className="form-field">
                      <label htmlFor={`${prefix}-placeholder`}>占位提示</label>
                      <input
                        id={`${prefix}-placeholder`}
                        name={`inputs.${index}.placeholder`}
                        value={spec.placeholder ?? ''}
                        onChange={(event) => updateInput(index, { placeholder: event.target.value })}
                      />
                    </div>
                  </div>
                  {kind === 'select' && (
                    <div className="form-field">
                      <label htmlFor={`${prefix}-options`}>选项（每行一项）</label>
                      <textarea
                        id={`${prefix}-options`}
                        name={`inputs.${index}.options`}
                        value={(spec.options ?? []).join('\n')}
                        onChange={(event) => updateInput(index, {
                          options: event.target.value.split('\n'),
                        })}
                      />
                    </div>
                  )}
                  <button type="button" className="soft-button" onClick={() => removeInput(index)}>
                    <Trash2 size={14} aria-hidden="true" />删除字段
                  </button>
                </fieldset>
              )
            })}
            <button type="button" className="soft-button" onClick={addInput}>
              <Plus size={14} aria-hidden="true" />新增字段
            </button>
          </fieldset>
          {submitting && <span role="status" aria-live="polite">正在保存工作流设置到后端…</span>}
          <div className="modal-actions">
            <button type="button" className="modal-cancel" onClick={requestClose} disabled={submitting}>取消</button>
            <button type="submit" className="modal-submit" disabled={submitting}>
              {submitting && <LoaderCircle size={15} className="animate-spin" aria-hidden="true" />}
              {submitting ? '保存中…' : '保存设置'}
            </button>
          </div>
        </form>
      )}
    </NativeDialog>
  )
}

const CREATE_MODES: Array<{ value: CreateWorkflowMode; label: string; hint: string }> = [
  { value: 'blank', label: '空白画布', hint: '含 1 个默认起始节点，进去自由拖拽增删' },
  { value: 'ai', label: '一句话 AI 生成', hint: '描述想要的流程，由本机 CLI Agent 自动构图' },
  { value: 'template', label: '复制现有模板', hint: '从已有工作流克隆一份再改' },
]

export function CreateWorkflowDialog({
  open,
  workflows,
  agents,
  onClose,
  onCreated,
}: {
  open: boolean
  workflows: WorkflowSpec[]
  agents: AgentProfileOut[]
  onClose: () => void
  onCreated: (workflow: WorkflowSpec) => void
}) {
  const [name, setName] = useState('')
  const [mode, setMode] = useState<CreateWorkflowMode>('blank')
  const [description, setDescription] = useState('')
  const [customId, setCustomId] = useState('')
  const [aiInstruction, setAiInstruction] = useState('')
  const [agentId, setAgentId] = useState('auto')
  const [templateId, setTemplateId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setName('')
    setMode('blank')
    setDescription('')
    setCustomId('')
    setAiInstruction('')
    setAgentId('auto')
    setTemplateId(workflows[0]?.id ?? '')
    setSubmitting(false)
    setError('')
  }, [open, workflows])

  const requestClose = () => {
    if (!submitting) onClose()
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (submitting) return
    if (mode === 'ai' && aiInstruction.trim().length < 2) {
      setError('请用至少 2 个字符描述要生成的流程')
      return
    }
    if (mode === 'template' && !templateId) {
      setError('请选择要复制的模板工作流')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const input: CreateWorkflowRequest = {
        name: name.trim(),
        id: customId.trim() || null,
        description: description.trim(),
        mode,
        template_id: mode === 'template' ? templateId : null,
        ai_instruction: mode === 'ai' ? aiInstruction.trim() : null,
        agent_id: mode === 'ai' ? agentId : 'auto',
      }
      const created = await api.createWorkflow(input)
      onCreated(created)
      onClose()
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <NativeDialog
      open={open}
      onClose={requestClose}
      labelledBy="create-workflow-title"
      className="run-modal"
      preventClose={submitting}
    >
      <div className="modal-heading">
        <div>
          <div>
            <span className="eyebrow">NEW WORKFLOW</span>
            <h2 id="create-workflow-title">新建工作流</h2>
          </div>
        </div>
        <button type="button" className="icon-button" aria-label="关闭新建工作流弹窗" onClick={requestClose} disabled={submitting}>
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      <form onSubmit={(event) => void handleSubmit(event)}>
        <div className="form-field">
          <label htmlFor="create-workflow-name">工作流名称<em>必填</em></label>
          <input
            id="create-workflow-name"
            name="name"
            required
            autoFocus
            placeholder="如：前后端并行验收流"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <fieldset className="create-mode-fieldset">
          <legend>创建起点</legend>
          {CREATE_MODES.map((item) => (
            <label key={item.value} className={`create-mode-option ${mode === item.value ? 'is-active' : ''}`}>
              <input
                type="radio"
                name="mode"
                value={item.value}
                checked={mode === item.value}
                onChange={() => setMode(item.value)}
              />
              <span>
                <strong>{item.label}</strong>
                <small>{item.hint}</small>
              </span>
            </label>
          ))}
        </fieldset>
        {mode === 'ai' && (
          <>
            <div className="form-field">
              <label htmlFor="create-workflow-ai-instruction">一句话描述流程<em>必填</em></label>
              <textarea
                id="create-workflow-ai-instruction"
                name="ai_instruction"
                required
                minLength={2}
                placeholder="如：先让 Codex 写方案并人工审批，通过后前后端并行开发再跑测试"
                value={aiInstruction}
                onChange={(event) => setAiInstruction(event.target.value)}
              />
            </div>
            <div className="form-field">
              <label htmlFor="create-workflow-agent">构图 CLI Agent</label>
              <div className="form-select">
                <select
                  id="create-workflow-agent"
                  name="agent_id"
                  value={agentId}
                  onChange={(event) => setAgentId(event.target.value)}
                >
                  <option value="auto">auto · 自动选择在线 Agent</option>
                  {agents.map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}（{agent.id}）{agent.available ? '' : ' · 未安装'}
                    </option>
                  ))}
                </select>
                <ChevronDown size={15} aria-hidden="true" />
              </div>
            </div>
          </>
        )}
        {mode === 'template' && (
          <div className="form-field">
            <label htmlFor="create-workflow-template">选择要复制的模板<em>必填</em></label>
            <div className="form-select">
              <select
                id="create-workflow-template"
                name="template_id"
                required
                value={templateId}
                onChange={(event) => setTemplateId(event.target.value)}
              >
                {workflows.length === 0 && <option value="">暂无可用模板</option>}
                {workflows.map((item) => (
                  <option key={item.id} value={item.id}>{item.name}（{item.id}）</option>
                ))}
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </div>
          </div>
        )}
        <div className="form-field">
          <label htmlFor="create-workflow-description">工作流描述（选填）</label>
          <textarea
            id="create-workflow-description"
            name="description"
            placeholder="这个工作流用来做什么？"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>
        <details className="advanced-details">
          <summary>高级：自定义工作流 ID（选填，留空自动生成）</summary>
          <div className="form-field">
            <label htmlFor="create-workflow-id">自定义工作流 ID</label>
            <input
              id="create-workflow-id"
              name="id"
              pattern={idPattern}
              title="仅限字母、数字、连字符 - 或下划线 _"
              placeholder="留空由后端自动生成唯一 ID"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              value={customId}
              onChange={(event) => setCustomId(event.target.value)}
            />
          </div>
        </details>
        {submitting && <span role="status" aria-live="polite">正在创建并落盘保存工作流…</span>}
        <div className="modal-actions">
          <button type="button" className="modal-cancel" onClick={requestClose} disabled={submitting}>取消</button>
          <button type="submit" className="modal-submit" disabled={submitting}>
            {submitting
              ? <LoaderCircle size={15} className="animate-spin" aria-hidden="true" />
              : mode === 'ai' ? <Sparkles size={15} aria-hidden="true" /> : <Plus size={15} aria-hidden="true" />}
            {submitting ? '创建中…' : '立即创建'}
          </button>
        </div>
      </form>
    </NativeDialog>
  )
}

interface AgentFormState {
  id: string
  name: string
  driver: AgentDriver
  binary: string
  model: string
  description: string
  argsTemplate: string
  commandTemplate: string
  timeoutSec: string
  env: string
}

function emptyAgentForm(): AgentFormState {
  return {
    id: '',
    name: '',
    driver: 'custom',
    binary: '',
    model: '',
    description: '',
    argsTemplate: '',
    commandTemplate: '',
    timeoutSec: '',
    env: '',
  }
}

function agentFormValue(agent: AgentProfileOut): AgentFormState {
  return {
    id: agent.id,
    name: agent.name,
    driver: agent.driver ?? 'custom',
    binary: agent.binary,
    model: agent.model ?? '',
    description: agent.description ?? '',
    argsTemplate: (agent.args_template ?? []).join('\n'),
    commandTemplate: agent.command_template ?? '',
    timeoutSec: agent.timeout_sec === undefined ? '' : String(agent.timeout_sec),
    env: Object.entries(agent.env ?? {}).map(([key, value]) => `${key}=${value}`).join('\n'),
  }
}

function parseEnvironment(source: string): Record<string, string> {
  const env: Record<string, string> = {}
  source.split('\n').forEach((rawLine, index) => {
    const line = rawLine.trim()
    if (!line) return
    const separator = line.indexOf('=')
    const key = separator >= 0 ? line.slice(0, separator).trim() : ''
    if (!key) throw new Error(`环境变量第 ${index + 1} 行必须使用 KEY=VALUE 格式`)
    env[key] = line.slice(separator + 1)
  })
  return env
}

export function AgentManagerDialog({
  open,
  agents,
  onClose,
  onChanged,
  onToast,
}: {
  open: boolean
  agents: AgentProfileOut[]
  onClose: () => void
  onChanged: () => Promise<void> | void
  onToast: (message: string) => void
}) {
  const [form, setForm] = useState<AgentFormState | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const pending = saving || deletingId !== null

  useEffect(() => {
    if (!open) return
    setForm(null)
    setEditingId(null)
    setError('')
    setSaving(false)
    setDeletingId(null)
  }, [open])

  const requestClose = () => {
    if (!pending) onClose()
  }

  const startNew = () => {
    setForm(emptyAgentForm())
    setEditingId(null)
    setError('')
  }

  const startEdit = (agent: AgentProfileOut) => {
    setForm(agentFormValue(agent))
    setEditingId(agent.id)
    setError('')
  }

  const updateForm = (patch: Partial<AgentFormState>) => {
    setForm((current) => current ? { ...current, ...patch } : current)
  }

  const handleSave = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!form || saving) return
    setSaving(true)
    setError('')
    try {
      const input: AgentProfileSpec = {
        id: form.id.trim(),
        name: form.name.trim(),
        driver: form.driver,
        binary: form.binary.trim(),
        model: form.model.trim() || null,
        description: form.description.trim() || undefined,
        args_template: form.argsTemplate.split('\n').map((item) => item.trim()).filter(Boolean),
        command_template: form.commandTemplate.trim() || null,
        timeout_sec: form.timeoutSec === '' ? undefined : Number(form.timeoutSec),
        env: parseEnvironment(form.env),
      }
      await api.saveAgent(input)
      await onChanged()
      onToast(`Agent「${input.name}」已保存`)
      setForm(null)
      setEditingId(null)
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (agent: AgentProfileOut) => {
    if (agent.builtin || deletingId) return
    setDeletingId(agent.id)
    setError('')
    try {
      await api.deleteAgent(agent.id)
      await onChanged()
      onToast(`Agent「${agent.name}」已删除`)
      if (editingId === agent.id) {
        setForm(null)
        setEditingId(null)
      }
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <NativeDialog
      open={open}
      onClose={requestClose}
      labelledBy="agent-manager-title"
      className="run-modal"
      preventClose={pending}
    >
      <div className="modal-heading">
        <div>
          <div>
            <span className="eyebrow">CLI AGENTS</span>
            <h2 id="agent-manager-title">Agent 管理</h2>
          </div>
        </div>
        <button type="button" className="icon-button" aria-label="关闭 Agent 管理" onClick={requestClose} disabled={pending}>
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      <ul style={listStyle}>
        {agents.map((agent) => (
          <li key={agent.id}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <strong>{agent.name}</strong>
              <code>{agent.id}</code>
              <span role="status">{agent.available ? '可用' : '不可用'}</span>
              <span className="status-badge status-badge--subtle">{agent.builtin ? '内置' : '自定义'}</span>
            </div>
            <small>{agent.resolved_path || '未探测到二进制路径'}</small>
            <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
              <button type="button" className="soft-button" onClick={() => startEdit(agent)} disabled={pending}>
                <Pencil size={13} aria-hidden="true" />编辑
              </button>
              {!agent.builtin && (
                <button type="button" className="soft-button" onClick={() => void handleDelete(agent)} disabled={pending}>
                  {deletingId === agent.id
                    ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" />
                    : <Trash2 size={13} aria-hidden="true" />}
                  {deletingId === agent.id ? '删除中…' : '删除'}
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
      {!form && (
        <button type="button" className="soft-button" onClick={startNew} disabled={pending} style={{ marginTop: 14 }}>
          <Plus size={14} aria-hidden="true" />新增 Agent
        </button>
      )}
      {form && (
        <form onSubmit={handleSave} style={{ marginTop: 18 }}>
          <h3>{editingId ? `编辑 ${editingId}` : '新增 Agent'}</h3>
          <div style={fieldGridStyle}>
            <div className="form-field">
              <label htmlFor="agent-id">Agent ID<em>必填</em></label>
              <input
                id="agent-id"
                name="agent_id"
                required
                pattern={idPattern}
                title="仅使用字母、数字、下划线和连字符"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                readOnly={editingId !== null}
                value={form.id}
                onChange={(event) => updateForm({ id: event.target.value })}
              />
            </div>
            <div className="form-field">
              <label htmlFor="agent-name">名称<em>必填</em></label>
              <input
                id="agent-name"
                name="agent_name"
                required
                value={form.name}
                onChange={(event) => updateForm({ name: event.target.value })}
              />
            </div>
            <div className="form-field">
              <label htmlFor="agent-driver">驱动</label>
              <div className="form-select">
                <select
                  id="agent-driver"
                  name="driver"
                  value={form.driver}
                  onChange={(event) => updateForm({ driver: event.target.value as AgentDriver })}
                >
                  <option value="custom">custom</option>
                  <option value="codex">codex</option>
                  <option value="claude">claude</option>
                  <option value="kimi">kimi</option>
                  <option value="gemini">gemini</option>
                </select>
                <ChevronDown size={15} aria-hidden="true" />
              </div>
            </div>
            <div className="form-field">
              <label htmlFor="agent-binary">二进制名或路径<em>必填</em></label>
              <input
                id="agent-binary"
                name="binary"
                required
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                value={form.binary}
                onChange={(event) => updateForm({ binary: event.target.value })}
              />
            </div>
            <div className="form-field">
              <label htmlFor="agent-model">模型</label>
              <input
                id="agent-model"
                name="model"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                value={form.model}
                onChange={(event) => updateForm({ model: event.target.value })}
              />
            </div>
            <div className="form-field">
              <label htmlFor="agent-timeout">超时时间（秒）</label>
              <input
                id="agent-timeout"
                name="timeout_sec"
                type="number"
                min="10"
                max="14400"
                step="1"
                value={form.timeoutSec}
                onChange={(event) => updateForm({ timeoutSec: event.target.value })}
              />
            </div>
          </div>
          <div className="form-field">
            <label htmlFor="agent-description">描述</label>
            <textarea
              id="agent-description"
              name="description"
              value={form.description}
              onChange={(event) => updateForm({ description: event.target.value })}
            />
          </div>
          <div className="form-field">
            <label htmlFor="agent-args-template">参数模板（每行一项）</label>
            <textarea
              id="agent-args-template"
              name="args_template"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              value={form.argsTemplate}
              onChange={(event) => updateForm({ argsTemplate: event.target.value })}
            />
          </div>
          <div className="form-field">
            <label htmlFor="agent-command-template">命令模板</label>
            <textarea
              id="agent-command-template"
              name="command_template"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              value={form.commandTemplate}
              onChange={(event) => updateForm({ commandTemplate: event.target.value })}
            />
          </div>
          <div className="form-field">
            <label htmlFor="agent-env">环境变量（每行 KEY=VALUE）</label>
            <textarea
              id="agent-env"
              name="env"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              value={form.env}
              onChange={(event) => updateForm({ env: event.target.value })}
            />
          </div>
          {(saving || deletingId) && <span role="status" aria-live="polite">{saving ? '正在保存 Agent…' : '正在删除 Agent…'}</span>}
          <div className="modal-actions">
            <button type="button" className="modal-cancel" onClick={() => setForm(null)} disabled={saving}>收起表单</button>
            <button type="submit" className="modal-submit" disabled={saving}>
              {saving && <LoaderCircle size={15} className="animate-spin" aria-hidden="true" />}
              {saving ? '保存中…' : '保存 Agent'}
            </button>
          </div>
        </form>
      )}
      <div className="modal-actions">
        <button type="button" className="modal-cancel" onClick={requestClose} disabled={pending}>关闭</button>
      </div>
    </NativeDialog>
  )
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  pending = false,
  onClose,
  onConfirm,
}: {
  open: boolean
  title: string
  description: string
  confirmLabel: string
  pending?: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const requestClose = () => {
    if (!pending) onClose()
  }

  return (
    <NativeDialog open={open} onClose={requestClose} labelledBy={titleId} className="run-modal" preventClose={pending}>
      <div className="modal-heading">
        <div>
          <div>
            <span className="eyebrow">CONFIRM</span>
            <h2 id={titleId}>{title}</h2>
          </div>
        </div>
        <button type="button" className="icon-button" aria-label="关闭确认弹窗" onClick={requestClose} disabled={pending}>
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      <p>{description}</p>
      {pending && <span role="status" aria-live="polite">正在处理…</span>}
      <div className="modal-actions">
        <button type="button" className="modal-cancel" onClick={requestClose} disabled={pending}>取消</button>
        <button type="button" className="modal-submit" onClick={onConfirm} disabled={pending}>
          {pending && <LoaderCircle size={15} className="animate-spin" aria-hidden="true" />}
          {confirmLabel}
        </button>
      </div>
    </NativeDialog>
  )
}

import { useEffect, useId, useState } from 'react'
import { Trash2, X } from 'lucide-react'
import type {
  EdgeCondition,
  EdgeSpec,
  RouteOperator,
  WorkflowSpec,
} from '../types'
import { NativeDialog } from './Dialogs'

const ROUTE_OPERATORS: RouteOperator[] = [
  '==', '!=', '<', '<=', '>', '>=',
  'contains', 'not_contains', 'regex', 'truthy', 'falsy',
]

const CONDITION_OUTPUTS = ['ok', 'exit_code', 'runs', 'output'] as const

export interface EdgeEditorDialogProps {
  edgeId: string | null
  workflow: WorkflowSpec
  upstreamMap: Record<string, string[]>
  onChangeEdge: (edgeId: string, patch: Partial<EdgeSpec>) => void
  onDeleteEdge: (edgeId: string) => void
  onClose: () => void
}

export default function EdgeEditorDialog({
  edgeId,
  workflow,
  upstreamMap,
  onChangeEdge,
  onDeleteEdge,
  onClose,
}: EdgeEditorDialogProps) {
  const [advancedOn, setAdvancedOn] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const dialogTitleId = useId()

  const found = (workflow.edges ?? [])
    .map((edge, index) => ({ edge, id: edge.id ?? `edge_${index}_${edge.source}_${edge.target}` }))
    .find(({ id }) => id === edgeId) ?? null
  const edge = found?.edge ?? null
  const sourceNode = edge ? workflow.nodes.find((node) => node.id === edge.source) ?? null : null
  const isConditionSource = sourceNode?.kind === 'condition'
  const upstreamIds = edge ? upstreamMap[edge.source] ?? [] : []

  useEffect(() => {
    setAdvancedOn(false)
    setConfirmingDelete(false)
  }, [edgeId])

  useEffect(() => {
    if (edgeId && !edge) onClose()
  }, [edgeId, edge, onClose])

  if (!edge || !edgeId) {
    return <NativeDialog open={false} onClose={onClose} labelledBy={dialogTitleId}><span className="visually-hidden">连线编辑</span></NativeDialog>
  }

  const prefix = `edge-editor-${edgeId}`
  const condition = edge.condition ?? { field: '', op: '==' as RouteOperator, value: '' }
  const showCondition = isConditionSource ? !(edge.is_default ?? false) : advancedOn || Boolean(edge.condition?.field)
  const changeCondition = (patch: Partial<EdgeCondition>) => onChangeEdge(edgeId, { condition: { ...condition, ...patch } })

  const handleClose = () => {
    if (!isConditionSource && edge.condition && !String(edge.condition.field ?? '').trim()) {
      onChangeEdge(edgeId, { condition: null })
    }
    onClose()
  }

  const targetOptions = [
    ...workflow.nodes.filter((node) => node.id !== edge.source).map((node) => ({ id: node.id, label: node.label })),
    { id: 'END', label: 'END · 结束' },
  ]

  return (
    <NativeDialog open onClose={handleClose} labelledBy={dialogTitleId} className="workbench-dialog workbench-dialog--edge">
      <header className="workbench-header">
        <span className="workbench-kind flow-node-icon--condition">连线</span>
        <h2 id={dialogTitleId}>{edge.source} → {edge.target ?? '未连接'}</h2>
        <div className="workbench-actions">
          <button
            type="button"
            className={`soft-button danger-button ${confirmingDelete ? 'is-confirming' : ''}`}
            onClick={() => {
              if (!confirmingDelete) {
                setConfirmingDelete(true)
                return
              }
              onDeleteEdge(edgeId)
              onClose()
            }}
          >
            <Trash2 size={13} aria-hidden="true" />{confirmingDelete ? '再次点击确认删除' : '删除连线'}
          </button>
          <button type="button" className="icon-button" aria-label="完成并关闭连线编辑" title="完成关闭" onClick={handleClose}>
            <X size={17} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="workbench-body">
        <section className="workbench-core" aria-label="连线属性">
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
              {targetOptions.map((option) => (
                <option key={option.id} value={option.id}>{option.label}（{option.id}）</option>
              ))}
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
          {isConditionSource && (
            <div className="field field--checkbox">
              <input
                id={`${prefix}-default`}
                name="is_default"
                type="checkbox"
                checked={edge.is_default ?? false}
                onChange={(event) => onChangeEdge(edgeId, event.target.checked
                  ? { is_default: true, condition: null }
                  : { is_default: false, condition: { field: '', op: '==', value: '' } })}
              />
              <label htmlFor={`${prefix}-default`}>兜底分支（is_default，勾选后不按条件路由）</label>
            </div>
          )}
          {!isConditionSource && (
            <div className="field field--checkbox">
              <input
                id={`${prefix}-advanced`}
                name="advanced_condition"
                type="checkbox"
                checked={advancedOn}
                onChange={(event) => {
                  const enabled = event.target.checked
                  setAdvancedOn(enabled)
                  if (!enabled) onChangeEdge(edgeId, { condition: null })
                  if (enabled && !edge.condition) onChangeEdge(edgeId, { condition: { field: '', op: '==', value: '' } })
                }}
              />
              <label htmlFor={`${prefix}-advanced`}>高级：按条件路由此连线</label>
            </div>
          )}
        </section>

        {showCondition && (
          <section className="workbench-core" aria-label="条件表达式">
            <h3 className="workbench-section-title">条件表达式（condition）</h3>
            <div className="field">
              <label htmlFor={`${prefix}-condition-field`}>判断字段（condition.field）</label>
              <select
                id={`${prefix}-condition-field`}
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
              <label htmlFor={`${prefix}-condition-op`}>运算符（condition.op）</label>
              <select
                id={`${prefix}-condition-op`}
                name="condition_op"
                value={condition.op ?? '=='}
                onChange={(event) => changeCondition({ op: event.target.value as RouteOperator })}
              >
                {ROUTE_OPERATORS.map((operator) => <option key={operator} value={operator}>{operator}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor={`${prefix}-condition-value`}>比较常量值（condition.value）</label>
              <input
                id={`${prefix}-condition-value`}
                name="condition_value"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                value={condition.value === undefined || condition.value === null ? '' : String(condition.value)}
                onChange={(event) => changeCondition({ value: event.target.value })}
              />
            </div>
            <div className="field">
              <label htmlFor={`${prefix}-condition-value-ref`}>引用值（condition.value_ref）</label>
              <input
                id={`${prefix}-condition-value-ref`}
                name="condition_value_ref"
                placeholder="例如 inputs.expected_status"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                value={condition.value_ref ?? ''}
                onChange={(event) => changeCondition({ value_ref: event.target.value })}
              />
            </div>
          </section>
        )}
        {!isConditionSource && !showCondition && (
          <p className="field-hint">普通边与并行边的 condition 保持为 null；需要按条件路由时再打开上方高级开关。</p>
        )}
      </div>
    </NativeDialog>
  )
}

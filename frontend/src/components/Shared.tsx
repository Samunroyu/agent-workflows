import { ChevronRight, LoaderCircle, Square } from 'lucide-react'
import type { CSSProperties } from 'react'
import type { RunStateOut, RunStatus } from '../types'
import { formatRelativeTime, runTask } from '../format'

export const statusMeta: Record<RunStatus, { label: string; color: string; bg: string }> = {
  waiting: { label: '待审批', color: '#92501d', bg: '#fff2dd' },
  running: { label: '执行中', color: '#466498', bg: '#edf3ff' },
  done: { label: '已完成', color: '#2f6d58', bg: '#eaf6f0' },
  error: { label: '出错', color: '#a74646', bg: '#fff0ef' },
  cancelled: { label: '已强杀', color: '#8b4c4c', bg: '#f9eeee' },
  interrupted: { label: '已中断', color: '#626262', bg: '#f1f1f1' },
  unknown: { label: '状态未知', color: '#626262', bg: '#f1f1f1' },
}

export function StatusBadge({ status, subtle = false }: { status: RunStatus; subtle?: boolean }) {
  const meta = statusMeta[status] ?? statusMeta.unknown
  const badgeStyle = { '--badge-color': meta.color, '--badge-bg': meta.bg } as CSSProperties
  return <span className={`status-badge ${subtle ? 'status-badge--subtle' : ''}`} style={badgeStyle}>
    {status === 'running' ? <LoaderCircle size={11} className="animate-spin" aria-hidden="true" /> : <i aria-hidden="true" />}
    {meta.label}
  </span>
}

export function RunRow({ run, selected, onClick, onCancel, cancelPending = false }: {
  run: RunStateOut
  selected: boolean
  onClick: () => void
  onCancel?: () => void
  cancelPending?: boolean
}) {
  return <article className={`run-row-shell ${selected ? 'is-selected' : ''}`}>
    <button type="button" className="run-row run-row--b" aria-pressed={selected} onClick={onClick}>
      <span className="run-row__top"><StatusBadge status={run.status} subtle /><small>{formatRelativeTime(run.created_at)}</small></span>
      <strong>{runTask(run)}</strong>
      <span className="run-row__meta"><span>{run.workflow_id}</span><span>·</span><span>{run.thread_id.slice(0, 14)}</span></span>
      <ChevronRight className="run-row__arrow" size={15} aria-hidden="true" />
    </button>
    {run.status === 'running' && onCancel && <button type="button" className="run-row-cancel" aria-label={`强杀运行 ${runTask(run)}`} disabled={cancelPending} onClick={onCancel}>{cancelPending ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Square size={13} aria-hidden="true" />}<span>强杀</span></button>}
  </article>
}

export function Avatar({ label = 'W' }: { label?: string }) {
  return <span className="avatar">{label}</span>
}

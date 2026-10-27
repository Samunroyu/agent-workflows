import { ChevronRight, LoaderCircle } from 'lucide-react'
import type { RunStateOut, RunStatus } from '../types'
import { formatRelativeTime, runTask } from '../format'

export const statusMeta: Record<RunStatus, { label: string; color: string; bg: string }> = {
  waiting: { label: '待审批', color: '#a15c24', bg: '#fff2dd' },
  running: { label: '执行中', color: '#5270a7', bg: '#edf3ff' },
  done: { label: '已完成', color: '#397765', bg: '#eaf6f0' },
  error: { label: '出错', color: '#b65757', bg: '#fff0ef' },
  interrupted: { label: '已中断', color: '#737373', bg: '#f1f1f1' },
  unknown: { label: '状态未知', color: '#737373', bg: '#f1f1f1' },
}

export function StatusBadge({ status, subtle = false }: { status: RunStatus; subtle?: boolean }) {
  const meta = statusMeta[status]
  return (
    <span className={`status-badge ${subtle ? 'status-badge--subtle' : ''}`} style={{ color: meta.color, background: meta.bg }}>
      {status === 'running' ? <LoaderCircle size={11} className="animate-spin" /> : <i style={{ background: meta.color }} />}
      {meta.label}
    </span>
  )
}

export function RunRow({ run, selected, onClick }: { run: RunStateOut; selected: boolean; onClick: () => void }) {
  return (
    <button className={`run-row run-row--b ${selected ? 'is-selected' : ''}`} onClick={onClick}>
      <span className="run-row__top"><StatusBadge status={run.status} subtle /><small>{formatRelativeTime(run.created_at)}</small></span>
      <strong>{runTask(run)}</strong>
      <span className="run-row__meta"><span>{run.workflow_id || 'coding'}</span><span>·</span><span>{run.thread_id.slice(0, 14)}</span></span>
      <ChevronRight className="run-row__arrow" size={15} />
    </button>
  )
}

export function Avatar({ label = 'W' }: { label?: string }) {
  return <span className="avatar">{label}</span>
}

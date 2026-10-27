export function formatRelativeTime(value?: string | null): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000))
  if (seconds < 45) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  if (seconds < 604800) return `${Math.floor(seconds / 86400)} 天前`
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

export function formatDateTime(value?: string | null): string {
  if (!value) return '尚未记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '尚未记录'
  return date.toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function isToday(value?: string | null): boolean {
  if (!value) return false
  const date = new Date(value)
  const now = new Date()
  return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate()
}

export function runTask(run: { thread_id: string; values?: { task?: string | null } }): string {
  return run.values?.task?.trim() || `运行 ${run.thread_id.slice(0, 12)}`
}

export function stageLabel(stage?: string | null): string {
  const labels: Record<string, string> = {
    plan_approval: '计划审批',
    final_approval: '最终验收',
    approval: '人工审批',
  }
  return stage ? labels[stage] ?? stage.replaceAll('_', ' ') : '人工审批'
}

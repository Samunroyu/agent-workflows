import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'
import {
  AlertCircle, ArrowUpRight, Bell, Check, ChevronDown, CircleCheck, Clock3, Cloud,
  FileCode2, GitBranch, Inbox, LayoutDashboard, LoaderCircle, Play, Plus, Search,
  Sparkles, Terminal, TimerReset, Workflow, X, Zap,
} from 'lucide-react'
import { api } from './api'
import { formatDateTime, formatRelativeTime, isToday, runTask, stageLabel } from './format'
import type { ApprovalAction, HealthOut, RunIn, RunStateOut, WorkflowSpec } from './types'
import FlowCanvas from './components/FlowCanvas'
import { Avatar, RunRow, StatusBadge } from './components/Shared'

type DetailTab = 'plan' | 'patch' | 'test' | 'review' | 'events'

const emptyForm: RunIn = { task: '', workflow_id: 'coding', repo: '', test_cmd: '', max_attempts: 2 }
const detailTabs: { id: DetailTab; label: string }[] = [
  { id: 'plan', label: '计划' },
  { id: 'patch', label: 'Diff' },
  { id: 'test', label: '测试输出' },
  { id: 'review', label: 'Review' },
  { id: 'events', label: '事件流' },
]

function isOutsideDialog(dialog: HTMLDialogElement, clientX: number, clientY: number) {
  const rect = dialog.getBoundingClientRect()
  return clientX < rect.left || clientX > rect.right || clientY < rect.top || clientY > rect.bottom
}

function DiffView({ value }: { value: string }) {
  return <pre className="detail-code diff-view" tabIndex={0} aria-label="代码差异">{value.split('\n').map((line, index) => <code key={`${index}-${line}`} className={line.startsWith('+') ? 'diff-plus' : line.startsWith('-') ? 'diff-minus' : ''}>{line || ' '}</code>)}</pre>
}

function EmptyContent({ children }: { children: string }) {
  return <div className="detail-empty"><FileCode2 size={22} aria-hidden="true" /><p>{children}</p></div>
}

export default function CloudConsole() {
  const [health, setHealth] = useState<HealthOut | null>(null)
  const [workflows, setWorkflows] = useState<WorkflowSpec[]>([])
  const [runs, setRuns] = useState<RunStateOut[]>([])
  const [selectedWorkflowId, setSelectedWorkflowId] = useState('coding')
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<RunStateOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [query, setQuery] = useState('')
  const [waitingOnly, setWaitingOnly] = useState(false)
  const [detailTab, setDetailTab] = useState<DetailTab>('plan')
  const [feedback, setFeedback] = useState('')
  const [actionPending, setActionPending] = useState<ApprovalAction | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [form, setForm] = useState<RunIn>(emptyForm)
  const [taskInvalid, setTaskInvalid] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')
  const [warnings, setWarnings] = useState<string[]>([])
  const [toast, setToast] = useState('')
  const [now, setNow] = useState(new Date())
  const searchRef = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const dialogPointerDownOutside = useRef(false)

  const loadInitial = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const [workflowResult, runResult, healthResult] = await Promise.all([
        api.workflows(),
        api.runs(),
        api.health().catch(() => null),
      ])
      setWorkflows(workflowResult.workflows)
      setRuns(runResult.runs)
      setHealth(healthResult)
      setSelectedWorkflowId((current) => workflowResult.workflows.some((item) => item.id === current) ? current : workflowResult.workflows[0]?.id || 'coding')
      setForm((current) => ({ ...current, workflow_id: workflowResult.workflows.some((item) => item.id === current.workflow_id) ? current.workflow_id : workflowResult.workflows[0]?.id || 'coding' }))
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '无法连接后端服务')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void loadInitial() }, [loadInitial])

  useEffect(() => {
    const timer = window.setInterval(() => {
      void api.runs().then((result) => setRuns(result.runs)).catch(() => undefined)
      void api.workflows().then((result) => setWorkflows(result.workflows)).catch(() => undefined)
      void api.health().then(setHealth).catch(() => setHealth(null))
    }, 2500)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!selectedRunId) return
    let cancelled = false
    const loadDetail = async () => {
      try {
        const detail = await api.run(selectedRunId)
        if (!cancelled) {
          setSelectedRun(detail)
          setDetailError('')
        }
      } catch (error) {
        if (!cancelled) setDetailError(error instanceof Error ? error.message : '运行详情加载失败')
      }
    }
    void loadDetail()
    const timer = window.setInterval(() => void loadDetail(), 2500)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [selectedRunId])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 3500)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        searchRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (modalOpen && !dialog.open) dialog.showModal()
    if (!modalOpen && dialog.open) dialog.close()
  }, [modalOpen])

  const currentWorkflow = workflows.find((item) => item.id === selectedWorkflowId) || workflows[0]
  const waitingRuns = useMemo(() => runs.filter((run) => run.status === 'waiting' && (run.interrupts?.length ?? 0) > 0), [runs])
  const filteredRuns = useMemo(() => runs.filter((run) => {
    const matchesQuery = `${runTask(run)} ${run.thread_id} ${run.workflow_id || ''}`.toLowerCase().includes(query.toLowerCase())
    return matchesQuery && (!waitingOnly || run.status === 'waiting')
  }), [query, runs, waitingOnly])
  const todayRuns = runs.filter((run) => isToday(run.created_at)).length
  const finishedRuns = runs.filter((run) => ['done', 'error', 'interrupted'].includes(run.status))
  const successfulRuns = runs.filter((run) => run.status === 'done' && run.values?.action !== 'abort').length
  const successRate = finishedRuns.length ? `${(successfulRuns / finishedRuns.length * 100).toFixed(1)}%` : '—'
  const greeting = now.getHours() < 12 ? '早上好' : now.getHours() < 18 ? '下午好' : '晚上好'

  const activeNodeIds = selectedRun && currentWorkflow && selectedRun.workflow_id === currentWorkflow.id ? selectedRun.next || [] : []
  const executedNodeIds = useMemo(() => {
    if (!currentWorkflow || !selectedRun || selectedRun.workflow_id !== currentWorkflow.id) return []
    if (selectedRun.status === 'done') return currentWorkflow.nodes.map((node) => node.id)
    const events = selectedRun.values?.events || []
    return currentWorkflow.nodes.filter((node) => events.some((event) => event === node.id || event.startsWith(`${node.id}:`) || event.startsWith(`${node.id}#`))).map((node) => node.id)
  }, [currentWorkflow, selectedRun])

  const selectRun = (run: RunStateOut) => {
    setSelectedRunId(run.thread_id)
    setSelectedRun(run)
    setSelectedWorkflowId(run.workflow_id || 'coding')
    setDetailTab(run.interrupts?.[0]?.patch ? 'patch' : 'plan')
    setFeedback('')
    setDetailError('')
  }

  const clearSelectedRun = () => {
    setSelectedRunId(null)
    setSelectedRun(null)
    setDetailError('')
  }

  const openNewRun = () => {
    setForm({ ...emptyForm, workflow_id: currentWorkflow?.id || 'coding' })
    setWarnings([])
    setFormError('')
    setTaskInvalid(false)
    setModalOpen(true)
  }

  const closeNewRun = () => {
    if (!submitting) setModalOpen(false)
  }

  const handleDialogPointerDown = (event: ReactPointerEvent<HTMLDialogElement>) => {
    const dialog = dialogRef.current
    dialogPointerDownOutside.current = Boolean(
      dialog && !('closedBy' in HTMLDialogElement.prototype) && event.target === dialog
        && isOutsideDialog(dialog, event.clientX, event.clientY),
    )
  }

  const handleDialogPointerUp = (event: ReactPointerEvent<HTMLDialogElement>) => {
    const dialog = dialogRef.current
    if (dialog && dialogPointerDownOutside.current && event.target === dialog
      && isOutsideDialog(dialog, event.clientX, event.clientY)) closeNewRun()
    dialogPointerDownOutside.current = false
  }

  const startRun = async (event: FormEvent) => {
    event.preventDefault()
    if (!form.task.trim()) {
      setTaskInvalid(true)
      return
    }
    setTaskInvalid(false)
    setSubmitting(true)
    setFormError('')
    setWarnings([])
    try {
      const result = await api.startRun({
        ...form,
        task: form.task.trim(),
        repo: form.repo?.trim() || null,
        test_cmd: form.test_cmd?.trim() || null,
      })
      const refreshed = await api.runs()
      setRuns(refreshed.runs)
      const created = refreshed.runs.find((run) => run.thread_id === result.thread_id)
      setSelectedRunId(result.thread_id)
      setSelectedRun(created || null)
      setSelectedWorkflowId(result.workflow_id)
      setWarnings(result.warnings || [])
      setToast('运行已创建，正在执行')
      if (!result.warnings?.length) setModalOpen(false)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '创建运行失败')
    } finally {
      setSubmitting(false)
    }
  }

  const onDetailTabKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % detailTabs.length
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + detailTabs.length) % detailTabs.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = detailTabs.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    const nextTab = detailTabs[nextIndex]
    setDetailTab(nextTab.id)
    document.getElementById(`detail-tab-${nextTab.id}`)?.focus()
  }

  const resumeRun = async (action: ApprovalAction) => {
    if (!selectedRun || actionPending) return
    setActionPending(action)
    setDetailError('')
    try {
      await api.resume(selectedRun.thread_id, action, feedback)
      const [detail, list] = await Promise.all([api.run(selectedRun.thread_id), api.runs()])
      setSelectedRun(detail)
      setRuns(list.runs)
      setFeedback('')
      setToast(action === 'approve' ? '审批已通过，流程继续执行' : action === 'revise' ? '已打回并提交反馈' : '运行已中止')
    } catch (error) {
      setDetailError(error instanceof Error ? error.message : '审批提交失败')
    } finally {
      setActionPending(null)
    }
  }

  const interrupt = selectedRun?.interrupts?.[0]
  const contentValue = selectedRun?.values
  const tabContent = interrupt?.[detailTab === 'test' ? 'test_output' : detailTab] ?? contentValue?.[detailTab === 'test' ? 'test_output' : detailTab]
  const customInterruptFields = Object.entries(interrupt || {}).filter(([key, value]) => !['stage', 'task', 'plan', 'patch', 'test_output', 'review', 'options'].includes(key) && ['string', 'number', 'boolean'].includes(typeof value))

  return (
    <div className="demo-b demo-shell cloud-console">
      <nav className="b-rail" aria-label="主导航">
        <div className="b-logo"><Cloud size={21} aria-hidden="true" /></div>
        <div className="b-rail__links">
          <button type="button" className="is-active" aria-label="总览" title="总览" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}><LayoutDashboard size={19} aria-hidden="true" /></button>
          <button type="button" aria-label="工作流" title="工作流" onClick={() => document.getElementById('workflow-panel')?.scrollIntoView({ behavior: 'smooth' })}><Workflow size={19} aria-hidden="true" /></button>
          <button type="button" aria-label="运行记录" title="运行记录" onClick={() => document.getElementById('runs-panel')?.scrollIntoView({ behavior: 'smooth' })}><TimerReset size={19} aria-hidden="true" /></button>
        </div>
        <div className="b-rail__bottom"><Avatar /></div>
      </nav>
      <div className="b-body">
        <header className="b-header">
          <div className="b-breadcrumb"><span>工作空间</span><span>/</span><strong>总览</strong></div>
          <div className="b-header__right">
            <search className="run-search">
              <Search size={15} aria-hidden="true" />
              <label className="visually-hidden" htmlFor="run-search">搜索运行记录</label>
              <input ref={searchRef} id="run-search" name="q" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索运行记录" autoCorrect="off" autoCapitalize="off" spellCheck={false} />
              <kbd aria-hidden="true">⌘ K</kbd>
            </search>
            <button type="button" className="icon-button notification-button" aria-label={`${waitingRuns.length} 项待审批`} title={`${waitingRuns.length} 项待审批`} onClick={() => waitingRuns[0] && selectRun(waitingRuns[0])}><Bell size={18} aria-hidden="true" />{waitingRuns.length > 0 && <i aria-hidden="true" />}</button>
            <span className="console-clock">{now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
            <span className={`health-pill ${health ? '' : 'is-offline'}`}><i aria-hidden="true" />{health ? `${health.agent} Agent 在线` : '后端离线'}</span>
          </div>
        </header>
        <main className="b-main">
          <section className="b-welcome">
            <div><span className="b-kicker"><Sparkles size={13} aria-hidden="true" />YOUR AGENT WORKSPACE</span><h1>{greeting} <span aria-hidden="true">👋</span></h1><p>{waitingRuns.length ? `工作流正在有序运行，有 ${waitingRuns.length} 项审批等待你确认。` : '工作流状态清晰可见，没有等待处理的审批。'}</p></div>
            <button type="button" className="b-new-run" onClick={openNewRun} disabled={!workflows.length}><Plus size={17} aria-hidden="true" /><span className="new-run-label">发起新运行</span></button>
          </section>

          {loadError && <div className="console-banner console-banner--error" role="alert"><AlertCircle size={16} aria-hidden="true" /><span>{loadError}。请确认后端已在 8080 端口启动。</span><button type="button" onClick={() => void loadInitial()}>重新连接</button></div>}
          {toast && <div className="console-toast" role="status" aria-live="polite" aria-atomic="true"><CircleCheck size={16} aria-hidden="true" />{toast}<button type="button" aria-label="关闭通知" onClick={() => setToast('')}><X size={14} aria-hidden="true" /></button></div>}

          <section className="b-stats" aria-label="运行统计">
            <div><span className="stat-icon stat-icon--purple"><Workflow size={18} aria-hidden="true" /></span><p>可用工作流<strong>{workflows.length}</strong><small><i aria-hidden="true" />来自实时定义</small></p></div>
            <div><span className="stat-icon stat-icon--blue"><Zap size={18} aria-hidden="true" /></span><p>今日运行<strong>{todayRuns}</strong><small>全部运行 {runs.length} 次</small></p></div>
            <div><span className="stat-icon stat-icon--green"><CircleCheck size={18} aria-hidden="true" /></span><p>运行成功率<strong>{successRate}</strong><small>{finishedRuns.length ? `${successfulRuns} / ${finishedRuns.length} 次已完成` : '暂无已结束运行'}</small></p></div>
          </section>

          <div className="b-workspace">
            <section className="b-canvas-card" id="workflow-panel">
              <div className="b-card-header">
                <div className="b-workflow-title"><span><GitBranch size={18} aria-hidden="true" /></span><div><strong>{currentWorkflow?.name || '工作流画布'}</strong><small>{currentWorkflow?.description || '等待工作流定义'}</small></div></div>
                <div>
                  <label className="workflow-select" htmlFor="workflow-switcher"><Workflow size={13} aria-hidden="true" /><span className="visually-hidden">切换工作流</span><select id="workflow-switcher" name="workflow_id" value={currentWorkflow?.id || ''} onChange={(event) => { setSelectedWorkflowId(event.target.value); clearSelectedRun() }} disabled={!workflows.length}>{workflows.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><ChevronDown size={12} aria-hidden="true" /></label>
                  <button type="button" className="soft-button" onClick={openNewRun} disabled={!currentWorkflow}><Play size={14} aria-hidden="true" />运行</button>
                </div>
              </div>
              {loading ? <div className="canvas-loading"><progress aria-label="正在加载工作流" /><span>正在加载工作流…</span></div> : currentWorkflow ? <FlowCanvas workflow={currentWorkflow} activeNodeIds={activeNodeIds} executedNodeIds={executedNodeIds} /> : <EmptyContent>暂无工作流，后端注册后会自动显示在这里。</EmptyContent>}
              <div className="b-canvas-footer"><span><i aria-hidden="true" />{selectedRun ? `正在查看 ${selectedRun.thread_id.slice(0, 14)}` : '来自 API 的动态拓扑'}</span><span>{currentWorkflow ? `${currentWorkflow.nodes.length} 个节点 · 更新于 ${formatRelativeTime(currentWorkflow.updated_at)}` : '暂无定义'}</span></div>
            </section>

            <aside className="b-approval-card console-detail-card">
              {selectedRun ? <>
                <div className="b-approval-top"><span className={`stat-icon ${selectedRun.status === 'waiting' ? 'stat-icon--amber' : 'stat-icon--purple'}`}>{selectedRun.status === 'waiting' ? <Clock3 size={18} aria-hidden="true" /> : <TimerReset size={18} aria-hidden="true" />}</span><StatusBadge status={selectedRun.status} /></div>
                <div className="detail-title-row"><div><span className="eyebrow">{interrupt ? stageLabel(interrupt.stage) : 'RUN DETAIL'}</span><h2>{runTask(selectedRun)}</h2></div><button type="button" className="icon-button" aria-label="关闭详情" title="关闭详情" onClick={clearSelectedRun}><X size={16} aria-hidden="true" /></button></div>
                <p className="detail-thread">{selectedRun.thread_id} · {selectedRun.workflow_id || 'coding'}</p>
                <div className="detail-tabs" role="tablist" aria-label="运行详情">{detailTabs.map((tab, index) => <button type="button" key={tab.id} id={`detail-tab-${tab.id}`} role="tab" aria-selected={detailTab === tab.id} aria-controls="detail-panel" tabIndex={detailTab === tab.id ? 0 : -1} className={detailTab === tab.id ? 'is-active' : ''} onClick={() => setDetailTab(tab.id)} onKeyDown={(event) => onDetailTabKeyDown(event, index)}>{tab.label}</button>)}</div>
                <div className="detail-content" id="detail-panel" role="tabpanel" aria-labelledby={`detail-tab-${detailTab}`}>
                  {detailError && <div className="inline-error" role="alert"><AlertCircle size={14} aria-hidden="true" />{detailError}</div>}
                  {selectedRun.error && <div className="inline-error" role="alert"><AlertCircle size={14} aria-hidden="true" />{selectedRun.error}</div>}
                  {detailTab === 'events' ? <div className="console-events">{(contentValue?.events?.length ?? 0) > 0 ? [...(contentValue?.events || [])].reverse().map((event, index) => <div key={`${index}-${event}`}><i aria-hidden="true" /><p>{event}</p></div>) : <EmptyContent>暂无事件记录</EmptyContent>}</div>
                    : detailTab === 'patch' && typeof tabContent === 'string' && tabContent ? <DiffView value={tabContent} />
                    : typeof tabContent === 'string' && tabContent ? <pre className="detail-code" tabIndex={0}><code>{tabContent}</code></pre>
                    : detailTab === 'plan' && contentValue?.result ? <pre className="detail-code" tabIndex={0}><code>{contentValue.result}</code></pre>
                    : <EmptyContent>{detailTab === 'plan' ? '当前运行尚未生成计划' : `暂无${detailTabs.find((tab) => tab.id === detailTab)?.label}内容`}</EmptyContent>}
                  {customInterruptFields.length > 0 && <dl className="interrupt-extra">{customInterruptFields.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl>}
                </div>
                {interrupt && selectedRun.status === 'waiting' ? <div className="approval-controls">
                  <label className="visually-hidden" htmlFor="approval-feedback">审批反馈</label>
                  <textarea id="approval-feedback" name="feedback" value={feedback} onChange={(event) => setFeedback(event.target.value)} placeholder="添加审批反馈（打回时建议填写）…" />
                  <button type="button" className="b-approve" disabled={!!actionPending} onClick={() => void resumeRun('approve')}>{actionPending === 'approve' ? <LoaderCircle className="animate-spin" size={15} aria-hidden="true" /> : <Check size={15} aria-hidden="true" />}确认并继续<ArrowUpRight size={16} aria-hidden="true" /></button>
                  <div className="secondary-actions"><button type="button" className="b-revise" disabled={!!actionPending} onClick={() => void resumeRun('revise')}>{actionPending === 'revise' ? '正在提交…' : '打回修改'}</button><button type="button" className="b-abort" disabled={!!actionPending} onClick={() => void resumeRun('abort')}>中止运行</button></div>
                </div> : <div className="detail-footnote"><span>创建于 {formatDateTime(selectedRun.created_at)}</span><span>{selectedRun.next?.length ? `停靠：${selectedRun.next.join(', ')}` : '流程已结束'}</span></div>}
              </> : <>
                <div className="b-approval-top"><span className="stat-icon stat-icon--purple"><Inbox size={18} aria-hidden="true" /></span><span className="workflow-count">{workflows.length} 个工作流</span></div>
                <span className="eyebrow">WORKFLOW OVERVIEW</span><h2>选择一个运行，查看完整细节。</h2><p>你可以直接预览工作流拓扑，或从下方运行记录进入事件、Diff 与审批详情。</p>
                <div className="workflow-summary"><div><span>当前工作流</span><strong>{currentWorkflow?.name || '—'}</strong></div><div><span>节点 / 连线</span><strong>{currentWorkflow ? `${currentWorkflow.nodes.length} / ${currentWorkflow.edges.length}` : '—'}</strong></div><div><span>待审批</span><strong>{waitingRuns.length}</strong></div></div>
                <button type="button" className="b-approve" onClick={openNewRun} disabled={!currentWorkflow}><Plus size={15} aria-hidden="true" />从这里发起运行<ArrowUpRight size={16} aria-hidden="true" /></button>
                <div className="b-approval-foot"><Avatar label="A" /><span>Agent 执行，关键步骤由你确认</span></div>
              </>}
            </aside>
          </div>

          <section className="b-recent" id="runs-panel">
            <div className="b-section-title"><div><strong>运行记录</strong><span>{filteredRuns.length} 条结果</span></div><button type="button" className={waitingOnly ? 'is-active' : ''} aria-pressed={waitingOnly} onClick={() => setWaitingOnly((value) => !value)}><Clock3 size={12} aria-hidden="true" />{waitingOnly ? '查看全部' : '仅看待审批'}<ChevronDown size={13} aria-hidden="true" /></button></div>
            {loading ? <div className="runs-loading"><progress aria-label="正在加载运行记录" /><span>正在加载运行记录…</span></div> : filteredRuns.length ? <div className="b-run-grid">{filteredRuns.map((run) => <RunRow key={run.thread_id} run={run} selected={selectedRunId === run.thread_id} onClick={() => selectRun(run)} />)}</div> : <div className="runs-empty"><Inbox size={22} aria-hidden="true" /><strong>{query || waitingOnly ? '没有匹配的运行' : '还没有运行记录'}</strong><p>{query || waitingOnly ? '试试调整搜索词或筛选条件。' : '发起第一个运行后，状态会每 2.5 秒自动更新。'}</p>{!query && !waitingOnly && <button type="button" onClick={openNewRun}><Plus size={14} aria-hidden="true" />新建运行</button>}</div>}
          </section>
        </main>
      </div>

      <dialog ref={dialogRef} className="run-modal" closedby="any" aria-labelledby="new-run-title" onClose={() => setModalOpen(false)} onCancel={(event) => { if (submitting) event.preventDefault() }} onPointerDown={handleDialogPointerDown} onPointerUp={handleDialogPointerUp}>
        <div className="modal-heading"><div><span className="stat-icon stat-icon--purple"><Play size={17} aria-hidden="true" /></span><div><span className="eyebrow">NEW RUN</span><h2 id="new-run-title">发起一个新运行</h2></div></div><button type="button" className="icon-button" aria-label="关闭新建运行弹窗" disabled={submitting} onClick={closeNewRun}><X size={18} aria-hidden="true" /></button></div>
        {warnings.length > 0 && <div className="warning-box" role="status"><AlertCircle size={16} aria-hidden="true" /><div><strong>运行已创建，但请注意：</strong>{warnings.map((warning) => <p key={warning}>{warning}</p>)}</div></div>}
        <form onSubmit={(event) => void startRun(event)}>
          <div className="form-field"><label htmlFor="new-run-workflow">工作流</label><div className="form-select"><select id="new-run-workflow" name="workflow_id" value={form.workflow_id} onChange={(event) => setForm({ ...form, workflow_id: event.target.value })}>{workflows.map((item) => <option key={item.id} value={item.id}>{item.name}（{item.id}）</option>)}</select><ChevronDown size={14} aria-hidden="true" /></div></div>
          <div className="form-field"><label htmlFor="new-run-task">任务描述 <em>必填</em></label><textarea id="new-run-task" name="task" required autoFocus value={form.task} aria-invalid={taskInvalid} aria-errormessage={taskInvalid ? 'new-run-task-error' : undefined} onChange={(event) => setForm({ ...form, task: event.target.value })} onInput={(event) => { if (taskInvalid) setTaskInvalid(!event.currentTarget.value.trim()) }} onBlur={(event) => setTaskInvalid(!event.currentTarget.value.trim())} onInvalid={() => setTaskInvalid(true)} placeholder="例如：修复计算器的加法函数，并补充回归测试" />{taskInvalid && <p className="field-error" id="new-run-task-error"><AlertCircle size={14} aria-hidden="true" />请填写任务描述后再创建运行。</p>}</div>
          <div className="form-field"><label htmlFor="new-run-repo">目标仓库路径 <small>可选</small></label><input id="new-run-repo" name="repo" value={form.repo || ''} onChange={(event) => setForm({ ...form, repo: event.target.value })} placeholder="/path/to/repository" autoCorrect="off" autoCapitalize="off" spellCheck={false} /></div>
          <div className="form-field"><label htmlFor="new-run-test-command">测试命令 <small>可选</small></label><div className="input-with-icon"><Terminal size={14} aria-hidden="true" /><input id="new-run-test-command" name="test_cmd" value={form.test_cmd || ''} onChange={(event) => setForm({ ...form, test_cmd: event.target.value })} placeholder="pytest -q" autoCorrect="off" autoCapitalize="off" spellCheck={false} /></div></div>
          <div className="form-field"><label htmlFor="new-run-max-attempts">最大尝试次数</label><input id="new-run-max-attempts" name="max_attempts" type="number" min={1} max={10} required value={form.max_attempts} onChange={(event) => setForm({ ...form, max_attempts: Number(event.target.value) })} /></div>
          {formError && <div className="inline-error" role="alert"><AlertCircle size={14} aria-hidden="true" />{formError}</div>}
          <div className="modal-actions"><button type="button" className="modal-cancel" disabled={submitting} onClick={closeNewRun}>{warnings.length ? '关闭' : '取消'}</button><button type="submit" className="modal-submit" disabled={submitting}>{submitting ? <LoaderCircle className="animate-spin" size={15} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}{submitting ? '正在创建…' : warnings.length ? '再次创建' : '创建并运行'}</button></div>
        </form>
      </dialog>
    </div>
  )
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle, Bell, ChevronDown, CircleCheck, Clock3, Cloud, Inbox,
  LayoutDashboard, LoaderCircle, Plus, Search, Settings2, Sparkles,
  TimerReset, Workflow, X, Zap,
} from 'lucide-react'
import { api } from './api'
import { formatRelativeTime, isToday, runTask } from './format'
import type {
  AgentProfileOut, HealthOut, RunStateOut, StartRunOut,
  WorkerInfoOut, WorkflowSpec,
} from './types'
import FlowCanvas from './components/FlowCanvas'
import WorkflowEditor from './components/WorkflowEditor'
import RunMonitor from './components/RunMonitor'
import { AgentManagerDialog, NewRunDialog } from './components/Dialogs'
import { Avatar, RunRow, StatusBadge } from './components/Shared'

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

export default function CloudConsole() {
  const [health, setHealth] = useState<HealthOut | null>(null)
  const [workflows, setWorkflows] = useState<WorkflowSpec[]>([])
  const [runs, setRuns] = useState<RunStateOut[]>([])
  const [agents, setAgents] = useState<AgentProfileOut[]>([])
  const [workers, setWorkers] = useState<WorkerInfoOut[]>([])
  const [selectedWorkflowId, setSelectedWorkflowId] = useState('')
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<RunStateOut | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [query, setQuery] = useState('')
  const [waitingOnly, setWaitingOnly] = useState(false)
  const [toast, setToast] = useState('')
  const [now, setNow] = useState(new Date())
  const [runDialogOpen, setRunDialogOpen] = useState(false)
  const [agentDialogOpen, setAgentDialogOpen] = useState(false)
  const [cancelPendingId, setCancelPendingId] = useState<string | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const runRequestSequence = useRef(0)
  const selectedRunIdRef = useRef<string | null>(null)
  selectedRunIdRef.current = selectedRunId

  const refreshWorkflows = useCallback(async () => {
    const result = await api.workflows()
    setWorkflows(result.workflows)
    setSelectedWorkflowId((current) => result.workflows.some((item) => item.id === current) ? current : result.workflows[0]?.id ?? '')
  }, [])

  const refreshAgents = useCallback(async () => {
    const [agentResult, healthResult] = await Promise.all([api.agents(), api.health()])
    setAgents(agentResult.agents)
    setHealth(healthResult)
  }, [])

  const refreshRuns = useCallback(async () => {
    const result = await api.runs()
    setRuns(result.runs)
  }, [])

  const loadInitial = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const [workflowResult, runResult, healthResult, agentResult, workerResult] = await Promise.all([
        api.workflows(), api.runs(), api.health(), api.agents(), api.workers(),
      ])
      setWorkflows(workflowResult.workflows)
      setRuns(runResult.runs)
      setHealth(healthResult)
      setAgents(agentResult.agents)
      setWorkers(workerResult.workers)
      setSelectedWorkflowId((current) => workflowResult.workflows.some((item) => item.id === current) ? current : workflowResult.workflows[0]?.id ?? '')
    } catch (error) {
      setLoadError(errorMessage(error, '无法连接后端服务'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void loadInitial() }, [loadInitial])

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refreshRuns().catch(() => undefined)
      void api.health().then(setHealth).catch(() => setHealth(null))
      void api.agents().then((result) => setAgents(result.agents)).catch(() => undefined)
    }, 2500)
    return () => window.clearInterval(timer)
  }, [refreshRuns])

  useEffect(() => {
    if (!selectedRunId) {
      setSelectedRun(null)
      return
    }
    let cancelled = false
    const sequence = ++runRequestSequence.current
    const loadDetail = async () => {
      try {
        const detail = await api.run(selectedRunId)
        if (!cancelled && runRequestSequence.current === sequence) setSelectedRun(detail)
      } catch (error) {
        if (!cancelled && runRequestSequence.current === sequence) setLoadError(errorMessage(error, '运行详情加载失败'))
      }
    }
    void loadDetail()
    const timer = window.setInterval(() => void loadDetail(), 1500)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [selectedRunId])

  useEffect(() => {
    if (!selectedRun || selectedNodeId) return
    const next = selectedRun.running_nodes?.[0] ?? selectedRun.next?.[0] ?? Object.keys(selectedRun.values?.nodes ?? {})[0] ?? null
    setSelectedNodeId(next)
  }, [selectedNodeId, selectedRun])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 4500)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && searchRef.current?.offsetParent !== null) {
        event.preventDefault()
        searchRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const selectRun = (run: RunStateOut) => {
    setSelectedRunId(run.thread_id)
    setSelectedRun(run)
    setSelectedNodeId(run.running_nodes?.[0] ?? run.next?.[0] ?? Object.keys(run.values?.nodes ?? {})[0] ?? null)
    setLoadError('')
  }

  const clearSelectedRun = () => {
    setSelectedRunId(null)
    setSelectedRun(null)
    setSelectedNodeId(null)
    setLoadError('')
  }

  const refreshSelectedRun = useCallback(async () => {
    const requestRunId = selectedRunId
    if (!requestRunId) return
    const [detail, list] = await Promise.all([api.run(requestRunId), api.runs()])
    if (selectedRunIdRef.current === requestRunId) setSelectedRun(detail)
    setRuns(list.runs)
  }, [selectedRunId])

  const onStarted = (result: StartRunOut) => {
    setRunDialogOpen(false)
    setSelectedRunId(result.thread_id)
    setSelectedNodeId(null)
    setSelectedRun(null)
    setToast(result.warnings?.length ? `运行已创建：${result.warnings.join('；')}` : '运行已创建，正在执行')
    void refreshRuns().catch(() => undefined)
  }

  const cancelFromList = async (run: RunStateOut) => {
    if (cancelPendingId) return
    setCancelPendingId(run.thread_id)
    try {
      const result = await api.cancel(run.thread_id)
      setToast(`已强杀 ${runTask(run)}，终止 ${result.killed_processes ?? 0} 个进程`)
      await refreshRuns()
      if (selectedRunId === run.thread_id) await refreshSelectedRun()
    } catch (error) {
      setToast(errorMessage(error, '强杀运行失败'))
    } finally {
      setCancelPendingId(null)
    }
  }

  const waitingRuns = useMemo(() => runs.filter((run) => run.status === 'waiting' && (run.interrupts?.length ?? 0) > 0), [runs])
  const filteredRuns = useMemo(() => runs.filter((run) => {
    const matchesQuery = `${runTask(run)} ${run.thread_id} ${run.workflow_id}`.toLowerCase().includes(query.toLowerCase())
    return matchesQuery && (!waitingOnly || run.status === 'waiting')
  }), [query, runs, waitingOnly])
  const todayRuns = runs.filter((run) => isToday(run.created_at)).length
  const finishedRuns = runs.filter((run) => ['done', 'error', 'interrupted', 'cancelled'].includes(run.status))
  const successfulRuns = runs.filter((run) => run.status === 'done' && run.values?.action !== 'abort').length
  const successRate = finishedRuns.length ? `${(successfulRuns / finishedRuns.length * 100).toFixed(1)}%` : '—'
  const greeting = now.getHours() < 12 ? '早上好' : now.getHours() < 18 ? '下午好' : '晚上好'
  const monitorWorkflow = selectedRun ? workflows.find((item) => item.id === selectedRun.workflow_id) ?? null : null
  const onlineAgents = health?.online_agents ?? agents.filter((agent) => agent.available).map((agent) => agent.id)

  return <div className="demo-b demo-shell cloud-console">
    <nav className="b-rail" aria-label="主导航">
      <div className="b-logo"><Cloud size={21} aria-hidden="true" /></div>
      <div className="b-rail__links">
        <button type="button" className="is-active" aria-label="总览" title="总览" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}><LayoutDashboard size={19} aria-hidden="true" /></button>
        <button type="button" aria-label="工作流" title="工作流" onClick={() => document.getElementById(selectedRun ? 'run-workflow-panel' : 'workflow-panel')?.scrollIntoView({ behavior: 'smooth' })}><Workflow size={19} aria-hidden="true" /></button>
        <button type="button" aria-label="运行记录" title="运行记录" onClick={() => document.getElementById('runs-panel')?.scrollIntoView({ behavior: 'smooth' })}><TimerReset size={19} aria-hidden="true" /></button>
      </div>
      <div className="b-rail__bottom"><Avatar /></div>
    </nav>
    <div className="b-body">
      <header className="b-header">
        <div className="b-breadcrumb"><span>工作空间</span><span>/</span><strong>{selectedRun ? '运行监控' : '编排编辑'}</strong></div>
        <div className="b-header__right">
          <search className="run-search"><Search size={15} aria-hidden="true" /><label className="visually-hidden" htmlFor="run-search">搜索运行记录</label><input ref={searchRef} id="run-search" name="q" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索运行记录" autoCorrect="off" autoCapitalize="off" spellCheck={false} /><kbd aria-hidden="true">⌘ K</kbd></search>
          <button type="button" className="soft-button agent-manager-button" onClick={() => setAgentDialogOpen(true)}><Settings2 size={14} aria-hidden="true" />Agent 管理</button>
          <button type="button" className="icon-button notification-button" aria-label={`${waitingRuns.length} 项待审批`} title={`${waitingRuns.length} 项待审批`} onClick={() => waitingRuns[0] && selectRun(waitingRuns[0])}><Bell size={18} aria-hidden="true" />{waitingRuns.length > 0 && <i aria-hidden="true" />}</button>
          <span className="console-clock">{now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
          <span className={`health-pill ${health ? '' : 'is-offline'}`}><i aria-hidden="true" />{health ? `${onlineAgents.length} 个 Agent 在线${onlineAgents.length ? `：${onlineAgents.join(', ')}` : ''}` : '后端离线'}</span>
        </div>
      </header>
      <main className="b-main">
        <section className="b-welcome"><div><span className="b-kicker"><Sparkles size={13} aria-hidden="true" />AGENT ORCHESTRATION</span><h1>{greeting} <span aria-hidden="true">👋</span></h1><p>{waitingRuns.length ? `有 ${waitingRuns.length} 项人工审批等待处理，运行中的并行节点可分别查看。` : '自由编排工作流，点击任意运行节点查看实时日志与产出。'}</p></div><button type="button" className="b-new-run" onClick={() => setRunDialogOpen(true)} disabled={!workflows.length}><Plus size={17} aria-hidden="true" /><span className="new-run-label">发起新运行</span></button></section>

        {loadError && <div className="console-banner console-banner--error" role="alert"><AlertCircle size={16} aria-hidden="true" /><span>{loadError}。请确认后端已在 8080 端口启动。</span><button type="button" onClick={() => void loadInitial()}>重新连接</button></div>}
        {toast && <div className="console-toast" role="status" aria-live="polite" aria-atomic="true"><CircleCheck size={16} aria-hidden="true" /><span>{toast}</span><button type="button" aria-label="关闭通知" onClick={() => setToast('')}><X size={14} aria-hidden="true" /></button></div>}

        <section className="b-stats" aria-label="运行统计">
          <div><span className="stat-icon stat-icon--purple"><Workflow size={18} aria-hidden="true" /></span><p>可用工作流<strong>{workflows.length}</strong><small><i aria-hidden="true" />{workers.length} 个执行主机</small></p></div>
          <div><span className="stat-icon stat-icon--blue"><Zap size={18} aria-hidden="true" /></span><p>今日运行<strong>{todayRuns}</strong><small>全部运行 {runs.length} 次</small></p></div>
          <div><span className="stat-icon stat-icon--green"><CircleCheck size={18} aria-hidden="true" /></span><p>运行成功率<strong>{successRate}</strong><small>{finishedRuns.length ? `${successfulRuns} / ${finishedRuns.length} 次已完成` : '暂无已结束运行'}</small></p></div>
        </section>

        {loading ? <div className="canvas-loading"><progress aria-label="正在加载操作台" /><span>正在加载工作流与运行状态…</span></div> : <div className={`b-workspace ${selectedRun ? 'is-monitor' : 'is-editor'}`}>
          <div className="editor-session" hidden={Boolean(selectedRun)}>
            <WorkflowEditor workflows={workflows} selectedWorkflowId={selectedWorkflowId} agents={agents} workers={workers} onSelectWorkflow={setSelectedWorkflowId} onWorkflowsChanged={refreshWorkflows} onOpenRun={() => setRunDialogOpen(true)} onToast={setToast} />
          </div>
          {selectedRun && (monitorWorkflow ? <>
            <section className="b-canvas-card run-canvas-card" id="run-workflow-panel">
              <div className="b-card-header"><div className="b-workflow-title"><span><Workflow size={18} aria-hidden="true" /></span><div><strong>{monitorWorkflow.name}</strong><small>运行视图基于已保存的工作流定义</small></div></div><StatusBadge status={selectedRun.status} /></div>
              <FlowCanvas workflow={monitorWorkflow} mode="monitor" run={selectedRun} selected={selectedNodeId ? { type: 'node', id: selectedNodeId } : null} onSelect={(selection) => { if (selection?.type === 'node' && !['START', 'END'].includes(selection.id)) setSelectedNodeId(selection.id) }} />
              <div className="b-canvas-footer"><span><i aria-hidden="true" />{selectedRun.running_nodes?.length ? `${selectedRun.running_nodes.length} 个节点并行执行中` : `运行 ${selectedRun.thread_id.slice(0, 16)}`}</span><span>点击节点查看详情</span></div>
            </section>
            <RunMonitor run={selectedRun} workflow={monitorWorkflow} agents={agents} selectedNodeId={selectedNodeId} onClose={clearSelectedRun} onRefresh={refreshSelectedRun} onToast={setToast} />
          </> : <section className="editor-empty" role="alert"><AlertCircle size={24} aria-hidden="true" /><h2>找不到此次运行的工作流定义</h2><p>工作流可能已被删除，运行记录仍保留在下方列表。</p><button type="button" className="soft-button" onClick={clearSelectedRun}>返回编排编辑</button></section>)}
        </div>}

        <section className="b-recent" id="runs-panel">
          <div className="b-section-title"><div><strong>运行记录</strong><span>{filteredRuns.length} 条结果</span></div><button type="button" className={waitingOnly ? 'is-active' : ''} aria-pressed={waitingOnly} onClick={() => setWaitingOnly((value) => !value)}><Clock3 size={12} aria-hidden="true" />{waitingOnly ? '查看全部' : '仅看待审批'}<ChevronDown size={13} aria-hidden="true" /></button></div>
          {loading ? <div className="runs-loading"><LoaderCircle className="animate-spin" size={18} aria-hidden="true" /><span>正在加载运行记录…</span></div> : filteredRuns.length ? <div className="b-run-grid">{filteredRuns.map((run) => <RunRow key={run.thread_id} run={run} selected={selectedRunId === run.thread_id} onClick={() => selectRun(run)} onCancel={() => void cancelFromList(run)} cancelPending={cancelPendingId === run.thread_id} />)}</div> : <div className="runs-empty"><Inbox size={22} aria-hidden="true" /><strong>{query || waitingOnly ? '没有匹配的运行' : '还没有运行记录'}</strong><p>{query || waitingOnly ? '试试调整搜索词或筛选条件。' : '发起第一个运行后，状态会自动更新。'}</p>{!query && !waitingOnly && <button type="button" onClick={() => setRunDialogOpen(true)} disabled={!workflows.length}><Plus size={14} aria-hidden="true" />新建运行</button>}</div>}
        </section>
        <p className="workspace-footnote">{health?.default_repo ? `默认工作区：${health.default_repo}` : '工作区路径由 Worker 配置决定'} · 最近更新 {formatRelativeTime(new Date().toISOString())}</p>
      </main>
    </div>

    <NewRunDialog open={runDialogOpen} workflows={workflows} initialWorkflowId={selectedWorkflowId} onClose={() => setRunDialogOpen(false)} onStarted={onStarted} />
    <AgentManagerDialog open={agentDialogOpen} agents={agents} onClose={() => setAgentDialogOpen(false)} onChanged={refreshAgents} onToast={setToast} />
  </div>
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle, Bell, ChevronDown, CircleCheck, Clock3, Cloud, Inbox,
  LoaderCircle, Play, Plus, Search, Settings2, Square, TimerReset, Workflow, X,
} from 'lucide-react'
import { api } from './api'
import { isToday, runTask } from './format'
import type {
  AgentProfileOut, HealthOut, RunStateOut, StartRunOut,
  WorkerInfoOut, WorkflowSpec,
} from './types'
import FlowCanvas from './components/FlowCanvas'
import WorkflowEditor from './components/WorkflowEditor'
import RunNodeWorkbenchDialog from './components/RunNodeWorkbenchDialog'
import { AgentManagerDialog, NewRunDialog } from './components/Dialogs'
import { Avatar, RunRow, StatusBadge } from './components/Shared'

type ActiveView = 'editor' | 'runs'

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

export default function CloudConsole() {
  const [activeView, setActiveView] = useState<ActiveView>('editor')
  const [health, setHealth] = useState<HealthOut | null>(null)
  const [workflows, setWorkflows] = useState<WorkflowSpec[]>([])
  const [runs, setRuns] = useState<RunStateOut[]>([])
  const [agents, setAgents] = useState<AgentProfileOut[]>([])
  const [workers, setWorkers] = useState<WorkerInfoOut[]>([])
  const [selectedWorkflowId, setSelectedWorkflowId] = useState('')
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<RunStateOut | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [workbenchNodeId, setWorkbenchNodeId] = useState<string | null>(null)
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
    setWorkbenchNodeId(null)
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
    setActiveView('runs')
    setSelectedRunId(result.thread_id)
    setSelectedNodeId(null)
    setSelectedRun(null)
    setWorkbenchNodeId(null)
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

  const openWaitingRun = () => {
    setActiveView('runs')
    const target = waitingRuns[0]
    if (!target) return
    selectRun(target)
    const interrupt = target.interrupts?.[0]
    setWorkbenchNodeId(interrupt?.node_id ?? null)
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
  const monitorWorkflow = selectedRun ? workflows.find((item) => item.id === selectedRun.workflow_id) ?? null : null
  const onlineAgents = health?.online_agents ?? agents.filter((agent) => agent.available).map((agent) => agent.id)
  const selectedInterrupt = selectedRun?.interrupts?.[0] ?? null

  return <div className="demo-b demo-shell cloud-console">
    <nav className="b-rail" aria-label="主导航">
      <div className="b-logo"><Cloud size={21} aria-hidden="true" /></div>
      <div className="b-rail__links">
        <button type="button" className={activeView === 'editor' ? 'is-active' : ''} aria-pressed={activeView === 'editor'} aria-label="工作流编排" title="工作流编排" onClick={() => setActiveView('editor')}><Workflow size={19} aria-hidden="true" /></button>
        <button type="button" className={activeView === 'runs' ? 'is-active' : ''} aria-pressed={activeView === 'runs'} aria-label="运行监控中心" title="运行监控中心" onClick={() => setActiveView('runs')}><TimerReset size={19} aria-hidden="true" /></button>
      </div>
      <div className="b-rail__bottom"><Avatar /></div>
    </nav>
    <div className="b-body">
      <header className="b-header">
        <div className="b-breadcrumb"><span>工作空间</span><span>/</span><strong>{activeView === 'editor' ? '工作流编排' : '运行监控中心'}</strong></div>
        <div className="b-header__right">
          <button type="button" className="soft-button agent-manager-button" onClick={() => setAgentDialogOpen(true)}><Settings2 size={14} aria-hidden="true" />Agent 管理</button>
          <button type="button" className="icon-button notification-button" aria-label={`${waitingRuns.length} 项待审批`} title={`${waitingRuns.length} 项待审批`} onClick={openWaitingRun}><Bell size={18} aria-hidden="true" />{waitingRuns.length > 0 && <i aria-hidden="true" />}</button>
          <span className="console-clock">{now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
          <span className={`health-pill ${health ? '' : 'is-offline'}`}><i aria-hidden="true" />{health ? `${onlineAgents.length} 个 Agent 在线${onlineAgents.length ? `：${onlineAgents.join(', ')}` : ''}` : '后端离线'}</span>
        </div>
      </header>

      {loadError && <div className="console-banner console-banner--error" role="alert"><AlertCircle size={16} aria-hidden="true" /><span>{loadError}。请确认后端已在 8080 端口启动。</span><button type="button" onClick={() => void loadInitial()}>重新连接</button></div>}
      {toast && <div className="console-toast" role="status" aria-live="polite" aria-atomic="true"><CircleCheck size={16} aria-hidden="true" /><span>{toast}</span><button type="button" aria-label="关闭通知" onClick={() => setToast('')}><X size={14} aria-hidden="true" /></button></div>}

      {activeView === 'editor' ? (
        <main className="view editor-view" aria-label="工作流编排页">
          {loading ? (
            <div className="canvas-loading"><progress aria-label="正在加载操作台" /><span>正在加载工作流与运行状态…</span></div>
          ) : (
            <WorkflowEditor
              workflows={workflows}
              selectedWorkflowId={selectedWorkflowId}
              agents={agents}
              workers={workers}
              onSelectWorkflow={setSelectedWorkflowId}
              onWorkflowsChanged={refreshWorkflows}
              onOpenRun={() => setRunDialogOpen(true)}
              onToast={setToast}
            />
          )}
        </main>
      ) : (
        <main className="runs-view" aria-label="运行监控中心">
          <div className="runs-toolbar">
            <div className="runs-stats" aria-label="运行统计">
              <span>今日运行 <strong>{todayRuns}</strong></span>
              <span>成功率 <strong>{successRate}</strong></span>
              <span>待审批 <strong>{waitingRuns.length}</strong></span>
              <span>全部 <strong>{runs.length}</strong></span>
            </div>
            <search className="run-search">
              <Search size={15} aria-hidden="true" />
              <label className="visually-hidden" htmlFor="run-search">搜索运行记录</label>
              <input ref={searchRef} id="run-search" name="q" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索运行记录" autoCorrect="off" autoCapitalize="off" spellCheck={false} />
              <kbd aria-hidden="true">⌘ K</kbd>
            </search>
            <button type="button" className={`runs-filter ${waitingOnly ? 'is-active' : ''}`} aria-pressed={waitingOnly} onClick={() => setWaitingOnly((value) => !value)}>
              <Clock3 size={12} aria-hidden="true" />{waitingOnly ? '查看全部' : '仅看待审批'}<ChevronDown size={13} aria-hidden="true" />
            </button>
            <button type="button" className="b-new-run" onClick={() => setRunDialogOpen(true)} disabled={!workflows.length}>
              <Plus size={15} aria-hidden="true" /><span className="new-run-label">发起新运行</span>
            </button>
          </div>

          <div className="runs-body">
            <div className="runs-list" aria-label="运行记录列表">
              {loading ? (
                <div className="runs-loading"><LoaderCircle className="animate-spin" size={18} aria-hidden="true" /><span>正在加载运行记录…</span></div>
              ) : filteredRuns.length ? (
                filteredRuns.map((run) => <RunRow key={run.thread_id} run={run} selected={selectedRunId === run.thread_id} onClick={() => selectRun(run)} onCancel={() => void cancelFromList(run)} cancelPending={cancelPendingId === run.thread_id} />)
              ) : (
                <div className="runs-empty"><Inbox size={22} aria-hidden="true" /><strong>{query || waitingOnly ? '没有匹配的运行' : '还没有运行记录'}</strong><p>{query || waitingOnly ? '试试调整搜索词或筛选条件。' : '发起第一个运行后，状态会自动更新。'}</p></div>
              )}
            </div>

            {selectedRun ? (monitorWorkflow ? (
              <section className="b-canvas-card run-canvas-card" id="run-workflow-panel">
                <div className="b-card-header">
                  <div className="b-workflow-title">
                    <span><Workflow size={18} aria-hidden="true" /></span>
                    <div><strong>{monitorWorkflow.name}</strong><small>{runTask(selectedRun)} · {selectedRun.thread_id.slice(0, 16)}</small></div>
                  </div>
                  <div>
                    <StatusBadge status={selectedRun.status} />
                    {selectedRun.status === 'running' && (
                      <button type="button" className="kill-button" onClick={() => void cancelFromList(selectedRun)} disabled={cancelPendingId === selectedRun.thread_id}>
                        <Square size={13} aria-hidden="true" />强杀中止
                      </button>
                    )}
                  </div>
                </div>
                {selectedRun.status === 'waiting' && selectedInterrupt && (
                  <div className="run-banner run-banner--waiting" role="status">
                    <span>运行停靠在人工卡点「{selectedInterrupt.label || selectedInterrupt.node_id || '人工审批'}」，等待审批。</span>
                    <button type="button" onClick={() => setWorkbenchNodeId(selectedInterrupt.node_id ?? selectedNodeId)}>打开人工审批工作台</button>
                  </div>
                )}
                {['error', 'cancelled', 'interrupted'].includes(selectedRun.status) && (
                  <div className="run-banner run-banner--error" role="alert">
                    <span>运行处于{selectedRun.status === 'error' ? '失败' : '中止'}断点{selectedRun.next?.length ? `（${selectedRun.next.join(', ')}）` : ''}，可原地重试。</span>
                    <button type="button" onClick={() => setWorkbenchNodeId(selectedRun.next?.[0] ?? selectedNodeId)}>打开断点重试工作台</button>
                  </div>
                )}
                <FlowCanvas
                  workflow={monitorWorkflow}
                  mode="monitor"
                  run={selectedRun}
                  selected={selectedNodeId ? { type: 'node', id: selectedNodeId } : null}
                  onSelect={(selection) => { if (selection?.type === 'node' && !['START', 'END'].includes(selection.id)) setSelectedNodeId(selection.id) }}
                  onOpenNode={(nodeId) => { setSelectedNodeId(nodeId); setWorkbenchNodeId(nodeId) }}
                  nodeActionLabel={(node) => {
                    if (selectedRun.status === 'waiting' && (selectedRun.interrupts ?? []).some((interrupt) => interrupt.node_id === node.id)) return '立即审批'
                    if (['error', 'cancelled', 'interrupted'].includes(selectedRun.status) && (selectedRun.next ?? []).includes(node.id)) return '重试'
                    return '查看日志'
                  }}
                />
                <div className="b-canvas-footer"><span><i aria-hidden="true" />{selectedRun.running_nodes?.length ? `${selectedRun.running_nodes.length} 个节点并行执行中` : `运行 ${selectedRun.thread_id.slice(0, 16)}`}</span><span>双击节点或点卡片按钮打开运行工作台</span></div>
              </section>
            ) : (
              <section className="editor-empty" role="alert"><AlertCircle size={24} aria-hidden="true" /><h2>找不到此次运行的工作流定义</h2><p>工作流可能已被删除，运行记录仍保留在左侧列表。</p></section>
            )) : (
              <section className="editor-empty"><Play size={24} aria-hidden="true" /><h2>选择一次运行开始盯盘</h2><p>从左侧运行记录中选择一次运行，实时拓扑、日志与人工审批都在这里。</p><button type="button" className="soft-button" onClick={() => setRunDialogOpen(true)} disabled={!workflows.length}><Plus size={14} aria-hidden="true" />发起新运行</button></section>
            )}
          </div>
        </main>
      )}
    </div>

    {selectedRun && monitorWorkflow && (
      <RunNodeWorkbenchDialog
        run={selectedRun}
        workflow={monitorWorkflow}
        agents={agents}
        nodeId={workbenchNodeId}
        onSelectNode={(nodeId) => { setWorkbenchNodeId(nodeId); setSelectedNodeId(nodeId) }}
        onClose={() => setWorkbenchNodeId(null)}
        onRefresh={refreshSelectedRun}
        onToast={setToast}
      />
    )}
    <NewRunDialog open={runDialogOpen} workflows={workflows} initialWorkflowId={selectedWorkflowId} onClose={() => setRunDialogOpen(false)} onStarted={onStarted} />
    <AgentManagerDialog open={agentDialogOpen} agents={agents} onClose={() => setAgentDialogOpen(false)} onChanged={refreshAgents} onToast={setToast} />
  </div>
}

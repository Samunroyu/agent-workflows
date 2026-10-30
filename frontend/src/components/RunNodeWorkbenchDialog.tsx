import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import {
  AlertCircle, Check, ChevronRight, FileCode2, LoaderCircle, Maximize2,
  Minimize2, Pencil, Play, ShieldCheck, X,
} from 'lucide-react'
import { api, runStreamUrl } from '../api'
import { formatDateTime, runTask, stageLabel } from '../format'
import type {
  AgentProfileOut, ApprovalAction, RetryRunIn, RunLogEntry,
  RunStateOut, WorkflowSpec,
} from '../types'
import { NativeDialog } from './Dialogs'
import { StatusBadge } from './Shared'

type DetailTab = 'logs' | 'output' | 'diff'
const tabs: Array<{ id: DetailTab; label: string }> = [
  { id: 'logs', label: '实时日志' },
  { id: 'output', label: '节点产出' },
  { id: 'diff', label: 'Git Diff' },
]

function DiffView({ value }: { value: string }) {
  return <pre className="detail-code diff-view" tabIndex={0} aria-label="Git Diff 代码差异"><code>{value.split('\n').map((line, index) => <span key={`${index}-${line}`} className={line.startsWith('+') ? 'diff-plus' : line.startsWith('-') ? 'diff-minus' : ''}>{line || ' '}{'\n'}</span>)}</code></pre>
}

function EmptyContent({ children }: { children: string }) {
  return <div className="detail-empty"><FileCode2 size={22} aria-hidden="true" /><p>{children}</p></div>
}

function useNodeLogs(run: RunStateOut, nodeId: string | null) {
  const [logs, setLogs] = useState<RunLogEntry[]>([])
  const [connection, setConnection] = useState('正在加载历史日志…')
  const [error, setError] = useState('')
  const cursorRef = useRef(0)
  const terminalRef = useRef<HTMLPreElement>(null)
  const stickToBottomRef = useRef(true)

  useEffect(() => {
    if (!nodeId) {
      setLogs([])
      setError('')
      setConnection('请选择节点')
      return
    }
    let disposed = false
    let source: EventSource | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    cursorRef.current = 0
    setLogs([])
    setError('')
    setConnection('正在加载历史日志…')

    const append = (entries: RunLogEntry[]) => {
      if (disposed) return
      const relevant = entries.filter((entry) => entry.node_id === nodeId)
      entries.forEach((entry) => { cursorRef.current = Math.max(cursorRef.current, entry.seq) })
      if (!relevant.length) return
      setLogs((current) => {
        const seen = new Set(current.map((entry) => entry.seq))
        const merged = [...current, ...relevant.filter((entry) => !seen.has(entry.seq))]
        return merged.sort((a, b) => a.seq - b.seq)
      })
    }

    const connect = () => {
      if (disposed) return
      if (run.status !== 'running') {
        setConnection('实时流已结束')
        return
      }
      if (source || reconnectTimer !== null) return
      setConnection('正在连接实时日志…')
      const nextSource = new EventSource(runStreamUrl(run.thread_id, cursorRef.current))
      source = nextSource
      nextSource.onopen = () => {
        if (!disposed && source === nextSource) setConnection('SSE 实时连接')
      }
      nextSource.addEventListener('log', (event) => {
        if (disposed || source !== nextSource) return
        try {
          const entry = JSON.parse((event as MessageEvent).data) as RunLogEntry
          if (typeof entry.seq === 'number' && typeof entry.text === 'string') append([entry])
        } catch {
          setError('收到无法解析的日志事件')
        }
      })
      nextSource.onerror = () => {
        if (disposed || source !== nextSource) return
        nextSource.close()
        source = null
        setConnection('连接中断，正在自动重连…')
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null
          connect()
        }, 1500)
      }
    }

    const loadHistory = async () => {
      let afterSeq = 0
      try {
        while (!disposed) {
          const result = await api.runLogs(run.thread_id, { node_id: nodeId, after_seq: afterSeq, limit: 1000 })
          if (disposed) return
          append(result.logs)
          cursorRef.current = Math.max(cursorRef.current, result.next_seq)
          if (result.logs.length < 1000 || result.next_seq <= afterSeq) break
          afterSeq = result.next_seq
        }
      } catch (requestError) {
        if (!disposed) setError(requestError instanceof Error ? requestError.message : '日志加载失败')
      }
      if (!disposed) connect()
    }

    void loadHistory()

    return () => {
      disposed = true
      if (reconnectTimer !== null) clearTimeout(reconnectTimer)
      reconnectTimer = null
      source?.close()
      source = null
    }
  }, [nodeId, run.status, run.thread_id])

  useEffect(() => {
    const terminal = terminalRef.current
    if (terminal && stickToBottomRef.current) terminal.scrollTop = terminal.scrollHeight
  }, [logs])

  return {
    logs,
    connection,
    error,
    terminalRef,
    onScroll: () => {
      const terminal = terminalRef.current
      if (terminal) stickToBottomRef.current = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 32
    },
  }
}

export interface RunNodeWorkbenchDialogProps {
  run: RunStateOut
  workflow: WorkflowSpec
  agents: AgentProfileOut[]
  nodeId: string | null
  onSelectNode: (nodeId: string) => void
  onClose: () => void
  onRefresh: () => Promise<void>
  onToast: (message: string) => void
}

export default function RunNodeWorkbenchDialog({
  run,
  workflow,
  agents,
  nodeId,
  onSelectNode,
  onClose,
  onRefresh,
  onToast,
}: RunNodeWorkbenchDialogProps) {
  const [tab, setTab] = useState<DetailTab>('logs')
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [maximized, setMaximized] = useState(false)
  const [retryOpen, setRetryOpen] = useState(false)
  const [retryAgent, setRetryAgent] = useState('')
  const [retryCwd, setRetryCwd] = useState('')
  const [retryTemplate, setRetryTemplate] = useState('')
  const [feedback, setFeedback] = useState('')
  const [rewindTarget, setRewindTarget] = useState('')
  const [editedOutputs, setEditedOutputs] = useState<Record<string, string>>({})
  const [editingNodes, setEditingNodes] = useState<string[]>([])
  const dialogTitleId = useId()
  const nodeSwitcherId = useId()

  const selectedNode = workflow.nodes.find((node) => node.id === nodeId) ?? null
  const execution = nodeId ? run.values?.nodes?.[nodeId] : undefined
  const interrupts = run.interrupts ?? []
  const interrupt = interrupts.find((candidate) => candidate.node_id === nodeId) ?? null
  const { logs, connection, error: logError, terminalRef, onScroll } = useNodeLogs(run, nodeId)

  useEffect(() => {
    setTab('logs')
    setRetryOpen(false)
    setRetryAgent('')
    setRetryCwd('')
    setRetryTemplate('')
    setError('')
  }, [run.thread_id, nodeId])

  useEffect(() => {
    setFeedback('')
    setEditedOutputs({})
    setEditingNodes([])
    setRewindTarget(interrupt?.rewind_options?.[0]?.id ?? '')
  }, [run.thread_id, interrupt?.node_id]) // 轮询不得覆盖审批人正在编辑的内容

  const snapshots = useMemo(() => Object.values(interrupt?.upstream_outputs ?? {}), [interrupt?.upstream_outputs])
  const retryEnabled = ['error', 'cancelled', 'interrupted'].includes(run.status)

  const refreshAction = async (key: string, action: () => Promise<void>) => {
    if (pending) return
    setPending(key)
    setError('')
    try {
      await action()
      await onRefresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '运行操作失败')
    } finally {
      setPending(null)
    }
  }

  const retry = () => refreshAction('retry', async () => {
    if (!nodeId) throw new Error('没有可重试的断点节点')
    const input: RetryRunIn = { node_id: nodeId }
    if (retryAgent) input.agent_id = retryAgent
    if (retryCwd.trim()) input.cwd = retryCwd.trim()
    if (retryTemplate.trim()) {
      if (selectedNode?.kind === 'shell') input.command = retryTemplate
      else input.prompt_template = retryTemplate
    }
    const result = await api.retry(run.thread_id, input)
    setRetryOpen(false)
    onToast(`已从节点 ${result.retried_node_id} 原地重试`)
  })

  const resume = (action: ApprovalAction) => refreshAction(action, async () => {
    if (action === 'revise' && !rewindTarget) throw new Error('请选择打回目标节点')
    const changedOutputs = Object.fromEntries(Object.entries(editedOutputs).filter(([nodeIdKey, value]) => value !== (interrupt?.upstream_outputs?.[nodeIdKey]?.output ?? '')))
    await api.resume(run.thread_id, {
      action,
      feedback,
      edited_outputs: action === 'approve' ? changedOutputs : {},
      target_node_id: action === 'revise' ? rewindTarget : null,
    })
    setFeedback('')
    setEditedOutputs({})
    setEditingNodes([])
    onToast(action === 'approve' ? '已确认并继续' : action === 'revise' ? `已打回 ${rewindTarget} 重跑` : '运行已中止')
  })

  const onTabKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: number | null = null
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
    if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
    if (event.key === 'Home') next = 0
    if (event.key === 'End') next = tabs.length - 1
    if (next === null) return
    event.preventDefault()
    setTab(tabs[next].id)
    document.getElementById(`run-workbench-tab-${tabs[next].id}`)?.focus()
  }

  if (!nodeId || !selectedNode) {
    return <NativeDialog open={false} onClose={onClose} labelledBy={dialogTitleId}><span className="visually-hidden">运行节点工作台</span></NativeDialog>
  }

  return (
    <NativeDialog
      open
      onClose={onClose}
      labelledBy={dialogTitleId}
      className={`workbench-dialog workbench-dialog--run ${maximized ? 'workbench-dialog--max' : ''}`}
    >
      <header className="workbench-header">
        <span className="workbench-kind flow-node-icon--summary"><ShieldCheck size={16} aria-hidden="true" />运行节点</span>
        <h2 id={dialogTitleId}>{selectedNode.label}</h2>
        <StatusBadge status={run.status} />
        <div className="workbench-actions">
          <label className="visually-hidden" htmlFor={nodeSwitcherId}>切换查看同一次运行的其它节点</label>
          <select
            id={nodeSwitcherId}
            name="node_id"
            className="workbench-node-switcher"
            value={nodeId}
            onChange={(event) => onSelectNode(event.target.value)}
          >
            {workflow.nodes.map((node) => (
              <option key={node.id} value={node.id}>
                {node.label}（{node.id}）{run.values?.nodes?.[node.id] ? ` · ${run.values.nodes[node.id].status ?? '已执行'}` : ''}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="icon-button"
            aria-label={maximized ? '还原窗口' : '最大化窗口'}
            title={maximized ? '还原窗口' : '最大化窗口'}
            onClick={() => setMaximized((value) => !value)}
          >
            {maximized ? <Minimize2 size={16} aria-hidden="true" /> : <Maximize2 size={16} aria-hidden="true" />}
          </button>
          <button type="button" className="icon-button" aria-label="关闭运行节点工作台" title="完成关闭" onClick={onClose}>
            <X size={17} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="workbench-body">
        <p className="workbench-meta">
          {runTask(run)} · <span className="workbench-thread" title={run.thread_id}>{run.thread_id}</span> · 创建于 {formatDateTime(run.created_at)}
          {execution ? ` · 已执行 ${execution.runs ?? 1} 次${execution.edited_by_human ? ' · 人工已修订' : ''}` : ''}
        </p>
        {(error || run.error) && <div className="inline-error" role="alert"><AlertCircle size={14} aria-hidden="true" />{error || run.error}</div>}

        {interrupt && run.status === 'waiting' && (
          <section className="approval-card-v2" aria-labelledby="run-workbench-approval-title">
            <div className="approval-card-title">
              <ShieldCheck size={18} aria-hidden="true" />
              <div>
                <span className="eyebrow">{stageLabel(interrupt.stage)}</span>
                <h3 id="run-workbench-approval-title">{interrupt.label || '人工审批'}</h3>
              </div>
            </div>
            <p>{interrupt.instruction || '请审阅上游产出后决定继续或打回。'}</p>
            {snapshots.map((snapshot) => {
              const editing = editingNodes.includes(snapshot.node_id)
              const value = editedOutputs[snapshot.node_id] ?? snapshot.output ?? ''
              const outputId = `run-workbench-output-${snapshot.node_id}`
              const outputTitleId = `${outputId}-title`
              const hasUnsubmittedChanges = value !== (snapshot.output ?? '')
              return (
                <article className="upstream-snapshot" key={snapshot.node_id}>
                  <div className="snapshot-heading">
                    <strong>{snapshot.label}</strong>
                    <code>{snapshot.node_id}</code>
                    {snapshot.edited_by_human && <span>人工已修订</span>}
                    {hasUnsubmittedChanges && <span>有未提交修改</span>}
                  </div>
                  {interrupt.allow_edit_output && (
                    <button type="button" className="soft-button" onClick={() => {
                      setEditedOutputs((current) => ({ ...current, [snapshot.node_id]: value }))
                      setEditingNodes((current) => editing ? current.filter((id) => id !== snapshot.node_id) : [...current, snapshot.node_id])
                    }}>
                      <Pencil size={13} aria-hidden="true" />{editing ? '收起编辑' : '在线编辑内容'}
                    </button>
                  )}
                  {editing && interrupt.allow_edit_output ? (
                    <>
                      <label htmlFor={outputId}>{snapshot.label}的产出</label>
                      <textarea
                        id={outputId}
                        name={`edited_outputs.${snapshot.node_id}`}
                        className="snapshot-editor"
                        value={value}
                        onChange={(event) => setEditedOutputs((current) => ({ ...current, [snapshot.node_id]: event.target.value }))}
                      />
                    </>
                  ) : (
                    <>
                      <span id={outputTitleId}>{snapshot.label}的产出</span>
                      <pre aria-labelledby={outputTitleId} tabIndex={0}><code>{value || '暂无产出'}</code></pre>
                    </>
                  )}
                  {snapshot.diff && <details><summary>查看 Git Diff</summary><DiffView value={snapshot.diff} /></details>}
                </article>
              )
            })}
            <div className="field">
              <label htmlFor="run-workbench-feedback">审批 / 打回意见</label>
              <textarea
                id="run-workbench-feedback"
                name="feedback"
                value={feedback}
                placeholder="反馈会注入下游或打回节点的 {feedback} 变量"
                onChange={(event) => setFeedback(event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="run-workbench-rewind">打回目标节点（target_node_id）</label>
              <select
                id="run-workbench-rewind"
                name="target_node_id"
                value={rewindTarget}
                onChange={(event) => setRewindTarget(event.target.value)}
              >
                <option value="">请选择目标</option>
                {(interrupt.rewind_options ?? []).map((option) => (
                  <option key={option.id} value={option.id}>{option.label}（{option.id}）</option>
                ))}
              </select>
            </div>
            <button type="button" className="approve-button" disabled={!!pending} onClick={() => void resume('approve')}>
              {pending === 'approve' ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
              确认并继续（approve）
            </button>
            <div className="approval-secondary">
              <button type="button" disabled={!!pending} onClick={() => void resume('revise')}>
                打回重跑（revise）<ChevronRight size={13} aria-hidden="true" />
              </button>
              <button type="button" className="abort-button" disabled={!!pending} onClick={() => void resume('abort')}>
                中止运行（abort）
              </button>
            </div>
          </section>
        )}

        {retryEnabled && (
          <section className="retry-panel" aria-labelledby="run-workbench-retry-title">
            <div className="retry-panel__head">
              <h3 id="run-workbench-retry-title">断点重试</h3>
              <button type="button" className="soft-button" disabled={!!pending} onClick={() => setRetryOpen((value) => !value)}>
                {retryOpen ? '收起重试参数' : '展开临时参数覆盖'}
              </button>
            </div>
            {retryOpen && (
              <>
                <p>仅覆盖本次重试，不修改工作流定义；留空沿用原配置。</p>
                {selectedNode.kind !== 'shell' && (
                  <div className="field">
                    <label htmlFor="run-workbench-retry-agent">临时切换 Agent</label>
                    <select id="run-workbench-retry-agent" name="agent_id" value={retryAgent} onChange={(event) => setRetryAgent(event.target.value)}>
                      <option value="">沿用原配置</option>
                      {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.id}{agent.available ? '' : '（未安装）'}</option>)}
                    </select>
                  </div>
                )}
                <div className="field">
                  <label htmlFor="run-workbench-retry-cwd">临时工作目录</label>
                  <input
                    id="run-workbench-retry-cwd"
                    name="cwd"
                    value={retryCwd}
                    placeholder={selectedNode.cwd || '沿用原目录'}
                    autoCorrect="off"
                    autoCapitalize="off"
                    spellCheck={false}
                    onChange={(event) => setRetryCwd(event.target.value)}
                  />
                </div>
                {selectedNode.kind !== 'gate' && selectedNode.kind !== 'condition' && (
                  <div className="field">
                    <label htmlFor="run-workbench-retry-template">{selectedNode.kind === 'shell' ? '临时 Shell 命令' : '临时 Prompt 模板'}</label>
                    <textarea
                      id="run-workbench-retry-template"
                      name={selectedNode.kind === 'shell' ? 'command' : 'prompt_template'}
                      value={retryTemplate}
                      placeholder={selectedNode.kind === 'shell' ? selectedNode.command || '沿用原命令' : selectedNode.prompt_template || '沿用原 Prompt'}
                      autoCorrect="off"
                      autoCapitalize="off"
                      spellCheck={false}
                      onChange={(event) => setRetryTemplate(event.target.value)}
                    />
                  </div>
                )}
              </>
            )}
            <button type="button" className="modal-submit" disabled={!!pending} onClick={() => void retry()}>
              {pending === 'retry' ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
              原地重试该节点
            </button>
          </section>
        )}

        <section className="node-runtime-detail" aria-labelledby="run-workbench-detail-title">
          <h3 id="run-workbench-detail-title" className="visually-hidden">节点运行详情</h3>
          <div className="detail-tabs" role="tablist" aria-label="节点运行详情">
            {tabs.map((item, index) => (
              <button
                type="button"
                key={item.id}
                id={`run-workbench-tab-${item.id}`}
                role="tab"
                aria-selected={tab === item.id}
                aria-controls="run-workbench-panel"
                tabIndex={tab === item.id ? 0 : -1}
                className={tab === item.id ? 'is-active' : ''}
                onClick={() => setTab(item.id)}
                onKeyDown={(event) => onTabKeyDown(event, index)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="detail-content" id="run-workbench-panel" role="tabpanel" aria-labelledby={`run-workbench-tab-${tab}`}>
            {tab === 'logs' ? (
              <>
                <div className="log-connection" role="status" aria-live="polite"><i aria-hidden="true" />{connection}</div>
                {(logError || error) && <div className="inline-error" role="alert">{logError || error}</div>}
                {logs.length ? (
                  <pre className="log-terminal" tabIndex={0} ref={terminalRef} onScroll={onScroll} aria-label={`${selectedNode.label} 实时终端日志`}>
                    <code>{logs.map((entry) => <span key={entry.seq} className={`log-line log-line--${entry.stream ?? 'stdout'}`}><time>{new Date(entry.ts).toLocaleTimeString('zh-CN', { hour12: false })}</time>{entry.text}{'\n'}</span>)}</code>
                  </pre>
                ) : <EmptyContent>该节点暂无终端日志</EmptyContent>}
              </>
            ) : tab === 'output' ? (
              execution?.output
                ? <pre className="detail-code" tabIndex={0}><code>{execution.output}</code></pre>
                : <EmptyContent>该节点尚未产生结构化输出</EmptyContent>
            ) : (
              execution?.diff
                ? <DiffView value={execution.diff} />
                : <EmptyContent>该节点没有 Git Diff 代码变更</EmptyContent>
            )}
          </div>
        </section>
      </div>
    </NativeDialog>
  )
}

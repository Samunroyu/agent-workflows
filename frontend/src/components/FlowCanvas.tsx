import { useEffect, useMemo, useRef } from 'react'
import type { CSSProperties, DragEvent as ReactDragEvent } from 'react'
import {
  Background,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from '@xyflow/react'
import type { Connection, Edge, Node } from '@xyflow/react'
import { Bot, CircleCheck, GitBranch, Pencil, ShieldCheck, Terminal } from 'lucide-react'
import type {
  EdgeSpec,
  NodeExecutionOut,
  NodeKind,
  NodeSpec,
  RunStateOut,
  WorkflowSpec,
} from '../types'

export type CanvasSelection = { type: 'node' | 'edge'; id: string } | null

export type RuntimeTone = 'idle' | 'running' | 'waiting' | 'error' | 'cancelled' | 'done'

export interface RuntimeDisplay {
  label: string
  tone: RuntimeTone
}

export interface FlowCanvasProps {
  workflow: WorkflowSpec
  mode: 'edit' | 'monitor'
  run?: RunStateOut | null
  selected?: CanvasSelection
  onSelect?: (selection: CanvasSelection) => void
  onNodePositionChange?: (nodeId: string, position: { x: number; y: number }) => void
  onConnect?: (source: string, target: string) => void
  onDeleteNode?: (nodeId: string) => void
  onDeleteEdge?: (edgeId: string) => void
  onDropNode?: (kind: NodeKind, position: { x: number; y: number }) => void
  onOpenNode?: (nodeId: string) => void
  onOpenEdge?: (edgeId: string) => void
  nodeActionLabel?: (node: NodeSpec, runtime: RuntimeDisplay | null) => string | null
}

const NODE_MIME = 'application/x-workflow-node'
const NODE_KINDS: NodeKind[] = ['agent', 'shell', 'gate', 'condition', 'summary']

const palette = {
  edge: 'var(--flow-edge)',
  background: 'var(--flow-grid)',
  node: 'var(--flow-node-bg)',
  border: 'var(--flow-node-border)',
  text: 'var(--flow-node-text)',
  active: 'var(--flow-node-active)',
}

const kindLabels: Record<NodeKind, string> = {
  agent: 'Agent',
  shell: 'Shell',
  gate: '人工卡点',
  condition: '条件判断',
  summary: '汇总',
}

const kindDescriptions: Record<NodeKind, string> = {
  agent: '调用 CLI Agent 执行任务',
  shell: '执行 Shell 命令',
  gate: '人工审批与运行控场',
  condition: '按条件路由执行分支',
  summary: '汇总工作流执行结果',
}

function NodeIcon({ kind }: { kind: NodeKind }) {
  if (kind === 'gate') return <ShieldCheck size={15} aria-hidden="true" />
  if (kind === 'shell') return <Terminal size={15} aria-hidden="true" />
  if (kind === 'condition') return <GitBranch size={15} aria-hidden="true" />
  if (kind === 'summary') return <CircleCheck size={15} aria-hidden="true" />
  return <Bot size={15} aria-hidden="true" />
}

function getPosition(node: NodeSpec, index: number) {
  return {
    x: node.position?.x ?? (index % 4) * 240,
    y: node.position?.y ?? Math.floor(index / 4) * 200 + 40,
  }
}

function getExecutionStatus(execution: NodeExecutionOut): RuntimeDisplay | null {
  const status = execution.status?.toLowerCase()
  if (status === 'running') return { label: '执行中', tone: 'running' }
  if (status === 'waiting') return { label: '等待执行', tone: 'waiting' }
  if (status === 'error' || status === 'failed' || status === 'failure') {
    return { label: '执行失败', tone: 'error' }
  }
  if (status === 'cancelled' || status === 'canceled') return { label: '已取消', tone: 'cancelled' }
  if (status === 'interrupted') return { label: '已中断', tone: 'cancelled' }
  if (status === 'skipped') return { label: '已跳过', tone: 'idle' }
  if (status === 'done' || status === 'success' || status === 'completed' || status === 'finished') {
    return execution.ok === false
      ? { label: '执行失败', tone: 'error' }
      : { label: '已完成', tone: 'done' }
  }
  if (execution.ok === true) return { label: '已完成', tone: 'done' }
  if (execution.ok === false) return { label: '执行失败', tone: 'error' }
  return status ? { label: status, tone: 'idle' } : null
}

function getRuntimeDisplay(nodeId: string, run?: RunStateOut | null): RuntimeDisplay | null {
  if (!run) return null
  if (run.running_nodes?.includes(nodeId)) return { label: '执行中', tone: 'running' }

  if (run.next?.includes(nodeId)) {
    if (run.status === 'waiting') return { label: '等待人工', tone: 'waiting' }
    if (run.status === 'error') return { label: '失败断点', tone: 'error' }
    if (run.status === 'cancelled') return { label: '中止断点', tone: 'cancelled' }
    if (run.status === 'interrupted') return { label: '中断断点', tone: 'cancelled' }
  }

  const execution = run.values?.nodes?.[nodeId]
  if (execution) return getExecutionStatus(execution) ?? { label: '已执行', tone: 'done' }
  return { label: '未执行', tone: 'idle' }
}

function directEdgeId(edge: EdgeSpec, edgeIndex: number) {
  return edge.id ?? `edge-${edgeIndex}-${edge.source}-${edge.target ?? 'unlinked'}`
}

function expandedEdges(workflow: WorkflowSpec, isEdit: boolean): Edge[] {
  return (workflow.edges ?? []).flatMap((edge, edgeIndex) => {
    const running = false
    if (edge.target) {
      const conditional = Boolean(edge.condition) || Boolean(edge.is_default)
      return [{
        id: directEdgeId(edge, edgeIndex),
        source: edge.source,
        target: edge.target,
        label: edge.label || (edge.is_default ? '默认' : undefined),
        type: 'smoothstep',
        animated: running,
        selectable: isEdit,
        focusable: isEdit,
        deletable: isEdit,
        style: {
          stroke: palette.edge,
          strokeWidth: 1.5,
          strokeDasharray: conditional ? '5 5' : undefined,
        },
        labelStyle: { fill: 'var(--flow-edge-label)' },
        labelBgStyle: { fill: 'var(--flow-edge-label-bg)', fillOpacity: 0.85 },
        labelBgPadding: [5, 3] as [number, number],
        markerEnd: { type: MarkerType.ArrowClosed, color: palette.edge, width: 14, height: 14 },
      }]
    }

    const branches = [
      ...(edge.conditions ?? []).map((rule, branchIndex) => ({
        id: `${edge.id ?? `edge-${edgeIndex}`}:condition-${branchIndex}`,
        target: rule.target,
        label: rule.label || undefined,
      })),
      ...(edge.default_target ? [{
        id: `${edge.id ?? `edge-${edgeIndex}`}:default`,
        target: edge.default_target,
        label: '默认',
      }] : []),
    ]

    return branches.map((branch) => ({
      id: branch.id,
      source: edge.source,
      target: branch.target,
      label: branch.label,
      type: 'smoothstep',
      animated: running,
      selectable: isEdit,
      focusable: isEdit,
      deletable: isEdit,
      style: { stroke: palette.edge, strokeWidth: 1.5, strokeDasharray: '5 5' },
      labelStyle: { fill: 'var(--flow-edge-label)' },
      labelBgStyle: { fill: 'var(--flow-edge-label-bg)', fillOpacity: 0.85 },
      labelBgPadding: [5, 3] as [number, number],
      markerEnd: { type: MarkerType.ArrowClosed, color: palette.edge, width: 14, height: 14 },
    }))
  })
}

function FlowCanvasInner({
  workflow,
  mode,
  run,
  selected = null,
  onSelect,
  onNodePositionChange,
  onConnect,
  onDeleteNode,
  onDeleteEdge,
  onDropNode,
  onOpenNode,
  onOpenEdge,
  nodeActionLabel,
}: FlowCanvasProps) {
  const isEdit = mode === 'edit'
  const { fitView, screenToFlowPosition } = useReactFlow()
  const measuredNodes = useRef(new Map<string, { width: number; height: number }>())
  const workflowEdges = useMemo(() => workflow.edges ?? [], [workflow.edges])

  const topologyKey = useMemo(() => [
    workflow.id,
    workflow.entry_point,
    ...workflow.nodes.map((node) => node.id),
    ...workflowEdges.flatMap((edge) => [
      `${edge.source}->${edge.target ?? ''}`,
      ...(edge.conditions ?? []).map((rule) => `${edge.source}->${rule.target}`),
      edge.default_target ? `${edge.source}->${edge.default_target}` : '',
    ]),
  ].join('|'), [workflow.entry_point, workflow.id, workflow.nodes, workflowEdges])

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      void fitView({ padding: 0.14, maxZoom: 1 })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [fitView, topologyKey])

  const nodes = useMemo<Node[]>(() => {
    const hasVirtualStart = workflowEdges.some((edge) => edge.source === 'START')
    const actualNodes = workflow.nodes.filter((node) => node.id !== 'START' && node.id !== 'END')
    const virtualStart: NodeSpec = {
      id: 'START',
      label: '开始',
      kind: 'summary',
      description: '工作流开始',
      position: null,
    }
    const virtualEnd: NodeSpec = {
      id: 'END',
      label: '结束',
      kind: 'summary',
      description: '工作流结束',
      position: null,
    }
    const specs = [
      ...(hasVirtualStart ? [virtualStart] : []),
      ...actualNodes,
      virtualEnd,
    ]
    const positions = new Map(actualNodes.map((node, index) => [node.id, getPosition(node, index)]))
    const actualPositions = [...positions.values()]
    const minX = Math.min(...actualPositions.map((position) => position.x), 0)
    const maxX = Math.max(...actualPositions.map((position) => position.x), 0)
    const averageY = actualPositions.length
      ? actualPositions.reduce((sum, position) => sum + position.y, 0) / actualPositions.length
      : 40
    if (hasVirtualStart) positions.set('START', { x: minX - 240, y: averageY })
    positions.set('END', { x: maxX + 260, y: averageY })

    return specs.map((node) => {
      const position = positions.get(node.id) ?? { x: 0, y: 40 }
      const virtual = node.id === 'START' || node.id === 'END'
      const runtime = getRuntimeDisplay(node.id, run)
      const running = runtime?.tone === 'running'
      const execution = run?.values?.nodes?.[node.id]
      const entry = node.id === workflow.entry_point
      const isSelected = selected?.type === 'node' && selected.id === node.id
      const actionLabel = !virtual && onOpenNode ? nodeActionLabel?.(node, runtime) ?? null : null
      const style: CSSProperties = {
        width: 196,
        borderRadius: 16,
        border: `1px solid ${running || isSelected ? palette.active : palette.border}`,
        background: runtime?.tone === 'done' ? 'var(--flow-node-complete-bg)' : palette.node,
        color: palette.text,
        boxShadow: running ? 'var(--flow-node-active-shadow)' : 'var(--flow-node-shadow)',
        padding: '0.75rem 0.875rem',
        fontWeight: 600,
        textAlign: 'left',
      }
      const stateClasses = [
        'flow-node',
        `flow-node--${node.kind}`,
        runtime ? `flow-node--${runtime.tone}` : '',
        running ? 'flow-node--pulse' : '',
        virtual ? 'flow-node--virtual' : '',
        isSelected ? 'flow-node--selected' : '',
      ].filter(Boolean).join(' ')

      return {
        id: node.id,
        position,
        initialWidth: 196,
        initialHeight: 100,
        measured: measuredNodes.current.get(node.id),
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        className: stateClasses,
        selected: isSelected,
        selectable: true,
        focusable: true,
        draggable: isEdit && !virtual,
        deletable: isEdit && !virtual && workflow.nodes.length > 1,
        style,
        data: {
          label: (
            <div className="flow-node-content" title={node.description || node.label}>
              <div className="flow-node-label">
                <span className={`flow-node-icon flow-node-icon--${node.kind}`}>
                  <NodeIcon kind={node.kind} />
                </span>
                <span className="flow-node-title">{node.label}</span>
                {entry && <span className="flow-node-entry-badge">入口 START</span>}
                {actionLabel && (
                  <button
                    type="button"
                    className="flow-node-action nodrag"
                    aria-label={`${actionLabel}节点 ${node.label}`}
                    onClick={(event) => {
                      event.stopPropagation()
                      onOpenNode?.(node.id)
                    }}
                  >
                    <Pencil size={11} aria-hidden="true" />{actionLabel}
                  </button>
                )}
              </div>
              <p className="flow-node-description">{node.description || kindDescriptions[node.kind]}</p>
              <div className="flow-node-meta">
                <span className={`flow-node-kind flow-node-kind--${node.kind}`}>{kindLabels[node.kind]}</span>
                {runtime && (
                  <span className={`flow-node-status flow-node-status--${runtime.tone}`} role="status">
                    {runtime.label}
                  </span>
                )}
                {execution?.runs !== undefined && (
                  <span className="flow-node-runs">运行 {execution.runs} 次</span>
                )}
                {execution?.edited_by_human && (
                  <span className="flow-node-human-edit">人工已修订</span>
                )}
              </div>
            </div>
          ),
        },
      }
    })
  }, [isEdit, nodeActionLabel, onOpenNode, run, selected, workflow.entry_point, workflow.nodes, workflowEdges])

  const edges = useMemo<Edge[]>(() => expandedEdges(workflow, isEdit).map((edge) => ({
    ...edge,
    animated: Boolean(run?.running_nodes?.includes(edge.source)),
    selected: selected?.type === 'edge' && selected.id === edge.id,
    className: [
      'flow-edge',
      edge.style?.strokeDasharray ? 'flow-edge--conditional' : '',
      run?.running_nodes?.includes(edge.source) ? 'flow-edge--running' : '',
    ].filter(Boolean).join(' '),
  })), [isEdit, run?.running_nodes, selected, workflow])

  function handleConnect(connection: Connection) {
    if (isEdit && connection.source && connection.target) {
      onConnect?.(connection.source, connection.target)
    }
  }

  function handleDragOver(event: ReactDragEvent<HTMLDivElement>) {
    if (!isEdit) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }

  function handleDrop(event: ReactDragEvent<HTMLDivElement>) {
    if (!isEdit) return
    event.preventDefault()
    const value = event.dataTransfer.getData(NODE_MIME)
    if (!NODE_KINDS.includes(value as NodeKind)) return
    const position = screenToFlowPosition({ x: event.clientX, y: event.clientY })
    onDropNode?.(value as NodeKind, position)
  }

  return (
    <div
      className={`flow-canvas flow-canvas--${mode}`}
      aria-label={`${workflow.name} 工作流拓扑图`}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.14, maxZoom: 1 }}
        minZoom={0.25}
        maxZoom={1.4}
        nodesDraggable={isEdit}
        nodesConnectable={isEdit}
        elementsSelectable
        deleteKeyCode={isEdit ? ['Backspace', 'Delete'] : null}
        zoomOnScroll={true}
        onNodesChange={(changes) => {
          changes.forEach((change) => {
            if (change.type === 'dimensions' && change.dimensions) {
              measuredNodes.current.set(change.id, change.dimensions)
            }
          })
        }}
        onNodeClick={(_, node) => onSelect?.({ type: 'node', id: node.id })}
        onNodeDoubleClick={(_, node) => {
          if (node.id === 'START' || node.id === 'END') return
          onOpenNode?.(node.id)
        }}
        onEdgeClick={(_, edge) => {
          if (isEdit) onSelect?.({ type: 'edge', id: edge.id })
        }}
        onEdgeDoubleClick={(_, edge) => {
          if (isEdit) onOpenEdge?.(edge.id)
        }}
        onPaneClick={() => onSelect?.(null)}
        onNodeDragStop={(_, node) => {
          if (isEdit && node.id !== 'START' && node.id !== 'END') {
            onNodePositionChange?.(node.id, node.position)
          }
        }}
        onConnect={handleConnect}
        onNodesDelete={(deletedNodes) => {
          if (!isEdit) return
          deletedNodes.forEach((node) => {
            if (node.id !== 'START' && node.id !== 'END') onDeleteNode?.(node.id)
          })
        }}
        onEdgesDelete={(deletedEdges) => {
          if (!isEdit) return
          deletedEdges.forEach((edge) => onDeleteEdge?.(edge.id))
        }}
      >
        <Background color={palette.background} gap={22} size={1} />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
      <div className="flow-legend" aria-label="画布操作提示">
        {isEdit ? <>
          <span>拖拽移动 · 滚轮缩放</span>
          <span>双击节点 / 连线打开编辑工作台 · Delete 删除</span>
        </> : <>
          <span><i className="legend-dot legend-dot--done" aria-hidden="true" />已执行</span>
          <span><i className="legend-dot legend-dot--active" aria-hidden="true" />执行中</span>
          <span>双击节点打开运行工作台</span>
        </>}
      </div>
    </div>
  )
}

export default function FlowCanvas(props: FlowCanvasProps) {
  return (
    <ReactFlowProvider>
      <FlowCanvasInner {...props} />
    </ReactFlowProvider>
  )
}

import { useMemo } from 'react'
import { Background, Controls, MarkerType, Position, ReactFlow } from '@xyflow/react'
import type { Edge, Node } from '@xyflow/react'
import { Bot, Check, CircleCheck, Flag, GitBranch, Terminal } from 'lucide-react'
import type { CSSProperties } from 'react'
import type { NodeSpec, WorkflowSpec } from '../types'

interface FlowCanvasProps {
  workflow: WorkflowSpec
  activeNodeIds?: string[]
  executedNodeIds?: string[]
}

const palette = { edge: '#b3b9df', bg: '#d4d8ed', node: '#ffffff', border: '#e1e4f2', text: '#464b70', active: '#7a82cf' }

function NodeIcon({ type }: { type: string }) {
  if (type === 'gate') return <GitBranch size={14} />
  if (type === 'shell') return <Terminal size={14} />
  if (type === 'summary') return <CircleCheck size={14} />
  return <Bot size={14} />
}

function getPosition(node: NodeSpec, index: number) {
  return node.position ?? { x: (index % 4) * 220, y: Math.floor(index / 4) * 220 + 40 }
}

export default function FlowCanvas({ workflow, activeNodeIds = [], executedNodeIds = [] }: FlowCanvasProps) {
  const { nodes, edges } = useMemo(() => {
    const needsEnd = workflow.edges.some((edge) => edge.target === 'END' || edge.default_target === 'END' || edge.conditions?.some((rule) => rule.target === 'END'))
    const specs: NodeSpec[] = needsEnd
      ? [...workflow.nodes, { id: 'END', label: '结束', kind: 'summary', position: null }]
      : workflow.nodes
    const positions = new Map(specs.map((node, index) => [node.id, getPosition(node, index)]))

    const nodes: Node[] = specs.map((node, index) => {
      const active = activeNodeIds.includes(node.id)
      const completed = executedNodeIds.includes(node.id)
      const position = positions.get(node.id) ?? getPosition(node, index)
      const outgoing = workflow.edges.find((edge) => edge.source === node.id)
      const primaryTargetId = outgoing?.target ?? outgoing?.conditions?.[0]?.target ?? outgoing?.default_target
      const primaryTarget = primaryTargetId ? positions.get(primaryTargetId) : undefined
      const incoming = workflow.edges.find((edge) => edge.target === node.id || edge.default_target === node.id || edge.conditions?.some((rule) => rule.target === node.id))
      const primarySource = incoming ? positions.get(incoming.source) : undefined
      const sourcePosition = primaryTarget && primaryTarget.y > position.y
        ? Position.Bottom
        : primaryTarget && primaryTarget.x < position.x ? Position.Left : Position.Right
      const targetPosition = primarySource && primarySource.y < position.y
        ? Position.Top
        : primarySource && primarySource.x > position.x ? Position.Right : Position.Left
      const style: CSSProperties = {
        width: 158,
        borderRadius: 16,
        border: `1px solid ${active ? palette.active : completed ? '#a9c8b6' : palette.border}`,
        background: completed ? '#f5faf7' : palette.node,
        color: palette.text,
        boxShadow: active ? `0 7px 24px ${palette.active}30` : '0 3px 12px rgba(50, 60, 85, .04)',
        padding: '12px 14px',
        fontSize: 13,
        fontWeight: 600,
        textAlign: 'left',
      }
      return {
        id: node.id,
        position,
        sourcePosition,
        targetPosition,
        data: {
          label: (
            <div className="flow-node-label" title={node.description || node.label}>
              <span className={`flow-node-icon flow-node-icon--${node.kind}`}><NodeIcon type={node.kind} /></span>
              <span>{node.label}</span>
              {completed && <Check size={13} className="ml-auto text-emerald-600" />}
              {active && <Flag size={12} className="ml-auto text-indigo-400" />}
            </div>
          ),
        },
        style,
      }
    })

    const edges: Edge[] = workflow.edges.flatMap((edge, edgeIndex) => {
      if (edge.target) {
        return [{
          id: `${edge.source}-${edge.target}-${edgeIndex}`,
          source: edge.source,
          target: edge.target,
          label: edge.label || undefined,
          type: 'smoothstep',
          animated: activeNodeIds.includes(edge.source),
          style: { stroke: palette.edge, strokeWidth: 1.5 },
          markerEnd: { type: MarkerType.ArrowClosed, color: palette.edge, width: 14, height: 14 },
        }]
      }
      const branches = [
        ...(edge.conditions ?? []).map((rule) => ({ target: rule.target, label: rule.label })),
        ...(edge.default_target ? [{ target: edge.default_target, label: 'default' }] : []),
      ]
      return branches.map((branch, branchIndex) => ({
        id: `${edge.source}-${branch.target}-${edgeIndex}-${branchIndex}`,
        source: edge.source,
        target: branch.target,
        label: branch.label || undefined,
        type: 'smoothstep',
        animated: activeNodeIds.includes(edge.source),
        style: { stroke: palette.edge, strokeWidth: 1.5, strokeDasharray: '5 5' },
        labelStyle: { fill: '#9298b5', fontSize: 9 },
        labelBgStyle: { fill: '#ffffff', fillOpacity: 0.85 },
        labelBgPadding: [5, 3] as [number, number],
        markerEnd: { type: MarkerType.ArrowClosed, color: palette.edge, width: 14, height: 14 },
      }))
    })
    return { nodes, edges }
  }, [activeNodeIds, executedNodeIds, workflow])

  return (
    <div className="flow-canvas" aria-label={`${workflow.name} 工作流拓扑图`}>
      <ReactFlow
        key={workflow.id}
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.14, maxZoom: 1 }}
        minZoom={0.25}
        maxZoom={1.4}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        zoomOnScroll={false}
        preventScrolling={false}
      >
        <Background color={palette.bg} gap={22} size={1} />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
      <div className="flow-legend">
        <span><i className="legend-dot legend-dot--done" />已执行</span>
        <span><i className="legend-dot legend-dot--active" />当前节点</span>
      </div>
    </div>
  )
}

import { useMemo, useReducer, useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ReactFlow, Background, Controls, MiniMap, Handle, Position,
  type Node, type Edge, type NodeProps, MarkerType,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from 'dagre'
import {
  Waypoints, RefreshCw, ChevronRight, Database as DatabaseIcon,
  Table as TableIcon, AlertCircle, CheckCircle2, MinusCircle,
  Info, ExternalLink, Loader2, Filter, X, Pin, ScanLine, ArrowRight,
  Layers,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  lineageApi, workflowsApi, assetsApi,
  type LineageNode, type LineageGraph, type WorkflowHighlight,
  type LineageDatabaseCard, type LineageEdge,
} from '../api/client'
import { useConnection } from '../ConnectionContext'
import { useTheme } from '../ThemeContext'

// ─────────────────────────────────────────────────────────────────────────
// View-mode reducer
// ─────────────────────────────────────────────────────────────────────────

type ViewMode =
  | { level: 'picker' }
  | { level: 'database'; database: string }
  | { level: 'schema'; database: string; schema: string }
  | { level: 'table'; database: string; schema: string; table: string; hops: number }

type State = {
  view: ViewMode
  workflowId: string | null
  focusFqn: string | null
  refreshKey: number
  showOrphans: boolean
}

type Action =
  | { type: 'goto'; view: ViewMode }
  | { type: 'pickWorkflow'; workflowId: string | null }
  | { type: 'focus'; fqn: string | null }
  | { type: 'toggleOrphans' }
  | { type: 'bumpRefresh' }

const initialState: State = {
  view: { level: 'picker' }, workflowId: null, focusFqn: null,
  refreshKey: 0, showOrphans: false,
}

function reducer(s: State, a: Action): State {
  switch (a.type) {
    case 'goto': return { ...s, view: a.view, focusFqn: null }
    case 'pickWorkflow': return { ...s, workflowId: a.workflowId }
    case 'focus': return { ...s, focusFqn: a.fqn }
    case 'toggleOrphans': return { ...s, showOrphans: !s.showOrphans }
    case 'bumpRefresh': return { ...s, refreshKey: s.refreshKey + 1 }
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Palette
// ─────────────────────────────────────────────────────────────────────────

function healthColor(score: number | null | undefined, dark: boolean): string {
  if (score == null) return dark ? '#4b5563' : '#9ca3af'
  if (score >= 0.9) return '#10b981'
  if (score >= 0.7) return '#f59e0b'
  return '#ef4444'
}
function healthLabel(score: number | null | undefined): string {
  if (score == null) return '—'
  return (score * 100).toFixed(0) + '%'
}
function kindBorderColor(kind: string): string {
  if (kind === 'stage' || kind === 'external_location') return '#a855f7'
  if (kind === 'dynamic_table') return '#0ea5e9'
  if (kind === 'view' || kind === 'materialized_view') return '#f59e0b'
  if (kind === 'external_table' || kind === 'iceberg_table') return '#14b8a6'
  return '#10b981'
}
function fmtBytes(b: number | null | undefined): string {
  if (!b) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0; let v = b
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`
}
function fmtRows(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return String(n)
}

// Deterministic schema color — hashes the schema name to one of N palette
// slots. Keeps a schema's tables visually grouped without container boxes.
const SCHEMA_PALETTE = [
  '#3b82f6', '#8b5cf6', '#ec4899', '#f97316', '#eab308',
  '#22c55e', '#14b8a6', '#06b6d4', '#a855f7', '#f43f5e',
]
function schemaColor(schema: string | undefined): string {
  if (!schema) return '#64748b'
  let h = 0
  for (let i = 0; i < schema.length; i++) h = (h * 31 + schema.charCodeAt(i)) >>> 0
  return SCHEMA_PALETTE[h % SCHEMA_PALETTE.length]
}

// ─────────────────────────────────────────────────────────────────────────
// Table node
// ─────────────────────────────────────────────────────────────────────────

type NodeData = LineageNode & {
  highlighted?: boolean
  dimmed?: boolean
  isOrigin?: boolean
  isFocus?: boolean
  dark: boolean
  onNodeClick?: () => void
  onNodeHover?: (n: LineageNode | null) => void
}

const TABLE_W = 200
const TABLE_H = 82

function TableNode({ data }: NodeProps) {
  const d = data as unknown as NodeData
  const openCt = d.open_findings ?? 0
  const score = d.health_score
  const kindCol = kindBorderColor(d.kind)
  const scCol = schemaColor(d.schema)
  const bg = d.dark ? 'bg-gray-800' : 'bg-white'
  const text = d.dark ? 'text-gray-100' : 'text-gray-900'
  const dim = d.dimmed ? 'opacity-25' : ''
  const focused = d.isFocus
    ? (d.dark ? 'ring-2 ring-primary-300 ring-offset-2 ring-offset-gray-900' : 'ring-2 ring-primary-600 ring-offset-2')
    : d.highlighted
      ? (d.dark ? 'ring-2 ring-primary-400/60' : 'ring-2 ring-primary-500/70')
      : ''
  const ghosted = d.is_external ? 'italic' : ''

  return (
    <div
      className={`${bg} ${text} ${dim} ${focused} ${ghosted} border border-gray-300 dark:border-gray-600 rounded-md shadow-sm relative cursor-pointer hover:shadow-lg transition-all overflow-hidden`}
      onClick={(e) => { e.stopPropagation(); d.onNodeClick?.() }}
      onMouseEnter={() => d.onNodeHover?.(d)}
      onMouseLeave={() => d.onNodeHover?.(null)}
      style={{ width: TABLE_W, height: TABLE_H, borderLeftWidth: 4, borderLeftColor: kindCol }}
    >
      <Handle type="target" position={Position.Left} className="!bg-primary-500 !w-2.5 !h-2.5 !border-2 !border-white dark:!border-gray-800" />

      {/* Schema color band at the top */}
      <div className="absolute top-0 left-0 right-0 h-1" style={{ background: scCol }} />

      {d.isOrigin && (
        <Pin className="absolute top-1 right-1 w-3.5 h-3.5 text-primary-500 fill-primary-500" />
      )}

      <div className="px-2.5 pt-2 pb-1.5 h-full flex flex-col">
        <div className="flex items-center gap-1.5 min-w-0">
          <TableIcon className="w-3.5 h-3.5 flex-shrink-0" style={{ color: kindCol }} />
          <span className="text-xs font-semibold truncate">{d.label}</span>
        </div>
        <div className="text-[10px] text-gray-500 dark:text-gray-400 truncate" style={{ color: scCol }}>
          {d.schema}
        </div>
        <div className="mt-auto flex items-center gap-1.5 pt-1">
          {score != null ? (
            <span
              className="inline-flex items-center px-1 py-0 rounded text-white text-[9px] font-bold"
              style={{ backgroundColor: healthColor(score, d.dark) }}
            >
              {healthLabel(score)}
            </span>
          ) : (
            <span className="inline-flex items-center px-1 py-0 rounded text-[9px] font-medium text-gray-400 dark:text-gray-500 border border-gray-300 dark:border-gray-600">
              no runs
            </span>
          )}
          {openCt > 0 && (
            <span className="inline-flex items-center gap-0.5 text-red-500 font-semibold text-[9px]">
              <AlertCircle className="w-2.5 h-2.5" /> {openCt}
            </span>
          )}
          {(d.rules_run ?? 0) > 0 && (
            <span className="text-[9px] text-gray-400 dark:text-gray-500">
              {d.rules_run} rule{d.rules_run === 1 ? '' : 's'}
            </span>
          )}
          <span className="ml-auto text-[9px] uppercase tracking-wide text-gray-400 dark:text-gray-500">
            {d.kind === 'table' ? '' : d.kind.replace('_', ' ')}
          </span>
        </div>
      </div>

      <Handle type="source" position={Position.Right} className="!bg-primary-500 !w-2.5 !h-2.5 !border-2 !border-white dark:!border-gray-800" />
    </div>
  )
}

const nodeTypes = {
  table: TableNode,
  view: TableNode,
  materialized_view: TableNode,
  dynamic_table: TableNode,
  external_table: TableNode,
  iceberg_table: TableNode,
  stage: TableNode,
  external_location: TableNode,
  semantic_view: TableNode,
}

// ─────────────────────────────────────────────────────────────────────────
// Dagre layout — LR (left-to-right) DAG. This is what's used for every
// horizontal flow diagram (Airflow, Prefect, dbt lineage, GitHub Actions).
// ─────────────────────────────────────────────────────────────────────────

function computeDagreLayout(
  tables: LineageNode[], edges: LineageEdge[],
): { positions: Record<string, { x: number; y: number }> } {
  const g = new dagre.graphlib.Graph({ multigraph: false, compound: false })
  g.setGraph({
    rankdir: 'LR',
    ranksep: 80,   // horizontal gap between columns
    nodesep: 24,   // vertical gap between siblings
    edgesep: 12,
    marginx: 20, marginy: 20,
    ranker: 'network-simplex',
  })
  g.setDefaultEdgeLabel(() => ({}))

  const tableIds = new Set(tables.map(t => t.id))
  for (const t of tables) {
    g.setNode(t.id, { width: TABLE_W, height: TABLE_H })
  }
  for (const e of edges) {
    if (!tableIds.has(e.source) || !tableIds.has(e.target)) continue
    if (e.source === e.target) continue
    g.setEdge(e.source, e.target)
  }

  dagre.layout(g)

  const positions: Record<string, { x: number; y: number }> = {}
  for (const id of g.nodes()) {
    const n = g.node(id)
    positions[id] = {
      x: n.x - TABLE_W / 2,
      y: n.y - TABLE_H / 2,
    }
  }
  return { positions }
}

// ─────────────────────────────────────────────────────────────────────────
// Path traversal for focus mode
// ─────────────────────────────────────────────────────────────────────────

function computePath(edges: LineageEdge[], focus: string) {
  const upstream = new Set<string>()
  const downstream = new Set<string>()
  const edgeSet = new Set<string>()
  const upBy: Record<string, LineageEdge[]> = {}
  const downBy: Record<string, LineageEdge[]> = {}
  for (const e of edges) {
    ;(upBy[e.target] ??= []).push(e)
    ;(downBy[e.source] ??= []).push(e)
  }
  const upQ = [focus]; const seenUp = new Set([focus])
  while (upQ.length) {
    const cur = upQ.shift()!
    for (const e of upBy[cur] || []) {
      edgeSet.add(e.id)
      if (!seenUp.has(e.source)) { seenUp.add(e.source); upstream.add(e.source); upQ.push(e.source) }
    }
  }
  const dnQ = [focus]; const seenDn = new Set([focus])
  while (dnQ.length) {
    const cur = dnQ.shift()!
    for (const e of downBy[cur] || []) {
      edgeSet.add(e.id)
      if (!seenDn.has(e.target)) { seenDn.add(e.target); downstream.add(e.target); dnQ.push(e.target) }
    }
  }
  return { nodes: new Set<string>([focus, ...upstream, ...downstream]), edges: edgeSet, upstream, downstream }
}

// ─────────────────────────────────────────────────────────────────────────
// Detail side panel
// ─────────────────────────────────────────────────────────────────────────

function DetailPanel({ node, edge, onClose }: {
  node: LineageNode | null; edge: LineageEdge | null; onClose: () => void
}) {
  if (!node && !edge) return null
  return (
    <div className="absolute top-3 right-3 w-72 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg p-3 z-20 pointer-events-auto">
      <button onClick={onClose} className="absolute top-2 right-2 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500" aria-label="Close">
        <X className="w-3.5 h-3.5" />
      </button>
      {node && <NodeDetails node={node} />}
      {edge && <EdgeDetails edge={edge} />}
    </div>
  )
}

function NodeDetails({ node }: { node: LineageNode }) {
  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center gap-1.5 pr-4">
        <TableIcon className="w-4 h-4" style={{ color: kindBorderColor(node.kind) }} />
        <div className="font-semibold text-sm text-gray-900 dark:text-gray-100 break-all">{node.label}</div>
      </div>
      <div className="text-[10px] text-gray-500 dark:text-gray-400 break-all">
        {node.database}.{node.schema}.{node.table}
      </div>
      <div className="text-[11px] uppercase tracking-wide text-gray-400 dark:text-gray-500">
        {node.kind.replace('_', ' ')}
      </div>
      <div className="space-y-1.5 pt-1">
        <Row label="Health">
          {node.health_score != null ? (
            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-white text-[10px] font-bold"
                  style={{ backgroundColor: healthColor(node.health_score, false) }}>
              {healthLabel(node.health_score)}
            </span>
          ) : <span className="text-gray-400">no runs</span>}
        </Row>
        <Row label="Open findings">
          {(node.open_findings ?? 0) > 0
            ? <span className="text-red-500 font-semibold">{node.open_findings}</span>
            : <span className="text-gray-400">0</span>}
        </Row>
        <Row label="Rules executed">
          <span className="text-gray-700 dark:text-gray-200">{node.rules_run ?? 0}</span>
        </Row>
        <Row label="Rows">
          <span className="text-gray-700 dark:text-gray-200">{fmtRows(node.row_count)}</span>
        </Row>
        <Row label="Size">
          <span className="text-gray-700 dark:text-gray-200">{fmtBytes(node.size_bytes)}</span>
        </Row>
        {node.last_scanned_at && (
          <Row label="Last scan">
            <span className="text-gray-700 dark:text-gray-200">{new Date(node.last_scanned_at).toLocaleString()}</span>
          </Row>
        )}
      </div>
    </div>
  )
}

function EdgeDetails({ edge }: { edge: LineageEdge }) {
  return (
    <div className="space-y-2 text-xs pr-4">
      <div className="font-semibold text-sm text-gray-900 dark:text-gray-100 flex items-center gap-1">
        <ArrowRight className="w-4 h-4 text-primary-500" /> Data flow
      </div>
      <div className="text-[10px] text-gray-500 dark:text-gray-400 break-all">
        <div><span className="text-gray-400">from</span> {edge.source}</div>
        <div><span className="text-gray-400">to</span> {edge.target}</div>
      </div>
      <div className="space-y-1.5 pt-1">
        {edge.edge_type && <Row label="Type"><span className="text-gray-700 dark:text-gray-200">{edge.edge_type}</span></Row>}
        {edge.discovery_source && <Row label="Source"><span className="text-gray-700 dark:text-gray-200">{edge.discovery_source}</span></Row>}
      </div>
    </div>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-gray-500 dark:text-gray-400">{label}</span>
      {children}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────
// DB picker card
// ─────────────────────────────────────────────────────────────────────────

function DatabaseCard({ card, onOpen, dark }: {
  card: LineageDatabaseCard; onOpen: () => void; dark: boolean;
}) {
  return (
    <button
      onClick={onOpen}
      className="text-left bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 hover:border-primary-400 dark:hover:border-primary-500 rounded-xl p-4 transition-colors"
    >
      <div className="flex items-center gap-2">
        <DatabaseIcon className="w-5 h-5 text-primary-500" />
        <span className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{card.database}</span>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
        <div><div className="text-gray-400 dark:text-gray-500">Schemas</div><div className="text-gray-900 dark:text-gray-100 font-semibold">{card.schema_count}</div></div>
        <div><div className="text-gray-400 dark:text-gray-500">Objects</div><div className="text-gray-900 dark:text-gray-100 font-semibold">{card.table_count}</div></div>
        <div><div className="text-gray-400 dark:text-gray-500">Lineage edges</div><div className="text-gray-900 dark:text-gray-100 font-semibold">{card.edge_count}</div></div>
        <div>
          <div className="text-gray-400 dark:text-gray-500">Avg. health</div>
          <div className="font-semibold" style={{ color: healthColor(card.avg_health_score, dark) }}>
            {healthLabel(card.avg_health_score)}
          </div>
        </div>
      </div>
      <div className="mt-3 flex items-center justify-between text-[10px] text-gray-500 dark:text-gray-400">
        <span>{card.last_refreshed_at ? `refreshed ${formatRelative(card.last_refreshed_at)}` : 'not refreshed'}</span>
        {card.discovery_method_used && (
          <span className={`px-1.5 py-0.5 rounded-full ${
            card.discovery_method_used === 'get_lineage'
              ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300'
              : 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300'
          }`}>
            {card.discovery_method_used === 'get_lineage' ? 'GET_LINEAGE' : 'OBJ_DEPS'}
          </span>
        )}
      </div>
    </button>
  )
}

function EmptyState({ icon: Icon, title, body, action }: {
  icon: any; title: string; body: string; action?: React.ReactNode;
}) {
  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-10 text-center">
      <Icon className="w-10 h-10 mx-auto text-gray-400 dark:text-gray-500" />
      <p className="mt-3 text-sm font-semibold text-gray-900 dark:text-gray-100">{title}</p>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{body}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────────

export default function Lineage() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const { selected, selectedId } = useConnection()
  const { resolved } = useTheme()
  const dark = resolved === 'dark'
  const qc = useQueryClient()

  const isSnowflake = selected?.type === 'snowflake'

  const [hoveredNode, setHoveredNode] = useState<LineageNode | null>(null)
  const [hoveredEdge, setHoveredEdge] = useState<LineageEdge | null>(null)

  const statusQ = useQuery({
    queryKey: ['lineage', 'status', selectedId, state.refreshKey],
    queryFn: () => lineageApi.status(selectedId).then(r => r.data),
    enabled: !!selectedId && isSnowflake,
  })

  const graphQ = useQuery<LineageGraph>({
    queryKey: ['lineage', 'graph', selectedId, state.view, state.refreshKey],
    queryFn: () => {
      const v = state.view
      if (v.level === 'picker') return lineageApi.allDatabases(selectedId).then(r => r.data)
      if (v.level === 'database') return lineageApi.database(v.database, selectedId).then(r => r.data)
      if (v.level === 'schema') return lineageApi.schema(v.database, v.schema, selectedId).then(r => r.data)
      return lineageApi.table(v.database, v.schema, v.table, v.hops, selectedId).then(r => r.data)
    },
    enabled: !!selectedId && isSnowflake,
  })

  // Cascade dropdowns — schemas + tables scoped to the currently-selected DB.
  const currentDb = state.view.level !== 'picker' ? state.view.database : null
  const currentSchema = state.view.level === 'schema' || state.view.level === 'table' ? state.view.schema : null
  const currentTable = state.view.level === 'table' ? state.view.table : null

  // Every database the connection's role can see (live SHOW DATABASES via
  // assetsApi.discoverDatabases) — NOT just already-indexed ones. Same source
  // the Agent Workflow page uses so the two dropdowns stay consistent.
  const databasesQ = useQuery({
    queryKey: ['lineage-databases', selectedId],
    queryFn: () => assetsApi.discoverDatabases(selectedId).then(r => r.data),
    enabled: !!selectedId && isSnowflake,
  })

  const schemasQ = useQuery({
    queryKey: ['lineage-schemas', selectedId, currentDb],
    queryFn: () => assetsApi.discoverSchemas(currentDb!, selectedId).then(r => r.data),
    enabled: !!selectedId && isSnowflake && !!currentDb,
  })
  const tablesQ = useQuery({
    queryKey: ['lineage-tables', selectedId, currentDb, currentSchema],
    queryFn: () => assetsApi.discoverTables(currentDb!, currentSchema!, selectedId).then(r => r.data),
    enabled: !!selectedId && isSnowflake && !!currentDb && !!currentSchema,
  })

  const workflowsQ = useQuery({
    queryKey: ['workflows'],
    queryFn: () => workflowsApi.list().then(r => r.data),
    enabled: isSnowflake,
  })

  const highlightQ = useQuery<WorkflowHighlight>({
    queryKey: ['lineage', 'highlight', selectedId, state.workflowId],
    queryFn: () => lineageApi.workflowHighlight(state.workflowId!, selectedId).then(r => r.data),
    enabled: !!state.workflowId && !!selectedId && isSnowflake,
  })

  const refreshMut = useMutation({
    mutationFn: async (database: string) => (await lineageApi.refresh(database, selectedId)).data,
    onSuccess: () => { dispatch({ type: 'bumpRefresh' }); qc.invalidateQueries({ queryKey: ['lineage'] }) },
  })

  const highlightSets = useMemo(() => {
    const nodeFqns = new Set<string>()
    let originFqn: string | null = null
    if (highlightQ.data) {
      for (const n of highlightQ.data.nodes) nodeFqns.add(n.fqn)
      if (highlightQ.data.origin) {
        const o = highlightQ.data.origin
        originFqn = `${o.database}.${o.schema}.${o.table}`
      }
    }
    return { nodeFqns, originFqn }
  }, [highlightQ.data])
  const filterActive = state.workflowId != null && highlightQ.data != null

  const focusPath = useMemo(() => {
    if (!state.focusFqn || !graphQ.data) return null
    return computePath(graphQ.data.edges, state.focusFqn)
  }, [state.focusFqn, graphQ.data])

  // ── Split tables into "connected" (in a lineage edge) vs "orphans" ────
  const { connectedTables, orphanTables, tableEdges } = useMemo(() => {
    const g = graphQ.data
    if (!g) return { connectedTables: [] as LineageNode[], orphanTables: [] as LineageNode[], tableEdges: [] as LineageEdge[] }
    const allTables = g.nodes.filter(n => n.kind !== 'database' && n.kind !== 'schema')
    const inEdgeSet = new Set<string>()
    for (const e of g.edges) { inEdgeSet.add(e.source); inEdgeSet.add(e.target) }
    const connected: LineageNode[] = []
    const orphans: LineageNode[] = []
    for (const t of allTables) {
      if (inEdgeSet.has(t.id)) connected.push(t)
      else orphans.push(t)
    }
    return { connectedTables: connected, orphanTables: orphans, tableEdges: g.edges }
  }, [graphQ.data])

  // ── Build reactflow nodes/edges from the DAG (no nested containers) ──
  const { rfNodes, rfEdges } = useMemo(() => {
    if (state.view.level === 'picker') return { rfNodes: [] as Node[], rfEdges: [] as Edge[] }
    const tables = connectedTables
    if (!tables.length) return { rfNodes: [] as Node[], rfEdges: [] as Edge[] }

    const { positions } = computeDagreLayout(tables, tableEdges)

    const fp = focusPath
    const wf = filterActive
    const isHighlighted = (n: LineageNode): boolean => {
      if (fp) return fp.nodes.has(n.id)
      if (wf) return highlightSets.nodeFqns.has(n.id)
      return false
    }
    const isDimmed = (n: LineageNode): boolean => {
      if (fp) return !fp.nodes.has(n.id)
      if (wf) return !highlightSets.nodeFqns.has(n.id)
      return false
    }

    const rfNodes: Node[] = tables.map(n => ({
      id: n.id,
      type: 'table',
      position: positions[n.id] ?? { x: 0, y: 0 },
      data: {
        ...n,
        highlighted: isHighlighted(n),
        dimmed: isDimmed(n),
        isOrigin: n.id === highlightSets.originFqn,
        isFocus: n.id === state.focusFqn,
        dark,
        onNodeClick: () => dispatch({ type: 'focus', fqn: state.focusFqn === n.id ? null : n.id }),
        onNodeHover: (hn: LineageNode | null) => { setHoveredNode(hn); setHoveredEdge(null) },
      } satisfies NodeData,
      draggable: true,
    }))

    const rfEdges: Edge[] = tableEdges.map(e => {
      const onPath = fp?.edges.has(e.id) ?? false
      const wfBoth = wf && highlightSets.nodeFqns.has(e.source) && highlightSets.nodeFqns.has(e.target)
      const isThick = onPath || wfBoth
      const isDimmedE = (fp && !onPath) || (wf && !wfBoth && !fp)
      const stroke = isThick
        ? (dark ? '#60a5fa' : '#2563eb')
        : (dark ? '#94a3b8' : '#64748b')
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        type: 'smoothstep',
        animated: isThick,
        markerEnd: { type: MarkerType.ArrowClosed, color: stroke, width: 18, height: 18 },
        style: {
          stroke,
          strokeWidth: isThick ? 2.5 : 1.5,
          opacity: isDimmedE ? 0.12 : 0.9,
        },
      }
    })

    return { rfNodes, rfEdges }
  }, [state.view.level, connectedTables, tableEdges, dark, filterActive, highlightSets, focusPath, state.focusFqn])

  const activeDatabase = state.view.level !== 'picker' ? state.view.database : null
  const method = graphQ.data?.discovery_method ?? statusQ.data?.databases[0]?.discovery_method_used ?? null
  const lastRefreshed = graphQ.data?.last_refreshed_at
  const cards = graphQ.data?.databases ?? []
  const pickerEmpty = state.view.level === 'picker' && cards.length === 0

  useEffect(() => { if (isSnowflake) statusQ.refetch() }, [selectedId, isSnowflake]) // eslint-disable-line

  if (!selected) {
    return (
      <div className="space-y-6">
        <PageHeader />
        <EmptyState icon={Waypoints} title="No connection selected" body="Pick a data source from the sidebar to view its lineage." />
      </div>
    )
  }
  if (!isSnowflake) {
    return (
      <div className="space-y-6">
        <PageHeader />
        <EmptyState
          icon={Waypoints}
          title="Snowflake connections only"
          body="Lineage discovery uses Snowflake's GET_LINEAGE function and OBJECT_DEPENDENCIES view."
          action={<Link to="/connections" className="inline-flex items-center gap-1.5 text-sm text-primary-600 hover:text-primary-700 dark:text-primary-400"><ExternalLink className="w-3.5 h-3.5" /> Manage connections</Link>}
        />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <PageHeader />

      {/* Toolbar */}
      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-3 flex flex-wrap items-center gap-3">
        {/* DB / Schema / Table cascade dropdowns — the same pattern as AgentWorkflow */}
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {/* Database */}
          <div className="flex items-center gap-1.5">
            <DatabaseIcon className="w-4 h-4 text-gray-400" />
            <select
              value={currentDb ?? ''}
              onChange={e => {
                const v = e.target.value
                dispatch({ type: 'goto', view: v ? { level: 'database', database: v } : { level: 'picker' } })
              }}
              className="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1.5 bg-white dark:bg-gray-700 dark:text-gray-100 min-w-[170px]"
            >
              <option value="">
                {databasesQ.isFetching ? 'Loading…' : '— Database —'}
              </option>
              {(() => {
                // Prefer the live SHOW DATABASES list (all DBs role can see).
                // Fall back to already-indexed ones only if the discover call
                // fails, so we never show an empty dropdown.
                const live = databasesQ.data?.databases ?? []
                const fallback = cards.length > 0
                  ? cards.map(c => c.database)
                  : (statusQ.data?.databases.map(d => d.database) ?? [])
                const list = live.length > 0 ? live : fallback
                return list.map(db => <option key={db} value={db}>{db}</option>)
              })()}
            </select>
          </div>

          {/* Schema — only enabled when a DB is chosen */}
          <div className="flex items-center gap-1.5">
            <Layers className="w-4 h-4 text-gray-400" />
            <select
              value={currentSchema ?? ''}
              disabled={!currentDb || schemasQ.isFetching}
              onChange={e => {
                const v = e.target.value
                if (!currentDb) return
                dispatch({ type: 'goto', view: v ? { level: 'schema', database: currentDb, schema: v } : { level: 'database', database: currentDb } })
              }}
              className="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1.5 bg-white dark:bg-gray-700 dark:text-gray-100 min-w-[160px] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <option value="">
                {!currentDb ? '— Schema —' : schemasQ.isFetching ? 'Loading…' : 'All schemas'}
              </option>
              {schemasQ.data?.schemas.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>

          {/* Table — only enabled when a schema is chosen */}
          <div className="flex items-center gap-1.5">
            <TableIcon className="w-4 h-4 text-gray-400" />
            <select
              value={currentTable ?? ''}
              disabled={!currentSchema || tablesQ.isFetching}
              onChange={e => {
                const v = e.target.value
                if (!currentDb || !currentSchema) return
                dispatch({
                  type: 'goto',
                  view: v
                    ? { level: 'table', database: currentDb, schema: currentSchema, table: v, hops: 3 }
                    : { level: 'schema', database: currentDb, schema: currentSchema },
                })
              }}
              className="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1.5 bg-white dark:bg-gray-700 dark:text-gray-100 min-w-[160px] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <option value="">
                {!currentSchema ? '— Table —' : tablesQ.isFetching ? 'Loading…' : 'All tables in schema'}
              </option>
              {tablesQ.data?.tables.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>

          {/* Reset filters — visible whenever any filter is active */}
          {state.view.level !== 'picker' && (
            <button
              onClick={() => dispatch({ type: 'goto', view: { level: 'picker' } })}
              className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 border border-gray-200 dark:border-gray-600"
              title="Clear all filters"
            >
              <X className="w-3.5 h-3.5" /> Clear
            </button>
          )}

          {/* Focus chip when tracing a specific table */}
          {state.focusFqn && (
            <span className="inline-flex items-center gap-1 px-2 py-1 rounded bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 font-medium text-xs">
              tracing: {state.focusFqn.split('.').slice(-1)[0]}
              <button onClick={() => dispatch({ type: 'focus', fqn: null })} className="ml-1"><X className="w-3 h-3" /></button>
            </span>
          )}
        </div>

        <div className="h-6 w-px bg-gray-200 dark:bg-gray-700" />

        <div className="flex items-center gap-2">
          <Filter className="w-4 h-4 text-gray-400" />
          <select
            value={state.workflowId ?? ''}
            onChange={e => dispatch({ type: 'pickWorkflow', workflowId: e.target.value || null })}
            className="text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1.5 bg-white dark:bg-gray-700 dark:text-gray-100 min-w-[220px]"
          >
            <option value="">Highlight a saved workflow…</option>
            {workflowsQ.data?.map(w => <option key={w.id} value={w.id}>{w.label}</option>)}
          </select>
          {state.workflowId && (
            <button onClick={() => dispatch({ type: 'pickWorkflow', workflowId: null })} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400">
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

        <div className="ml-auto flex items-center gap-3 text-xs">
          {method && (
            <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full font-medium ${
              method === 'get_lineage'
                ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300'
                : 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300'
            }`}>
              {method === 'get_lineage'
                ? <><CheckCircle2 className="w-3.5 h-3.5" /> GET_LINEAGE</>
                : <><MinusCircle className="w-3.5 h-3.5" /> OBJECT_DEPENDENCIES</>}
            </span>
          )}
          {lastRefreshed && <span className="text-gray-500 dark:text-gray-400" title={lastRefreshed}>Updated {formatRelative(lastRefreshed)}</span>}
          {activeDatabase && (
            <button
              onClick={() => refreshMut.mutate(activeDatabase)}
              disabled={refreshMut.isPending}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary-600 hover:bg-primary-700 text-white text-sm font-medium disabled:opacity-60"
            >
              {refreshMut.isPending ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Refreshing…</> : <><RefreshCw className="w-3.5 h-3.5" /> Refresh {activeDatabase}</>}
            </button>
          )}
        </div>
      </div>

      {filterActive && (highlightQ.data?.unmatched_targets?.length ?? 0) > 0 && (
        <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800/50 rounded-lg px-3 py-2 text-xs text-amber-800 dark:text-amber-200 flex items-center gap-2">
          <Info className="w-3.5 h-3.5 flex-shrink-0" />
          {highlightQ.data!.unmatched_targets.length} workflow table{highlightQ.data!.unmatched_targets.length === 1 ? '' : 's'} not in current lineage — refresh their database.
        </div>
      )}
      {refreshMut.isError && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800/50 rounded-lg px-3 py-2 text-sm text-red-700 dark:text-red-300">
          Refresh failed: {(refreshMut.error as any)?.response?.data?.detail ?? String(refreshMut.error)}
        </div>
      )}

      {graphQ.isLoading ? (
        <div className="h-[720px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl flex items-center justify-center">
          <Loader2 className="w-6 h-6 animate-spin text-primary-500" />
        </div>
      ) : state.view.level === 'picker' && (databasesQ.data?.databases.length ?? 0) === 0 && pickerEmpty ? (
        <EmptyState
          icon={ScanLine}
          title="No databases found"
          body="The current Snowflake connection can't see any databases. Check the connection's role has USAGE on at least one database."
        />
      ) : state.view.level === 'picker' ? (
        <div>
          <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">Pick a database to see its full flow diagram. Databases already indexed show live metrics; the rest open on first click.</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {(() => {
              // Every DB the role can see (SHOW DATABASES) ∪ every already-
              // indexed one. Merge so un-indexed DBs still get a card.
              const indexedByName = new Map(cards.map(c => [c.database, c]))
              const liveNames = databasesQ.data?.databases ?? []
              const allNames = Array.from(new Set([...liveNames, ...cards.map(c => c.database)])).sort()
              return allNames.map(name => {
                const c: LineageDatabaseCard = indexedByName.get(name) ?? {
                  database: name,
                  schema_count: 0, table_count: 0, edge_count: 0,
                  avg_health_score: null,
                  last_refreshed_at: null,
                  discovery_method_used: null,
                  last_status: null,
                }
                return (
                  <DatabaseCard
                    key={name}
                    card={c} dark={dark}
                    onOpen={() => dispatch({ type: 'goto', view: { level: 'database', database: name } })}
                  />
                )
              })
            })()}
          </div>
        </div>
      ) : connectedTables.length === 0 && orphanTables.length === 0 ? (
        // No catalog rows either — first open. Backend auto-crawls, but if it
        // failed or returned nothing at all, show a prominent CTA.
        <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-10 text-center">
          <Waypoints className="w-12 h-12 mx-auto text-primary-400" />
          <p className="mt-3 text-lg font-semibold text-gray-900 dark:text-gray-100">
            {activeDatabase} has no lineage yet
          </p>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400 max-w-md mx-auto">
            Discover lineage runs <code className="text-xs px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-700">SNOWFLAKE.CORE.GET_LINEAGE</code> across every table + view here, then caches the edges so this graph loads instantly next time.
          </p>
          <button
            onClick={() => activeDatabase && refreshMut.mutate(activeDatabase)}
            disabled={refreshMut.isPending || !activeDatabase}
            className="mt-5 inline-flex items-center gap-2 px-5 py-2.5 rounded-lg bg-primary-600 hover:bg-primary-700 text-white text-sm font-semibold disabled:opacity-60"
          >
            {refreshMut.isPending
              ? <><Loader2 className="w-4 h-4 animate-spin" /> Running GET_LINEAGE…</>
              : <><ScanLine className="w-4 h-4" /> Discover lineage for {activeDatabase}</>}
          </button>
        </div>
      ) : connectedTables.length === 0 ? (
        <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-6">
          <div className="text-center">
            <Waypoints className="w-10 h-10 mx-auto text-gray-400 dark:text-gray-500" />
            <p className="mt-3 text-lg font-semibold text-gray-900 dark:text-gray-100">
              No data flows found in {activeDatabase}
            </p>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400 max-w-lg mx-auto">
              {method === 'get_lineage'
                ? `${orphanTables.length} object${orphanTables.length === 1 ? '' : 's'} indexed, but Snowflake's lineage tracking didn't find any incoming or outgoing data flows between them. Try re-discovering to pick up new activity.`
                : method === 'object_dependencies'
                  ? `${orphanTables.length} object${orphanTables.length === 1 ? '' : 's'} indexed. Using the OBJECT_DEPENDENCIES fallback (view→table refs only) — grant VIEW LINEAGE + USAGE on SNOWFLAKE.CORE.GET_LINEAGE for COPY INTO / CTAS / dynamic-table coverage.`
                  : `${orphanTables.length} object${orphanTables.length === 1 ? '' : 's'} indexed. Click below to run lineage discovery.`}
            </p>
            <button
              onClick={() => activeDatabase && refreshMut.mutate(activeDatabase)}
              disabled={refreshMut.isPending || !activeDatabase}
              className="mt-4 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary-600 hover:bg-primary-700 text-white text-sm font-semibold disabled:opacity-60"
            >
              {refreshMut.isPending
                ? <><Loader2 className="w-4 h-4 animate-spin" /> Running GET_LINEAGE…</>
                : <><RefreshCw className="w-4 h-4" /> Re-discover lineage</>}
            </button>
          </div>
          <OrphanGrid tables={orphanTables} onHover={(n) => { setHoveredNode(n); setHoveredEdge(null) }} onClick={() => {}} dark={dark} />
        </div>
      ) : (
        <>
          <div className="relative h-[720px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl overflow-hidden">
            <ReactFlow
              nodes={rfNodes}
              edges={rfEdges}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{ padding: 0.15, maxZoom: 1.2 }}
              proOptions={{ hideAttribution: true }}
              minZoom={0.05}
              maxZoom={2}
              nodesDraggable
              onPaneClick={() => { setHoveredNode(null); setHoveredEdge(null); dispatch({ type: 'focus', fqn: null }) }}
              onEdgeMouseEnter={(_, edge) => {
                const raw = graphQ.data!.edges.find(e => e.id === edge.id)
                setHoveredEdge(raw ?? null); setHoveredNode(null)
              }}
              onEdgeMouseLeave={() => setHoveredEdge(null)}
            >
              <Background color={dark ? '#374151' : '#e5e7eb'} gap={16} />
              <MiniMap
                className="!bg-gray-100 dark:!bg-gray-900"
                nodeColor={(n) => {
                  const d = n.data as unknown as NodeData
                  return healthColor(d.health_score, dark)
                }}
                maskColor={dark ? 'rgba(31,41,55,0.7)' : 'rgba(249,250,251,0.7)'}
              />
              <Controls className="!bg-white dark:!bg-gray-700 !border-gray-200 dark:!border-gray-600" />
            </ReactFlow>

            <DetailPanel
              node={hoveredNode} edge={hoveredEdge}
              onClose={() => { setHoveredNode(null); setHoveredEdge(null) }}
            />

            {state.focusFqn && (
              <div className="absolute bottom-3 left-3 text-[11px] bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg px-2 py-1 text-gray-700 dark:text-gray-200 shadow z-10">
                Tracing {state.focusFqn.split('.').slice(-1)[0]} · click empty space to reset
              </div>
            )}
          </div>

          {/* Orphans (no lineage edges) — collapsed below the main graph */}
          {orphanTables.length > 0 && (
            <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl overflow-hidden">
              <button
                onClick={() => dispatch({ type: 'toggleOrphans' })}
                className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-700/40 text-left"
              >
                <div className="flex items-center gap-2 text-sm">
                  <Layers className="w-4 h-4 text-gray-400" />
                  <span className="font-semibold text-gray-900 dark:text-gray-100">
                    {orphanTables.length} object{orphanTables.length === 1 ? '' : 's'} without discovered lineage
                  </span>
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    (tables that aren't the source or target of any known edge)
                  </span>
                </div>
                <ChevronRight className={`w-4 h-4 text-gray-400 transition-transform ${state.showOrphans ? 'rotate-90' : ''}`} />
              </button>
              {state.showOrphans && (
                <div className="p-3 border-t border-gray-200 dark:border-gray-700">
                  <OrphanGrid tables={orphanTables} onHover={(n) => { setHoveredNode(n); setHoveredEdge(null) }} onClick={() => {}} dark={dark} />
                </div>
              )}
            </div>
          )}
        </>
      )}

      {state.view.level !== 'picker' && (
        <div className="flex flex-wrap items-center gap-4 text-xs text-gray-500 dark:text-gray-400 px-2">
          <LegendSwatch color="#10b981" label="Healthy (≥90%)" />
          <LegendSwatch color="#f59e0b" label="Degraded (70–90%)" />
          <LegendSwatch color="#ef4444" label="Failing (<70%)" />
          <LegendSwatch color={dark ? '#4b5563' : '#9ca3af'} label="No runs" />
          <span className="w-px h-4 bg-gray-200 dark:bg-gray-700" />
          <LegendBorder color="#10b981" label="Table" />
          <LegendBorder color="#f59e0b" label="View" />
          <LegendBorder color="#0ea5e9" label="Dynamic table" />
          <LegendBorder color="#a855f7" label="Stage / external" />
          <span className="w-px h-4 bg-gray-200 dark:bg-gray-700" />
          <span>Top-of-card colour = schema · click a table to trace its path.</span>
        </div>
      )}
    </div>
  )
}

// Simple grid for orphaned tables (no lineage). Reused for the "no edges yet"
// state and the collapsed "objects without lineage" section.
function OrphanGrid({ tables, onHover, onClick, dark }: {
  tables: LineageNode[]; onHover: (n: LineageNode | null) => void; onClick: (n: LineageNode) => void; dark: boolean;
}) {
  // Group by schema so at least the layout is readable.
  const bySchema = new Map<string, LineageNode[]>()
  for (const t of tables) {
    const k = t.schema || '(no schema)'
    if (!bySchema.has(k)) bySchema.set(k, [])
    bySchema.get(k)!.push(t)
  }
  const schemas = Array.from(bySchema.keys()).sort()
  return (
    <div className="space-y-4 mt-3">
      {schemas.map(sc => (
        <div key={sc}>
          <div className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-2 flex items-center gap-1.5">
            <span className="inline-block w-2 h-2 rounded-full" style={{ background: schemaColor(sc) }} />
            {sc} <span className="text-gray-400 font-normal">({bySchema.get(sc)!.length})</span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-2">
            {bySchema.get(sc)!.slice().sort((a, b) => a.label.localeCompare(b.label)).map(t => (
              <div
                key={t.id}
                onMouseEnter={() => onHover(t)}
                onMouseLeave={() => onHover(null)}
                onClick={() => onClick(t)}
                className="text-left bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 rounded-md p-2 cursor-pointer hover:border-primary-400"
                style={{ borderLeftWidth: 3, borderLeftColor: kindBorderColor(t.kind) }}
              >
                <div className="text-xs font-semibold text-gray-900 dark:text-gray-100 truncate">{t.label}</div>
                <div className="flex items-center gap-1.5 mt-0.5">
                  {t.health_score != null && (
                    <span className="inline-flex items-center px-1 py-0 rounded text-white text-[9px] font-bold"
                          style={{ backgroundColor: healthColor(t.health_score, dark) }}>
                      {healthLabel(t.health_score)}
                    </span>
                  )}
                  {(t.open_findings ?? 0) > 0 && (
                    <span className="inline-flex items-center gap-0.5 text-red-500 font-semibold text-[9px]">
                      <AlertCircle className="w-2.5 h-2.5" /> {t.open_findings}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function PageHeader() {
  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
        <Waypoints className="w-6 h-6 text-primary-500" /> Data Lineage
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
        Pick a database to see how data flows through it — tables laid out left-to-right in dependency order. Click a table to trace its full upstream and downstream path. Hover for health, findings, and rule details.
      </p>
    </div>
  )
}

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block w-3 h-3 rounded-full" style={{ backgroundColor: color }} />
      {label}
    </span>
  )
}
function LegendBorder({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block w-3 h-3 rounded-sm bg-gray-100 dark:bg-gray-800 border border-gray-300 dark:border-gray-600" style={{ borderLeftWidth: 3, borderLeftColor: color }} />
      {label}
    </span>
  )
}

function formatRelative(iso: string): string {
  try {
    const then = new Date(iso).getTime()
    const now = Date.now()
    const diffSec = Math.max(0, (now - then) / 1000)
    if (diffSec < 60) return 'just now'
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`
    return `${Math.floor(diffSec / 86400)}d ago`
  } catch {
    return iso
  }
}

import { useState, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { assetsApi, agentRunsApi, findingsApi } from '../api/client'
import type { AgentTask, RuleReviewEntry } from '../api/client'
import { useConnection } from '../ConnectionContext'
import {
  GitBranch, Database, BrainCircuit, Shield, AlertCircle,
  Wrench, CheckCircle2, Loader2, ChevronDown, ChevronRight,
  AlertTriangle, ArrowRight, Clock, Play, ExternalLink,
  RefreshCw, Sparkles, Network, BarChart3,
  BookmarkPlus, X,
} from 'lucide-react'

// ── Pipeline definition ───────────────────────────────────────────────────────

const AGENTS = [
  {
    name: 'coordinator',
    label: 'Coordinator',
    icon: GitBranch,
    desc: 'Validates table exists in Snowflake',
    parallel: false,
  },
  {
    name: 'metadata_agent',
    label: 'Metadata',
    icon: Database,
    desc: 'Fetches columns, types, sample data',
    parallel: true,   // runs in parallel with rules_fetch_agent
    parallelGroup: 'A',
  },
  {
    name: 'rules_fetch_agent',
    label: 'Rules Fetch',
    icon: Shield,
    desc: 'Loads all active quality rules',
    parallel: true,
    parallelGroup: 'A',
  },
  {
    name: 'relationship_discovery_agent',
    label: 'Relationships',
    icon: Network,
    desc: 'Finds and verifies cross-table FK relationships (cached per schema)',
    parallel: true,
    parallelGroup: 'A',
  },
  {
    name: 'profiling_agent',
    label: 'Profiling',
    icon: BarChart3,
    desc: 'Profiles data — stats, anomalies & deterministic signals (uniqueness, freshness, closed sets) for smarter rules',
    parallel: true,
    parallelGroup: 'A',
  },
  {
    name: 'rule_intelligence_agent',
    label: 'Rule Intelligence',
    icon: BrainCircuit,
    desc: 'Deterministic + Claude-proposed rules, tunes severity',
    parallel: false,
  },
  {
    name: 'findings_agent',
    label: 'Findings',
    icon: AlertCircle,
    desc: 'Runs selected rules, creates all findings',
    parallel: false,
  },
  {
    name: 'fix_issues',
    label: 'Fix Issues',
    icon: Wrench,
    desc: 'Developer applies SQL fixes',
    parallel: false,
    uiOnly: true,
  },
  {
    name: 'verification_agent',
    label: 'Verify',
    icon: CheckCircle2,
    desc: 'Re-scans Snowflake, auto-completes when resolved',
    parallel: false,
  },
] as const

function getAgentDef(name: typeof AGENTS[number]['name']): typeof AGENTS[number] {
  return AGENTS.find(a => a.name === name)!
}

function sourceBadge(source: RuleReviewEntry['source']) {
  if (source !== 'deterministic') return null
  return (
    <span className="text-xs bg-teal-100 text-teal-700 px-1.5 py-0.5 rounded font-medium"
      title="Objective fact verified against live data — not an LLM guess">
      Auto-detected
    </span>
  )
}

type RunStatus = 'pending' | 'running' | 'awaiting_rule_review' | 'awaiting_fixes' | 'completed' | 'failed'

function isPolling(status: RunStatus) {
  // 'pending' MUST be here: a freshly-created run is always 'pending' for the
  // moment before the background coordinator flips it to 'running'. If the UI
  // catches that first 'pending' and pending isn't polled, refetchInterval
  // returns false and the view freezes on 'pending' forever even though the
  // backend advances normally.
  return status === 'pending' || status === 'running' ||
         status === 'awaiting_rule_review' || status === 'awaiting_fixes'
}

function fixIssuesStatus(runStatus: RunStatus): string {
  // A run only reaches 'completed' once verification confirms all findings are
  // resolved, so green here means "all issues fixed". While issues remain
  // (awaiting_fixes) the node is an actionable 'active' link; otherwise pending.
  if (runStatus === 'completed') return 'completed'
  if (runStatus === 'awaiting_fixes') return 'active'
  return 'pending'
}

function nodeBorderColor(status: string) {
  switch (status) {
    case 'running':   return 'border-blue-400 dark:border-blue-500 bg-blue-50 dark:bg-blue-900/60'
    case 'completed': return 'border-green-400 dark:border-green-500 bg-green-50 dark:bg-green-900/60'
    // 'partial' = verification ran but issues remain — amber, never green.
    case 'partial':   return 'border-amber-400 dark:border-amber-500 bg-amber-50 dark:bg-amber-900/50'
    case 'failed':    return 'border-red-400 dark:border-red-500 bg-red-50 dark:bg-red-900/60'
    case 'skipped':   return 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 opacity-50'
    case 'active':    return 'border-primary-400 dark:border-primary-400 bg-primary-50 dark:bg-primary-900/60'
    default:          return 'border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800'
  }
}

function nodeIconColor(status: string) {
  switch (status) {
    case 'running':   return 'text-blue-500'
    case 'completed': return 'text-green-600'
    case 'partial':   return 'text-amber-600 dark:text-amber-400'
    case 'failed':    return 'text-red-500'
    case 'active':    return 'text-primary-600'
    default:          return 'text-gray-400 dark:text-gray-500'
  }
}

function statusBadge(status: string, partialRemaining?: number) {
  switch (status) {
    case 'running':
      return <span className="flex items-center gap-1 text-blue-700 dark:text-blue-300 text-xs font-medium"><Loader2 className="w-3 h-3 animate-spin" />Running</span>
    case 'completed':
      return <span className="flex items-center gap-1 text-green-700 dark:text-green-300 text-xs font-medium"><CheckCircle2 className="w-3 h-3" />Done</span>
    case 'partial':
      return <span className="flex items-center gap-1 text-amber-700 dark:text-amber-300 text-xs font-medium"><AlertTriangle className="w-3 h-3" />{partialRemaining != null ? `Partial — ${partialRemaining} left` : 'Partial'}</span>
    case 'failed':
      return <span className="flex items-center gap-1 text-red-700 dark:text-red-300 text-xs font-medium"><AlertTriangle className="w-3 h-3" />Failed</span>
    case 'skipped':
      return <span className="text-gray-400 dark:text-gray-500 text-xs">Skipped</span>
    case 'active':
      return <span className="flex items-center gap-1 text-primary-700 dark:text-primary-300 text-xs font-medium"><ArrowRight className="w-3 h-3" />Ready</span>
    default:
      return <span className="text-gray-400 dark:text-gray-400 text-xs">Waiting</span>
  }
}

function formatDuration(s: number | null) {
  if (s === null) return null
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${(s % 60).toFixed(0)}s`
}

// ── Agent Node ────────────────────────────────────────────────────────────────

function AgentNode({
  agentDef, task, isLast, runStatus, scanId, navigate,
}: {
  agentDef: typeof AGENTS[number]
  task: AgentTask | undefined
  isLast: boolean
  runStatus: RunStatus
  scanId: string | null
  navigate: (to: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const Icon = agentDef.icon
  const isFixNode = 'uiOnly' in agentDef && agentDef.uiOnly

  let status: string
  if (isFixNode) {
    status = fixIssuesStatus(runStatus)
  } else {
    status = task?.status ?? 'pending'
  }

  // Verification ran but issues remain → show amber "Partial", never green. The
  // backend leaves the task 'completed' (so the result banner still renders), so
  // we derive 'partial' here from its output.remaining.
  const verifyRemaining = agentDef.name === 'verification_agent' ? (task?.output?.remaining as number | undefined) : undefined
  if (status === 'completed' && verifyRemaining != null && verifyRemaining > 0) {
    status = 'partial'
  }

  const duration    = task ? formatDuration(task.duration_seconds ?? null) : null
  const liveProgress= task?.output?.progress as string | undefined
  const hasLogs     = !isFixNode && task?.output && Object.keys(task.output).length > 0

  return (
    <div className="flex items-start gap-0 min-w-0">
      <div className="flex flex-col items-center flex-1 min-w-0">
        <div
          className={`w-full border-2 rounded-xl p-3 transition-all ${nodeBorderColor(status)} ${
            hasLogs ? 'cursor-pointer' : ''
          } ${isFixNode && (status === 'active' || status === 'completed') ? 'cursor-pointer hover:shadow-md' : ''}`}
          onClick={() => {
            if (isFixNode && scanId && (status === 'active' || status === 'completed')) {
              // Active → jump to the open issues; completed → show the (resolved) set.
              navigate(status === 'completed'
                ? `/findings?scan_id=${scanId}`
                : `/findings?scan_id=${scanId}&status=detected`)
            } else if (hasLogs) {
              setExpanded(e => !e)
            }
          }}
        >
          <div className="flex items-center justify-between mb-1.5">
            <div className="flex items-center gap-2">
              <Icon className={`w-4 h-4 flex-shrink-0 ${nodeIconColor(status)}`} />
              <span className="font-semibold text-xs text-gray-900 dark:text-gray-100 truncate">{agentDef.label}</span>
            </div>
            {hasLogs && (
              expanded
                ? <ChevronDown className="w-3 h-3 text-gray-400 dark:text-gray-400 flex-shrink-0" />
                : <ChevronRight className="w-3 h-3 text-gray-400 dark:text-gray-400 flex-shrink-0" />
            )}
            {isFixNode && (status === 'active' || status === 'completed') && (
              <ExternalLink className={`w-3 h-3 flex-shrink-0 ${status === 'completed' ? 'text-green-500' : 'text-primary-500'}`} />
            )}
          </div>

          <div className="flex items-center justify-between gap-1 flex-wrap">
            {statusBadge(status, verifyRemaining)}
            {duration && (
              <span className="flex items-center gap-0.5 text-xs text-gray-400 dark:text-gray-400">
                <Clock className="w-2.5 h-2.5" />{duration}
              </span>
            )}
          </div>

          {isFixNode && status === 'active' && scanId && (
            <p className="mt-1.5 text-xs text-primary-700 dark:text-primary-300 font-medium">Go to Findings →</p>
          )}
          {status === 'running' && liveProgress && (
            <p className="mt-1.5 text-xs text-blue-700 dark:text-blue-300 font-medium truncate">{liveProgress}</p>
          )}
          {status === 'failed' && task?.error_message && (
            <p className="mt-1.5 text-xs text-red-600 dark:text-red-300 truncate" title={task.error_message}>
              {task.error_message}
            </p>
          )}
        </div>

        {expanded && hasLogs && (
          <div className="w-full mt-1.5 p-2.5 bg-gray-900 rounded-lg text-xs font-mono text-green-400 max-h-56 overflow-y-auto">
            {Object.entries(task!.output!).map(([k, v]) => (
              <div key={k} className="py-0.5">
                <span className="text-gray-500 dark:text-gray-300">{k}: </span>
                <span>{typeof v === 'object' ? JSON.stringify(v, null, 0) : String(v)}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {!isLast && (
        <div className="flex items-center mt-4 mx-0.5 flex-shrink-0">
          <ArrowRight className="w-4 h-4 text-gray-300" />
        </div>
      )}
    </div>
  )
}

// ── Parallel group wrapper ────────────────────────────────────────────────────

function ParallelGroup({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-0 min-w-0">
      <div className="flex flex-col items-center flex-1 min-w-0">
        <div className="w-full border-2 border-dashed border-gray-200 dark:border-gray-700 rounded-xl p-2 bg-gray-50 dark:bg-gray-900/50">
          <p className="text-xs text-gray-400 dark:text-gray-400 font-medium mb-2 text-center">parallel</p>
          <div className="flex items-start gap-2">
            {children}
          </div>
        </div>
      </div>
      <div className="flex items-center mt-8 mx-0.5 flex-shrink-0">
        <ArrowRight className="w-4 h-4 text-gray-300" />
      </div>
    </div>
  )
}

// ── Batch progress strip ──────────────────────────────────────────────────────

function batchRunTone(status: RunStatus) {
  switch (status) {
    case 'completed':            return 'border-green-300 dark:border-green-500/40 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-300'
    case 'failed':               return 'border-red-300 dark:border-red-500/40 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300'
    case 'running':              return 'border-blue-300 dark:border-blue-500/40 bg-blue-50 dark:bg-blue-950/40 text-blue-800 dark:text-blue-300'
    case 'awaiting_rule_review': return 'border-purple-300 dark:border-purple-500/40 bg-purple-50 dark:bg-purple-950/40 text-purple-800 dark:text-purple-300'
    case 'awaiting_fixes':       return 'border-primary-300 dark:border-primary-500/40 bg-primary-50 dark:bg-primary-950/40 text-primary-800 dark:text-primary-300'
    default:                     return 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-500 dark:text-gray-300'
  }
}

// ── Main page ─────────────────────────────────────────────────────────────────

function usePersistedId(storageKey: string) {
  const [id, setId] = useState<string | null>(() => {
    try { return localStorage.getItem(storageKey) } catch { return null }
  })
  const set = (v: string | null) => {
    setId(v)
    try {
      if (v) localStorage.setItem(storageKey, v)
      else localStorage.removeItem(storageKey)
    } catch {}
  }
  return [id, set] as const
}

type WorkflowScope = 'table' | 'schema' | 'database'

const SCOPE_OPTIONS: { value: WorkflowScope; label: string; hint: string }[] = [
  { value: 'table',    label: 'Single Table', hint: 'One table' },
  { value: 'schema',   label: 'Whole Schema', hint: 'All tables in a schema' },
  { value: 'database', label: 'Whole Database', hint: 'Every table, all schemas' },
]

export default function AgentWorkflow() {
  const navigate       = useNavigate()
  const [searchParams]  = useSearchParams()
  const queryClient    = useQueryClient()
  const { selectedId: connId } = useConnection()

  const [scope,            setScope]            = useState<WorkflowScope>('table')
  const [selectedDatabase, setSelectedDatabase] = useState('')
  const [selectedSchema,   setSelectedSchema]   = useState('')
  const [selectedTable,    setSelectedTable]     = useState('')
  const [activeRunId,      setActiveRunId]       = usePersistedId('dq_active_run_id')
  const [activeBatchId,    setActiveBatchId]     = usePersistedId('dq_active_batch_id')
  const [collapsed,        setCollapsed]         = useState(false)
  // Local editable copy of instance review state — initialized from server on pause
  const [reviewActive,  setReviewActive]  = useState<RuleReviewEntry[]>([])
  const [reviewSkipped, setReviewSkipped] = useState<RuleReviewEntry[]>([])
  // Library definitions with no instance on this table (neither existing nor
  // newly proposed). Surfaced so the reviewer can discover applicable checks
  // that Claude ignored. Activation is stubbed for now — clicking prompts a
  // toast; the target/threshold modal + real create-instance wiring lands in
  // the next round.
  type UnusedLibraryEntry = {
    definition_id: string
    name: string
    description: string
    category?: string
    template_shape?: string | null
    check_kind?: string | null
    default_severity?: string
  }
  const [reviewUnusedLibrary, setReviewUnusedLibrary] = useState<UnusedLibraryEntry[]>([])
  const [editingRule,   setEditingRule]   = useState<string | null>(null) // instance_id being edited
  const [editForm,      setEditForm]      = useState<Partial<RuleReviewEntry>>({})
  const [selectedActiveIds,  setSelectedActiveIds]  = useState<Set<string>>(new Set())
  const [selectedSkippedIds, setSelectedSkippedIds] = useState<Set<string>>(new Set())
  const [approvedIds,        setApprovedIds]        = useState<Set<string>>(new Set()) // AI rules explicitly approved by the user
  const [saveWfOpen,    setSaveWfOpen]    = useState(false)
  const [saveWfLabel,   setSaveWfLabel]   = useState('')
  const [saveWfDesc,    setSaveWfDesc]    = useState('')

  const { data: databases } = useQuery({
    queryKey: ['databases', connId],
    queryFn: () => assetsApi.discoverDatabases(connId).then(r => r.data),
    staleTime: 5 * 60 * 1000,
  })
  const { data: schemas, isFetching: schemasFetching, isError: schemasError } = useQuery({
    queryKey: ['schemas', connId, selectedDatabase],
    queryFn: () => assetsApi.discoverSchemas(selectedDatabase, connId).then(r => r.data),
    enabled: !!selectedDatabase,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })
  const { data: tables, isFetching: tablesFetching, isError: tablesError } = useQuery({
    queryKey: ['tables', connId, selectedDatabase, selectedSchema],
    queryFn: () => assetsApi.discoverTables(selectedDatabase, selectedSchema, connId).then(r => r.data),
    enabled: !!selectedDatabase && !!selectedSchema,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })

  const { data: activeRun, error: activeRunError } = useQuery({
    queryKey: ['agent-run', activeRunId],
    queryFn: () => agentRunsApi.get(activeRunId!).then(r => r.data),
    enabled: !!activeRunId,
    // Don't retry a missing run — a stale id persisted in localStorage (e.g. a
    // run that was later deleted) would otherwise 404 forever and leave the
    // page blank with no nodes. We self-heal below instead.
    retry: false,
    refetchInterval: (query) => {
      const s = query.state.data?.status as RunStatus | undefined
      return s && isPolling(s) ? 2000 : false
    },
  })

  // If navigated here with ?run_id=... (e.g. from Run History), load that run
  useEffect(() => {
    const runIdParam = searchParams.get('run_id')
    if (runIdParam && runIdParam !== activeRunId) {
      setActiveRunId(runIdParam)
      setCollapsed(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  // Self-heal: if the persisted active run can't be loaded (deleted / 404),
  // clear the stale id so the UI falls back to the start screen instead of
  // appearing stuck with no pipeline nodes.
  useEffect(() => {
    if (activeRunId && activeRunError) {
      setActiveRunId(null)
      setActiveBatchId(null)
    }
  }, [activeRunId, activeRunError])

  // Batch progress — polls while any run in the batch is still working
  const { data: activeBatch } = useQuery({
    queryKey: ['agent-batch', activeBatchId],
    queryFn: () => agentRunsApi.getBatch(activeBatchId!).then(r => r.data),
    enabled: !!activeBatchId,
    retry: false,   // stale/deleted batch id shouldn't hang the view
    refetchInterval: (query) => {
      const runs = query.state.data?.runs ?? []
      const anyActive = runs.some(r => r.status === 'pending' || r.status === 'running')
      return anyActive ? 3000 : false
    },
  })
  const isBatch = !!activeBatch && activeBatch.total > 1

  const runStatus    = (activeRun?.status ?? 'pending') as RunStatus
  const isRunning    = runStatus === 'running'
  const isReviewing  = runStatus === 'awaiting_rule_review'
  const isAwaiting   = runStatus === 'awaiting_fixes'
  const isCompleted  = runStatus === 'completed'
  const isFailed     = runStatus === 'failed'

  const startMutation = useMutation({
    mutationFn: (data: { scope: WorkflowScope; database: string; schema_name?: string; table?: string; connection_id?: string | null }) =>
      agentRunsApi.startBatch(data).then(r => r.data),
    onSuccess: (batch) => {
      // Focus the first run; track the batch when it spans multiple tables
      const firstId = batch.runs[0]?.id ?? null
      setActiveRunId(firstId)
      setActiveBatchId(batch.total > 1 ? batch.batch_id : null)
      setCollapsed(false)
      queryClient.invalidateQueries({ queryKey: ['agent-runs'] })
      // Kick the run query immediately so the pipeline starts polling from the
      // pending state right away (don't wait for the first refetchInterval tick).
      if (firstId) queryClient.invalidateQueries({ queryKey: ['agent-run', firstId] })
    },
  })

  const verifyMutation = useMutation({
    mutationFn: (runId: string) => agentRunsApi.verify(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-run', activeRunId] })
      const iv = setInterval(() => queryClient.invalidateQueries({ queryKey: ['agent-run', activeRunId] }), 2000)
      setTimeout(() => clearInterval(iv), 15000)
    },
  })

  const saveReviewMutation = useMutation({
    mutationFn: (data: { active: RuleReviewEntry[]; skipped: RuleReviewEntry[] }) =>
      agentRunsApi.reviewRules(activeRunId!, data).then(r => r.data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['agent-run', activeRunId] }),
  })

  // Bulk actions operate on local review state only (same as single-item
  // reject/activate) — they're persisted together on "Run Pipeline", not
  // sent to the server immediately. This avoids clobbering any unsaved
  // single-item edits with a server round-trip.

  const runPipelineMutation = useMutation({
    mutationFn: () => agentRunsApi.runPipeline(activeRunId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-run', activeRunId] })
      const iv = setInterval(() => queryClient.invalidateQueries({ queryKey: ['agent-run', activeRunId] }), 2000)
      setTimeout(() => clearInterval(iv), 60000)
    },
  })

  const saveWorkflowMutation = useMutation({
    // Build patterns server-side from the run's active rule instances. Works for
    // every run type (AI pipeline, saved-workflow template, scheduled) — it does
    // not depend on instance_review_state, which only exists for runs that
    // paused for review. Origin (scope/db/schema/table) is captured server-side.
    mutationFn: () =>
      agentRunsApi.saveAsWorkflow(activeRunId!, {
        label: saveWfLabel.trim(),
        description: saveWfDesc.trim(),
        created_by: '',
      }),
    onSuccess: () => {
      setSaveWfOpen(false)
      setSaveWfLabel('')
      setSaveWfDesc('')
      queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
  })

  // Sync server instance_review_state → local editable state when run enters review
  const serverReviewState = activeRun?.instance_review_state
  // Only initialize local state once when we first enter review mode
  const [reviewInitialized, setReviewInitialized] = useState(false)
  if (isReviewing && serverReviewState && !reviewInitialized) {
    setReviewActive(serverReviewState.active || [])
    setReviewSkipped(serverReviewState.skipped || [])
    setReviewUnusedLibrary(serverReviewState.unused_library || [])
    setReviewInitialized(true)
  }
  if (!isReviewing && reviewInitialized) {
    setReviewInitialized(false)
  }

  // Signals the model never addressed (freshness has no deterministic
  // backstop, so an omission means no check was proposed) and whether the
  // model's response was unparseable — both surfaced from the server review
  // state so the reviewer isn't misled by a clean-looking "0 proposals".
  const signalsMissed = serverReviewState?.signals_missed ?? []
  const parseFailed   = serverReviewState?.parse_failed ?? false

  // Group active instances by definition so the reviewer sees one card per
  // library concept ("Not-Null Constraint Violation") with its target columns
  // listed inside, instead of a flat list of 27 rows where the same concept
  // repeats. Preserves the order of first appearance so the display stays
  // stable across renders even when a user reorders things client-side.
  const groupedActive = useMemo(() => {
    const groups: Record<string, { definition_id: string; header: RuleReviewEntry; instances: RuleReviewEntry[] }> = {}
    const order: string[] = []
    for (const rule of reviewActive) {
      const key = rule.definition_id || `_no_def_${rule.instance_id}`
      if (!groups[key]) {
        groups[key] = { definition_id: key, header: rule, instances: [] }
        order.push(key)
      }
      groups[key].instances.push(rule)
    }
    return order.map(k => groups[k])
  }, [reviewActive])

  // Move an instance from active → skipped
  const rejectRule = (instanceId: string) => {
    const rule = reviewActive.find(r => r.instance_id === instanceId)
    if (!rule) return
    setReviewActive(prev => prev.filter(r => r.instance_id !== instanceId))
    setReviewSkipped(prev => [...prev, { ...rule, reason: rule.reason || 'Rejected by user' }])
    setSelectedActiveIds(prev => { const n = new Set(prev); n.delete(instanceId); return n })
    setApprovedIds(prev => { const n = new Set(prev); n.delete(instanceId); return n })
  }

  // Explicitly approve an AI-generated rule (it stays in Active; this just
  // records the affirmative decision for clear visual feedback).
  const approveRule = (instanceId: string) => {
    setApprovedIds(prev => new Set(prev).add(instanceId))
    setSelectedActiveIds(prev => { const n = new Set(prev); n.delete(instanceId); return n })
  }

  // Move a rule from skipped → active (activate/approve a skipped one)
  const activateRule = (instanceId: string) => {
    const rule = reviewSkipped.find(r => r.instance_id === instanceId)
    if (!rule) return
    setReviewSkipped(prev => prev.filter(r => r.instance_id !== instanceId))
    setReviewActive(prev => [...prev, rule])
    setSelectedSkippedIds(prev => { const n = new Set(prev); n.delete(instanceId); return n })
  }

  // Save edits to a new instance/definition
  const saveEdit = (instanceId: string) => {
    setReviewActive(prev => prev.map(r =>
      r.instance_id === instanceId ? { ...r, ...editForm } : r
    ))
    setEditingRule(null)
    setEditForm({})
  }

  const toggleActiveSelected = (instanceId: string) => {
    setSelectedActiveIds(prev => {
      const n = new Set(prev)
      if (n.has(instanceId)) n.delete(instanceId)
      else n.add(instanceId)
      return n
    })
  }

  const toggleSkippedSelected = (instanceId: string) => {
    setSelectedSkippedIds(prev => {
      const n = new Set(prev)
      if (n.has(instanceId)) n.delete(instanceId)
      else n.add(instanceId)
      return n
    })
  }

  const bulkSkip = (ids: Set<string>) => {
    const toMove = reviewActive.filter(r => ids.has(r.instance_id))
    if (toMove.length === 0) return
    setReviewActive(prev => prev.filter(r => !ids.has(r.instance_id)))
    setReviewSkipped(prev => [...prev, ...toMove.map(r => ({ ...r, reason: r.reason || 'Bulk-skipped by user' }))])
    setSelectedActiveIds(new Set())
  }

  const bulkActivate = (ids: Set<string>) => {
    const toMove = reviewSkipped.filter(r => ids.has(r.instance_id))
    if (toMove.length === 0) return
    setReviewSkipped(prev => prev.filter(r => !ids.has(r.instance_id)))
    setReviewActive(prev => [...prev, ...toMove])
    setSelectedSkippedIds(new Set())
  }

  const getTask = (name: string): AgentTask | undefined =>
    activeRun?.tasks.find(t => t.agent_name === name)

  const verifyTask   = getTask('verification_agent')
  const verifyOutput = verifyTask?.output
  const verifyDone   = verifyTask?.status === 'completed'

  // Live finding stats — polls every 5s when awaiting fixes so resolved count
  // updates immediately when developer fixes something (no manual verify needed)
  const { data: liveFindings } = useQuery({
    queryKey: ['workflow-findings-stats', activeRun?.scan_id],
    queryFn: () =>
      findingsApi.list({ scan_id: activeRun!.scan_id!, limit: 500 }).then(r => r.data),
    enabled: !!activeRun?.scan_id && (isAwaiting || isCompleted),
    refetchInterval: isAwaiting ? 5000 : false,
  })

  const liveResolved  = liveFindings?.findings.filter(
    f => ['resolved', 'false_positive', 'wont_fix', 'closed'].includes(f.status)
  ).length ?? null
  const liveTotal     = liveFindings?.findings.length ?? activeRun?.findings_count ?? 0
  const liveRemaining = liveTotal - (liveResolved ?? 0)

  const totalDuration = (() => {
    if (!activeRun?.started_at || !activeRun?.completed_at) return null
    const s = (new Date(activeRun.completed_at).getTime() - new Date(activeRun.started_at).getTime()) / 1000
    return formatDuration(s)
  })()

  const findingsOutput = getTask('findings_agent')?.output

  return (
    <div className="space-y-6">

      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Agent Workflow</h1>
        <p className="mt-1 text-gray-600 dark:text-gray-300">
          AI-powered data quality pipeline — parallel scan, intelligent rule selection, findings, verify.
        </p>
      </div>

      {/* Target selector */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-6">
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-3 uppercase tracking-wide">Select Scan Scope</h2>

        {/* Scope selector */}
        <div className="grid grid-cols-3 gap-2 mb-4">
          {SCOPE_OPTIONS.map(opt => {
            const selected = scope === opt.value
            return (
              <button key={opt.value}
                type="button"
                onClick={() => { setScope(opt.value); setSelectedTable('') }}
                disabled={isRunning}
                className={`text-left rounded-lg border-2 px-3 py-2.5 transition-all disabled:opacity-50 ${
                  selected ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/40' : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 bg-white dark:bg-gray-800'
                }`}>
                <p className={`text-sm font-semibold ${selected ? 'text-primary-800 dark:text-primary-200' : 'text-gray-800 dark:text-gray-200'}`}>{opt.label}</p>
                <p className="text-xs text-gray-500 dark:text-gray-300 mt-0.5">{opt.hint}</p>
              </button>
            )
          })}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-4">
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-300 mb-1">Database</label>
            <select value={selectedDatabase}
              onChange={e => { setSelectedDatabase(e.target.value); setSelectedSchema(''); setSelectedTable('') }}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500"
              disabled={isRunning}>
              <option value="">Choose database...</option>
              {databases?.databases.map(db => <option key={db} value={db}>{db}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-300 mb-1">
              Schema {scope === 'database' && <span className="text-gray-400 dark:text-gray-400 font-normal">(all)</span>}
            </label>
            <select value={selectedSchema}
              onChange={e => { setSelectedSchema(e.target.value); setSelectedTable('') }}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 disabled:bg-gray-50"
              disabled={!selectedDatabase || isRunning || scope === 'database' || schemasFetching}>
              {schemasFetching
                ? <option value="">Loading schemas...</option>
                : schemasError
                  ? <option value="">Unable to load schemas</option>
                  : <>
                      <option value="">{scope === 'database' ? 'All schemas' : schemas?.schemas.length === 0 ? 'No schemas found' : 'Choose schema...'}</option>
                      {schemas?.schemas.map(s => <option key={s} value={s}>{s}</option>)}
                    </>
              }
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-300 mb-1">
              Table {scope !== 'table' && <span className="text-gray-400 dark:text-gray-400 font-normal">(all)</span>}
            </label>
            <select value={selectedTable}
              onChange={e => setSelectedTable(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 disabled:bg-gray-50"
              disabled={!selectedSchema || isRunning || scope !== 'table' || tablesFetching}>
              {tablesFetching
                ? <option value="">Loading tables...</option>
                : tablesError
                  ? <option value="">Unable to load tables</option>
                  : <>
                      <option value="">{scope !== 'table' ? 'All tables' : tables?.tables.length === 0 ? 'No tables found' : 'Choose table...'}</option>
                      {tables?.tables.map(t => <option key={t} value={t}>{t}</option>)}
                    </>
              }
            </select>
          </div>
        </div>

        {(() => {
          const canRun =
            !isRunning && !!selectedDatabase &&
            (scope === 'database' ||
             (scope === 'schema' && !!selectedSchema) ||
             (scope === 'table' && !!selectedSchema && !!selectedTable))
          const scopeLabel =
            scope === 'table'    ? `${selectedSchema || '…'}.${selectedTable || '…'}` :
            scope === 'schema'   ? `all tables in ${selectedDatabase || '…'}.${selectedSchema || '…'}` :
                                   `all tables in ${selectedDatabase || '…'} (every schema)`
          return (
            <div className="flex items-center gap-3 flex-wrap">
              <button onClick={() => startMutation.mutate({
                  scope,
                  database: selectedDatabase,
                  schema_name: scope === 'database' ? undefined : selectedSchema,
                  table: scope === 'table' ? selectedTable : undefined,
                  connection_id: connId,
                })}
                disabled={!canRun || startMutation.isPending}
                className="flex items-center gap-2 px-5 py-2.5 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm">
                {isRunning || startMutation.isPending
                  ? <><Loader2 className="w-4 h-4 animate-spin" />Running...</>
                  : <><Play className="w-4 h-4" />Run Workflow</>
                }
              </button>
              {canRun && (
                <span className="text-xs text-gray-500 dark:text-gray-300">
                  Will scan <span className="font-medium text-gray-700 dark:text-gray-200">{scopeLabel}</span>
                  {scope !== 'table' && ' — one table at a time, review rules for each'}
                </span>
              )}
            </div>
          )
        })()}
      </div>

      {/* Batch progress — shown for schema/database scans */}
      {isBatch && activeBatch && (() => {
        const runs = activeBatch.runs
        const done      = runs.filter(r => r.status === 'completed').length
        const failed    = runs.filter(r => r.status === 'failed').length
        const inProgress= runs.filter(r => ['running', 'awaiting_rule_review', 'awaiting_fixes'].includes(r.status)).length
        const pct = Math.round(((done + failed) / runs.length) * 100)
        return (
          <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-6">
            <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
              <div>
                <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200 uppercase tracking-wide flex items-center gap-2">
                  <GitBranch className="w-4 h-4 text-primary-500" />
                  Batch Scan — {activeBatch.scope === 'database'
                    ? activeBatch.database
                    : `${activeBatch.database}.${activeBatch.schema_name}`}
                </h2>
                <p className="text-xs text-gray-500 dark:text-gray-300 mt-0.5">
                  {done} done · {inProgress} in progress · {failed > 0 && <span className="text-red-600">{failed} failed · </span>}{runs.length} tables total
                </p>
              </div>
              <button onClick={() => { setActiveBatchId(null); setActiveRunId(null) }}
                className="text-xs text-gray-400 dark:text-gray-400 hover:text-gray-700 px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors">
                Close batch ✕
              </button>
            </div>
            <div className="w-full h-1.5 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden mb-4">
              <div className="h-full bg-primary-500 transition-all" style={{ width: `${pct}%` }} />
            </div>
            <div className="flex flex-wrap gap-2">
              {runs.map(r => {
                const isActive = r.id === activeRunId
                return (
                  <button key={r.id}
                    onClick={() => { setActiveRunId(r.id); setCollapsed(false) }}
                    title={`${r.schema_name}.${r.table} — ${r.status.replace(/_/g, ' ')}`}
                    className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-all ${batchRunTone(r.status)} ${
                      isActive ? 'ring-2 ring-primary-400 ring-offset-1' : 'hover:shadow-sm'
                    }`}>
                    {r.status === 'running' && <Loader2 className="w-3 h-3 animate-spin" />}
                    {r.status === 'completed' && <CheckCircle2 className="w-3 h-3" />}
                    {r.status === 'failed' && <AlertTriangle className="w-3 h-3" />}
                    {r.status === 'awaiting_rule_review' && <BrainCircuit className="w-3 h-3" />}
                    {r.status === 'awaiting_fixes' && <Wrench className="w-3 h-3" />}
                    <span className="max-w-[10rem] truncate">{r.table}</span>
                    {r.findings_count > 0 && (
                      <span className="opacity-70">({r.findings_count})</span>
                    )}
                  </button>
                )
              })}
            </div>
            <p className="text-xs text-gray-400 dark:text-gray-400 mt-3">
              Tables are processed one at a time. Review rules for the active table below — the next table starts automatically.
            </p>
          </div>
        )
      })()}

      {/* Pipeline visualization */}
      {activeRunId && activeRun && (
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
          {/* Clickable header — always visible, click to expand/collapse */}
          <div
            className="flex items-center justify-between px-6 py-4 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors"
            onClick={() => setCollapsed(c => !c)}
          >
            <div className="flex items-center gap-3 min-w-0">
              {collapsed
                ? <ChevronRight className="w-4 h-4 text-gray-400 dark:text-gray-400 flex-shrink-0" />
                : <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-400 flex-shrink-0" />
              }
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200 uppercase tracking-wide">Pipeline</h2>
                  {isRunning && (
                    <span className="flex items-center gap-1 text-xs text-blue-700 dark:text-blue-300 bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-500/40 px-2 py-0.5 rounded-full font-medium">
                      <Loader2 className="w-3 h-3 animate-spin" />Running
                    </span>
                  )}
                  {isReviewing && (
                    <span className="flex items-center gap-1 text-xs text-purple-700 dark:text-purple-300 bg-purple-50 dark:bg-purple-950/40 border border-purple-200 dark:border-purple-500/40 px-2 py-0.5 rounded-full font-medium">
                      <BrainCircuit className="w-3 h-3" />Review Rules
                    </span>
                  )}
                  {isAwaiting && (
                    <span className="flex items-center gap-1 text-xs text-primary-700 dark:text-primary-300 bg-primary-50 dark:bg-primary-950/40 border border-primary-200 dark:border-primary-500/40 px-2 py-0.5 rounded-full font-medium">
                      <Wrench className="w-3 h-3" />Awaiting Fixes
                    </span>
                  )}
                  {isCompleted && (
                    <span className="flex items-center gap-1 text-xs text-green-700 dark:text-green-300 bg-green-50 dark:bg-green-950/40 border border-green-200 dark:border-green-500/40 px-2 py-0.5 rounded-full font-medium">
                      <CheckCircle2 className="w-3 h-3" />Completed
                    </span>
                  )}
                  {isFailed && (
                    <span className="flex items-center gap-1 text-xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-500/40 px-2 py-0.5 rounded-full font-medium">
                      <AlertTriangle className="w-3 h-3" />Failed
                    </span>
                  )}
                  {activeRun.schedule_id && (
                    <span className="flex items-center gap-1 text-xs text-indigo-700 dark:text-indigo-300 bg-indigo-50 dark:bg-indigo-950/40 border border-indigo-200 dark:border-indigo-500/40 px-2 py-0.5 rounded-full font-medium" title="Fired by a schedule">
                      <Clock className="w-3 h-3" />Scheduled
                    </span>
                  )}
                </div>
                <p className="text-xs text-gray-400 dark:text-gray-400 mt-0.5 font-mono truncate">
                  {activeRun.database}.{activeRun.schema_name}.{activeRun.table}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-3 flex-shrink-0" onClick={e => e.stopPropagation()}>
              {totalDuration && <span className="text-xs text-gray-400 dark:text-gray-400 flex items-center gap-1"><Clock className="w-3 h-3" />{totalDuration}</span>}
              <button
                onClick={() => setActiveRunId(null)}
                className="text-xs text-gray-400 dark:text-gray-400 hover:text-gray-700 px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                title="Close this run"
              >
                ✕
              </button>
            </div>
          </div>

          {/* Collapsible body */}
          {!collapsed && (
            <div className="px-6 pb-6">
              <div className="flex items-start overflow-x-auto pb-2 gap-0">
                <AgentNode agentDef={getAgentDef('coordinator')} task={getTask('coordinator')}
                  isLast={false} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
                <ParallelGroup>
                  <AgentNode agentDef={getAgentDef('metadata_agent')} task={getTask('metadata_agent')}
                    isLast={true} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
                  <AgentNode agentDef={getAgentDef('rules_fetch_agent')} task={getTask('rules_fetch_agent')}
                    isLast={true} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
                  <AgentNode agentDef={getAgentDef('relationship_discovery_agent')} task={getTask('relationship_discovery_agent')}
                    isLast={true} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
                  <AgentNode agentDef={getAgentDef('profiling_agent')} task={getTask('profiling_agent')}
                    isLast={true} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
                </ParallelGroup>
                <AgentNode agentDef={getAgentDef('rule_intelligence_agent')} task={getTask('rule_intelligence_agent')}
                  isLast={false} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
                <AgentNode agentDef={getAgentDef('findings_agent')} task={getTask('findings_agent')}
                  isLast={false} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
                <AgentNode agentDef={getAgentDef('fix_issues')} task={undefined}
                  isLast={false} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
                <AgentNode agentDef={getAgentDef('verification_agent')} task={getTask('verification_agent')}
                  isLast={true} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
              </div>
              <div className="mt-5 pt-4 border-t border-gray-100 dark:border-gray-700 grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div className="text-center">
                  <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">{activeRun.findings_count}</p>
                  <p className="text-xs text-gray-500 dark:text-gray-300">Findings</p>
                </div>
                <div className="text-center">
                  <p className="text-2xl font-bold text-purple-600 flex items-center justify-center gap-1">
                    <Sparkles className="w-5 h-5" />
                    {activeRun.ai_rules_count}
                    {activeRun.ai_rules_proposed > activeRun.ai_rules_count && (
                      <span className="text-sm font-normal text-gray-400 dark:text-gray-500">
                        /{activeRun.ai_rules_proposed}
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-gray-500 dark:text-gray-300">
                    AI rules approved
                    {activeRun.ai_rules_proposed > activeRun.ai_rules_count && (
                      <span className="text-gray-400 dark:text-gray-600"> of {activeRun.ai_rules_proposed} proposed</span>
                    )}
                  </p>
                </div>
                <div className="text-center">
                  {liveResolved !== null ? (
                    <>
                      <p className={`text-2xl font-bold ${liveResolved === liveTotal ? 'text-green-600' : 'text-primary-600'}`}>
                        {liveResolved}/{liveTotal}
                      </p>
                      <p className="text-xs text-gray-500 dark:text-gray-300">Resolved (live)</p>
                    </>
                  ) : (
                    <>
                      <p className="text-2xl font-bold text-gray-300">—</p>
                      <p className="text-xs text-gray-400 dark:text-gray-400">Resolved</p>
                    </>
                  )}
                </div>
                <div className="text-center">
                  {activeRun.scan_id ? (
                    <button onClick={() => navigate(`/findings?scan_id=${activeRun.scan_id}`)}
                      className="text-primary-600 hover:text-primary-800 text-xs font-medium flex items-center gap-1 mx-auto mt-1">
                      View Findings <ExternalLink className="w-3 h-3" />
                    </button>
                  ) : (
                    <p className="text-xs text-gray-400 dark:text-gray-400">—</p>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Verification result banner — directly below pipeline */}
      {(isAwaiting || isCompleted) && verifyDone && verifyOutput && (() => {
        const resolved  = liveResolved  ?? verifyOutput.resolved  ?? 0
        const total     = (liveTotal || verifyOutput.total_findings) ?? 0
        const remaining = liveRemaining ?? verifyOutput.remaining ?? 0
        const pct       = total > 0 ? Math.round(resolved / total * 100) : 0
        const newAuto   = verifyOutput.newly_auto_resolved ?? 0
        const allDone   = remaining === 0
        return (
          <div className={`border-2 rounded-xl p-5 ${allDone ? 'bg-green-50 dark:bg-green-950/40 border-green-300 dark:border-green-500/40' : 'bg-blue-50 dark:bg-blue-950/40 border-blue-300 dark:border-blue-500/40'}`}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <h3 className={`font-semibold text-base mb-1 ${allDone ? 'text-green-900 dark:text-green-200' : 'text-blue-900 dark:text-blue-200'}`}>
                  {allDone
                    ? '✅ All issues resolved — workflow complete!'
                    : `📊 Verification: ${resolved}/${total} fixed (${pct}%) — ${remaining} remaining`
                  }
                </h3>
                <p className={`text-sm ${allDone ? 'text-green-800 dark:text-green-300' : 'text-blue-800 dark:text-blue-300'}`}>
                  {allDone
                    ? 'Every finding has been resolved. Great work!'
                    : `${remaining} finding${remaining !== 1 ? 's' : ''} still need attention.`
                  }
                </p>
                {newAuto > 0 && (
                  <p className="text-xs mt-1.5 text-green-700 dark:text-green-400 font-medium">
                    ✓ {newAuto} auto-resolved by live re-scan
                  </p>
                )}
              </div>
              {!allDone && (
                <div className="flex flex-col gap-2 flex-shrink-0">
                  <button onClick={() => navigate(`/findings?scan_id=${activeRun?.scan_id}&status=detected`)}
                    className="flex items-center gap-1.5 px-3 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors">
                    <Wrench className="w-3.5 h-3.5" />Fix Remaining
                  </button>
                  <button onClick={() => verifyMutation.mutate(activeRunId!)}
                    disabled={verifyMutation.isPending}
                    className="flex items-center gap-1.5 px-3 py-2 border border-blue-300 dark:border-blue-500/40 text-blue-700 dark:text-blue-300 rounded-lg text-sm font-medium hover:bg-blue-50 dark:hover:bg-blue-900/40 disabled:opacity-50 transition-colors">
                    <RefreshCw className={`w-3.5 h-3.5 ${verifyMutation.isPending ? 'animate-spin' : ''}`} />
                    Verify Again
                  </button>
                </div>
              )}
            </div>
          </div>
        )
      })()}

      {/* ── RULE REVIEW PANEL (shown when awaiting_rule_review) ─────────────── */}
      {isReviewing && activeRunId && (
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-100 dark:border-gray-700 bg-purple-50 dark:bg-purple-950/40">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-base font-semibold text-purple-900 dark:text-purple-200 flex items-center gap-2">
                  <BrainCircuit className="w-5 h-5 text-purple-600" />
                  Review Rules Before Running
                </h2>
                <p className="text-xs text-purple-700 dark:text-purple-300 mt-0.5">
                  Claude kept {reviewActive.length} active and skipped {reviewSkipped.length}.
                  Approve or reject AI-generated rules, activate skipped ones, edit new rules, or select several and use bulk actions. Then click Run Pipeline.
                </p>
              </div>
              <button
                onClick={() => {
                  saveReviewMutation.mutate(
                    { active: reviewActive, skipped: reviewSkipped },
                    { onSuccess: () => runPipelineMutation.mutate() }
                  )
                }}
                disabled={saveReviewMutation.isPending || runPipelineMutation.isPending}
                className="flex items-center gap-2 px-5 py-2.5 bg-purple-600 text-white font-medium rounded-lg hover:bg-purple-700 disabled:opacity-50 transition-colors text-sm"
              >
                {(saveReviewMutation.isPending || runPipelineMutation.isPending)
                  ? <><Loader2 className="w-4 h-4 animate-spin" />Starting...</>
                  : <><Play className="w-4 h-4" />Run Pipeline ({reviewActive.length} rules)</>
                }
              </button>
            </div>
          </div>

          {/* Parse-failure warning — "0 proposals" may be a broken response, not full coverage */}
          {parseFailed && (
            <div className="px-6 py-3 bg-red-50 dark:bg-red-950/40 border-b border-red-200 dark:border-red-500/40 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-red-600 flex-shrink-0 mt-0.5" />
              <p className="text-xs text-red-800 dark:text-red-300">
                <span className="font-semibold">Rule Intelligence response could not be parsed</span>{' '}
                (even after a retry). Any missing proposals below may be due to a broken or truncated
                model response — treat this list as incomplete rather than as confirmation the table is
                fully covered. Consider re-running the workflow for this table.
              </p>
            </div>
          )}

          {/* Unaddressed-signals warning — deterministic signals with no proposed check */}
          {signalsMissed.length > 0 && (
            <div className="px-6 py-3 bg-amber-50 dark:bg-amber-950/40 border-b border-amber-200 dark:border-amber-500/40 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-amber-600 flex-shrink-0 mt-0.5" />
              <div className="text-xs text-amber-800 dark:text-amber-300">
                <span className="font-semibold">
                  {signalsMissed.length} signal{signalsMissed.length !== 1 ? 's' : ''} unaddressed
                </span>{' '}
                — the model did not propose a check for these, and freshness signals have no automatic
                fallback:
                <div className="flex flex-wrap gap-1.5 mt-1.5">
                  {signalsMissed.map(sig => (
                    <span key={sig} className="font-mono bg-amber-100 dark:bg-amber-900/40 border border-amber-200 dark:border-amber-700 text-amber-800 dark:text-amber-300 px-1.5 py-0.5 rounded">
                      {sig}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 divide-y md:divide-y-0 md:divide-x divide-gray-100 dark:divide-gray-700">
            {/* Active Rules column */}
            <div className="p-5">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full bg-green-500 inline-block" />
                  Active Rules ({groupedActive.length} {groupedActive.length === 1 ? 'definition' : 'definitions'} · {reviewActive.length} {reviewActive.length === 1 ? 'instance' : 'instances'})
                </h3>
              </div>
              {selectedActiveIds.size > 0 && (
                <div className="mb-2 flex items-center gap-2 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-500/40 rounded-lg px-3 py-1.5">
                  <span className="text-xs font-medium text-red-800 dark:text-red-300">{selectedActiveIds.size} selected</span>
                  <button
                    onClick={() => bulkSkip(selectedActiveIds)}
                    className="ml-auto flex items-center gap-1 text-xs px-2.5 py-1 bg-red-600 text-white rounded hover:bg-red-700"
                  >
                    <AlertTriangle className="w-3 h-3" />Skip Selected
                  </button>
                  <button
                    onClick={() => setSelectedActiveIds(new Set())}
                    className="text-xs px-2 py-1 text-red-400 hover:text-red-700"
                  >
                    Clear
                  </button>
                </div>
              )}
              <div className="space-y-3 max-h-96 overflow-y-auto pr-1">
                {groupedActive.map(group => (
                  <div key={group.definition_id} className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 overflow-hidden">
                    {/* Group header — definition-level info */}
                    <div className="px-3 py-2 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <p className="text-xs font-semibold text-gray-800 dark:text-gray-100 truncate">
                          {group.header.name}
                        </p>
                        {group.header.description && (
                          <p className="text-[11px] text-gray-500 dark:text-gray-400 truncate">
                            {group.header.description}
                          </p>
                        )}
                      </div>
                      <span className="text-[11px] text-gray-500 dark:text-gray-400 flex-shrink-0">
                        {group.instances.length} {group.instances.length === 1 ? 'instance' : 'instances'}
                      </span>
                    </div>
                    {/* Instance rows within this group */}
                    <div className="p-2 space-y-2">
                {group.instances.map(rule => (
                  <div key={rule.instance_id} className="rounded-lg border p-3 text-sm border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
                    {editingRule === rule.instance_id ? (
                      <div className="space-y-2">
                        <input
                          value={editForm.name ?? rule.name}
                          onChange={e => setEditForm(f => ({ ...f, name: e.target.value }))}
                          className="w-full px-2 py-1 border border-gray-300 dark:border-gray-600 rounded text-xs font-medium"
                          placeholder="Instance name"
                        />
                        <textarea
                          value={editForm.description ?? rule.description}
                          onChange={e => setEditForm(f => ({ ...f, description: e.target.value }))}
                          className="w-full px-2 py-1 border border-gray-300 dark:border-gray-600 rounded text-xs resize-none"
                          rows={2}
                          placeholder="Description"
                        />
                        <select
                          value={editForm.severity ?? rule.severity}
                          onChange={e => setEditForm(f => ({ ...f, severity: e.target.value }))}
                          className="w-full px-2 py-1 border border-gray-300 dark:border-gray-600 rounded text-xs"
                        >
                          {['critical', 'high', 'medium', 'low'].map(s => (
                            <option key={s} value={s}>{s}</option>
                          ))}
                        </select>
                        <div className="flex gap-2">
                          <button onClick={() => saveEdit(rule.instance_id)}
                            className="flex-1 px-2 py-1 bg-purple-600 text-white rounded text-xs font-medium hover:bg-purple-700">
                            Save
                          </button>
                          <button onClick={() => { setEditingRule(null); setEditForm({}) }}
                            className="px-2 py-1 border border-gray-300 dark:border-gray-600 rounded text-xs text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40">
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="flex items-start justify-between gap-2">
                        <input
                          type="checkbox"
                          checked={selectedActiveIds.has(rule.instance_id)}
                          onChange={() => toggleActiveSelected(rule.instance_id)}
                          className="mt-1 flex-shrink-0"
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5 mb-0.5 flex-wrap">
                            {rule.is_new_definition ? (
                              <span className="text-xs bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded font-medium">New concept</span>
                            ) : rule.is_new_instance ? (
                              <span className="text-xs bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded font-medium">Reused check, new target</span>
                            ) : (
                              <span className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded font-medium">Existing check</span>
                            )}
                            {sourceBadge(rule.source)}
                            {rule.violated && (
                              <span className="text-xs bg-orange-100 text-orange-700 px-1.5 py-0.5 rounded font-medium">⚠ violated</span>
                            )}
                            <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                              rule.severity === 'critical' ? 'bg-red-100 text-red-700' :
                              rule.severity === 'high'     ? 'bg-orange-100 text-orange-700' :
                              rule.severity === 'medium'   ? 'bg-yellow-100 text-yellow-700' :
                                                             'bg-blue-100 text-blue-700'
                            }`}>{rule.severity}</span>
                            {rule.severity !== rule.original_severity && rule.original_severity && (
                              <span className="text-xs text-gray-400 dark:text-gray-400 line-through">{rule.original_severity}</span>
                            )}
                          </div>
                          <p className="text-xs font-semibold text-gray-700 dark:text-gray-200 truncate">{rule.name}</p>
                          <p className="text-xs text-gray-600 dark:text-gray-300 truncate">{rule.description}</p>
                          {rule.reason && (
                            <p className="text-xs text-gray-400 dark:text-gray-400 mt-0.5 truncate" title={rule.reason}>
                              {rule.reason}
                            </p>
                          )}
                        </div>
                        <div className="flex gap-1 flex-shrink-0">
                          {rule.is_new_instance && (
                            <button
                              onClick={() => { setEditingRule(rule.instance_id); setEditForm({}) }}
                              className="text-xs px-1.5 py-1 text-purple-600 dark:text-purple-300 border border-purple-200 dark:border-purple-500/40 rounded hover:bg-purple-50 dark:hover:bg-purple-900/40"
                              title="Edit this new rule"
                            >
                              Edit
                            </button>
                          )}
                          {rule.is_new_instance ? (
                            <>
                              <button
                                onClick={() => approveRule(rule.instance_id)}
                                disabled={approvedIds.has(rule.instance_id)}
                                className={`flex items-center gap-1 text-xs px-1.5 py-1 rounded border ${
                                  approvedIds.has(rule.instance_id)
                                    ? 'text-green-700 dark:text-green-300 border-green-300 dark:border-green-500/40 bg-green-50 dark:bg-green-950/40'
                                    : 'text-green-600 dark:text-green-400 border-green-200 dark:border-green-500/40 hover:bg-green-50 dark:hover:bg-green-900/40'
                                }`}
                                title="Approve this AI-generated rule"
                              >
                                <CheckCircle2 className="w-3 h-3" />
                                {approvedIds.has(rule.instance_id) ? 'Approved' : 'Approve'}
                              </button>
                              <button
                                onClick={() => rejectRule(rule.instance_id)}
                                className="text-xs px-1.5 py-1 text-red-600 dark:text-red-400 border border-red-200 dark:border-red-500/40 rounded hover:bg-red-50 dark:hover:bg-red-900/40"
                                title="Reject this AI-generated rule"
                              >
                                Reject
                              </button>
                            </>
                          ) : (
                            <button
                              onClick={() => rejectRule(rule.instance_id)}
                              className="text-xs px-1.5 py-1 text-red-600 dark:text-red-400 border border-red-200 dark:border-red-500/40 rounded hover:bg-red-50 dark:hover:bg-red-900/40"
                              title="Skip this rule"
                            >
                              Skip
                            </button>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
                    </div>
                  </div>
                ))}
                {groupedActive.length === 0 && (
                  <p className="text-xs text-gray-400 dark:text-gray-400 text-center py-4">No active rules — activate some from the Skipped column.</p>
                )}
              </div>
            </div>

            {/* Skipped Rules column */}
            <div className="p-5">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full bg-gray-300 inline-block" />
                  Skipped Rules ({reviewSkipped.length})
                </h3>
              </div>
              {selectedSkippedIds.size > 0 && (
                <div className="mb-2 flex items-center gap-2 bg-green-50 dark:bg-green-950/40 border border-green-200 dark:border-green-500/40 rounded-lg px-3 py-1.5">
                  <span className="text-xs font-medium text-green-800 dark:text-green-300">{selectedSkippedIds.size} selected</span>
                  <button
                    onClick={() => bulkActivate(selectedSkippedIds)}
                    className="ml-auto flex items-center gap-1 text-xs px-2.5 py-1 bg-green-600 text-white rounded hover:bg-green-700"
                  >
                    <CheckCircle2 className="w-3 h-3" />Activate Selected
                  </button>
                  <button
                    onClick={() => setSelectedSkippedIds(new Set())}
                    className="text-xs px-2 py-1 text-green-400 hover:text-green-700"
                  >
                    Clear
                  </button>
                </div>
              )}
              <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
                {reviewSkipped.map(rule => (
                  <div key={rule.instance_id} className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-3 text-sm opacity-75">
                    <div className="flex items-start justify-between gap-2">
                      <input
                        type="checkbox"
                        checked={selectedSkippedIds.has(rule.instance_id)}
                        onChange={() => toggleSkippedSelected(rule.instance_id)}
                        className="mt-1 flex-shrink-0"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 mb-0.5 flex-wrap">
                          {rule.is_new_definition ? (
                            <span className="text-xs bg-purple-200 text-purple-700 px-1.5 py-0.5 rounded font-medium">New concept</span>
                          ) : rule.is_new_instance ? (
                            <span className="text-xs bg-blue-200 text-blue-700 px-1.5 py-0.5 rounded font-medium">Reused check, new target</span>
                          ) : (
                            <span className="text-xs bg-gray-200 text-gray-600 px-1.5 py-0.5 rounded font-medium">Existing check</span>
                          )}
                          {sourceBadge(rule.source)}
                        </div>
                        <p className="text-xs font-medium text-gray-600 dark:text-gray-300 truncate">{rule.name}</p>
                        {rule.reason && (
                          <p className="text-xs text-gray-400 dark:text-gray-400 mt-0.5 truncate" title={rule.reason}>
                            {rule.reason}
                          </p>
                        )}
                      </div>
                      <button
                        onClick={() => activateRule(rule.instance_id)}
                        className="flex-shrink-0 text-xs px-1.5 py-1 text-green-600 dark:text-green-400 border border-green-200 dark:border-green-500/40 rounded hover:bg-green-50 dark:hover:bg-green-900/40"
                        title="Activate this rule"
                      >
                        Activate
                      </button>
                    </div>
                  </div>
                ))}
                {reviewSkipped.length === 0 && (
                  <p className="text-xs text-gray-400 dark:text-gray-400 text-center py-4">No skipped rules.</p>
                )}
              </div>
            </div>
          </div>

          {/* ── Available in Library ─────────────────────────────────────────
              Library definitions with NO instance on this table. Reviewer can
              activate one to add a new check without going back to the Rule
              Library page. Activation is stubbed for now — the target/threshold
              modal + create-instance wiring lands next round. */}
          {reviewUnusedLibrary.length > 0 && (
            <div className="border-t border-gray-100 dark:border-gray-700 p-5">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full bg-blue-400 inline-block" />
                  Available in Library ({reviewUnusedLibrary.length})
                </h3>
                <span className="text-[11px] text-gray-500 dark:text-gray-400">
                  Definitions Claude didn't apply to this table — activate any you want to run.
                </span>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 max-h-80 overflow-y-auto pr-1">
                {reviewUnusedLibrary.map((d) => (
                  <div
                    key={d.definition_id}
                    className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-3 text-sm flex flex-col gap-1"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <p className="text-xs font-semibold text-gray-800 dark:text-gray-100 truncate" title={d.name}>
                          {d.name}
                        </p>
                        <div className="flex flex-wrap items-center gap-1 mt-0.5">
                          {d.template_shape && (
                            <span className="text-[10px] bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 px-1.5 py-0.5 rounded">
                              {d.template_shape}
                            </span>
                          )}
                          {d.category && (
                            <span className="text-[10px] bg-indigo-50 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300 px-1.5 py-0.5 rounded">
                              {d.category}
                            </span>
                          )}
                          {d.default_severity && (
                            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                              d.default_severity === 'critical' ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300' :
                              d.default_severity === 'high'     ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300' :
                              d.default_severity === 'medium'   ? 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300' :
                                                                  'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                            }`}>{d.default_severity}</span>
                          )}
                        </div>
                      </div>
                      <button
                        onClick={() => window.alert(
                          `Activation UI is coming in the next update.\n\n"${d.name}"\n\nFor now, activate this definition on this table from the Rule Library page.`
                        )}
                        className="flex-shrink-0 text-xs px-2 py-1 text-blue-600 dark:text-blue-400 border border-blue-200 dark:border-blue-500/40 rounded hover:bg-blue-50 dark:hover:bg-blue-900/40"
                        title="Coming soon"
                      >
                        Activate
                      </button>
                    </div>
                    {d.description && (
                      <p className="text-[11px] text-gray-500 dark:text-gray-400 line-clamp-2" title={d.description}>
                        {d.description}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Awaiting fixes banner */}
      {isAwaiting && activeRun?.scan_id && !verifyDone && (
        <div className="bg-primary-50 dark:bg-gray-800 border-2 border-primary-300 dark:border-primary-500 rounded-xl p-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h3 className="font-semibold text-primary-900 dark:text-primary-100 text-base mb-1">
                🔧 Pipeline complete — fix the findings
              </h3>
              <p className="text-sm text-primary-800 dark:text-gray-200">
                {liveResolved !== null ? (
                  <>
                    <strong className="text-green-700 dark:text-green-400">{liveResolved} resolved</strong>
                    {' · '}
                    <strong>{liveRemaining} remaining</strong>
                    {' of '}
                    {liveTotal} total
                  </>
                ) : (
                  <>
                    <strong>{activeRun.findings_count}</strong> findings detected
                    {activeRun.ai_rules_count > 0 && (
                      <> · <strong>{activeRun.ai_rules_count}</strong> AI rules</>
                    )}
                  </>
                )}.
                Open Findings, select issues, get AI SQL fixes, then verify.
              </p>
            </div>
            <div className="flex flex-col gap-2 flex-shrink-0">
              <button onClick={() => navigate(`/findings?scan_id=${activeRun.scan_id}&status=detected`)}
                className="flex items-center gap-1.5 px-4 py-2 bg-primary-600 text-white rounded-lg text-sm font-medium hover:bg-primary-700 transition-colors">
                <Wrench className="w-4 h-4" />
                {liveRemaining > 0 ? `Fix ${liveRemaining} Remaining` : 'View Findings'}
              </button>
              <button onClick={() => verifyMutation.mutate(activeRunId!)}
                disabled={verifyMutation.isPending}
                className="flex items-center gap-1.5 px-4 py-2 border border-primary-300 dark:border-primary-500/40 text-primary-700 dark:text-primary-300 rounded-lg text-sm font-medium hover:bg-primary-100 dark:hover:bg-primary-900/40 disabled:opacity-50 transition-colors">
                {verifyMutation.isPending
                  ? <><Loader2 className="w-4 h-4 animate-spin" />Verifying...</>
                  : <><RefreshCw className="w-4 h-4" />Verify Fixes</>
                }
              </button>
              <button
                onClick={() => setSaveWfOpen(true)}
                className="flex items-center gap-1.5 px-4 py-2 border border-gray-300 text-gray-700 dark:text-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 rounded-lg text-sm font-medium hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
              >
                <BookmarkPlus className="w-4 h-4" />
                Save as Workflow
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Findings Report — rules that fired vs rules that ran clean (mirrors
          the active/skipped split, but after findings ran) */}
      {findingsOutput && getTask('findings_agent')?.status === 'completed' &&
       Array.isArray(findingsOutput.rules_used) && (
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-100 dark:border-gray-700">
            <div className="flex items-center gap-2">
              <AlertCircle className="w-5 h-5 text-primary-600" />
              <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">Findings Report</h2>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-300 mt-0.5">
              {findingsOutput.rules_executed} rules executed ·{' '}
              <span className="text-orange-700 dark:text-orange-300 font-medium">{findingsOutput.rules_used_count} fired</span> ·{' '}
              <span className="text-green-700 dark:text-green-400 font-medium">{findingsOutput.rules_unused_count} clean</span>
              {findingsOutput.findings_count != null && <> · {findingsOutput.findings_count} findings</>}
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 divide-y md:divide-y-0 md:divide-x divide-gray-100 dark:divide-gray-700">
            {/* Rules that fired */}
            <div className="p-5">
              <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-3 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-orange-500 inline-block" />
                Rules Used ({findingsOutput.rules_used_count})
              </h3>
              <div className="space-y-1.5 max-h-96 overflow-y-auto pr-1">
                {findingsOutput.rules_used.map((r: any, i: number) => (
                  <div key={r.instance_id ?? r.code ?? i} className="flex items-start justify-between gap-2 text-sm rounded-lg border border-orange-200 dark:border-orange-500/30 bg-orange-50/40 dark:bg-orange-500/10 p-2.5">
                    <div className="min-w-0">
                      <span className="font-mono text-xs font-bold text-gray-700 dark:text-gray-200">{r.code}</span>
                      <p className="text-xs text-gray-600 dark:text-gray-300 truncate">{r.name}</p>
                    </div>
                    {activeRun?.scan_id && (
                      <button
                        onClick={() => navigate(`/findings?scan_id=${activeRun.scan_id}&instance=${r.instance_id}`)}
                        className="flex-shrink-0 text-xs px-1.5 py-1 text-orange-700 dark:text-orange-300 border border-orange-300 dark:border-orange-500/40 rounded hover:bg-orange-100 dark:hover:bg-orange-500/20 font-medium"
                        title="View these findings"
                      >
                        {r.findings} finding{r.findings !== 1 ? 's' : ''}
                      </button>
                    )}
                  </div>
                ))}
                {findingsOutput.rules_used_count === 0 && (
                  <p className="text-xs text-gray-400 dark:text-gray-400 text-center py-4">No rules fired — the data is clean.</p>
                )}
              </div>
            </div>

            {/* Rules that ran clean */}
            <div className="p-5">
              <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-3 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-green-500 inline-block" />
                Rules Clean ({findingsOutput.rules_unused_count})
              </h3>
              <div className="space-y-1.5 max-h-96 overflow-y-auto pr-1">
                {(findingsOutput.rules_unused || []).map((r: any, i: number) => (
                  <div key={r.instance_id ?? r.code ?? i} className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-2.5 text-sm">
                    <span className="font-mono text-xs font-bold text-gray-500 dark:text-gray-300">{r.code}</span>
                    <p className="text-xs text-gray-500 dark:text-gray-300 truncate">{r.name}</p>
                  </div>
                ))}
                {findingsOutput.rules_unused_count === 0 && (
                  <p className="text-xs text-gray-400 dark:text-gray-400 text-center py-4">Every executed rule found at least one issue.</p>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Save as Workflow modal ─────────────────────────────────────────── */}
      {saveWfOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-md p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
                Save as Workflow
              </h3>
              <button onClick={() => setSaveWfOpen(false)} className="text-gray-400 dark:text-gray-400 hover:text-gray-600 dark:hover:text-gray-200">
                <X className="w-4 h-4" />
              </button>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
              Saves the active rules applied on this run's table as a reusable workflow.
              You can run it on any table or schema later.
            </p>
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Workflow Label <span className="text-red-500">*</span>
                </label>
                <input
                  value={saveWfLabel}
                  onChange={e => setSaveWfLabel(e.target.value)}
                  placeholder="e.g. Orders Table Standard Checks"
                  className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100"
                  autoFocus
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Description <span className="text-gray-400">(optional)</span>
                </label>
                <textarea
                  value={saveWfDesc}
                  onChange={e => setSaveWfDesc(e.target.value)}
                  rows={2}
                  placeholder="What does this workflow check for?"
                  className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100"
                />
              </div>
            </div>
            {saveWorkflowMutation.isError && (
              <p className="mt-3 text-xs text-red-600">
                {(saveWorkflowMutation.error as any)?.response?.data?.detail || 'Failed to save workflow'}
              </p>
            )}
            {saveWorkflowMutation.isSuccess && (
              <p className="mt-3 text-xs text-green-600">Workflow saved! View it in Saved Workflows.</p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setSaveWfOpen(false)}
                className="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:text-gray-800 dark:hover:text-gray-100"
              >
                Cancel
              </button>
              <button
                onClick={() => saveWorkflowMutation.mutate()}
                disabled={saveWorkflowMutation.isPending || !saveWfLabel.trim()}
                className="flex items-center gap-2 px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
              >
                {saveWorkflowMutation.isPending
                  ? <><Loader2 className="w-4 h-4 animate-spin" />Saving...</>
                  : <><BookmarkPlus className="w-4 h-4" />Save Workflow</>
                }
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  )
}

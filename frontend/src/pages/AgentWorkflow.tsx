import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { assetsApi, agentRunsApi } from '../api/client'
import type { AgentRun, AgentTask } from '../api/client'
import {
  GitBranch, Database, BrainCircuit, Shield, AlertCircle,
  Wrench, CheckCircle2, Loader2, ChevronDown, ChevronRight,
  AlertTriangle, ArrowRight, Clock, Play, ExternalLink,
  RefreshCw, Sparkles,
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
    name: 'rule_intelligence_agent',
    label: 'Rule Intelligence',
    icon: BrainCircuit,
    desc: 'Claude selects rules, tunes severity, generates AI rules',
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

type RunStatus = 'pending' | 'running' | 'awaiting_fixes' | 'completed' | 'failed'

function isPolling(status: RunStatus) {
  return status === 'running' || status === 'awaiting_fixes'
}

function fixIssuesStatus(runStatus: RunStatus): string {
  return runStatus === 'awaiting_fixes' || runStatus === 'completed' ? 'active' : 'pending'
}

function nodeBorderColor(status: string) {
  switch (status) {
    case 'running':   return 'border-blue-400 bg-blue-50'
    case 'completed': return 'border-green-400 bg-green-50'
    case 'failed':    return 'border-red-400 bg-red-50'
    case 'skipped':   return 'border-gray-200 bg-gray-50 opacity-50'
    case 'active':    return 'border-primary-400 bg-primary-50'
    default:          return 'border-gray-200 bg-white'
  }
}

function nodeIconColor(status: string) {
  switch (status) {
    case 'running':   return 'text-blue-500'
    case 'completed': return 'text-green-600'
    case 'failed':    return 'text-red-500'
    case 'active':    return 'text-primary-600'
    default:          return 'text-gray-300'
  }
}

function statusBadge(status: string) {
  switch (status) {
    case 'running':
      return <span className="flex items-center gap-1 text-blue-700 text-xs font-medium"><Loader2 className="w-3 h-3 animate-spin" />Running</span>
    case 'completed':
      return <span className="flex items-center gap-1 text-green-700 text-xs font-medium"><CheckCircle2 className="w-3 h-3" />Done</span>
    case 'failed':
      return <span className="flex items-center gap-1 text-red-700 text-xs font-medium"><AlertTriangle className="w-3 h-3" />Failed</span>
    case 'skipped':
      return <span className="text-gray-400 text-xs">Skipped</span>
    case 'active':
      return <span className="flex items-center gap-1 text-primary-700 text-xs font-medium"><ArrowRight className="w-3 h-3" />Ready</span>
    default:
      return <span className="text-gray-300 text-xs">Waiting</span>
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

  const duration    = task ? formatDuration(task.duration_seconds ?? null) : null
  const liveProgress= task?.output?.progress as string | undefined
  const hasLogs     = !isFixNode && task?.output && Object.keys(task.output).length > 0

  return (
    <div className="flex items-start gap-0 min-w-0">
      <div className="flex flex-col items-center flex-1 min-w-0">
        <div
          className={`w-full border-2 rounded-xl p-3 transition-all ${nodeBorderColor(status)} ${
            hasLogs ? 'cursor-pointer' : ''
          } ${isFixNode && status === 'active' ? 'cursor-pointer hover:shadow-md' : ''}`}
          onClick={() => {
            if (isFixNode && status === 'active' && scanId) {
              navigate(`/findings?scan_id=${scanId}`)
            } else if (hasLogs) {
              setExpanded(e => !e)
            }
          }}
        >
          <div className="flex items-center justify-between mb-1.5">
            <div className="flex items-center gap-2">
              <Icon className={`w-4 h-4 flex-shrink-0 ${nodeIconColor(status)}`} />
              <span className="font-semibold text-xs text-gray-900 truncate">{agentDef.label}</span>
            </div>
            {hasLogs && (
              expanded
                ? <ChevronDown className="w-3 h-3 text-gray-400 flex-shrink-0" />
                : <ChevronRight className="w-3 h-3 text-gray-400 flex-shrink-0" />
            )}
            {isFixNode && status === 'active' && (
              <ExternalLink className="w-3 h-3 text-primary-500 flex-shrink-0" />
            )}
          </div>

          <div className="flex items-center justify-between gap-1 flex-wrap">
            {statusBadge(status)}
            {duration && (
              <span className="flex items-center gap-0.5 text-xs text-gray-400">
                <Clock className="w-2.5 h-2.5" />{duration}
              </span>
            )}
          </div>

          {isFixNode && status === 'active' && scanId && (
            <p className="mt-1.5 text-xs text-primary-700 font-medium">Go to Findings →</p>
          )}
          {status === 'running' && liveProgress && (
            <p className="mt-1.5 text-xs text-blue-700 font-medium truncate">{liveProgress}</p>
          )}
          {status === 'failed' && task?.error_message && (
            <p className="mt-1.5 text-xs text-red-600 truncate" title={task.error_message}>
              {task.error_message}
            </p>
          )}
        </div>

        {expanded && hasLogs && (
          <div className="w-full mt-1.5 p-2.5 bg-gray-900 rounded-lg text-xs font-mono text-green-400 max-h-56 overflow-y-auto">
            {Object.entries(task!.output!).map(([k, v]) => (
              <div key={k} className="py-0.5">
                <span className="text-gray-500">{k}: </span>
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
        <div className="w-full border-2 border-dashed border-gray-200 rounded-xl p-2 bg-gray-50/50">
          <p className="text-xs text-gray-400 font-medium mb-2 text-center">parallel</p>
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

// ── Main page ─────────────────────────────────────────────────────────────────

export default function AgentWorkflow() {
  const navigate    = useNavigate()
  const queryClient = useQueryClient()

  const [selectedDatabase, setSelectedDatabase] = useState('')
  const [selectedSchema,   setSelectedSchema]   = useState('')
  const [selectedTable,    setSelectedTable]     = useState('')
  const [activeRunId,      setActiveRunId]       = useState<string | null>(null)

  const { data: databases } = useQuery({
    queryKey: ['databases'],
    queryFn: () => assetsApi.discoverDatabases().then(r => r.data),
    staleTime: 5 * 60 * 1000,
  })
  const { data: schemas } = useQuery({
    queryKey: ['schemas', selectedDatabase],
    queryFn: () => assetsApi.discoverSchemas(selectedDatabase).then(r => r.data),
    enabled: !!selectedDatabase,
    staleTime: 5 * 60 * 1000,
  })
  const { data: tables } = useQuery({
    queryKey: ['tables', selectedDatabase, selectedSchema],
    queryFn: () => assetsApi.discoverTables(selectedDatabase, selectedSchema).then(r => r.data),
    enabled: !!selectedDatabase && !!selectedSchema,
    staleTime: 5 * 60 * 1000,
  })

  const { data: activeRun } = useQuery({
    queryKey: ['agent-run', activeRunId],
    queryFn: () => agentRunsApi.get(activeRunId!).then(r => r.data),
    enabled: !!activeRunId,
    refetchInterval: (query) => {
      const s = query.state.data?.status as RunStatus | undefined
      return s && isPolling(s) ? 2000 : false
    },
  })

  const runStatus    = (activeRun?.status ?? 'pending') as RunStatus
  const isRunning    = runStatus === 'running'
  const isAwaiting   = runStatus === 'awaiting_fixes'
  const isCompleted  = runStatus === 'completed'
  const isFailed     = runStatus === 'failed'

  const { data: recentRuns } = useQuery({
    queryKey: ['agent-runs'],
    queryFn: () => agentRunsApi.list().then(r => r.data),
    refetchInterval: activeRunId ? 5000 : false,
  })

  const startMutation = useMutation({
    mutationFn: (data: { database: string; schema_name: string; table: string }) =>
      agentRunsApi.start(data).then(r => r.data),
    onSuccess: (run) => {
      setActiveRunId(run.id)
      queryClient.invalidateQueries({ queryKey: ['agent-runs'] })
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

  const getTask = (name: string): AgentTask | undefined =>
    activeRun?.tasks.find(t => t.agent_name === name)

  const verifyTask   = getTask('verification_agent')
  const verifyOutput = verifyTask?.output
  const verifyDone   = verifyTask?.status === 'completed'

  const totalDuration = (() => {
    if (!activeRun?.started_at || !activeRun?.completed_at) return null
    const s = (new Date(activeRun.completed_at).getTime() - new Date(activeRun.started_at).getTime()) / 1000
    return formatDuration(s)
  })()

  // Rule Intelligence summary from task output
  const intelOutput = getTask('rule_intelligence_agent')?.output
  const findingsOutput = getTask('findings_agent')?.output

  return (
    <div className="max-w-7xl mx-auto space-y-6">

      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold text-gray-900">Agent Workflow</h1>
        <p className="mt-1 text-gray-600">
          AI-powered data quality pipeline — parallel scan, intelligent rule selection, findings, verify.
        </p>
      </div>

      {/* Target selector */}
      <div className="bg-white rounded-xl shadow p-6">
        <h2 className="text-sm font-semibold text-gray-700 mb-3 uppercase tracking-wide">Select Target Table</h2>
        <div className="grid grid-cols-3 gap-4 mb-4">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Database</label>
            <select value={selectedDatabase}
              onChange={e => { setSelectedDatabase(e.target.value); setSelectedSchema(''); setSelectedTable('') }}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-primary-500"
              disabled={isRunning}>
              <option value="">Choose database...</option>
              {databases?.databases.map(db => <option key={db} value={db}>{db}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Schema</label>
            <select value={selectedSchema}
              onChange={e => { setSelectedSchema(e.target.value); setSelectedTable('') }}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-primary-500"
              disabled={!selectedDatabase || isRunning}>
              <option value="">Choose schema...</option>
              {schemas?.schemas.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Table</label>
            <select value={selectedTable}
              onChange={e => setSelectedTable(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-primary-500"
              disabled={!selectedSchema || isRunning}>
              <option value="">Choose table...</option>
              {tables?.tables.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
        </div>
        <button onClick={() => startMutation.mutate({ database: selectedDatabase, schema_name: selectedSchema, table: selectedTable })}
          disabled={!selectedTable || isRunning}
          className="flex items-center gap-2 px-5 py-2.5 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm">
          {isRunning
            ? <><Loader2 className="w-4 h-4 animate-spin" />Running...</>
            : <><Play className="w-4 h-4" />Run Workflow</>
          }
        </button>
      </div>

      {/* Pipeline visualization */}
      {activeRunId && activeRun && (
        <div className="bg-white rounded-xl shadow p-6">
          <div className="flex items-center justify-between mb-5">
            <div>
              <h2 className="text-sm font-semibold text-gray-700 uppercase tracking-wide">Pipeline</h2>
              <p className="text-xs text-gray-400 mt-0.5 font-mono">
                {activeRun.database}.{activeRun.schema_name}.{activeRun.table}
              </p>
            </div>
            <div className="flex items-center gap-3">
              {totalDuration && <span className="text-xs text-gray-400 flex items-center gap-1"><Clock className="w-3 h-3" />{totalDuration}</span>}
              {isRunning && (
                <span className="flex items-center gap-1.5 text-xs text-blue-700 bg-blue-50 border border-blue-200 px-2.5 py-1 rounded-full font-medium">
                  <Loader2 className="w-3 h-3 animate-spin" />Running
                </span>
              )}
              {isAwaiting && (
                <span className="flex items-center gap-1.5 text-xs text-primary-700 bg-primary-50 border border-primary-200 px-2.5 py-1 rounded-full font-medium">
                  <Wrench className="w-3 h-3" />Awaiting Fixes
                </span>
              )}
              {isCompleted && (
                <span className="flex items-center gap-1.5 text-xs text-green-700 bg-green-50 border border-green-200 px-2.5 py-1 rounded-full font-medium">
                  <CheckCircle2 className="w-3.5 h-3.5" />Completed
                </span>
              )}
              {isFailed && (
                <span className="flex items-center gap-1.5 text-xs text-red-700 bg-red-50 border border-red-200 px-2.5 py-1 rounded-full font-medium">
                  <AlertTriangle className="w-3.5 h-3.5" />Failed
                </span>
              )}
            </div>
          </div>

          {/* Pipeline nodes with parallel group */}
          <div className="flex items-start overflow-x-auto pb-2 gap-0">
            {/* Coordinator */}
            <AgentNode agentDef={AGENTS[0]} task={getTask('coordinator')}
              isLast={false} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />

            {/* Parallel: Metadata + Rules Fetch */}
            <ParallelGroup>
              <AgentNode agentDef={AGENTS[1]} task={getTask('metadata_agent')}
                isLast={true} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
              <AgentNode agentDef={AGENTS[2]} task={getTask('rules_fetch_agent')}
                isLast={true} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
            </ParallelGroup>

            {/* Rule Intelligence */}
            <AgentNode agentDef={AGENTS[3]} task={getTask('rule_intelligence_agent')}
              isLast={false} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />

            {/* Findings */}
            <AgentNode agentDef={AGENTS[4]} task={getTask('findings_agent')}
              isLast={false} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />

            {/* Fix Issues (UI only) */}
            <AgentNode agentDef={AGENTS[5]} task={undefined}
              isLast={false} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />

            {/* Verification */}
            <AgentNode agentDef={AGENTS[6]} task={getTask('verification_agent')}
              isLast={true} runStatus={runStatus} scanId={activeRun.scan_id} navigate={navigate} />
          </div>

          {/* Stats row */}
          <div className="mt-5 pt-4 border-t border-gray-100 grid grid-cols-4 gap-4">
            <div className="text-center">
              <p className="text-2xl font-bold text-gray-900">{activeRun.findings_count}</p>
              <p className="text-xs text-gray-500">Findings</p>
            </div>
            <div className="text-center">
              <p className="text-2xl font-bold text-purple-600 flex items-center justify-center gap-1">
                <Sparkles className="w-5 h-5" />{activeRun.ai_rules_count}
              </p>
              <p className="text-xs text-gray-500">AI rules generated</p>
            </div>
            <div className="text-center">
              {verifyDone && verifyOutput ? (
                <>
                  <p className="text-2xl font-bold text-green-600">
                    {verifyOutput.resolved}/{verifyOutput.total_findings}
                  </p>
                  <p className="text-xs text-gray-500">Resolved</p>
                </>
              ) : (
                <>
                  <p className="text-2xl font-bold text-gray-300">—</p>
                  <p className="text-xs text-gray-400">Resolved</p>
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
                <p className="text-xs text-gray-400">—</p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Rule Intelligence summary (shown after it completes) */}
      {intelOutput && getTask('rule_intelligence_agent')?.status === 'completed' && (
        <div className="bg-white rounded-xl shadow p-6">
          <div className="flex items-center gap-2 mb-4">
            <BrainCircuit className="w-5 h-5 text-purple-600" />
            <h2 className="text-base font-semibold text-gray-900">Rule Intelligence Report</h2>
            <span className="text-xs bg-purple-50 border border-purple-200 text-purple-700 px-2 py-0.5 rounded-full font-medium">
              {intelOutput.table_type} · {intelOutput.table_type_confidence}% confidence
            </span>
          </div>
          <p className="text-sm text-gray-600 mb-4">{intelOutput.table_type_reason}</p>

          <div className="grid grid-cols-2 gap-6">
            {/* Rules used */}
            {intelOutput.existing_rules_selected > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-2">
                  Rules Used ({intelOutput.existing_rules_selected})
                </h3>
                <div className="space-y-1.5 max-h-48 overflow-y-auto">
                  {Object.entries(intelOutput.selected_with_overrides || {}).map(([code, info]: [string, any]) => (
                    <div key={code} className="flex items-start gap-2 text-xs">
                      <span className="text-green-500 mt-0.5 flex-shrink-0">✓</span>
                      <div>
                        <span className="font-mono font-medium text-gray-800">{code}</span>
                        {info.severity_override && (
                          <span className="ml-1 text-amber-600 font-medium">→ {info.severity_override}</span>
                        )}
                        {info.reason && <p className="text-gray-500 mt-0.5">{info.reason}</p>}
                      </div>
                    </div>
                  ))}
                  {/* Show remaining selected without overrides as a count */}
                  {intelOutput.existing_rules_selected - Object.keys(intelOutput.selected_with_overrides || {}).length > 0 && (
                    <p className="text-xs text-gray-400 mt-1">
                      + {intelOutput.existing_rules_selected - Object.keys(intelOutput.selected_with_overrides || {}).length} more rules applied without changes
                    </p>
                  )}
                </div>
              </div>
            )}

            {/* Rules skipped */}
            {Object.keys(intelOutput.skipped || {}).length > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-2">
                  Rules Skipped ({intelOutput.existing_rules_skipped})
                </h3>
                <div className="space-y-1.5 max-h-48 overflow-y-auto">
                  {Object.entries(intelOutput.skipped || {}).map(([code, reason]: [string, any]) => (
                    <div key={code} className="flex items-start gap-2 text-xs">
                      <span className="text-gray-300 mt-0.5 flex-shrink-0">–</span>
                      <div>
                        <span className="font-mono font-medium text-gray-500">{code}</span>
                        {reason && <p className="text-gray-400 mt-0.5">{reason}</p>}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* AI rules generated */}
          {intelOutput.ai_rules && intelOutput.ai_rules.length > 0 && (
            <div className="mt-4 pt-4 border-t border-gray-100">
              <h3 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-2 flex items-center gap-1">
                <Sparkles className="w-3.5 h-3.5 text-purple-500" />
                AI Rules Generated ({intelOutput.ai_rules.length})
              </h3>
              <div className="flex flex-wrap gap-2">
                {intelOutput.ai_rules.map((r: any) => (
                  <span key={r.code}
                    className={`text-xs px-2 py-1 rounded-full font-medium border ${
                      r.violated
                        ? 'bg-orange-50 border-orange-300 text-orange-800'
                        : 'bg-gray-50 border-gray-200 text-gray-600'
                    }`}>
                    {r.violated ? '⚠ ' : '✓ '}{r.code}
                  </span>
                ))}
              </div>
              <p className="text-xs text-gray-400 mt-2">
                Orange = violation detected · Gray = rule generated, no current violation
              </p>
            </div>
          )}
        </div>
      )}

      {/* Awaiting fixes banner */}
      {isAwaiting && activeRun?.scan_id && !verifyDone && (
        <div className="bg-primary-50 border-2 border-primary-300 rounded-xl p-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h3 className="font-semibold text-primary-900 text-base mb-1">
                🔧 Pipeline complete — fix the findings
              </h3>
              <p className="text-sm text-primary-800">
                <strong>{activeRun.findings_count}</strong> findings detected
                {activeRun.ai_rules_count > 0 && (
                  <> · <strong>{activeRun.ai_rules_count}</strong> AI rules generated</>
                )}.
                Open the Findings page, select issues, get AI SQL fixes, then verify.
              </p>
            </div>
            <div className="flex flex-col gap-2 flex-shrink-0">
              <button onClick={() => navigate(`/findings?scan_id=${activeRun.scan_id}`)}
                className="flex items-center gap-1.5 px-4 py-2 bg-primary-600 text-white rounded-lg text-sm font-medium hover:bg-primary-700 transition-colors">
                <Wrench className="w-4 h-4" />Fix Issues
              </button>
              <button onClick={() => verifyMutation.mutate(activeRunId!)}
                disabled={verifyMutation.isPending}
                className="flex items-center gap-1.5 px-4 py-2 border border-primary-300 text-primary-700 rounded-lg text-sm font-medium hover:bg-primary-100 disabled:opacity-50 transition-colors">
                {verifyMutation.isPending
                  ? <><Loader2 className="w-4 h-4 animate-spin" />Verifying...</>
                  : <><RefreshCw className="w-4 h-4" />Verify Fixes</>
                }
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Verification result banner */}
      {(isAwaiting || isCompleted) && verifyDone && verifyOutput && (() => {
        const resolved  = verifyOutput.resolved  ?? 0
        const total     = verifyOutput.total_findings ?? 0
        const remaining = verifyOutput.remaining ?? 0
        const pct       = verifyOutput.resolution_pct ?? 0
        const newAuto   = verifyOutput.newly_auto_resolved ?? 0
        const allDone   = remaining === 0
        return (
          <div className={`border-2 rounded-xl p-5 ${allDone ? 'bg-green-50 border-green-300' : 'bg-blue-50 border-blue-300'}`}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <h3 className={`font-semibold text-base mb-1 ${allDone ? 'text-green-900' : 'text-blue-900'}`}>
                  {allDone
                    ? '✅ All issues resolved — workflow complete!'
                    : `📊 Verification: ${resolved}/${total} fixed (${pct}%) — ${remaining} remaining`
                  }
                </h3>
                <p className={`text-sm ${allDone ? 'text-green-800' : 'text-blue-800'}`}>
                  {allDone
                    ? 'Every finding has been resolved. Great work!'
                    : `${remaining} finding${remaining !== 1 ? 's' : ''} still need attention.`
                  }
                </p>
                {newAuto > 0 && (
                  <p className="text-xs mt-1.5 text-green-700 font-medium">
                    ✓ {newAuto} auto-resolved by live Snowflake re-scan
                  </p>
                )}
              </div>
              {!allDone && (
                <div className="flex flex-col gap-2 flex-shrink-0">
                  <button onClick={() => navigate(`/findings?scan_id=${activeRun?.scan_id}`)}
                    className="flex items-center gap-1.5 px-3 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors">
                    <Wrench className="w-3.5 h-3.5" />Fix Remaining
                  </button>
                  <button onClick={() => verifyMutation.mutate(activeRunId!)}
                    disabled={verifyMutation.isPending}
                    className="flex items-center gap-1.5 px-3 py-2 border border-blue-300 text-blue-700 rounded-lg text-sm font-medium hover:bg-blue-50 disabled:opacity-50 transition-colors">
                    <RefreshCw className={`w-3.5 h-3.5 ${verifyMutation.isPending ? 'animate-spin' : ''}`} />
                    Verify Again
                  </button>
                </div>
              )}
            </div>
          </div>
        )
      })()}

      {/* Recent runs */}
      {!activeRunId && recentRuns && recentRuns.runs.length > 0 && (
        <div className="bg-white rounded-xl shadow p-6">
          <h2 className="text-base font-semibold text-gray-900 mb-4">Recent Runs</h2>
          <div className="space-y-2">
            {recentRuns.runs.slice(0, 5).map(run => (
              <button key={run.id} onClick={() => setActiveRunId(run.id)}
                className="w-full flex items-center justify-between p-3 rounded-lg border border-gray-200 hover:bg-gray-50 text-left transition-colors">
                <div>
                  <p className="text-sm font-medium text-gray-900">
                    {run.database}.{run.schema_name}.{run.table}
                  </p>
                  <p className="text-xs text-gray-400 mt-0.5">{new Date(run.created_at).toLocaleString()}</p>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-xs text-gray-500">{run.findings_count} findings</span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                    run.status === 'completed'      ? 'bg-green-100 text-green-700' :
                    run.status === 'failed'         ? 'bg-red-100 text-red-700' :
                    run.status === 'running'        ? 'bg-blue-100 text-blue-700' :
                    run.status === 'awaiting_fixes' ? 'bg-primary-100 text-primary-700' :
                    'bg-gray-100 text-gray-600'
                  }`}>{run.status.replace(/_/g, ' ')}</span>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

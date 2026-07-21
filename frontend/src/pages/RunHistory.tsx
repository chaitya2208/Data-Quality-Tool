import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { agentRunsApi } from '../api/client'
import { useConnection } from '../ConnectionContext'
import { fmtIST } from '../utils/dates'
import {
  History, Loader2, CheckCircle2, AlertTriangle, BrainCircuit,
  Wrench, Database, Search, Filter, ExternalLink, Clock, XCircle,
} from 'lucide-react'

type StatusFilter = 'all' | 'completed' | 'failed' | 'running' | 'awaiting_rule_review' | 'awaiting_fixes'
type OriginFilter = 'all' | 'scheduled' | 'manual'

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all',                  label: 'All'             },
  { value: 'completed',            label: 'Completed'       },
  { value: 'running',              label: 'Running'         },
  { value: 'awaiting_rule_review', label: 'Awaiting Review' },
  { value: 'awaiting_fixes',       label: 'Awaiting Fixes'  },
  { value: 'failed',               label: 'Failed'          },
]

// Human-readable node names for the failed-node indicator.
const AGENT_LABELS: Record<string, string> = {
  coordinator: 'Coordinator',
  metadata_agent: 'Metadata',
  rules_fetch_agent: 'Rules Fetch',
  relationship_discovery_agent: 'Relationship Discovery',
  profiling_agent: 'Profiling',
  rule_intelligence_agent: 'Rule Intelligence',
  findings_agent: 'Findings',
  verification_agent: 'Verification',
}

function statusBadge(status: string) {
  switch (status) {
    case 'completed':
      return <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium bg-green-100 text-green-700"><CheckCircle2 className="w-3 h-3" />Completed</span>
    case 'failed':
      return <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium bg-red-100 text-red-700"><AlertTriangle className="w-3 h-3" />Failed</span>
    case 'running':
      return <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium bg-blue-100 text-blue-700"><Loader2 className="w-3 h-3 animate-spin" />Running</span>
    case 'awaiting_rule_review':
      return <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium bg-purple-100 text-purple-700"><BrainCircuit className="w-3 h-3" />Awaiting Review</span>
    case 'awaiting_fixes':
      return <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium bg-orange-100 text-orange-700"><Wrench className="w-3 h-3" />Awaiting Fixes</span>
    default:
      return <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-gray-100 text-gray-600">{status.replace(/_/g, ' ')}</span>
  }
}

const PAGE_SIZE = 20

export default function RunHistory() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [cancellingId, setCancellingId] = useState<string | null>(null)
  const [searchParams] = useSearchParams()
  const [search, setSearch]             = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [originFilter, setOriginFilter] = useState<OriginFilter>('all')
  const [dbFilter, setDbFilter]         = useState(() => searchParams.get('db') ?? '')
  const [schemaFilter, setSchemaFilter] = useState(() => searchParams.get('schema') ?? '')
  const [tableFilter, setTableFilter]   = useState(() => searchParams.get('table') ?? '')
  const [page, setPage] = useState(1)

  // Non-Snowflake sources scope by connection_id. Snowflake shows all runs
  // (connection row may be deleted/recreated but runs should survive that).
  const { selectedId: connId, selected } = useConnection()
  const isSnowflake = (selected?.type ?? '').toLowerCase() === 'snowflake'
  const effectiveConnId = (!connId || isSnowflake) ? undefined : connId

  const queryParams = {
    page,
    page_size: PAGE_SIZE,
    status:      statusFilter !== 'all' ? statusFilter : undefined,
    origin:      originFilter !== 'all' ? originFilter : undefined,
    database:    dbFilter     || undefined,
    schema_name: schemaFilter || undefined,
    table:       tableFilter  || undefined,
    search:      search       || undefined,
    connection_id: effectiveConnId,
  }

  const { data, isLoading } = useQuery({
    queryKey: ['agent-runs-history', queryParams],
    queryFn: () => agentRunsApi.list(queryParams).then(r => r.data),
    refetchInterval: 10_000,
  })

  // Filter options — distinct db/schema/table values across ALL runs (not just
  // the current page), used to populate the cascading dropdowns.
  const { data: filterOpts } = useQuery({
    queryKey: ['agent-runs-filter-options'],
    queryFn: () => agentRunsApi.filterOptions().then(r => r.data),
    staleTime: 60_000,
  })

  const totalRuns  = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(totalRuns / PAGE_SIZE))
  const runs       = data?.runs ?? []

  const dbOptions     = filterOpts?.databases ?? []
  const schemaOptions = dbFilter ? (filterOpts?.schemas?.[dbFilter] ?? []) : []
  const tableOptions  = (dbFilter && schemaFilter) ? (filterOpts?.tables?.[`${dbFilter}.${schemaFilter}`] ?? []) : []

  async function handleCancel(runId: string) {
    if (!window.confirm('Cancel this run? It will be marked as failed.')) return
    setCancellingId(runId)
    try {
      await agentRunsApi.cancel(runId)
      queryClient.invalidateQueries({ queryKey: ['agent-runs-history'] })
    } catch {
      // ignore — run list will refresh on its own
    } finally {
      setCancellingId(null)
    }
  }

  const stats = {
    total:     totalRuns,
    completed: runs.filter(r => r.status === 'completed').length,
    failed:    runs.filter(r => r.status === 'failed').length,
    active:    runs.filter(r => ['running', 'awaiting_rule_review', 'awaiting_fixes', 'pending'].includes(r.status)).length,
  }

  return (
    <div className="space-y-6">

      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Run History</h1>
        <p className="mt-1 text-gray-600 dark:text-gray-300">All past and active agent workflow runs.</p>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {[
          { label: 'Total Runs',  value: stats.total,     color: 'text-gray-900 dark:text-gray-100' },
          { label: 'Completed',   value: stats.completed, color: 'text-green-600' },
          { label: 'Active',      value: stats.active,    color: 'text-blue-600'  },
          { label: 'Failed',      value: stats.failed,    color: 'text-red-600'   },
        ].map(s => (
          <div key={s.label} className="bg-white dark:bg-gray-800 rounded-xl shadow p-4 text-center">
            <p className={`text-2xl font-bold ${s.color}`}>{s.value}</p>
            <p className="text-xs text-gray-500 dark:text-gray-300 mt-0.5">{s.label}</p>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-4 space-y-3">
        <div className="flex flex-col sm:flex-row gap-3">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              value={search}
              onChange={e => { setSearch(e.target.value); setPage(1) }}
              placeholder="Search by database, schema, or table..."
              className="w-full pl-9 pr-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 dark:bg-gray-800 dark:text-gray-100"
            />
          </div>
          <div className="flex items-center gap-2">
            <Filter className="w-4 h-4 text-gray-400 flex-shrink-0" />
            <select
              value={statusFilter}
              onChange={e => { setStatusFilter(e.target.value as StatusFilter); setPage(1) } }
              className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 dark:bg-gray-800 dark:text-gray-100"
            >
              {STATUS_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
        </div>

        {/* Cascading DB / schema / table + origin filters */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <select
            value={dbFilter}
            onChange={e => { setDbFilter(e.target.value); setSchemaFilter(''); setTableFilter(''); setPage(1) } }
            className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 dark:bg-gray-800 dark:text-gray-100"
          >
            <option value="">All Databases</option>
            {dbOptions.map(d => <option key={d} value={d}>{d}</option>)}
          </select>
          <select
            value={schemaFilter}
            onChange={e => { setSchemaFilter(e.target.value); setTableFilter(''); setPage(1) } }
            className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 dark:bg-gray-800 dark:text-gray-100"
          >
            <option value="">All Schemas</option>
            {schemaOptions.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <select
            value={tableFilter}
            onChange={e => setTableFilter(e.target.value)}
            className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 dark:bg-gray-800 dark:text-gray-100"
          >
            <option value="">All Tables</option>
            {tableOptions.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          <select
            value={originFilter}
            onChange={e => { setOriginFilter(e.target.value as OriginFilter); setPage(1) } }
            className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 dark:bg-gray-800 dark:text-gray-100"
          >
            <option value="all">All Runs</option>
            <option value="scheduled">Scheduled</option>
            <option value="manual">Manual</option>
          </select>
        </div>
      </div>

      {/* Run list */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
        {isLoading ? (
          <div className="p-12 flex items-center justify-center gap-2 text-gray-400">
            <Loader2 className="w-5 h-5 animate-spin" />Loading runs...
          </div>
        ) : runs.length === 0 ? (
          <div className="p-12 text-center">
            <History className="w-12 h-12 text-gray-200 mx-auto mb-3" />
            <p className="text-gray-900 dark:text-gray-100 font-medium mb-1">
              {totalRuns === 0 ? 'No runs yet' : 'No runs match your filters'}
            </p>
            <p className="text-sm text-gray-400 dark:text-gray-400">
              {totalRuns === 0
                ? 'Start a workflow from the Workflow page to see runs here.'
                : 'Try changing the status filter or search term.'}
            </p>
          </div>
        ) : (
          <div className="divide-y divide-gray-100 dark:divide-gray-700">
            {/* Table header */}
            <div className="hidden sm:grid grid-cols-[1fr_minmax(130px,auto)_minmax(90px,auto)_minmax(80px,auto)_80px] gap-4 px-6 py-3 bg-gray-50 dark:bg-gray-900 text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
              <span>Target</span>
              <span>Status</span>
              <span>Findings</span>
              <span>AI Rules</span>
              <span className="text-right">Actions</span>
            </div>

            {runs.map(run => {
              // Nodes that failed during this run — surfaced even when the run
              // itself isn't 'failed' (e.g. a soft node failure the pipeline
              // continued past), so a partial failure is never invisible.
              const failedTasks = (run.tasks ?? []).filter(t => t.status === 'failed')
              return (
                <div
                  key={run.id}
                  className="px-6 py-4 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors"
                >
                  <div className="flex flex-col sm:grid sm:grid-cols-[1fr_minmax(130px,auto)_minmax(90px,auto)_minmax(80px,auto)_80px] sm:gap-4 sm:items-center gap-2">

                    {/* Target */}
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Database className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
                        <span className="text-sm font-medium text-gray-900 dark:text-gray-100 font-mono truncate">
                          {run.database}.{run.schema_name}.{run.table}
                        </span>
                        {run.schedule_id && (
                          <span className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300 flex-shrink-0" title="Fired by a schedule">
                            <Clock className="w-2.5 h-2.5" />Scheduled
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 mt-1">
                        <span className="text-xs text-gray-400 dark:text-gray-400">
                          {fmtIST(run.created_at)}
                        </span>
                        {run.batch_id && (
                          <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">
                            batch #{run.batch_index + 1}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Status */}
                    <div className="flex items-center">{statusBadge(run.status)}</div>

                    {/* Findings */}
                    <div className="flex items-center gap-1">
                      {run.findings_count > 0 ? (
                        <button
                          onClick={() => run.scan_id && navigate(`/findings?scan_id=${run.scan_id}`)}
                          disabled={!run.scan_id}
                          className="flex items-center gap-1 text-xs text-orange-700 dark:text-orange-300 font-medium hover:underline disabled:no-underline disabled:text-gray-500"
                        >
                          {run.findings_count} findings
                          {run.scan_id && <ExternalLink className="w-3 h-3" />}
                        </button>
                      ) : (
                        <span className="text-xs text-gray-400 dark:text-gray-500">—</span>
                      )}
                    </div>

                    {/* AI rules */}
                    <div className="flex items-center text-xs text-gray-500 dark:text-gray-400">
                      {run.instance_review_state != null
                        ? <span className="text-purple-600 font-medium">{run.ai_rules_proposed} AI</span>
                        : <span className="text-gray-400">—</span>
                      }
                    </div>

                    {/* Actions */}
                    <div className="flex items-center justify-end gap-2">
                      {(run.status === 'running' || run.status === 'pending') && (
                        <button
                          onClick={() => handleCancel(run.id)}
                          disabled={cancellingId === run.id}
                          className="text-xs px-2 py-1 border border-red-300 dark:border-red-700 rounded-lg text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex-shrink-0 flex items-center gap-1"
                          title="Cancel this run"
                        >
                          {cancellingId === run.id
                            ? <Loader2 className="w-3 h-3 animate-spin" />
                            : <XCircle className="w-3 h-3" />
                          }
                          Cancel
                        </button>
                      )}
                      <button
                        onClick={() => navigate(`/workflow?run_id=${run.id}`)}
                        className="text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors flex-shrink-0"
                        title="Open in Workflow"
                      >
                        Open
                      </button>
                    </div>

                  </div>

                  {/* Error message */}
                  {run.status === 'failed' && run.error_message && (
                    <p className="mt-2 text-xs text-red-600 dark:text-red-400 font-mono bg-red-50 dark:bg-red-950/30 px-3 py-1.5 rounded truncate" title={run.error_message}>
                      {run.error_message}
                    </p>
                  )}

                  {/* Per-node failures — which node failed, when, and why. Shown
                      even if the run status isn't 'failed' (soft node failures). */}
                  {failedTasks.length > 0 && (
                    <div className="mt-2 flex flex-col gap-1">
                      {failedTasks.map(t => (
                        <div
                          key={t.id}
                          className="flex items-start gap-2 text-xs bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800/50 px-3 py-1.5 rounded"
                          title={t.error_message ?? undefined}
                        >
                          <AlertTriangle className="w-3.5 h-3.5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
                          <span className="min-w-0">
                            <span className="font-medium text-amber-800 dark:text-amber-200">
                              {AGENT_LABELS[t.agent_name] ?? t.agent_name} failed
                            </span>
                            {t.completed_at && (
                              <span className="text-amber-600 dark:text-amber-400/80">
                                {' · '}{fmtIST(t.completed_at)}
                              </span>
                            )}
                            {t.error_message && (
                              <span className="block text-amber-700 dark:text-amber-300/90 font-mono truncate">
                                {t.error_message}
                              </span>
                            )}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Pagination */}
      {!isLoading && totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-xs text-gray-400 dark:text-gray-500">
            Page {page} of {totalPages} · {totalRuns} total runs · auto-refreshes every 10s
          </p>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page === 1}
              className="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-600 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              Previous
            </button>
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page === totalPages}
              className="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-600 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              Next
            </button>
          </div>
        </div>
      )}
      {!isLoading && totalPages === 1 && runs.length > 0 && (
        <p className="text-xs text-gray-400 dark:text-gray-500 text-right">
          {totalRuns} runs · auto-refreshes every 10s
        </p>
      )}
    </div>
  )
}

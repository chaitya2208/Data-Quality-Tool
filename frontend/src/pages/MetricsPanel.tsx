import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { LineChart, Line, ResponsiveContainer, YAxis } from 'recharts'
import {
  Activity, AlertTriangle, ChevronDown, ChevronRight, Loader2, Sparkles,
  ArrowUpRight, Info, Plus, X, Trash2,
} from 'lucide-react'
import { tableHealthApi, metricsApi, profilingApi } from '../api/client'
import type { AssetMetricRow, MetricCatalogEntry, ColumnMeta } from '../api/client'
import { useConnection } from '../ConnectionContext'

// ── Panel props ──────────────────────────────────────────────────────────

interface Props {
  database: string
  schema: string
  table: string
}

// ── Constants ────────────────────────────────────────────────────────────

const MATURITY_MIN_SAMPLES = 14
// Anything ≥ this many MADs from median counts as "breached" for the status
// pill. Matches AnomalyProposalAgent's default threshold.
const BREACH_DEVIATIONS = 3.0
// Yellow zone starts here — user should be aware but not alarmed.
const WATCH_DEVIATIONS = 2.0

type Status = 'breached' | 'watch' | 'warming_up' | 'healthy'

// ── Helpers ──────────────────────────────────────────────────────────────

function fmt(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
  return v.toLocaleString(undefined, { maximumFractionDigits: digits })
}

function statusOf(m: AssetMetricRow): Status {
  if (m.sample_count < MATURITY_MIN_SAMPLES) return 'warming_up'
  const d = m.deviations_from_median
  if (d === null || d === undefined) return 'healthy'
  if (d >= BREACH_DEVIATIONS) return 'breached'
  if (d >= WATCH_DEVIATIONS) return 'watch'
  return 'healthy'
}

function metricLabel(m: string): string {
  switch (m) {
    case 'row_count':            return 'Row count'
    case 'freshness_lag_hours':  return 'Freshness lag'
    case 'null_pct':             return 'Null %'
    case 'distinct_count':       return 'Distinct count'
    case 'observed_categories':  return 'Observed categories'
    default: return m
  }
}

function metricUnit(m: string): string {
  switch (m) {
    case 'freshness_lag_hours': return 'hrs'
    case 'null_pct':            return '%'
    default: return ''
  }
}

function statusMeta(s: Status): { pill: string; label: string; ring: string; dot: string } {
  switch (s) {
    case 'breached':
      return {
        pill: 'bg-red-50 text-red-700 border-red-200 dark:bg-red-900/30 dark:text-red-300 dark:border-red-700/50',
        label: 'Breached',
        ring: 'ring-1 ring-red-200 dark:ring-red-800/50',
        dot: 'bg-red-500',
      }
    case 'watch':
      return {
        pill: 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-900/30 dark:text-amber-300 dark:border-amber-700/50',
        label: 'Watch',
        ring: '',
        dot: 'bg-amber-500',
      }
    case 'warming_up':
      return {
        pill: 'bg-gray-50 text-gray-600 border-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:border-gray-700',
        label: 'Warming up',
        ring: '',
        dot: 'bg-gray-300',
      }
    case 'healthy':
    default:
      return {
        pill: 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-900/30 dark:text-emerald-300 dark:border-emerald-700/50',
        label: 'Healthy',
        ring: '',
        dot: 'bg-emerald-500',
      }
  }
}

function hrefFor(assetId: string, m: AssetMetricRow): string {
  const q = new URLSearchParams({ metric: m.metric_name })
  if (m.column_name) q.set('column', m.column_name)
  return `/metrics/${assetId}?${q.toString()}`
}

// ── Panel ────────────────────────────────────────────────────────────────

export default function MetricsPanel({ database, schema, table }: Props) {
  const health = useQuery({
    queryKey: ['table-health', database, schema, table],
    queryFn: () => tableHealthApi.get(database, schema, table).then(r => r.data),
    staleTime: 60_000,
  })
  const assetId = health.data?.asset_id ?? null
  const [pickerOpen, setPickerOpen] = useState(false)

  const metrics = useQuery({
    queryKey: ['asset-metrics', assetId],
    queryFn: () => metricsApi.listForAsset(assetId!).then(r => r.data),
    enabled: !!assetId,
    staleTime: 30_000,
  })

  const rows = metrics.data?.metrics ?? []
  const tableLevel = useMemo(() => rows.filter(m => !m.column_name), [rows])
  const byColumn = useMemo(() => {
    const g: Record<string, AssetMetricRow[]> = {}
    for (const m of rows) if (m.column_name) (g[m.column_name] ??= []).push(m)
    return g
  }, [rows])

  const attention = useMemo(() => {
    return rows
      .map(m => ({ m, s: statusOf(m) }))
      .filter(x => x.s === 'breached' || x.s === 'watch')
      .sort((a, b) => (b.m.deviations_from_median ?? 0) - (a.m.deviations_from_median ?? 0))
  }, [rows])

  if (health.isLoading || (assetId && metrics.isLoading)) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
      </div>
    )
  }

  if (!assetId) {
    return <EmptyState kind="no-asset" />
  }

  if (rows.length === 0) {
    return (
      <>
        <EmptyState kind="no-metrics" onTrack={() => setPickerOpen(true)} />
        {pickerOpen && (
          <TrackMetricModal
            assetId={assetId} database={database} schema={schema} table={table}
            existing={rows}
            onClose={() => setPickerOpen(false)}
          />
        )}
      </>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header actions */}
      <div className="flex items-center justify-end">
        <button
          onClick={() => setPickerOpen(true)}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700"
        >
          <Plus className="w-4 h-4" /> Track metric
        </button>
      </div>

      {/* Attention row */}
      {attention.length > 0 && (
        <div className="space-y-2">
          <SectionHeader
            icon={<AlertTriangle className="w-4 h-4 text-red-500" />}
            title="Needs attention"
            subtitle={`${attention.length} metric${attention.length === 1 ? '' : 's'} outside baseline`}
          />
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {attention.slice(0, 4).map(({ m }) => (
              <AttentionCard key={`${m.metric_name}:${m.column_name ?? ''}`} m={m} assetId={assetId} />
            ))}
          </div>
        </div>
      )}

      {/* Table-level */}
      {tableLevel.length > 0 && (
        <div className="space-y-2">
          <SectionHeader
            icon={<Activity className="w-4 h-4 text-primary-600" />}
            title="Table-level"
            subtitle="Vital signs captured at every scan"
          />
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {tableLevel.map(m => (
              <MetricCard key={m.metric_name} m={m} assetId={assetId} />
            ))}
          </div>
        </div>
      )}

      {/* Per-column */}
      {Object.keys(byColumn).length > 0 && (
        <div className="space-y-2">
          <SectionHeader
            icon={<Sparkles className="w-4 h-4 text-primary-600" />}
            title="Per-column"
            subtitle={`${Object.keys(byColumn).length} column${Object.keys(byColumn).length === 1 ? '' : 's'} monitored`}
          />
          <div className="space-y-2">
            {Object.entries(byColumn)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([col, ms]) => (
                <ColumnGroup key={col} column={col} metrics={ms} assetId={assetId} />
              ))}
          </div>
        </div>
      )}

      {pickerOpen && (
        <TrackMetricModal
          assetId={assetId} database={database} schema={schema} table={table}
          existing={rows}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  )
}

// ── Section header ──────────────────────────────────────────────────────

function SectionHeader({
  icon, title, subtitle,
}: { icon: React.ReactNode; title: string; subtitle?: string }) {
  return (
    <div className="flex items-baseline gap-2 pb-1">
      <span className="flex items-center gap-1.5">
        {icon}
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100 uppercase tracking-wide">{title}</h3>
      </span>
      {subtitle && (
        <span className="text-xs text-gray-500 dark:text-gray-400">· {subtitle}</span>
      )}
    </div>
  )
}

// ── Metric card ─────────────────────────────────────────────────────────

function MetricCard({ m, assetId }: { m: AssetMetricRow; assetId: string }) {
  const status = statusOf(m)
  const meta = statusMeta(status)
  const unit = metricUnit(m.metric_name)
  const data = m.history.map(h => ({ v: h.value }))
  const showBaseline = m.median !== null && status !== 'warming_up'
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)

  const unmonitor = useMutation({
    mutationFn: () => metricsApi.unenroll(assetId, {
      column_name: m.column_name, metric_name: m.metric_name,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['asset-metrics', assetId] })
    },
  })

  return (
    <div
      className={`group relative bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 hover:border-primary-300 dark:hover:border-primary-600 transition-colors ${meta.ring}`}
    >
      <Link to={hrefFor(assetId, m)} className="block p-4">
        <div className="flex items-start justify-between gap-2 mb-2">
          <div className="min-w-0 pr-8">
            <div className="flex items-center gap-1.5">
              <span className={`inline-block w-1.5 h-1.5 rounded-full ${meta.dot}`} />
              <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                {metricLabel(m.metric_name)}
              </p>
            </div>
            {m.column_name && (
              <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-0.5 font-mono truncate">{m.column_name}</p>
            )}
          </div>
          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border whitespace-nowrap ${meta.pill}`}>
            {meta.label}
          </span>
        </div>

        <div className="flex items-baseline gap-1">
          <p className="text-2xl font-semibold text-gray-900 dark:text-gray-100 tabular-nums">
            {fmt(m.latest_value)}
          </p>
          {unit && <span className="text-xs text-gray-500 dark:text-gray-400">{unit}</span>}
        </div>

        <div className="h-10 mt-2 -mx-1">
          {data.length >= 2 ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 4, right: 2, left: 2, bottom: 0 }}>
                <YAxis hide domain={['dataMin', 'dataMax']} />
                <Line
                  type="monotone" dataKey="v" stroke="currentColor"
                  strokeWidth={1.5} dot={false} isAnimationActive={false}
                  className={
                    status === 'breached' ? 'text-red-500' :
                    status === 'watch'    ? 'text-amber-500' :
                    status === 'warming_up' ? 'text-gray-400' :
                                            'text-emerald-500'
                  }
                />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-full flex items-center text-[11px] text-gray-400 dark:text-gray-500">Not enough history yet</div>
          )}
        </div>

        <div className="mt-2 flex items-center justify-between text-[11px] text-gray-500 dark:text-gray-400">
          <span>
            {showBaseline
              ? <>median <span className="tabular-nums text-gray-700 dark:text-gray-300">{fmt(m.median)}</span></>
              : <>{m.sample_count}/{MATURITY_MIN_SAMPLES} samples</>}
          </span>
          <span className="inline-flex items-center gap-0.5 text-primary-600 dark:text-primary-400 opacity-0 group-hover:opacity-100 transition-opacity">
            View <ArrowUpRight className="w-3 h-3" />
          </span>
        </div>
      </Link>
      {/* Overlaid unmonitor affordance — appears on hover, top-right. */}
      {!confirming ? (
        <button
          onClick={() => setConfirming(true)}
          title="Stop tracking this metric"
          className="absolute top-3 right-3 p-1 rounded text-gray-300 hover:text-red-600 dark:text-gray-500 dark:hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity"
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      ) : (
        <div className="absolute top-2 right-2 flex items-center gap-1 bg-white dark:bg-gray-800 border border-red-200 dark:border-red-700/60 rounded px-1 py-0.5 shadow">
          <button
            disabled={unmonitor.isPending}
            onClick={() => unmonitor.mutate()}
            className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
          >
            {unmonitor.isPending ? 'Removing…' : 'Confirm'}
          </button>
          <button
            onClick={() => setConfirming(false)}
            className="text-[10px] font-medium px-1.5 py-0.5 rounded text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  )
}

// ── Attention card (larger, emphasises the deviation) ───────────────────

function AttentionCard({ m, assetId }: { m: AssetMetricRow; assetId: string }) {
  const status = statusOf(m)
  const meta = statusMeta(status)
  const unit = metricUnit(m.metric_name)
  const data = m.history.map(h => ({ v: h.value }))
  const deviation = m.deviations_from_median

  return (
    <Link
      to={hrefFor(assetId, m)}
      className={`group block bg-white dark:bg-gray-800 rounded-xl border-2 p-4 transition-colors ${
        status === 'breached'
          ? 'border-red-300 dark:border-red-700/60 hover:border-red-400 dark:hover:border-red-500'
          : 'border-amber-300 dark:border-amber-700/60 hover:border-amber-400 dark:hover:border-amber-500'
      }`}
    >
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">
            {metricLabel(m.metric_name)}
            {m.column_name && <span className="text-gray-400 font-normal"> · {m.column_name}</span>}
          </p>
          <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-0.5">
            {deviation !== null ? `${deviation.toFixed(1)}σ from baseline` : 'outside baseline'}
          </p>
        </div>
        <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${meta.pill}`}>
          {meta.label}
        </span>
      </div>

      <div className="flex items-end justify-between gap-3">
        <div>
          <div className="flex items-baseline gap-1">
            <p className="text-3xl font-semibold text-gray-900 dark:text-gray-100 tabular-nums">{fmt(m.latest_value)}</p>
            {unit && <span className="text-sm text-gray-500 dark:text-gray-400">{unit}</span>}
          </div>
          <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-1">
            baseline <span className="tabular-nums text-gray-700 dark:text-gray-300">{fmt(m.median)}</span>
          </p>
        </div>
        <div className="w-24 h-10">
          {data.length >= 2 && (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 2, right: 2, left: 2, bottom: 0 }}>
                <YAxis hide domain={['dataMin', 'dataMax']} />
                <Line
                  type="monotone" dataKey="v"
                  stroke={status === 'breached' ? '#ef4444' : '#f59e0b'}
                  strokeWidth={2} dot={false} isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </Link>
  )
}

// ── Column group (collapsible) ──────────────────────────────────────────

function ColumnGroup({
  column, metrics, assetId,
}: { column: string; metrics: AssetMetricRow[]; assetId: string }) {
  const anyUnhealthy = metrics.some(m => {
    const s = statusOf(m)
    return s === 'breached' || s === 'watch'
  })
  const [open, setOpen] = useState<boolean>(anyUnhealthy)

  const counts = useMemo(() => {
    const c = { breached: 0, watch: 0, warming_up: 0, healthy: 0 }
    for (const m of metrics) c[statusOf(m)]++
    return c
  }, [metrics])

  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors"
      >
        {open ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />}
        <span className="text-sm font-mono font-medium text-gray-900 dark:text-gray-100 flex-1 text-left truncate">
          {column}
        </span>
        <span className="flex items-center gap-1.5">
          {counts.breached > 0 && <StatusChip count={counts.breached} kind="breached" />}
          {counts.watch > 0 &&    <StatusChip count={counts.watch} kind="watch" />}
          {counts.warming_up > 0 && <StatusChip count={counts.warming_up} kind="warming_up" />}
          {counts.healthy > 0 && counts.breached === 0 && counts.watch === 0 && (
            <StatusChip count={counts.healthy} kind="healthy" />
          )}
        </span>
      </button>
      {open && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 p-3 pt-1 border-t border-gray-100 dark:border-gray-700">
          {metrics.map(m => (
            <MetricCard key={m.metric_name} m={m} assetId={assetId} />
          ))}
        </div>
      )}
    </div>
  )
}

function StatusChip({ count, kind }: { count: number; kind: Status }) {
  const meta = statusMeta(kind)
  return (
    <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded border ${meta.pill}`}>
      <span className={`inline-block w-1.5 h-1.5 rounded-full ${meta.dot}`} />
      {count}
    </span>
  )
}

// ── Empty state ─────────────────────────────────────────────────────────

function EmptyState({ kind, onTrack }: { kind: 'no-asset' | 'no-metrics'; onTrack?: () => void }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl border border-dashed border-gray-300 dark:border-gray-600 p-10 text-center">
      <Activity className="w-10 h-10 text-gray-300 dark:text-gray-600 mx-auto mb-3" />
      {kind === 'no-asset' ? (
        <>
          <p className="text-gray-900 dark:text-gray-100 font-medium mb-1">No asset record for this table yet</p>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Metrics are captured during scans. Run a scan on this table to register it and start recording.
          </p>
        </>
      ) : (
        <>
          <p className="text-gray-900 dark:text-gray-100 font-medium mb-1">No metric baselines yet</p>
          <p className="text-sm text-gray-500 dark:text-gray-400 max-w-md mx-auto">
            Metrics are captured every scan. After {MATURITY_MIN_SAMPLES} scans, rolling median + MAD is computed and anomaly detection kicks in automatically.
          </p>
          <p className="text-[11px] text-gray-400 mt-3 inline-flex items-center gap-1">
            <Info className="w-3 h-3" /> Approving an anomaly proposal in the Rule Library also seeds the first metric.
          </p>
          {onTrack && (
            <button
              onClick={onTrack}
              className="mt-5 inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700"
            >
              <Plus className="w-4 h-4" /> Track a metric manually
            </button>
          )}
        </>
      )}
    </div>
  )
}

// ── Track-metric modal ──────────────────────────────────────────────────
// Metric-first flow: user picks WHAT to measure, then WHERE. The metric list
// is grouped by category (Table-level / Any column / Numeric-only / …) so
// browsing maps to a mental model. The column list is filtered to types the
// chosen metric is compatible with, so incompatibility is prevented not
// explained.

function isNumericType(dt: string): boolean {
  const t = (dt || '').toUpperCase()
  return ['NUMBER', 'INT', 'BIGINT', 'SMALLINT', 'TINYINT', 'FLOAT',
          'DOUBLE', 'DECIMAL', 'NUMERIC', 'REAL'].some(p => t.startsWith(p))
}
function isStringType(dt: string): boolean {
  const t = (dt || '').toUpperCase()
  return ['VARCHAR', 'STRING', 'TEXT', 'CHAR'].some(p => t.startsWith(p))
}

// Buckets shown in the metric picker, in display order. Each metric_name maps
// to exactly one bucket — the bucket determines both the section header and
// which columns are compatible.
type MetricBucket = 'table' | 'any_col' | 'numeric' | 'string' | 'special'

function bucketFor(entry: MetricCatalogEntry): MetricBucket {
  if (entry.scope === 'table') return 'table'
  if (entry.compatible.includes('any')) return 'any_col'
  if (entry.compatible.includes('numeric')) return 'numeric'
  if (entry.compatible.includes('string')) return 'string'
  return 'special'
}

const BUCKET_ORDER: MetricBucket[] = ['table', 'any_col', 'numeric', 'string', 'special']
const BUCKET_HEADINGS: Record<MetricBucket, string> = {
  table:   'Whole table',
  any_col: 'Any column',
  numeric: 'Numeric columns',
  string:  'String columns',
  special: 'Special',
}
const BUCKET_HINTS: Record<MetricBucket, string> = {
  table:   'measured across the entire table',
  any_col: 'works on columns of any type',
  numeric: 'requires a numeric column',
  string:  'requires a string column',
  special: 'requires a matching column classification',
}

function columnCompatibleWith(entry: MetricCatalogEntry, col: ColumnMeta): boolean {
  if (entry.compatible.includes('any')) return true
  if (entry.compatible.includes('numeric') && isNumericType(col.data_type)) return true
  if (entry.compatible.includes('string')  && isStringType(col.data_type))  return true
  if (entry.compatible.some(c => ['categorical', 'email', 'phone'].includes(c))) {
    return isStringType(col.data_type) || isNumericType(col.data_type)
  }
  return false
}

function TrackMetricModal({
  assetId, database, schema, table, existing, onClose,
}: {
  assetId: string
  database: string; schema: string; table: string
  existing: AssetMetricRow[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const { selectedId } = useConnection()
  const [selectedMetric, setSelectedMetric] = useState<MetricCatalogEntry | null>(null)
  const [column, setColumn] = useState<string | null>(null)
  const [columnFilter, setColumnFilter] = useState('')
  const [error, setError] = useState<string | null>(null)

  const catalog = useQuery({
    queryKey: ['metric-catalog'],
    queryFn: () => metricsApi.catalog().then(r => r.data),
    staleTime: 5 * 60_000,
  })
  const columnsQuery = useQuery({
    queryKey: ['columns', database, schema, table, selectedId],
    queryFn: () => profilingApi.columns(database, schema, table, selectedId).then(r => r.data),
    staleTime: 60_000,
  })

  const alreadyEnrolled = useMemo(() => {
    return new Set(existing.map(m => `${m.column_name ?? ''}::${m.metric_name}`))
  }, [existing])

  // Group all metrics into buckets so the UI can render sections in one pass.
  const metricsByBucket = useMemo(() => {
    const groups: Record<MetricBucket, MetricCatalogEntry[]> = {
      table: [], any_col: [], numeric: [], string: [], special: [],
    }
    for (const e of (catalog.data?.metrics ?? [])) {
      groups[bucketFor(e)].push(e)
    }
    return groups
  }, [catalog.data])

  const columnOptions = columnsQuery.data?.columns ?? []

  // Compatible columns for the currently-picked metric (empty if none picked
  // or the metric is table-level).
  const compatibleColumns = useMemo(() => {
    if (!selectedMetric || selectedMetric.scope === 'table') return []
    const q = columnFilter.trim().toLowerCase()
    return columnOptions
      .filter(c => columnCompatibleWith(selectedMetric, c))
      .filter(c => !q || c.column_name.toLowerCase().includes(q))
  }, [selectedMetric, columnOptions, columnFilter])

  const enroll = useMutation({
    mutationFn: () => metricsApi.enroll(assetId, {
      column_name: selectedMetric?.scope === 'table' ? null : column,
      metric_name: selectedMetric!.metric_name,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['asset-metrics', assetId] })
      onClose()
    },
    onError: (err: any) => {
      setError(err?.response?.data?.detail || err?.message || 'Enrollment failed')
    },
  })

  const targetChosen = selectedMetric?.scope === 'table' || !!column
  const canSubmit = !!selectedMetric && targetChosen && !enroll.isPending

  // Confirmation label reads back the whole decision as a sentence.
  const confirmLabel = selectedMetric
    ? selectedMetric.scope === 'table'
      ? `Track ${selectedMetric.label} on this table`
      : column
        ? `Track ${selectedMetric.label} on ${column}`
        : `Choose a column`
    : 'Pick a metric first'

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        onClick={e => e.stopPropagation()}
        className="w-full max-w-2xl mx-4 max-h-[90vh] flex flex-col bg-white dark:bg-gray-800 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-700 overflow-hidden"
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700 flex-shrink-0">
          <div>
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">Track a metric</h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              {!selectedMetric
                ? 'What do you want to measure?'
                : selectedMetric.scope === 'table'
                  ? 'This metric is measured across the whole table.'
                  : 'Which column should we track it on?'}
            </p>
          </div>
          <button onClick={onClose} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700">
            <X className="w-4 h-4 text-gray-500" />
          </button>
        </div>

        <div className="p-5 overflow-y-auto flex-1">
          {!selectedMetric ? (
            // ── STEP 1: pick metric ──────────────────────────────────
            catalog.isLoading ? (
              <div className="text-xs text-gray-500 dark:text-gray-400 py-8 text-center">Loading catalog…</div>
            ) : (
              <div className="space-y-4">
                {BUCKET_ORDER.map(bucket => {
                  const items = metricsByBucket[bucket]
                  if (items.length === 0) return null
                  return (
                    <div key={bucket}>
                      <div className="flex items-baseline gap-2 mb-1.5">
                        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                          {BUCKET_HEADINGS[bucket]}
                        </h3>
                        <span className="text-[11px] text-gray-400 dark:text-gray-500">· {BUCKET_HINTS[bucket]}</span>
                      </div>
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                        {items.map(entry => {
                          // For table-level, greying if already enrolled; column
                          // metrics can't be pre-disabled because they may still
                          // fit an un-enrolled column.
                          const disabled = entry.scope === 'table' &&
                            alreadyEnrolled.has(`::${entry.metric_name}`)
                          return (
                            <button
                              key={entry.metric_name}
                              disabled={disabled}
                              onClick={() => { setSelectedMetric(entry); setColumn(null); setError(null) }}
                              className={`text-left px-3 py-2 rounded-lg text-sm border transition-colors ${
                                disabled
                                  ? 'opacity-50 cursor-not-allowed border-gray-200 dark:border-gray-700'
                                  : 'border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-200 hover:border-primary-400 hover:bg-primary-50/50 dark:hover:bg-primary-900/10'
                              }`}
                            >
                              <div className="font-medium flex items-center justify-between gap-1">
                                <span>{entry.label}</span>
                                {disabled && <span className="text-[10px] text-gray-400">already tracked</span>}
                              </div>
                              <div className="text-[11px] text-gray-500 dark:text-gray-400 mt-0.5">{entry.description}</div>
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  )
                })}
              </div>
            )
          ) : selectedMetric.scope === 'table' ? (
            // ── STEP 2a: table-level metric — nothing to pick ─────────
            <div className="px-3 py-4 rounded-lg bg-primary-50 dark:bg-primary-900/20 border border-primary-200 dark:border-primary-800 text-sm text-primary-900 dark:text-primary-100">
              <p className="font-medium">{selectedMetric.label}</p>
              <p className="mt-0.5 text-xs text-primary-700 dark:text-primary-300">
                {selectedMetric.description}
              </p>
              <p className="mt-2 text-xs">
                This metric is measured across the whole table — no column selection needed.
              </p>
            </div>
          ) : (
            // ── STEP 2b: pick compatible column ──────────────────────
            <div className="space-y-2">
              <div className="px-3 py-2 rounded-lg bg-primary-50 dark:bg-primary-900/20 border border-primary-200 dark:border-primary-800 text-xs text-primary-800 dark:text-primary-200">
                Tracking <span className="font-semibold">{selectedMetric.label}</span> — {selectedMetric.description}
              </div>
              {columnOptions.length > 8 && (
                <input
                  autoFocus
                  type="text"
                  value={columnFilter}
                  onChange={e => setColumnFilter(e.target.value)}
                  placeholder="Filter columns…"
                  className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 bg-white dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-primary-500"
                />
              )}
              {columnsQuery.isLoading ? (
                <div className="text-xs text-gray-500 dark:text-gray-400 py-2">Loading columns…</div>
              ) : compatibleColumns.length === 0 ? (
                <div className="p-4 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-center text-sm text-gray-500 dark:text-gray-400">
                  No columns match this metric's requirements
                  {columnFilter ? ' with that filter' : ''}.
                </div>
              ) : (
                <div className="max-h-64 overflow-y-auto grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                  {compatibleColumns.map(c => {
                    const active = column === c.column_name
                    const disabled = alreadyEnrolled.has(`${c.column_name}::${selectedMetric.metric_name}`)
                    return (
                      <button
                        key={c.column_name}
                        disabled={disabled}
                        onClick={() => setColumn(c.column_name)}
                        className={`text-left px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                          disabled
                            ? 'opacity-50 cursor-not-allowed border-gray-200 dark:border-gray-700'
                            : active
                              ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/20 text-primary-800 dark:text-primary-200'
                              : 'border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-200 hover:border-primary-400 hover:bg-primary-50/50 dark:hover:bg-primary-900/10'
                        }`}
                      >
                        <div className="font-mono truncate flex items-center justify-between gap-1">
                          <span>{c.column_name}</span>
                          {disabled && <span className="text-[9px] text-gray-400 font-sans normal-case">already tracked</span>}
                        </div>
                        <div className="text-[10px] text-gray-500 dark:text-gray-400 uppercase truncate">{c.data_type}</div>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {error && (
            <div className="mt-3 p-2 rounded bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-700 text-xs text-red-700 dark:text-red-300">
              {error}
            </div>
          )}
        </div>

        <div className="px-5 py-3 border-t border-gray-100 dark:border-gray-700 flex items-center justify-between gap-2 flex-shrink-0">
          {selectedMetric ? (
            <button
              onClick={() => { setSelectedMetric(null); setColumn(null); setColumnFilter(''); setError(null) }}
              className="text-xs font-medium text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100"
            >
              ← Pick a different metric
            </button>
          ) : <span />}
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
            >
              Cancel
            </button>
            <button
              disabled={!canSubmit}
              onClick={() => enroll.mutate()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {enroll.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
              {confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { LineChart, Line, ResponsiveContainer, YAxis } from 'recharts'
import {
  Activity, AlertTriangle, ChevronDown, ChevronRight, Loader2, Sparkles,
  ArrowUpRight, Info,
} from 'lucide-react'
import { tableHealthApi, metricsApi } from '../api/client'
import type { AssetMetricRow } from '../api/client'

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
    return <EmptyState kind="no-metrics" />
  }

  return (
    <div className="space-y-6">
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

  return (
    <Link
      to={hrefFor(assetId, m)}
      className={`group block bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-4 hover:border-primary-300 dark:hover:border-primary-600 transition-colors ${meta.ring}`}
    >
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0">
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

function EmptyState({ kind }: { kind: 'no-asset' | 'no-metrics' }) {
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
        </>
      )}
    </div>
  )
}

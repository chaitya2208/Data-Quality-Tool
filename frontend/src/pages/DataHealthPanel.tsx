import { useQuery } from '@tanstack/react-query'
import { tableHealthApi } from '../api/client'
import type { TableHealth, TableHealthRule, HealthDot } from '../api/client'
import { fmtIST } from '../utils/dates'
import {
  ShieldCheck, AlertTriangle, Activity, Clock, Loader2,
  CheckCircle2, XCircle, HelpCircle, AlertCircle, BellOff, RotateCcw, TrendingUp,
} from 'lucide-react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'

// ── helpers ──────────────────────────────────────────────────────────────────

const DOT_TONE: Record<HealthDot, string> = {
  green: 'bg-green-500',
  amber: 'bg-amber-500',
  red:   'bg-red-500',
  gray:  'bg-gray-300 dark:bg-gray-600',
}

const SEVERITY_TONE: Record<string, string> = {
  critical: 'bg-red-100 text-red-700',
  high:     'bg-orange-100 text-orange-700',
  medium:   'bg-amber-100 text-amber-700',
  low:      'bg-sky-100 text-sky-700',
  info:     'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
}

function StatusIcon({ status }: { status: string | null }) {
  if (status === 'passed')             return <CheckCircle2 className="w-4 h-4 text-green-600" />
  if (status === 'failed')             return <XCircle       className="w-4 h-4 text-red-600" />
  if (status === 'error')              return <AlertCircle   className="w-4 h-4 text-orange-600" />
  return <HelpCircle className="w-4 h-4 text-gray-400" />
}

function healthTone(score: number | null): string {
  if (score === null) return 'text-gray-400'
  if (score >= 0.95)  return 'text-green-600'
  if (score >= 0.8)   return 'text-amber-600'
  return 'text-red-600'
}

function fmtScore(score: number | null): string {
  if (score === null) return '—'
  return `${Math.round(score * 100)}%`
}

function fmtDaysAgo(iso: string | null): string | null {
  if (!iso) return null
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 3600)  return 'today'
  if (s < 86400) return `${Math.round(s / 3600)}h`
  return `${Math.round(s / 86400)}d`
}

function fmtRelTime(iso: string | null): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  const now = Date.now()
  const s = Math.max(0, Math.round((now - then) / 1000))
  if (s < 60)      return `${s}s ago`
  if (s < 3600)    return `${Math.round(s / 60)}m ago`
  if (s < 86400)   return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

// ── sparkline ────────────────────────────────────────────────────────────────

function Sparkline({ history }: { history: TableHealthRule['history'] }) {
  if (!history.length) return <span className="text-gray-300 text-xs">no runs</span>
  const w = 80, h = 18, gap = 1
  const barW = Math.max(1, (w - gap * (history.length - 1)) / history.length)
  return (
    <svg width={w} height={h} className="inline-block align-middle">
      {history.map((e, i) => {
        const tone =
          e.status === 'passed' ? '#16a34a' :
          e.status === 'failed' ? '#dc2626' :
          e.status === 'error'  ? '#ea580c' : '#d1d5db'
        return <rect key={i} x={i * (barW + gap)} y={0} width={barW} height={h} fill={tone} rx="1" />
      })}
    </svg>
  )
}

// ── KPI tile ─────────────────────────────────────────────────────────────────

function KpiTile({
  icon: Icon, label, value, tone,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: React.ReactNode
  tone?: string
}) {
  return (
    <div className="flex-1 min-w-[10rem] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-4 py-3">
      <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-wider text-gray-400 dark:text-gray-400">
        <Icon className="w-3.5 h-3.5" />
        {label}
      </div>
      <div className={`mt-1 text-2xl font-bold tabular-nums ${tone ?? 'text-gray-900 dark:text-gray-100'}`}>{value}</div>
    </div>
  )
}

// ── trend chart ──────────────────────────────────────────────────────────────

function TrendChart({ database, schema, table }: { database: string; schema: string; table: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['table-health-history', database, schema, table, 30],
    queryFn: () => tableHealthApi.history(database, schema, table, 30).then(r => r.data),
    staleTime: 60 * 1000,
  })
  if (isLoading) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-8 flex items-center gap-2 text-sm text-gray-500 dark:text-gray-300">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading trend…
      </div>
    )
  }
  const series = data?.series ?? []
  if (series.length === 0) {
    return null   // no run history yet — hide the chart entirely
  }
  const points = series.map(p => ({
    day: p.day,
    passRate: p.pass_rate === null ? null : Math.round(p.pass_rate * 100),
    failed: p.failed + p.error,
  }))
  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-4">
      <div className="flex items-center gap-2 mb-3">
        <TrendingUp className="w-4 h-4 text-primary-600" />
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Health trend — last 30 days</h3>
        <span className="text-xs text-gray-400 dark:text-gray-400 ml-auto">pass-rate % · failed runs</span>
      </div>
      <div style={{ width: '100%', height: 180 }}>
        <ResponsiveContainer>
          <LineChart data={points} margin={{ top: 5, right: 20, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
            <XAxis dataKey="day" tick={{ fontSize: 10 }} />
            <YAxis yAxisId="left"  domain={[0, 100]} tick={{ fontSize: 10 }} />
            <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 10 }} />
            <Tooltip contentStyle={{ fontSize: 12 }} />
            <Line yAxisId="left"  type="monotone" dataKey="passRate" stroke="#16a34a" strokeWidth={2} dot={false} name="Pass rate %" />
            <Line yAxisId="right" type="monotone" dataKey="failed"   stroke="#dc2626" strokeWidth={2} dot={false} name="Failed runs" />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

// ── main panel ───────────────────────────────────────────────────────────────

export default function DataHealthPanel({
  database, schema, table,
}: { database: string; schema: string; table: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['table-health', database, schema, table],
    queryFn: () => tableHealthApi.get(database, schema, table).then(r => r.data),
    staleTime: 30 * 1000,
  })

  if (isLoading) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-8 flex items-center gap-2 text-sm text-gray-500 dark:text-gray-300">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading data health…
      </div>
    )
  }
  if (isError) {
    return (
      <div className="bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-500/40 rounded-xl px-5 py-3 text-sm text-red-700 dark:text-red-300 flex items-center gap-2">
        <AlertCircle className="w-4 h-4" />
        Failed to load health: {(error as any)?.response?.data?.detail ?? (error as Error)?.message}
      </div>
    )
  }
  const h = data as TableHealth
  if (h.rules_total === 0) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-8 text-center">
        <ShieldCheck className="w-10 h-10 text-gray-200 mx-auto mb-3" />
        <p className="text-gray-900 dark:text-gray-100 font-medium mb-1">No rules applied to this table yet</p>
        <p className="text-sm text-gray-400">
          Run an Agent Workflow on this table to propose rules, then approve them to start tracking data health.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* KPI tiles */}
      <div className="flex flex-wrap gap-3">
        <KpiTile icon={ShieldCheck} label="Health Score" value={fmtScore(h.health_score)} tone={healthTone(h.health_score)} />
        <KpiTile icon={Activity}    label="Active Instances" value={h.rules_total.toLocaleString()} />
        <KpiTile icon={AlertTriangle} label="Failed Instances"
                 value={h.rules_failing.toLocaleString()}
                 tone={h.rules_failing > 0 ? 'text-red-600' : 'text-green-600'} />
        <KpiTile icon={AlertCircle} label="Open Findings"
                 value={h.open_findings.toLocaleString()}
                 tone={h.open_findings > 0 ? 'text-amber-600' : 'text-green-600'} />
        <KpiTile icon={Clock}       label="Last Run" value={fmtRelTime(h.last_run_at)} />
      </div>

      {/* Trend chart */}
      <TrendChart database={database} schema={schema} table={table} />

      {/* Rules table */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-100 dark:border-gray-700 bg-primary-50/50 dark:bg-primary-900/20">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <ShieldCheck className="w-5 h-5 text-primary-600" />
            Rules applied to this table
          </h2>
          <p className="text-xs text-gray-600 dark:text-gray-300 mt-1">
            {h.rules_passing} passing · {h.rules_failing} failed · {h.rules_unrun} not yet run
          </p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 dark:text-gray-300 uppercase tracking-wide border-b border-gray-100 dark:border-gray-700">
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Rule</th>
                <th className="px-3 py-2 font-medium">Severity</th>
                <th className="px-3 py-2 font-medium">Columns</th>
                <th className="px-3 py-2 font-medium">Pass rate</th>
                <th className="px-3 py-2 font-medium">Recent runs</th>
                <th className="px-3 py-2 font-medium">Last run</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50 dark:divide-gray-700/50">
              {h.rules.map(r => (
                <tr key={r.instance_id} className="hover:bg-gray-50 dark:hover:bg-gray-700/40 align-top">
                  <td className="px-4 py-2"><StatusIcon status={r.latest_status} /></td>
                  <td className="px-3 py-2">
                    <div className="font-medium text-gray-800 dark:text-gray-200 flex items-center gap-1.5 flex-wrap">
                      {r.name}
                      {r.first_detected_at && !r.muted && (
                        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-red-50 text-red-700 border border-red-100"
                              title={`First detected ${fmtIST(r.first_detected_at)}`}>
                          failing {fmtDaysAgo(r.first_detected_at)}
                          {r.current_fail_count != null && r.current_total_count != null && r.current_total_count > 1
                            ? ` · ${r.current_fail_count}/${r.current_total_count} rows (${((r.current_fail_count / r.current_total_count) * 100).toFixed(1)}%)`
                            : ''}
                        </span>
                      )}
                      {r.reopened_count > 0 && (
                        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200 inline-flex items-center gap-1"
                              title="This rule keeps flapping — investigate the root cause">
                          <RotateCcw className="w-2.5 h-2.5" /> reopened {r.reopened_count}×
                        </span>
                      )}
                      {r.muted && (
                        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-gray-200 text-gray-600 inline-flex items-center gap-1">
                          <BellOff className="w-2.5 h-2.5" /> muted
                        </span>
                      )}
                    </div>
                    {r.category && <div className="text-xs text-gray-400">{r.category}</div>}
                  </td>
                  <td className="px-3 py-2">
                    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${SEVERITY_TONE[r.severity] ?? SEVERITY_TONE.info}`}>
                      {r.severity}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-gray-600 dark:text-gray-300">
                    {r.columns.length ? r.columns.join(', ') : <span className="text-gray-300">table-level</span>}
                  </td>
                  <td className="px-3 py-2 tabular-nums">
                    {r.pass_rate === null
                      ? <span className="text-gray-300">—</span>
                      : <span className={r.pass_rate >= 0.95 ? 'text-green-600' : r.pass_rate >= 0.8 ? 'text-amber-600' : 'text-red-600'}>
                          {Math.round(r.pass_rate * 100)}%
                        </span>}
                    <span className="text-xs text-gray-400 ml-1">({r.total_runs})</span>
                  </td>
                  <td className="px-3 py-2"><Sparkline history={r.history} /></td>
                  <td className="px-3 py-2 text-xs text-gray-500 dark:text-gray-300">{fmtRelTime(r.last_executed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

// Column-status dot — exported for the Column Stats overlay in DataExplorer.
export function ColumnStatusDot({ status }: { status: HealthDot | undefined }) {
  const s = status ?? 'gray'
  const label = s === 'green' ? 'All rules passing'
              : s === 'amber' ? 'Some rules not yet run or partial'
              : s === 'red'   ? 'One or more rules failing'
              : 'No rules on this column'
  return <span className={`inline-block w-2 h-2 rounded-full ${DOT_TONE[s]}`} title={label} />
}

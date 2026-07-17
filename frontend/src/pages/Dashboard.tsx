import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { findingsApi, agentRunsApi, tableHealthApi } from '../api/client'
import type { FleetOverview } from '../api/client'
import { AlertCircle, CheckCircle, Clock, Database, ChevronRight, ArrowLeft, Table, ShieldCheck, RotateCcw, TrendingUp } from 'lucide-react'
import {
  PieChart, Pie, Cell, ResponsiveContainer, Legend, Tooltip,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  LineChart, Line,
} from 'recharts'
import { useTheme } from '../ThemeContext'
import { useConnection } from '../ConnectionContext'

const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ef4444',
  high:     '#f97316',
  medium:   '#eab308',
  low:      '#3b82f6',
  info:     '#6b7280',
}

export default function Dashboard() {
  const navigate = useNavigate()
  const { resolved: theme } = useTheme()
  // Everything on the dashboard is scoped to the selected data source. Including
  // connId in each query key means switching sources auto-refetches.
  const { selectedId: connId } = useConnection()
  // Recharts renders SVG text with an inline fill that Tailwind's dark: classes
  // can't reach — drive axis/grid colors from the theme explicitly.
  const axisColor = theme === 'dark' ? '#9ca3af' : '#6b7280'
  const gridColor = theme === 'dark' ? '#374151' : '#e5e7eb'
  // null = showing databases, string = drilled into that database
  const [selectedDb, setSelectedDb] = useState<string | null>(null)

  const { data: stats, isLoading: loadingStats } = useQuery({
    queryKey: ['findings-stats', connId],
    queryFn: () => findingsApi.stats(connId).then(r => r.data),
  })

  const { data: dbData = [], isLoading: loadingDb } = useQuery({
    queryKey: ['findings-by-database', connId],
    queryFn: () => findingsApi.byDatabase(connId).then(r => r.data),
  })

  // Count workflow runs (same source as the Run History page) so the card
  // matches that page's run count, not raw scan rows. Runs already carry
  // connection_id, so the source filter is applied client-side below.
  const { data: runsData, isLoading: loadingRuns } = useQuery({
    queryKey: ['agent-runs'],
    queryFn: () => agentRunsApi.list().then(r => r.data),
    staleTime: 30_000,  // don't refetch-and-flash 0 on every dashboard revisit
  })

  // Fleet-wide health aggregation — powers the KPI row + worst-tables list +
  // 30-day trend line. Scoped to the selected connection just like everything
  // else on this page.
  const { data: fleet, isLoading: loadingFleet } = useQuery({
    queryKey: ['fleet-health', connId, 30],
    queryFn: () => tableHealthApi.fleet({
      connection_id: connId || undefined, days: 30, top_n: 8,
    }).then(r => r.data),
    staleTime: 60_000,
  })

  // Workflow-run count scoped to the selected source. NULL-connection (legacy)
  // runs are attributed to Snowflake, matching the findings scoping rule.
  const { selected } = useConnection()
  const isSnowflake = (selected?.type ?? '').toLowerCase() === 'snowflake'
  const scopedRuns = (runsData?.runs ?? []).filter(r =>
    !connId
      ? true
      : r.connection_id === connId || (isSnowflake && !r.connection_id)
  )

  // ── Severity pie data ──
  const severityData = stats?.by_severity
    ? Object.entries(stats.by_severity)
        .map(([key, val]) => ({
          name: key.charAt(0).toUpperCase() + key.slice(1),
          value: val as number,
          color: SEVERITY_COLORS[key] ?? '#6b7280',
        }))
        .filter(d => d.value > 0)
    : []

  // ── Status bar data ──
  const statusData = stats?.by_status
    ? Object.entries(stats.by_status)
        .map(([key, val]) => ({
          name: key.charAt(0).toUpperCase() + key.slice(1),
          value: val as number,
        }))
        .filter(d => d.value > 0)
    : []

  // ── Bar chart data — database view or table drill-down ──
  const drillData: { name: string; total: number; critical: number; high: number; medium: number; low: number }[] =
    selectedDb
      ? (dbData.find(d => d.database === selectedDb)?.tables ?? []).map(t => ({
          name: t.table_name,
          total:    t.total,
          critical: t.by_severity.critical ?? 0,
          high:     t.by_severity.high     ?? 0,
          medium:   t.by_severity.medium   ?? 0,
          low:      t.by_severity.low      ?? 0,
        }))
      : dbData.map(d => ({
          name:     d.database,
          total:    d.total,
          critical: d.tables.reduce((s, t) => s + (t.by_severity.critical ?? 0), 0),
          high:     d.tables.reduce((s, t) => s + (t.by_severity.high     ?? 0), 0),
          medium:   d.tables.reduce((s, t) => s + (t.by_severity.medium   ?? 0), 0),
          low:      d.tables.reduce((s, t) => s + (t.by_severity.low      ?? 0), 0),
        }))

  // ── Custom bar tooltip ──
  const CustomTooltip = ({ active, payload, label }: any) => {
    if (!active || !payload?.length) return null
    return (
      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg p-3 text-sm min-w-[160px]">
        <p className="font-semibold text-gray-900 dark:text-gray-100 mb-2 truncate">{label}</p>
        {payload.map((p: any) => (
          <div key={p.name} className="flex justify-between gap-4">
            <span style={{ color: p.color }}>{p.name}</span>
            <span className="font-medium text-gray-900 dark:text-gray-100">{p.value}</span>
          </div>
        ))}
        <div className="border-t border-gray-200 dark:border-gray-700 mt-2 pt-1 flex justify-between font-semibold text-gray-900 dark:text-gray-100">
          <span>Total</span>
          <span>{payload.reduce((s: number, p: any) => s + (p.value ?? 0), 0)}</span>
        </div>
      </div>
    )
  }

  // ── Handle bar click — drill into DB or navigate to findings for table ──
  const handleBarClick = (data: any) => {
    if (!data?.activePayload?.[0]) return
    const name = data.activePayload[0].payload.name
    if (!selectedDb) {
      setSelectedDb(name)
    } else {
      // Navigate to findings filtered by table
      navigate(`/findings`)
    }
  }

  const StatCard = ({ title, value, icon: Icon, color, href, loading }: any) => (
    <div
      onClick={() => href && navigate(href)}
      className={`bg-white dark:bg-gray-800 rounded-lg shadow p-6 flex items-center justify-between ${
        href ? 'cursor-pointer hover:shadow-md hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-all' : ''
      }`}
    >
      <div>
        <p className="text-sm font-medium text-gray-500 dark:text-gray-300">{title}</p>
        {/* Pulsing skeleton while loading — avoids flashing a misleading 0
            before the query resolves. */}
        {loading ? (
          <div className="mt-2 h-8 w-16 rounded bg-gray-200 dark:bg-gray-700 animate-pulse" />
        ) : (
          <p className="mt-1 text-3xl font-bold text-gray-900 dark:text-gray-100">{value}</p>
        )}
        {href && <p className="text-xs text-primary-600 mt-1">Click to view →</p>}
      </div>
      <div className={`p-3 rounded-full ${color}`}>
        <Icon className="w-6 h-6 text-white" />
      </div>
    </div>
  )

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Dashboard</h1>
        <p className="mt-1 text-gray-500 dark:text-gray-300">Overview of your data quality metrics</p>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-6">
        <StatCard title="Total Findings"   value={stats?.total ?? 0}               icon={AlertCircle} color="bg-red-500"    href="/findings"           loading={loadingStats} />
        <StatCard title="Pending Issues"   value={stats?.by_status?.detected ?? 0} icon={Clock}       color="bg-yellow-500" href="/findings?status=detected" loading={loadingStats} />
        <StatCard title="Workflow Runs"    value={scopedRuns.length}               icon={CheckCircle} color="bg-green-500"  href="/run-history"        loading={loadingRuns} />
      </div>

      {/* ── Fleet Health KPI row ─────────────────────────────────────────
          Overall pass-rate across every rule execution on every table in
          the last 30 days; open + flapping incident counts across the
          fleet; oldest currently-open incident. */}
      <FleetHealthKpis fleet={fleet} loading={loadingFleet} />

      {/* ── Overall trend + Worst tables ─────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 bg-white dark:bg-gray-800 rounded-xl shadow p-4 flex flex-col">
          <div className="flex items-center gap-2 mb-3">
            <TrendingUp className="w-4 h-4 text-primary-600" />
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Fleet health trend — last 30 days</h2>
            <span className="ml-auto text-xs text-gray-400 dark:text-gray-400">pass-rate % · failed runs</span>
          </div>
          <div className="flex-1 min-h-[180px]">
            <FleetTrendChart fleet={fleet} axisColor={axisColor} gridColor={gridColor} loading={loadingFleet} />
          </div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-4">
          <div className="flex items-center gap-2 mb-3">
            <AlertCircle className="w-4 h-4 text-red-500" />
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Worst tables</h2>
            <span className="ml-auto text-xs text-gray-400 dark:text-gray-400">click to drill in</span>
          </div>
          <WorstTablesList
            fleet={fleet} loading={loadingFleet}
            onOpen={(db, sc, tb) => {
              // Persist the drill-down so DataExplorer opens with the right selection.
              try {
                localStorage.setItem('dq_explorer_db', db)
                localStorage.setItem('dq_explorer_schema', sc)
                localStorage.setItem('dq_explorer_table', tb)
              } catch {}
              navigate('/explorer')
            }}
          />
        </div>
      </div>

      {/* ── Database / Table issues chart ── */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-6">
        {/* Chart header with breadcrumb */}
        <div className="flex flex-wrap items-start justify-between gap-3 mb-5">
          <div className="flex items-center gap-2 min-w-0">
            {selectedDb ? (
              <>
                <button
                  onClick={() => setSelectedDb(null)}
                  className="flex items-center gap-1 text-sm text-primary-600 hover:text-primary-800 font-medium"
                >
                  <ArrowLeft className="w-4 h-4" />
                  All Databases
                </button>
                <ChevronRight className="w-4 h-4 text-gray-400 dark:text-gray-400" />
                <span className="flex items-center gap-1.5 text-sm font-semibold text-gray-900 dark:text-gray-100">
                  <Database className="w-4 h-4 text-primary-500" />
                  {selectedDb}
                </span>
                <span className="text-xs text-gray-400 dark:text-gray-400 ml-1">— click a bar to view findings</span>
              </>
            ) : (
              <div>
                <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Issues by Database</h2>
                <p className="text-xs text-gray-400 dark:text-gray-400 mt-0.5">Click any bar to drill into its tables</p>
              </div>
            )}
          </div>

          {/* Legend — hidden on very small screens */}
          <div className="hidden sm:flex items-center gap-3 text-xs flex-wrap text-gray-600 dark:text-gray-300">
            {Object.entries(SEVERITY_COLORS).map(([key, color]) => (
              <span key={key} className="flex items-center gap-1">
                <span className="w-2.5 h-2.5 rounded-sm inline-block" style={{ background: color }} />
                {key.charAt(0).toUpperCase() + key.slice(1)}
              </span>
            ))}
          </div>
        </div>

        {loadingDb ? (
          <div className="h-64 flex items-center justify-center text-gray-400 dark:text-gray-400">
            Loading…
          </div>
        ) : drillData.length === 0 ? (
          <div className="h-64 flex flex-col items-center justify-center text-gray-400 dark:text-gray-400 gap-3">
            <Database className="w-12 h-12 text-gray-200" />
            <p>No findings yet. Scan a table to see data here.</p>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={Math.max(220, drillData.length * 48)}>
            <BarChart
              data={drillData}
              layout="vertical"
              margin={{ left: 10, right: 30, top: 4, bottom: 4 }}
              onClick={handleBarClick}
              style={{ cursor: selectedDb ? 'default' : 'pointer' }}
            >
              <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke={gridColor} />
              <XAxis type="number" tick={{ fontSize: 12, fill: axisColor }} stroke={axisColor} />
              <YAxis
                type="category"
                dataKey="name"
                width={140}
                tick={{ fontSize: 11, fill: axisColor }}
                stroke={axisColor}
                tickFormatter={(v: string) =>
                  v.length > 20 ? v.slice(0, 18) + '…' : v
                }
              />
              <Tooltip content={<CustomTooltip />} cursor={{ fill: theme === 'dark' ? '#374151' : '#f3f4f6' }} />
              <Bar dataKey="critical" stackId="a" fill={SEVERITY_COLORS.critical} name="Critical" />
              <Bar dataKey="high"     stackId="a" fill={SEVERITY_COLORS.high}     name="High"     />
              <Bar dataKey="medium"   stackId="a" fill={SEVERITY_COLORS.medium}   name="Medium"   />
              <Bar dataKey="low"      stackId="a" fill={SEVERITY_COLORS.low}      name="Low"      radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        )}

        {/* Database cards summary (top level only) */}
        {!selectedDb && dbData.length > 0 && (
          <div className="mt-5 pt-4 border-t border-gray-100 dark:border-gray-700 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            {dbData.map(d => (
              <button
                key={d.database}
                onClick={() => setSelectedDb(d.database)}
                className="text-left p-3 rounded-lg border border-gray-200 dark:border-gray-700 hover:border-primary-400 hover:bg-primary-50 dark:hover:bg-primary-900/20 transition-all group"
              >
                <div className="flex items-center gap-1.5 mb-1">
                  <Database className="w-3.5 h-3.5 text-gray-400 dark:text-gray-400 group-hover:text-primary-500" />
                  <span className="text-xs font-semibold text-gray-700 dark:text-gray-200 truncate">{d.database}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-2xl font-bold text-gray-900 dark:text-gray-100">{d.total}</span>
                  <span className="text-xs text-gray-400 dark:text-gray-400">{d.tables.length} table{d.tables.length !== 1 ? 's' : ''}</span>
                </div>
              </button>
            ))}
          </div>
        )}

        {/* Table rows summary (drill-down level) */}
        {selectedDb && (
          <div className="mt-5 pt-4 border-t border-gray-100 dark:border-gray-700">
            <p className="text-xs font-medium text-gray-500 dark:text-gray-300 mb-2">Tables in {selectedDb}</p>
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {(dbData.find(d => d.database === selectedDb)?.tables ?? []).map(t => (
                <button
                  key={t.table_name}
                  onClick={() => navigate(`/findings?table_name=${encodeURIComponent(t.table_name)}&database=${encodeURIComponent(selectedDb)}`)}
                  className="w-full flex items-center justify-between py-2.5 px-1 hover:bg-gray-50 dark:hover:bg-gray-700/40 text-left group transition-colors"
                >
                  <div className="flex items-center gap-2">
                    <Table className="w-3.5 h-3.5 text-gray-400 dark:text-gray-400" />
                    <span className="text-sm text-gray-800 dark:text-gray-200 font-medium">{t.table_name}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    {Object.entries(t.by_severity).map(([sev, cnt]) =>
                      cnt > 0 ? (
                        <span
                          key={sev}
                          className="text-xs font-semibold px-1.5 py-0.5 rounded"
                          style={{
                            color: SEVERITY_COLORS[sev],
                            background: SEVERITY_COLORS[sev] + '20',
                          }}
                        >
                          {cnt} {sev}
                        </span>
                      ) : null
                    )}
                    <span className="text-xs font-bold text-gray-700 dark:text-gray-200 ml-1">{t.total} total</span>
                    <ChevronRight className="w-3.5 h-3.5 text-gray-300 group-hover:text-primary-500" />
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Bottom row: severity + status */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-6">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100 mb-4">Findings by Severity</h2>
          {severityData.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <PieChart>
                <Pie
                  data={severityData}
                  cx="50%"
                  cy="50%"
                  innerRadius={55}
                  outerRadius={90}
                  paddingAngle={3}
                  dataKey="value"
                >
                  {severityData.map((entry, i) => (
                    <Cell key={i} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(v: any) => [`${v} issues`]}
                  contentStyle={theme === 'dark' ? { backgroundColor: '#1f2937', border: '1px solid #374151', borderRadius: 8, color: '#f3f4f6' } : undefined}
                  labelStyle={theme === 'dark' ? { color: '#f3f4f6' } : undefined}
                  itemStyle={theme === 'dark' ? { color: '#f3f4f6' } : undefined}
                />
                <Legend iconType="circle" iconSize={10} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-52 flex items-center justify-center text-gray-400 dark:text-gray-400">No findings yet</div>
          )}
        </div>

        <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-6">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100 mb-4">Findings by Status</h2>
          {statusData.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={statusData} margin={{ top: 4, right: 20, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke={gridColor} />
                <XAxis dataKey="name" tick={{ fontSize: 12, fill: axisColor }} stroke={axisColor} />
                <YAxis tick={{ fontSize: 12, fill: axisColor }} stroke={axisColor} />
                <Tooltip
                  cursor={{ fill: theme === 'dark' ? '#374151' : '#f3f4f6' }}
                  contentStyle={theme === 'dark' ? { backgroundColor: '#1f2937', border: '1px solid #374151', borderRadius: 8, color: '#f3f4f6' } : undefined}
                  labelStyle={theme === 'dark' ? { color: '#f3f4f6' } : undefined}
                  itemStyle={theme === 'dark' ? { color: '#f3f4f6' } : undefined}
                />
                <Bar dataKey="value" fill="#3b82f6" radius={[4, 4, 0, 0]} name="Findings" />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-52 flex items-center justify-center text-gray-400 dark:text-gray-400">No findings yet</div>
          )}
        </div>
      </div>

    </div>
  )
}

// ── Fleet Health components ─────────────────────────────────────────────

function healthTone(score: number | null | undefined): string {
  if (score == null) return 'text-gray-400'
  if (score >= 0.95) return 'text-green-600'
  if (score >= 0.8)  return 'text-amber-600'
  return 'text-red-600'
}

function fmtDaysAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const normalized = iso.replace(' ', 'T').replace(/([+-]\d{2}:\d{2}|Z)$/, '') + 'Z'
  const ms = new Date(normalized).getTime()
  if (isNaN(ms)) return '—'
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (s < 3600)  return 'today'
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

function FleetKpiTile({
  icon: Icon, label, value, tone,
}: { icon: React.ComponentType<{ className?: string }>; label: string; value: React.ReactNode; tone?: string }) {
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

function FleetHealthKpis({ fleet, loading }: { fleet: FleetOverview | undefined; loading: boolean }) {
  if (loading) {
    return (
      <div className="flex flex-wrap gap-3">
        {[0, 1, 2, 3].map(i => (
          <div key={i} className="flex-1 min-w-[10rem] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl px-4 py-3">
            <div className="h-3 w-20 rounded bg-gray-100 dark:bg-gray-700 animate-pulse" />
            <div className="mt-2 h-6 w-24 rounded bg-gray-200 dark:bg-gray-700 animate-pulse" />
          </div>
        ))}
      </div>
    )
  }
  const score = fleet?.overall_health_score ?? null
  return (
    <div className="flex flex-wrap gap-3">
      <FleetKpiTile icon={ShieldCheck} label="Fleet health score"
        value={score == null ? '—' : `${Math.round(score * 100)}%`}
        tone={healthTone(score)} />
      <FleetKpiTile icon={AlertCircle} label="Open incidents"
        value={(fleet?.fleet_open_findings ?? 0).toLocaleString()}
        tone={(fleet?.fleet_open_findings ?? 0) > 0 ? 'text-red-600' : 'text-green-600'} />
      <FleetKpiTile icon={RotateCcw} label="Flapping incidents"
        value={(fleet?.fleet_flapping_findings ?? 0).toLocaleString()}
        tone={(fleet?.fleet_flapping_findings ?? 0) > 0 ? 'text-amber-600' : undefined} />
      <FleetKpiTile icon={Clock} label="Oldest open"
        value={fmtDaysAgo(fleet?.fleet_oldest_open_at)}
        tone={fleet?.fleet_oldest_open_at ? 'text-red-600' : undefined} />
    </div>
  )
}

function FleetTrendChart({
  fleet, axisColor, gridColor, loading,
}: { fleet: FleetOverview | undefined; axisColor: string; gridColor: string; loading: boolean }) {
  if (loading) {
    return <div className="h-full rounded bg-gray-50 dark:bg-gray-700/40 animate-pulse" />
  }
  const series = fleet?.trend ?? []
  if (series.length === 0) {
    return <div className="h-full flex items-center justify-center text-sm text-gray-400 dark:text-gray-400">No execution history in the last 30 days.</div>
  }
  const points = series.map(p => ({
    day: p.day,
    passRate: p.pass_rate === null ? null : Math.round(p.pass_rate * 100),
    failed: (p.failed ?? 0) + (p.error ?? 0),
  }))
  return (
    <div style={{ width: '100%', height: '100%' }}>
      <ResponsiveContainer>
        <LineChart data={points} margin={{ top: 5, right: 20, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
          <XAxis dataKey="day" tick={{ fontSize: 10, fill: axisColor }} />
          <YAxis yAxisId="left"  domain={[0, 100]} tick={{ fontSize: 10, fill: axisColor }} />
          <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 10, fill: axisColor }} />
          <Tooltip contentStyle={{ fontSize: 12 }} />
          <Line yAxisId="left"  type="monotone" dataKey="passRate" stroke="#16a34a" strokeWidth={2} dot={false} name="Pass rate %" />
          <Line yAxisId="right" type="monotone" dataKey="failed"   stroke="#dc2626" strokeWidth={2} dot={false} name="Failed runs" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function WorstTablesList({
  fleet, loading, onOpen,
}: {
  fleet: FleetOverview | undefined
  loading: boolean
  onOpen: (db: string, sc: string, tb: string) => void
}) {
  if (loading) {
    return (
      <div className="space-y-2">
        {[0, 1, 2, 3].map(i => (
          <div key={i} className="h-10 rounded bg-gray-50 dark:bg-gray-700/40 animate-pulse" />
        ))}
      </div>
    )
  }
  const tables = fleet?.tables ?? []
  if (tables.length === 0) {
    return <div className="text-sm text-gray-400 dark:text-gray-400">No tables with executions yet.</div>
  }
  return (
    <ul className="space-y-1.5">
      {tables.map(t => {
        const key = `${t.database}.${t.schema}.${t.table}`
        const pr = t.pass_rate == null ? null : Math.round(t.pass_rate * 100)
        return (
          <li key={key}>
            <button
              onClick={() => onOpen(t.database, t.schema, t.table)}
              className="w-full flex items-center justify-between gap-2 text-left px-2.5 py-1.5 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors"
            >
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate" title={key}>{t.table}</div>
                <div className="text-[11px] text-gray-400 dark:text-gray-400 truncate">{t.database}.{t.schema}</div>
              </div>
              <div className="flex items-center gap-3 flex-shrink-0">
                {t.open_findings > 0 && (
                  <span className="text-xs font-semibold text-red-600">{t.open_findings} open</span>
                )}
                {t.flapping > 0 && (
                  <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200 inline-flex items-center gap-0.5">
                    <RotateCcw className="w-2.5 h-2.5" />{t.flapping}
                  </span>
                )}
                {pr != null && (
                  <span className={`text-xs font-medium tabular-nums ${pr >= 95 ? 'text-green-600' : pr >= 80 ? 'text-amber-600' : 'text-red-600'}`}>{pr}%</span>
                )}
                <ChevronRight className="w-3.5 h-3.5 text-gray-300" />
              </div>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

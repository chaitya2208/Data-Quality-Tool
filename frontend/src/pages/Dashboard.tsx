import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { findingsApi, scansApi, assetsApi, rulesApi } from '../api/client'
import type { DatabaseFindingSummary } from '../api/client'
import { AlertCircle, CheckCircle, Clock, Database, TrendingUp, ChevronRight, ArrowLeft, Table, ShieldCheck } from 'lucide-react'
import {
  PieChart, Pie, Cell, ResponsiveContainer, Legend, Tooltip,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
} from 'recharts'

const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ef4444',
  high:     '#f97316',
  medium:   '#eab308',
  low:      '#3b82f6',
  info:     '#6b7280',
}

export default function Dashboard() {
  const navigate = useNavigate()
  // null = showing databases, string = drilled into that database
  const [selectedDb, setSelectedDb] = useState<string | null>(null)

  const { data: stats } = useQuery({
    queryKey: ['findings-stats'],
    queryFn: () => findingsApi.stats().then(r => r.data),
  })

  const { data: dbData = [], isLoading: loadingDb } = useQuery({
    queryKey: ['findings-by-database'],
    queryFn: () => findingsApi.byDatabase().then(r => r.data),
  })

  const { data: scansData } = useQuery({
    queryKey: ['scans'],
    queryFn: () => scansApi.list().then(r => r.data),
  })

  const { data: assetsData } = useQuery({
    queryKey: ['assets'],
    queryFn: () => assetsApi.list().then(r => r.data),
  })

  const { data: recentFindings } = useQuery({
    queryKey: ['findings-recent'],
    queryFn: () => findingsApi.list({ limit: 5 }).then(r => r.data),
  })

  const { data: ruleStats } = useQuery({
    queryKey: ['rules-stats'],
    queryFn: () => rulesApi.stats().then(r => r.data),
  })

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
      <div className="bg-white border border-gray-200 rounded-lg shadow-lg p-3 text-sm min-w-[160px]">
        <p className="font-semibold text-gray-900 mb-2 truncate">{label}</p>
        {payload.map((p: any) => (
          <div key={p.name} className="flex justify-between gap-4">
            <span style={{ color: p.color }}>{p.name}</span>
            <span className="font-medium">{p.value}</span>
          </div>
        ))}
        <div className="border-t border-gray-200 mt-2 pt-1 flex justify-between font-semibold">
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

  const StatCard = ({ title, value, icon: Icon, color, href }: any) => (
    <div
      onClick={() => href && navigate(href)}
      className={`bg-white rounded-lg shadow p-6 flex items-center justify-between ${
        href ? 'cursor-pointer hover:shadow-md hover:bg-gray-50 transition-all' : ''
      }`}
    >
      <div>
        <p className="text-sm font-medium text-gray-500">{title}</p>
        <p className="mt-1 text-3xl font-bold text-gray-900">{value}</p>
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
        <h1 className="text-3xl font-bold text-gray-900">Dashboard</h1>
        <p className="mt-1 text-gray-500">Overview of your data quality metrics</p>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-6">
        <StatCard title="Total Findings"   value={stats?.total ?? 0}               icon={AlertCircle} color="bg-red-500"    href="/findings"           />
        <StatCard title="Assets Scanned"   value={assetsData?.total ?? 0}          icon={Database}    color="bg-blue-500"   href="/assets"             />
        <StatCard title="Scans Completed"  value={scansData?.total ?? 0}           icon={CheckCircle} color="bg-green-500"  href="/workflow"           />
        <StatCard title="Pending Issues"   value={stats?.by_status?.detected ?? 0} icon={Clock}       color="bg-yellow-500" href="/findings?status=detected" />
      </div>

      {/* ── Database / Table issues chart ── */}
      <div className="bg-white rounded-xl shadow p-6">
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
                <ChevronRight className="w-4 h-4 text-gray-400" />
                <span className="flex items-center gap-1.5 text-sm font-semibold text-gray-900">
                  <Database className="w-4 h-4 text-primary-500" />
                  {selectedDb}
                </span>
                <span className="text-xs text-gray-400 ml-1">— click a bar to view findings</span>
              </>
            ) : (
              <div>
                <h2 className="text-lg font-semibold text-gray-900">Issues by Database</h2>
                <p className="text-xs text-gray-400 mt-0.5">Click any bar to drill into its tables</p>
              </div>
            )}
          </div>

          {/* Legend — hidden on very small screens */}
          <div className="hidden sm:flex items-center gap-3 text-xs flex-wrap">
            {Object.entries(SEVERITY_COLORS).map(([key, color]) => (
              <span key={key} className="flex items-center gap-1">
                <span className="w-2.5 h-2.5 rounded-sm inline-block" style={{ background: color }} />
                {key.charAt(0).toUpperCase() + key.slice(1)}
              </span>
            ))}
          </div>
        </div>

        {loadingDb ? (
          <div className="h-64 flex items-center justify-center text-gray-400">
            Loading…
          </div>
        ) : drillData.length === 0 ? (
          <div className="h-64 flex flex-col items-center justify-center text-gray-400 gap-3">
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
              <CartesianGrid strokeDasharray="3 3" horizontal={false} />
              <XAxis type="number" tick={{ fontSize: 12 }} />
              <YAxis
                type="category"
                dataKey="name"
                width={140}
                tick={{ fontSize: 11 }}
                tickFormatter={(v: string) =>
                  v.length > 20 ? v.slice(0, 18) + '…' : v
                }
              />
              <Tooltip content={<CustomTooltip />} cursor={{ fill: '#f3f4f6' }} />
              <Bar dataKey="critical" stackId="a" fill={SEVERITY_COLORS.critical} name="Critical" />
              <Bar dataKey="high"     stackId="a" fill={SEVERITY_COLORS.high}     name="High"     />
              <Bar dataKey="medium"   stackId="a" fill={SEVERITY_COLORS.medium}   name="Medium"   />
              <Bar dataKey="low"      stackId="a" fill={SEVERITY_COLORS.low}      name="Low"      radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        )}

        {/* Database cards summary (top level only) */}
        {!selectedDb && dbData.length > 0 && (
          <div className="mt-5 pt-4 border-t border-gray-100 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            {dbData.map(d => (
              <button
                key={d.database}
                onClick={() => setSelectedDb(d.database)}
                className="text-left p-3 rounded-lg border border-gray-200 hover:border-primary-400 hover:bg-primary-50 transition-all group"
              >
                <div className="flex items-center gap-1.5 mb-1">
                  <Database className="w-3.5 h-3.5 text-gray-400 group-hover:text-primary-500" />
                  <span className="text-xs font-semibold text-gray-700 truncate">{d.database}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-2xl font-bold text-gray-900">{d.total}</span>
                  <span className="text-xs text-gray-400">{d.tables.length} table{d.tables.length !== 1 ? 's' : ''}</span>
                </div>
              </button>
            ))}
          </div>
        )}

        {/* Table rows summary (drill-down level) */}
        {selectedDb && (
          <div className="mt-5 pt-4 border-t border-gray-100">
            <p className="text-xs font-medium text-gray-500 mb-2">Tables in {selectedDb}</p>
            <div className="divide-y divide-gray-100">
              {(dbData.find(d => d.database === selectedDb)?.tables ?? []).map(t => (
                <button
                  key={t.table_name}
                  onClick={() => navigate(`/findings?table_name=${encodeURIComponent(t.table_name)}&database=${encodeURIComponent(selectedDb)}`)}
                  className="w-full flex items-center justify-between py-2.5 px-1 hover:bg-gray-50 text-left group transition-colors"
                >
                  <div className="flex items-center gap-2">
                    <Table className="w-3.5 h-3.5 text-gray-400" />
                    <span className="text-sm text-gray-800 font-medium">{t.table_name}</span>
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
                    <span className="text-xs font-bold text-gray-700 ml-1">{t.total} total</span>
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
        <div className="bg-white rounded-xl shadow p-6">
          <h2 className="text-base font-semibold text-gray-900 mb-4">Findings by Severity</h2>
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
                <Tooltip formatter={(v: any) => [`${v} issues`]} />
                <Legend iconType="circle" iconSize={10} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-52 flex items-center justify-center text-gray-400">No findings yet</div>
          )}
        </div>

        <div className="bg-white rounded-xl shadow p-6">
          <h2 className="text-base font-semibold text-gray-900 mb-4">Findings by Status</h2>
          {statusData.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={statusData} margin={{ top: 4, right: 20, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="name" tick={{ fontSize: 12 }} />
                <YAxis tick={{ fontSize: 12 }} />
                <Tooltip />
                <Bar dataKey="value" fill="#3b82f6" radius={[4, 4, 0, 0]} name="Findings" />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-52 flex items-center justify-center text-gray-400">No findings yet</div>
          )}
        </div>
      </div>

      {/* Rules Widget */}
      <div className="bg-white rounded-xl shadow p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <ShieldCheck className="w-5 h-5 text-primary-600" />
            <h2 className="text-base font-semibold text-gray-900">Active Rules</h2>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-gray-500">
              <span className="font-semibold text-gray-900">{ruleStats?.active ?? 0}</span> active
              {' / '}
              <span className="font-semibold text-gray-900">{ruleStats?.total ?? 0}</span> total
            </span>
            <button
              onClick={() => navigate('/rules')}
              className="text-sm text-primary-600 hover:text-primary-800 font-medium"
            >
              Manage →
            </button>
          </div>
        </div>

        {ruleStats ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            {/* By Category */}
            <div>
              <p className="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">By Category</p>
              <div className="space-y-1.5">
                {Object.entries(ruleStats.by_category)
                  .sort((a, b) => b[1] - a[1])
                  .map(([cat, count]) => {
                    const pct = ruleStats.total > 0 ? Math.round((count / ruleStats.total) * 100) : 0
                    const label = cat.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
                    return (
                      <div key={cat} className="flex items-center gap-2 text-sm">
                        <span className="w-28 text-gray-600 truncate">{label}</span>
                        <div className="flex-1 bg-gray-100 rounded-full h-1.5">
                          <div
                            className="bg-primary-500 h-1.5 rounded-full"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <span className="w-6 text-right text-xs font-medium text-gray-700">{count}</span>
                      </div>
                    )
                  })}
              </div>
            </div>

            {/* By Severity */}
            <div>
              <p className="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">By Severity</p>
              <div className="space-y-1.5">
                {(['critical','high','medium','low','info'] as const)
                  .filter(s => (ruleStats.by_severity[s] ?? 0) > 0)
                  .map(sev => {
                    const count = ruleStats.by_severity[sev] ?? 0
                    const pct = ruleStats.total > 0 ? Math.round((count / ruleStats.total) * 100) : 0
                    const barColor = {
                      critical: 'bg-red-500', high: 'bg-orange-500',
                      medium: 'bg-yellow-500', low: 'bg-blue-500', info: 'bg-gray-400',
                    }[sev]
                    return (
                      <div key={sev} className="flex items-center gap-2 text-sm">
                        <span className="w-16 text-gray-600 capitalize">{sev}</span>
                        <div className="flex-1 bg-gray-100 rounded-full h-1.5">
                          <div className={`${barColor} h-1.5 rounded-full`} style={{ width: `${pct}%` }} />
                        </div>
                        <span className="w-6 text-right text-xs font-medium text-gray-700">{count}</span>
                      </div>
                    )
                  })}
              </div>
            </div>
          </div>
        ) : (
          <p className="text-sm text-gray-400">Loading rule statistics...</p>
        )}
      </div>

      {/* Recent Findings */}
      <div className="bg-white rounded-xl shadow">
        <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
          <h2 className="text-base font-semibold text-gray-900">Recent Findings</h2>
          <button
            onClick={() => navigate('/findings')}
            className="text-sm text-primary-600 hover:text-primary-800 font-medium"
          >
            View all →
          </button>
        </div>
        <div className="divide-y divide-gray-100">
          {recentFindings?.findings.map(finding => (
            <div key={finding.id} className="px-6 py-4 hover:bg-gray-50 transition-colors">
              <div className="flex items-start gap-3">
                <span className={`mt-0.5 inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold flex-shrink-0 ${
                  finding.severity === 'critical' ? 'bg-red-100 text-red-800' :
                  finding.severity === 'high'     ? 'bg-orange-100 text-orange-800' :
                  finding.severity === 'medium'   ? 'bg-yellow-100 text-yellow-800' :
                                                    'bg-blue-100 text-blue-800'
                }`}>
                  {finding.severity.toUpperCase()}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-gray-900 truncate">{finding.title}</p>
                  <p className="text-xs text-gray-500 mt-0.5 font-mono truncate">
                    {finding.context?.fqn}
                  </p>
                </div>
                <span className="text-xs text-gray-400 flex-shrink-0">
                  {new Date(finding.detected_at).toLocaleDateString()}
                </span>
              </div>
            </div>
          ))}
          {!recentFindings?.findings.length && (
            <div className="px-6 py-12 text-center">
              <Database className="w-12 h-12 text-gray-200 mx-auto mb-3" />
              <p className="text-gray-900 font-medium mb-1">No findings yet</p>
              <p className="text-sm text-gray-400 mb-4">Scan a table to discover quality issues</p>
              <button
                onClick={() => navigate('/scanner')}
                className="px-4 py-2 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700"
              >
                Scan Your First Table
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation } from '@tanstack/react-query'
import { assetsApi, profilingApi, tableHealthApi } from '../api/client'
import type { ColumnMeta, TableProfile, TopValue, ColumnProfile, HealthDot } from '../api/client'
import { useConnection } from '../ConnectionContext'
import {
  Database, Table2, Columns3, BarChart3, Loader2, ChevronRight,
  KeyRound, Hash, AlertCircle, ShieldCheck, History, Activity,
} from 'lucide-react'
import DataHealthPanel, { ColumnStatusDot } from './DataHealthPanel'
import MetricsPanel from './MetricsPanel'

type ExplorerTab = 'overview' | 'stats' | 'health' | 'metrics'

// ── helpers ───────────────────────────────────────────────────────────────────

function formatValue(v: string | number | boolean | null): string {
  if (v === null || v === undefined) return '—'
  const text = typeof v === 'object' ? JSON.stringify(v) : String(v)
  return text.length > 60 ? text.slice(0, 60) + '…' : text
}

function formatBytes(bytes: number | null): string {
  if (bytes == null) return '—'
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`
}

function nullPctTone(pct: number | null): string {
  if (pct === null) return 'text-gray-400 dark:text-gray-400'
  if (pct === 0)    return 'text-green-600'
  if (pct < 5)      return 'text-gray-700 dark:text-gray-200'
  if (pct < 30)     return 'text-yellow-600'
  return 'text-red-600'
}

// ── selector column ───────────────────────────────────────────────────────────

function SelectorColumn({
  title, icon: Icon, items, selected, onSelect, loading, disabled, emptyHint,
}: {
  title: string
  icon: React.ComponentType<{ className?: string }>
  items: string[]
  selected: string | null
  onSelect: (name: string) => void
  loading: boolean
  disabled: boolean
  emptyHint: string
}) {
  return (
    <div className={`flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded-xl bg-white dark:bg-gray-800 flex flex-col ${disabled ? 'opacity-50' : ''}`}>
      <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100 dark:border-gray-700">
        <Icon className="w-4 h-4 text-primary-500 flex-shrink-0" />
        <h3 className="text-xs font-semibold text-gray-700 dark:text-gray-200 uppercase tracking-wide">{title}</h3>
        {items.length > 0 && <span className="ml-auto text-xs text-gray-400 dark:text-gray-400">{items.length}</span>}
      </div>
      <div className="p-2 max-h-72 overflow-y-auto">
        {loading ? (
          <p className="text-xs text-gray-400 dark:text-gray-400 px-2 py-3 flex items-center gap-1.5"><Loader2 className="w-3 h-3 animate-spin" />Loading…</p>
        ) : disabled ? (
          <p className="text-xs text-gray-400 dark:text-gray-400 px-2 py-3">{emptyHint}</p>
        ) : items.length === 0 ? (
          <p className="text-xs text-gray-400 dark:text-gray-400 px-2 py-3">No items found</p>
        ) : (
          <ul className="space-y-0.5">
            {items.map(name => (
              <li key={name}>
                <button
                  onClick={() => onSelect(name)}
                  className={`w-full text-left px-2.5 py-1.5 rounded-lg text-sm truncate transition-colors ${
                    name === selected
                      ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-700 dark:text-primary-300 font-medium'
                      : 'text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700/40'
                  }`}
                  title={name}
                >
                  {name}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function TopValuesCell({ topValues }: { topValues: TopValue[] }) {
  if (!topValues || topValues.length === 0) return <span className="text-gray-400 dark:text-gray-400">—</span>
  return (
    <ul className="space-y-0.5">
      {topValues.map((tv, i) => (
        <li key={i} className="flex items-center gap-1.5 text-xs">
          <span className="font-mono text-gray-700 dark:text-gray-200 truncate max-w-[10rem]">{formatValue(tv.value)}</span>
          <span className="text-gray-400 dark:text-gray-400">({tv.count.toLocaleString()})</span>
        </li>
      ))}
    </ul>
  )
}

// Table meta strip — key/value chips describing the table (no data shown).
function MetaItem({ label, value, full = false }: { label: string; value: React.ReactNode; full?: boolean }) {
  return (
    <div className={`flex flex-col gap-0.5 pr-6 ${full ? 'basis-full pr-0' : ''}`}>
      <span className="text-[10px] font-bold uppercase tracking-wider text-gray-400 dark:text-gray-400">{label}</span>
      <span className="text-sm font-semibold text-gray-800 dark:text-gray-200 tabular-nums">{value}</span>
    </div>
  )
}

function TableMetaStrip({ info }: { info: {
  row_count: number | null; bytes: number | null; kind: string | null;
  owner: string | null; comment: string | null;
} }) {
  return (
    <div className="flex flex-wrap gap-y-2 px-4 py-3 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg">
      <MetaItem label="Rows"  value={info.row_count != null ? info.row_count.toLocaleString() : '—'} />
      <MetaItem label="Size"  value={formatBytes(info.bytes)} />
      <MetaItem label="Type"  value={info.kind ?? '—'} />
      {info.owner && <MetaItem label="Owner" value={info.owner} />}
      {info.comment && <MetaItem label="Comment" value={<span className="font-normal text-gray-600 dark:text-gray-300">{info.comment}</span>} full />}
    </div>
  )
}

// Animated progress bar — estimates ~3s/column, advances to 90% then holds,
// so the user always sees forward motion while profiling runs.
function ProfileProgressBar({ tableName, columnCount }: { tableName: string; columnCount: number }) {
  const [pct, setPct] = useState(0)
  useEffect(() => {
    const totalMs = Math.min(Math.max((columnCount || 5) * 3000, 8000), 120000)
    const intervalMs = 300
    const step = (intervalMs / totalMs) * 90
    setPct(0)
    const id = setInterval(() => {
      setPct(p => {
        const next = p + step
        if (next >= 90) { clearInterval(id); return 90 }
        return next
      })
    }, intervalMs)
    return () => clearInterval(id)
  }, [tableName, columnCount])

  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-5">
      <div className="flex items-center justify-between mb-2.5">
        <span className="text-sm font-semibold text-gray-800 dark:text-gray-200 flex items-center gap-2">
          <BarChart3 className="w-4 h-4 text-primary-600" />
          Profiling {tableName}
        </span>
        <span className="text-sm font-bold text-primary-600 tabular-nums">{Math.round(pct)}%</span>
      </div>
      <div className="h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div className="h-full bg-primary-500 rounded-full transition-[width] duration-300 ease-linear" style={{ width: `${pct}%` }} />
      </div>
      <p className="text-xs text-gray-400 dark:text-gray-400 mt-2">
        Computing per-column stats — null %, distinct count, min/max, top values
        {columnCount ? ` across ${columnCount} columns` : ''}…
      </p>
    </div>
  )
}

// All possible stat columns, keyed to the `relevant_stats` slugs the backend sends.
const STAT_COLUMNS: {
  key: string
  label: string
  render: (c: ColumnProfile) => React.ReactNode
}[] = [
  { key: 'null_percentage', label: 'Null %',
    render: c => <span className={nullPctTone(c.null_percentage)}>{c.null_percentage === null ? '—' : `${c.null_percentage}%`}</span> },
  { key: 'distinct_count', label: 'Distinct',
    render: c => c.distinct_count?.toLocaleString() ?? '—' },
  { key: 'distinct_pct', label: 'Distinct %',
    render: c => c.distinct_pct === null ? '—' : `${c.distinct_pct}%` },
  { key: 'duplicate_count', label: 'Dup Values',
    render: c => c.duplicate_count == null ? '—'
      : c.duplicate_count === 0
        ? <span className="text-green-600">none</span>
        : <span className="text-red-600 font-medium">{c.duplicate_count.toLocaleString()}</span> },
  { key: 'min_value', label: 'Min',
    render: c => <span className="font-mono text-xs">{formatValue(c.min_value)}</span> },
  { key: 'max_value', label: 'Max',
    render: c => (
      <span className="font-mono text-xs">
        {formatValue(c.max_value)}
        {c.outlier_hint && <span className="ml-1 text-red-600" title="Max is far from the mean — possible outlier">⚠</span>}
      </span>
    ) },
  { key: 'avg_value', label: 'Avg',
    render: c => <span className="font-mono text-xs">{formatValue(c.avg_value)}</span> },
  { key: 'stddev', label: 'Std Dev',
    render: c => <span className="font-mono text-xs">{formatValue(c.stddev)}</span> },
  { key: 'freshness_days', label: 'Freshness',
    render: c => c.freshness_days == null ? '—'
      : c.freshness_days < 1 ? <span className="text-green-600">today</span>
      : `${c.freshness_days.toLocaleString()}d old` },
  { key: 'pattern_match_pct', label: 'Pattern Match',
    render: c => c.pattern_match_pct == null ? '—'
      : <span className={c.pattern_match_pct >= 95 ? 'text-green-600' : c.pattern_match_pct >= 70 ? 'text-yellow-600' : 'text-red-600'}>{c.pattern_match_pct}%</span> },
  { key: 'top_values', label: 'Top Values',
    render: c => <TopValuesCell topValues={c.top_values} /> },
]

const CATEGORY_TONE: Record<string, string> = {
  id: 'bg-amber-100 text-amber-700', date: 'bg-sky-100 text-sky-700',
  amount: 'bg-emerald-100 text-emerald-700', measure: 'bg-emerald-100 text-emerald-700',
  status: 'bg-purple-100 text-purple-700', categorical: 'bg-purple-100 text-purple-700',
  email: 'bg-blue-100 text-blue-700', phone: 'bg-blue-100 text-blue-700',
  text: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
}

// One table per category — showing only the stat columns relevant to it.
function CategoryStatsTable({ profile, category }: { profile: TableProfile; category: string }) {
  const cols = profile.columns.filter(c => c.category === category)
  if (cols.length === 0) return null

  // Union of relevant stats for this category, in STAT_COLUMNS order.
  const relevant = new Set(profile.category_stats[category] ?? [])
  const shown = STAT_COLUMNS.filter(sc => relevant.has(sc.key))
  const label = profile.category_labels[category] ?? category

  return (
    <div className="border border-gray-100 dark:border-gray-700 rounded-lg overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2 bg-gray-50 dark:bg-gray-900 border-b border-gray-100 dark:border-gray-700">
        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${CATEGORY_TONE[category] ?? 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'}`}>{label}</span>
        <span className="text-xs text-gray-400 dark:text-gray-400">{cols.length} column{cols.length !== 1 ? 's' : ''}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500 dark:text-gray-300 uppercase tracking-wide border-b border-gray-100 dark:border-gray-700">
              <th className="px-4 py-2 font-medium">Column</th>
              <th className="px-3 py-2 font-medium">Type</th>
              {shown.map(sc => <th key={sc.key} className="px-3 py-2 font-medium">{sc.label}</th>)}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-700/50">
            {cols.map(c => (
              <tr key={c.column_name} className="hover:bg-gray-50 dark:hover:bg-gray-700/40 align-top">
                <td className="px-4 py-2 font-medium text-gray-800 dark:text-gray-200">{c.column_name}</td>
                <td className="px-3 py-2 text-gray-500 dark:text-gray-300 font-mono text-xs">{c.data_type}</td>
                {shown.map(sc => (
                  <td key={sc.key} className="px-3 py-2 text-gray-700 dark:text-gray-200">{sc.render(c)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ColumnStatsPanel({ tableName, profile }: { tableName: string; profile: TableProfile }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100 dark:border-gray-700 bg-primary-50/50 dark:bg-primary-900/20">
        <div className="flex items-center gap-2">
          <BarChart3 className="w-5 h-5 text-primary-600" />
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">Column Statistics — {tableName}</h2>
        </div>
        <p className="text-xs text-gray-600 dark:text-gray-300 mt-1 flex items-center gap-1.5 flex-wrap">
          <Hash className="w-3 h-3" />
          {profile.table.row_count.toLocaleString()} rows · {profile.table.column_count} columns · grouped by inferred category
          {profile.table.is_sampled && (
            <span className="ml-1 px-1.5 py-0.5 rounded bg-yellow-100 text-yellow-700 font-medium">
              sampled ({profile.table.sample_size?.toLocaleString()} rows) — counts are estimates
            </span>
          )}
        </p>
      </div>
      <div className="p-4 space-y-4">
        {profile.categories.map(cat => (
          <CategoryStatsTable key={cat} profile={profile} category={cat} />
        ))}
      </div>
    </div>
  )
}

// ── main page ─────────────────────────────────────────────────────────────────

// Persist a value to localStorage so the drill-down survives page navigation
// (DataExplorer unmounts when you switch pages; without this its selections reset).
function usePersisted(key: string) {
  const [val, setVal] = useState<string | null>(() => {
    try { return localStorage.getItem(key) } catch { return null }
  })
  const set = (v: string | null) => {
    setVal(v)
    try {
      if (v) localStorage.setItem(key, v)
      else localStorage.removeItem(key)
    } catch {}
  }
  return [val, set] as const
}

// In-memory profile cache keyed by table FQN. Module-level, so it survives
// page navigation (component unmount) but is cleared on a hard refresh (the
// module re-evaluates) — exactly the "keep on nav, refetch on refresh" behavior.
const profileCache = new Map<string, TableProfile>()
const fqnKey = (db: string | null, sc: string | null, tb: string | null) =>
  db && sc && tb ? `${db}.${sc}.${tb}` : ''

export default function DataExplorer() {
  const navigate = useNavigate()
  const [selectedDatabase, setSelectedDatabase] = usePersisted('dq_explorer_db')
  const [selectedSchema,   setSelectedSchema]   = usePersisted('dq_explorer_schema')
  const [selectedTable,    setSelectedTable]    = usePersisted('dq_explorer_table')
  // Restore any cached profile for the currently-selected table on mount.
  const [profile, setProfile] = useState<TableProfile | null>(
    () => profileCache.get(fqnKey(selectedDatabase, selectedSchema, selectedTable)) ?? null
  )
  const [tab, setTab] = useState<ExplorerTab>('overview')

  const { selectedId: connId } = useConnection()

  // Reset the drill-down when the active connection ACTUALLY changes — a
  // DB/schema/table from one source is meaningless on another. We skip the
  // first run (mount) so navigating back to this page keeps the persisted
  // selection instead of wiping it every time the component remounts.
  const prevConnId = useRef(connId)
  useEffect(() => {
    if (prevConnId.current !== connId) {
      prevConnId.current = connId
      setSelectedDatabase(null); setSelectedSchema(null); setSelectedTable(null); setProfile(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connId])

  const { data: databases, isLoading: loadingDbs } = useQuery({
    queryKey: ['databases', connId],
    queryFn: () => assetsApi.discoverDatabases(connId).then(r => r.data),
    staleTime: 5 * 60 * 1000,
  })
  const { data: schemas, isLoading: loadingSchemas } = useQuery({
    queryKey: ['schemas', connId, selectedDatabase],
    queryFn: () => assetsApi.discoverSchemas(selectedDatabase!, connId).then(r => r.data),
    enabled: !!selectedDatabase,
    staleTime: 5 * 60 * 1000,
  })
  const { data: tables, isLoading: loadingTables } = useQuery({
    queryKey: ['tables', connId, selectedDatabase, selectedSchema],
    queryFn: () => assetsApi.discoverTables(selectedDatabase!, selectedSchema!, connId).then(r => r.data),
    enabled: !!selectedDatabase && !!selectedSchema,
    staleTime: 5 * 60 * 1000,
  })
  const { data: tableInfo } = useQuery({
    queryKey: ['table-info', connId, selectedDatabase, selectedSchema, selectedTable],
    queryFn: () => profilingApi.tableInfo(selectedDatabase!, selectedSchema!, selectedTable!, connId).then(r => r.data),
    enabled: !!selectedDatabase && !!selectedSchema && !!selectedTable,
    staleTime: 5 * 60 * 1000,
  })
  const { data: columnsData, isLoading: loadingColumns } = useQuery({
    queryKey: ['columns', connId, selectedDatabase, selectedSchema, selectedTable],
    queryFn: () => profilingApi.columns(selectedDatabase!, selectedSchema!, selectedTable!, connId).then(r => r.data),
    enabled: !!selectedDatabase && !!selectedSchema && !!selectedTable,
    staleTime: 5 * 60 * 1000,
  })
  const columns: ColumnMeta[] = columnsData?.columns ?? []

  // Shared with DataHealthPanel via matching queryKey (react-query dedupes).
  // Powers the per-column status dots on the Overview columns table.
  const { data: health } = useQuery({
    queryKey: ['table-health', selectedDatabase, selectedSchema, selectedTable],
    queryFn: () => tableHealthApi.get(selectedDatabase!, selectedSchema!, selectedTable!).then(r => r.data),
    enabled: !!selectedDatabase && !!selectedSchema && !!selectedTable,
    staleTime: 30 * 1000,
  })
  const columnStatus: Record<string, HealthDot> = health?.column_status ?? {}

  const profileMutation = useMutation({
    mutationFn: () => profilingApi.profile(selectedDatabase!, selectedSchema!, selectedTable!, connId).then(r => r.data),
    onSuccess: (data) => {
      // Re-profile always brings fresh stats and refreshes the in-memory cache.
      profileCache.set(fqnKey(selectedDatabase, selectedSchema, selectedTable), data)
      setProfile(data)
    },
  })

  const selectDatabase = (db: string) => {
    setSelectedDatabase(db); setSelectedSchema(null); setSelectedTable(null); setProfile(null)
  }
  const selectSchema = (s: string) => {
    setSelectedSchema(s); setSelectedTable(null); setProfile(null)
  }
  const selectTable = (t: string) => {
    setSelectedTable(t)
    // Show a cached profile for this table if we have one; otherwise clear.
    setProfile(profileCache.get(fqnKey(selectedDatabase, selectedSchema, t)) ?? null)
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Data Explorer</h1>
        <p className="mt-1 text-gray-600 dark:text-gray-300">
          Browse Snowflake databases, inspect a table's columns, and profile its data — null %,
          distinct counts, ranges, and top values that rule checks alone can't reveal.
        </p>
      </div>

      {/* Breadcrumb */}
      {selectedDatabase && (
        <div className="flex items-center gap-1.5 text-sm text-gray-500 dark:text-gray-300 flex-wrap">
          <Database className="w-3.5 h-3.5 text-primary-500" />
          <span className="font-medium text-gray-700 dark:text-gray-200">{selectedDatabase}</span>
          {selectedSchema && (<><ChevronRight className="w-3.5 h-3.5" /><span className="font-medium text-gray-700 dark:text-gray-200">{selectedSchema}</span></>)}
          {selectedTable && (<><ChevronRight className="w-3.5 h-3.5" /><span className="font-medium text-gray-900 dark:text-gray-100">{selectedTable}</span></>)}
        </div>
      )}

      {/* Three-column drill-down */}
      <div className="flex flex-col sm:flex-row gap-3">
        <SelectorColumn
          title="Databases" icon={Database}
          items={databases?.databases ?? []}
          selected={selectedDatabase} onSelect={selectDatabase}
          loading={loadingDbs} disabled={false} emptyHint=""
        />
        <SelectorColumn
          title="Schemas" icon={Columns3}
          items={schemas?.schemas ?? []}
          selected={selectedSchema} onSelect={selectSchema}
          loading={loadingSchemas} disabled={!selectedDatabase}
          emptyHint="Select a database first"
        />
        <SelectorColumn
          title="Tables" icon={Table2}
          items={tables?.tables ?? []}
          selected={selectedTable} onSelect={selectTable}
          loading={loadingTables} disabled={!selectedSchema}
          emptyHint="Select a schema first"
        />
      </div>

      {/* Tab bar — only meaningful once a table is picked */}
      {selectedTable && (
        <div className="flex items-center gap-1 border-b border-gray-200 dark:border-gray-700">
          {([
            { id: 'overview', label: 'Overview', icon: Table2 },
            { id: 'stats',    label: 'Column Stats', icon: BarChart3 },
            { id: 'health',   label: 'Data Health', icon: ShieldCheck,
              badge: health && health.rules_failing > 0 ? health.rules_failing : undefined },
            { id: 'metrics',  label: 'Metrics', icon: Activity },
          ] as const).map(t => {
            const Icon = t.icon
            const active = tab === t.id
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id as ExplorerTab)}
                className={`flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
                  active
                    ? 'border-primary-500 text-primary-700 dark:text-primary-300'
                    : 'border-transparent text-gray-500 dark:text-gray-300 hover:text-gray-800 dark:hover:text-gray-100'
                }`}
              >
                <Icon className="w-4 h-4" />
                {t.label}
                {'badge' in t && t.badge !== undefined && (
                  <span className="ml-1 text-xs font-semibold px-1.5 py-0.5 rounded-full bg-red-100 text-red-700">{t.badge}</span>
                )}
              </button>
            )
          })}
        </div>
      )}

      {/* ── Column Stats tab — profile progress / results ─────────────────── */}
      {selectedTable && tab === 'stats' && profileMutation.isPending && (
        <ProfileProgressBar tableName={selectedTable} columnCount={columns.length} />
      )}

      {selectedTable && tab === 'stats' && profileMutation.isError && !profileMutation.isPending && (
        <div className="bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-500/40 rounded-xl px-5 py-3 text-sm text-red-700 dark:text-red-300 flex items-center gap-2">
          <AlertCircle className="w-4 h-4" />
          Profiling failed: {(profileMutation.error as any)?.response?.data?.detail ?? (profileMutation.error as Error).message}
        </div>
      )}

      {selectedTable && tab === 'stats' && profile && !profileMutation.isPending && (
        <ColumnStatsPanel tableName={selectedTable} profile={profile} />
      )}

      {selectedTable && tab === 'stats' && !profile && !profileMutation.isPending && (
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-8 text-center">
          <BarChart3 className="w-10 h-10 text-gray-200 mx-auto mb-3" />
          <p className="text-gray-900 dark:text-gray-100 font-medium mb-1">No profile yet</p>
          <p className="text-sm text-gray-400 mb-4">Run profiling to see null %, distinct counts, ranges, and top values.</p>
          <button
            onClick={() => profileMutation.mutate()}
            className="inline-flex items-center gap-2 px-4 py-2 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700 transition-colors"
          >
            <BarChart3 className="w-4 h-4" />Profile this table
          </button>
        </div>
      )}

      {/* ── Data Health tab ────────────────────────────────────────────────── */}
      {selectedTable && tab === 'health' && (
        <DataHealthPanel database={selectedDatabase!} schema={selectedSchema!} table={selectedTable} />
      )}

      {/* ── Metrics tab ─────────────────────────────────────────────────────── */}
      {selectedTable && tab === 'metrics' && (
        <MetricsPanel database={selectedDatabase!} schema={selectedSchema!} table={selectedTable} />
      )}

      {/* ── Overview tab: meta strip + columns table ──────────────────────── */}
      {selectedTable && tab === 'overview' && (
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
          <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-700">
            <div className="flex items-center gap-2 min-w-0">
              <Table2 className="w-4 h-4 text-primary-500 flex-shrink-0" />
              <h2 className="text-sm font-semibold text-gray-800 dark:text-gray-200 truncate">{selectedTable}</h2>
              {columns.length > 0 && <span className="text-xs text-gray-400 dark:text-gray-400">{columns.length} columns</span>}
            </div>
            <button
              onClick={() => {
                const params = new URLSearchParams()
                if (selectedDatabase) params.set('db', selectedDatabase)
                if (selectedSchema)   params.set('schema', selectedSchema)
                if (selectedTable)    params.set('table', selectedTable)
                navigate(`/run-history?${params.toString()}`)
              }}
              className="flex items-center gap-2 px-4 py-2 bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-gray-700 dark:text-gray-200 text-sm font-medium rounded-lg hover:bg-gray-50 dark:hover:bg-gray-600 transition-colors flex-shrink-0"
            >
              <History className="w-4 h-4" />View Run History
            </button>
          </div>

          <div className="px-6 py-4">
            {tableInfo && <TableMetaStrip info={tableInfo} />}

            {loadingColumns ? (
              <div className="py-8 text-sm text-gray-400 dark:text-gray-400 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />Loading columns…</div>
            ) : (
              <div className="overflow-x-auto mt-4">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-gray-500 dark:text-gray-300 uppercase tracking-wide border-b border-gray-100 dark:border-gray-700">
                      <th className="py-2.5 pr-4 font-medium">Column</th>
                      <th className="py-2.5 px-4 font-medium">Data Type</th>
                      <th className="py-2.5 px-4 font-medium">Nullable</th>
                      <th className="py-2.5 px-4 font-medium">Key</th>
                      <th className="py-2.5 px-4 font-medium">Comment</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50 dark:divide-gray-700/50">
                    {columns.map(c => (
                      <tr key={c.column_name} className="hover:bg-gray-50 dark:hover:bg-gray-700/40">
                        <td className="py-2.5 pr-4 font-medium text-gray-800 dark:text-gray-200">
                          <span className="inline-flex items-center gap-2">
                            <ColumnStatusDot status={columnStatus[c.column_name]} />
                            {c.column_name}
                          </span>
                        </td>
                        <td className="py-2.5 px-4 text-gray-500 dark:text-gray-300 font-mono text-xs">{c.data_type}</td>
                        <td className="py-2.5 px-4">
                          {c.is_nullable
                            ? <span className="text-gray-400 dark:text-gray-400 text-xs">nullable</span>
                            : <span className="text-gray-700 dark:text-gray-200 text-xs font-medium">NOT NULL</span>}
                        </td>
                        <td className="py-2.5 px-4">
                          {c.primary_key ? (
                            <span className="inline-flex items-center gap-1 text-xs text-amber-600 font-medium"><KeyRound className="w-3 h-3" />PK</span>
                          ) : c.unique_key ? (
                            <span className="inline-flex items-center gap-1 text-xs text-indigo-600 font-medium"><KeyRound className="w-3 h-3" />Unique</span>
                          ) : (
                            <span className="text-gray-300">—</span>
                          )}
                        </td>
                        <td className="py-2.5 px-4 text-gray-500 dark:text-gray-300 text-xs max-w-xs truncate" title={c.comment ?? ''}>
                          {c.comment || <span className="text-gray-300">—</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Empty state */}
      {!selectedTable && (
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-12 text-center">
          <Database className="w-12 h-12 text-gray-200 mx-auto mb-3" />
          <p className="text-gray-900 dark:text-gray-100 font-medium mb-1">Pick a table to explore</p>
          <p className="text-sm text-gray-400 dark:text-gray-400">Select a database → schema → table above, then profile its data.</p>
        </div>
      )}
    </div>
  )
}

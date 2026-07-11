import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ruleLibraryApi } from '../api/client'
import type { RuleDefinition, RuleInstance } from '../api/client'
import {
  ShieldCheck, FileText, Database, Tag, Filter, Search, X,
  ArrowLeft, ChevronDown, ChevronRight, Code2, Sparkles, Layers,
  CheckCircle2, XCircle, AlertTriangle, Clock, Hash, GitBranch,
} from 'lucide-react'

// ── Constants (mirrors Rules.tsx's palette so both pages feel like one system) ─

const CATEGORIES = ['documentation', 'ownership', 'schema', 'naming', 'data_quality', 'security', 'performance']

const CATEGORY_COLORS: Record<string, string> = {
  security: 'bg-red-100 text-red-800 border-red-200',
  data_quality: 'bg-orange-100 text-orange-800 border-orange-200',
  schema: 'bg-blue-100 text-blue-800 border-blue-200',
  naming: 'bg-purple-100 text-purple-800 border-purple-200',
  documentation: 'bg-yellow-100 text-yellow-800 border-yellow-200',
  ownership: 'bg-green-100 text-green-800 border-green-200',
  performance: 'bg-gray-100 text-gray-800 border-gray-200',
}

const SEVERITY_COLORS: Record<string, string> = {
  critical: 'bg-red-100 text-red-800',
  high: 'bg-orange-100 text-orange-800',
  medium: 'bg-yellow-100 text-yellow-800',
  low: 'bg-blue-100 text-blue-800',
  info: 'bg-gray-100 text-gray-800',
}

const STATUS_STYLES: Record<string, { pill: string; label: string }> = {
  active: { pill: 'bg-green-100 text-green-700', label: 'Active' },
  proposed: { pill: 'bg-yellow-100 text-yellow-700', label: 'Proposed' },
  pending: { pill: 'bg-yellow-100 text-yellow-700', label: 'Pending' },
  disabled: { pill: 'bg-gray-100 text-gray-500', label: 'Disabled' },
  rejected: { pill: 'bg-red-100 text-red-700', label: 'Rejected' },
}

const CATEGORY_ICONS: Record<string, React.ReactNode> = {
  security: <ShieldCheck className="w-4 h-4" />,
  documentation: <FileText className="w-4 h-4" />,
  schema: <Database className="w-4 h-4" />,
  naming: <Tag className="w-4 h-4" />,
  ownership: <ShieldCheck className="w-4 h-4" />,
  data_quality: <Filter className="w-4 h-4" />,
}

function cap(s: string) {
  return s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

function formatTarget(scope: string, targetConfig: Record<string, any>): string {
  if (scope === 'column' && targetConfig.column) return `column: ${targetConfig.column}`
  if (scope === 'multi_column' && targetConfig.columns) return `columns: [${targetConfig.columns.join(', ')}]`
  if (scope === 'cross_table' && targetConfig.column) {
    const ref = targetConfig.ref_table
      ? ` → ${targetConfig.ref_database ?? ''}.${targetConfig.ref_schema ?? ''}.${targetConfig.ref_table}.${targetConfig.ref_column ?? ''}`
      : ''
    return `${targetConfig.column}${ref}`
  }
  if (scope === 'table') return 'table-level'
  return Object.keys(targetConfig).length === 0 ? 'table-level' : JSON.stringify(targetConfig)
}

function CheckKindBadge({ checkKind }: { checkKind: string }) {
  return (
    <span className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded font-medium flex items-center gap-1">
      {checkKind === 'sql_template' ? <Code2 className="w-3 h-3" /> : <ShieldCheck className="w-3 h-3" />}
      {checkKind === 'sql_template' ? 'SQL' : 'Handler'}
    </span>
  )
}

function executionDot(status: string) {
  if (status === 'passed') return <CheckCircle2 key="p" className="w-3.5 h-3.5 text-green-500" />
  if (status === 'failed') return <XCircle key="f" className="w-3.5 h-3.5 text-red-500" />
  return <AlertTriangle key="e" className="w-3.5 h-3.5 text-amber-500" />
}

// ── Stat card (mirrors Dashboard.tsx's StatCard) ──────────────────────────────

function StatCard({ title, value, icon: Icon, color }: { title: string; value: number; icon: any; color: string }) {
  return (
    <div className="bg-white rounded-lg shadow p-6 flex items-center justify-between">
      <div>
        <p className="text-sm font-medium text-gray-500">{title}</p>
        <p className="mt-1 text-3xl font-bold text-gray-900">{value}</p>
      </div>
      <div className={`p-3 rounded-full ${color}`}>
        <Icon className="w-6 h-6 text-white" />
      </div>
    </div>
  )
}

// ── Instance row (expandable) ─────────────────────────────────────────────────

function InstanceRow({ instance }: { instance: RuleInstance }) {
  const [expanded, setExpanded] = useState(false)

  const { data: executionsData } = useQuery({
    queryKey: ['rule-instance-executions', instance.id],
    queryFn: () => ruleLibraryApi.listExecutions(instance.id).then(r => r.data),
    enabled: expanded,
  })

  const statusStyle = STATUS_STYLES[instance.status] ?? STATUS_STYLES.active

  return (
    <div className="px-6 py-4">
      <div className="flex items-start gap-4 cursor-pointer" onClick={() => setExpanded(e => !e)}>
        <div className="flex-shrink-0 mt-0.5">
          {expanded ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-1">
            <span className={`text-xs font-semibold px-2 py-0.5 rounded border ${SEVERITY_COLORS[instance.severity] ?? ''}`}>
              {instance.severity.toUpperCase()}
            </span>
            <span className="text-sm font-semibold text-gray-900 font-mono">
              {instance.database_name}.{instance.schema_name}.{instance.table_name}
            </span>
            <span className="text-xs text-gray-500">{formatTarget(instance.scope, instance.target_config)}</span>
          </div>
          {instance.rationale && (
            <p className="text-sm text-gray-600">{instance.rationale}</p>
          )}
          {instance.rejection_reason && (
            <p className="text-xs text-red-600 bg-red-50 px-2 py-1 rounded mt-1 inline-block">
              ✗ Rejected: {instance.rejection_reason}
            </p>
          )}
        </div>
        <span className={`text-xs font-medium px-2.5 py-1 rounded-full flex-shrink-0 ${statusStyle.pill}`}>
          {statusStyle.label}
        </span>
      </div>

      {expanded && (
        <div className="mt-3 ml-8 space-y-3">
          {instance.rule_sql ? (
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1 flex items-center gap-1">
                <Code2 className="w-3.5 h-3.5" /> SQL
              </p>
              <pre className="text-xs bg-gray-900 text-green-400 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">
                {instance.rule_sql}
              </pre>
            </div>
          ) : (
            <p className="text-xs text-gray-400">No SQL rendered for this instance (python_handler check).</p>
          )}

          <div>
            <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1 flex items-center gap-1">
              <Clock className="w-3.5 h-3.5" /> Recent runs
            </p>
            {executionsData && executionsData.executions.length > 0 ? (
              <div className="flex items-center gap-2 flex-wrap">
                {executionsData.executions.map(e => (
                  <span key={e.id} className="flex items-center gap-1 text-xs text-gray-500" title={e.executed_at}>
                    {executionDot(e.status)}
                    {new Date(e.executed_at).toLocaleDateString()}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-400">No execution history yet.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Instances view ────────────────────────────────────────────────────────────

function InstancesView({ definition, onBack }: { definition: RuleDefinition; onBack: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ['rule-definition-instances', definition.id],
    queryFn: () => ruleLibraryApi.listInstances(definition.id).then(r => r.data),
  })

  return (
    <div className="space-y-6">
      <button onClick={onBack} className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-800">
        <ArrowLeft className="w-4 h-4" /> Back to Rule Library
      </button>

      <div className={`bg-white rounded-xl shadow overflow-hidden`}>
        <div className={`px-6 py-4 flex items-center gap-2 border-b ${CATEGORY_COLORS[definition.category] ?? 'bg-gray-50 text-gray-700 border-gray-200'}`}>
          {CATEGORY_ICONS[definition.category] ?? <ShieldCheck className="w-4 h-4" />}
          <span className="text-base font-semibold">{definition.name}</span>
          <CheckKindBadge checkKind={definition.check_kind} />
          {definition.source === 'claude' && (
            <span className="text-xs bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded font-medium flex items-center gap-1">
              <Sparkles className="w-3 h-3" /> AI-proposed
            </span>
          )}
          <span className="ml-auto text-xs opacity-70">
            {definition.instance_count} instance{definition.instance_count !== 1 ? 's' : ''} · {definition.approval_count} approved
          </span>
        </div>
        <div className="px-6 py-4">
          <p className="text-sm text-gray-600">{definition.description}</p>
          {definition.template_shape && (
            <p className="text-xs text-gray-400 mt-2 font-mono">shape: {definition.template_shape}</p>
          )}
        </div>
      </div>

      <div className="bg-white rounded-xl shadow overflow-hidden">
        {isLoading ? (
          <div className="p-12 text-center text-gray-400">Loading instances…</div>
        ) : !data || data.instances.length === 0 ? (
          <div className="p-12 text-center text-gray-400">No instances of this definition yet.</div>
        ) : (
          <div className="divide-y divide-gray-100">
            {data.instances.map(instance => (
              <InstanceRow key={instance.id} instance={instance} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Definitions view ──────────────────────────────────────────────────────────

function DefinitionsView({ onSelect }: { onSelect: (d: RuleDefinition) => void }) {
  const [categoryFilter, setCategoryFilter] = useState('')
  const [checkKindFilter, setCheckKindFilter] = useState('')
  const [search, setSearch] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['rule-definitions'],
    queryFn: () => ruleLibraryApi.listDefinitions({}).then(r => r.data),
    staleTime: 30_000,
  })

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (data?.definitions ?? []).filter(d => {
      if (categoryFilter && d.category !== categoryFilter) return false
      if (checkKindFilter && d.check_kind !== checkKindFilter) return false
      if (q) return (
        d.name.toLowerCase().includes(q) ||
        d.description.toLowerCase().includes(q) ||
        d.category.toLowerCase().includes(q) ||
        (d.template_shape ?? '').toLowerCase().includes(q)
      )
      return true
    })
  }, [data, categoryFilter, checkKindFilter, search])

  const grouped = filtered.reduce<Record<string, RuleDefinition[]>>((acc, d) => {
    const cat = d.category || 'other'
    acc[cat] = acc[cat] ? [...acc[cat], d] : [d]
    return acc
  }, {})

  const totalInstances = (data?.definitions ?? []).reduce((sum, d) => sum + d.instance_count, 0)
  const anyFilter = search || categoryFilter || checkKindFilter

  return (
    <div className="space-y-6">
      {/* Stat cards */}
      <div className="grid grid-cols-2 gap-3 sm:gap-6">
        <StatCard title="Definitions" value={data?.total ?? 0} icon={Layers} color="bg-blue-500" />
        <StatCard title="Instances" value={totalInstances} icon={Hash} color="bg-primary-500" />
      </div>

      {/* Search + Filters */}
      <div className="bg-white rounded-xl shadow p-4 space-y-3">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
          <input
            type="text"
            placeholder="Search by name, description, or template shape…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full pl-9 pr-9 py-2.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
          />
          {search && (
            <button onClick={() => setSearch('')} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

        <div className="flex flex-wrap gap-3">
          <select value={categoryFilter} onChange={e => setCategoryFilter(e.target.value)}
            className="px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary-500">
            <option value="">All Categories</option>
            {CATEGORIES.map(c => <option key={c} value={c}>{cap(c)}</option>)}
          </select>

          <select value={checkKindFilter} onChange={e => setCheckKindFilter(e.target.value)}
            className="px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary-500">
            <option value="">All Check Kinds</option>
            <option value="sql_template">SQL</option>
            <option value="python_handler">Handler</option>
          </select>

          {anyFilter && (
            <button
              onClick={() => { setSearch(''); setCategoryFilter(''); setCheckKindFilter('') }}
              className="flex items-center gap-1 px-3 py-2 text-sm text-gray-500 hover:text-gray-700 border border-dashed border-gray-300 rounded-lg"
            >
              <X className="w-3.5 h-3.5" /> Clear
            </button>
          )}
        </div>
      </div>

      {/* Definitions grouped by category */}
      {isLoading ? (
        <div className="bg-white rounded-xl shadow p-12 text-center text-gray-400">Loading definitions…</div>
      ) : Object.keys(grouped).length === 0 ? (
        <div className="bg-white rounded-xl shadow p-12 text-center text-gray-400">No definitions match the current filters.</div>
      ) : (
        Object.entries(grouped).map(([category, definitions]) => (
          <div key={category} className="bg-white rounded-xl shadow overflow-hidden">
            <div className={`px-6 py-3 flex items-center gap-2 border-b ${CATEGORY_COLORS[category] ?? 'bg-gray-50 text-gray-700 border-gray-200'}`}>
              {CATEGORY_ICONS[category] ?? <ShieldCheck className="w-4 h-4" />}
              <span className="text-sm font-semibold">{cap(category)}</span>
              <span className="ml-auto text-xs opacity-70">{definitions.length} definition{definitions.length !== 1 ? 's' : ''}</span>
            </div>

            <div className="divide-y divide-gray-100">
              {definitions
                .slice()
                .sort((a, b) => b.instance_count - a.instance_count)
                .map(d => (
                  <div
                    key={d.id}
                    onClick={() => onSelect(d)}
                    className="px-6 py-4 flex items-start gap-4 hover:bg-gray-50 cursor-pointer transition-colors"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex flex-wrap items-center gap-2 mb-1">
                        <span className="text-sm font-semibold text-gray-900">{d.name}</span>
                        <CheckKindBadge checkKind={d.check_kind} />
                        {d.source === 'claude' && (
                          <span className="text-xs bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded font-medium flex items-center gap-1">
                            <Sparkles className="w-3 h-3" /> AI
                          </span>
                        )}
                        {d.template_shape && (
                          <span className="text-xs font-mono text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">{d.template_shape}</span>
                        )}
                      </div>
                      <p className="text-sm text-gray-600 leading-relaxed">{d.description}</p>
                      <div className="flex flex-wrap items-center gap-3 text-xs text-gray-400 mt-2">
                        <span className="flex items-center gap-1"><Hash className="w-3 h-3" /> {d.instance_count} instance{d.instance_count !== 1 ? 's' : ''}</span>
                        <span className="flex items-center gap-1"><GitBranch className="w-3 h-3" /> {d.approval_count} approved</span>
                      </div>
                    </div>
                    <div className="flex flex-col items-end gap-2 flex-shrink-0">
                      <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${(STATUS_STYLES[d.status] ?? STATUS_STYLES.active).pill}`}>
                        {(STATUS_STYLES[d.status] ?? STATUS_STYLES.active).label}
                      </span>
                      <ChevronRight className="w-4 h-4 text-gray-300" />
                    </div>
                  </div>
                ))}
            </div>
          </div>
        ))
      )}
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function RuleLibrary() {
  const [selectedDefinition, setSelectedDefinition] = useState<RuleDefinition | null>(null)

  return (
    <div className="space-y-6">
      {!selectedDefinition && (
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Rule Library</h1>
          <p className="mt-1 text-gray-500">
            Check definitions (concepts) and the specific table/column instances applying them.
          </p>
        </div>
      )}

      {selectedDefinition ? (
        <InstancesView definition={selectedDefinition} onBack={() => setSelectedDefinition(null)} />
      ) : (
        <DefinitionsView onSelect={setSelectedDefinition} />
      )}
    </div>
  )
}

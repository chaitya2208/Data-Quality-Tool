import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ruleLibraryApi, rulesApi } from '../api/client'
import type { RuleDefinition, RuleInstance, RuleCreatePayload, GeneratedRule } from '../api/client'
import {
  ShieldCheck, FileText, Database, Tag, Filter, Search, X,
  ArrowLeft, ChevronDown, ChevronRight, Code2, Sparkles, Layers,
  CheckCircle2, XCircle, AlertTriangle, Clock, Hash, GitBranch,
  ToggleLeft, ToggleRight, Plus, ExternalLink, Loader2, RefreshCw,
} from 'lucide-react'

// ── Constants (mirrors Rules.tsx's palette so both pages feel like one system) ─

const CATEGORIES = ['documentation', 'ownership', 'schema', 'naming', 'data_quality', 'security', 'performance']
const ASSET_TYPES = ['table', 'column', 'schema', 'database']

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

const emptyForm = (): RuleCreatePayload => ({
  code: '', name: '', description: '',
  category: 'schema', severity: 'medium',
  applies_to: ['table'], rule_config: {},
  is_active: false,   // starts pending/inactive
  owner: '', created_by: '', jira_ticket: '',
})

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
  const navigate = useNavigate()

  const { data: executionsData } = useQuery({
    queryKey: ['rule-instance-executions', instance.id],
    queryFn: () => ruleLibraryApi.listExecutions(instance.id).then(r => r.data),
    enabled: expanded,
  })

  const statusStyle = STATUS_STYLES[instance.status] ?? STATUS_STYLES.active

  return (
    <div className="px-6 py-4">
      <div className="flex items-start gap-4">
        <div className="flex-shrink-0 mt-0.5 cursor-pointer" onClick={() => setExpanded(e => !e)}>
          {expanded ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />}
        </div>
        <div className="flex-1 min-w-0 cursor-pointer" onClick={() => setExpanded(e => !e)}>
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
        <div className="flex flex-col items-end gap-2 flex-shrink-0">
          <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${statusStyle.pill}`}>
            {statusStyle.label}
          </span>
          <button
            onClick={e => { e.stopPropagation(); navigate(`/findings?instance=${instance.id}`) }}
            className="flex items-center gap-1 text-xs text-primary-600 hover:text-primary-800 font-medium"
            title="View findings for this instance"
          >
            <ExternalLink className="w-3 h-3" /> Findings
          </button>
        </div>
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

// ── Definition toggle (enable/disable the whole check concept — gates every
// instance under it at execution time, see RuleEngine.get_active_instances) ──

function DefinitionToggle({ definition }: { definition: RuleDefinition }) {
  const queryClient = useQueryClient()
  const canToggle = definition.status === 'active' || definition.status === 'disabled'

  const toggleMutation = useMutation({
    mutationFn: (is_active: boolean) => ruleLibraryApi.toggleDefinition(definition.id, is_active),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['rule-definitions'] })
      queryClient.invalidateQueries({ queryKey: ['rules-stats'] })
    },
  })

  if (!canToggle) return <div className="w-6 flex-shrink-0" />

  return (
    <button
      onClick={e => { e.stopPropagation(); toggleMutation.mutate(!(definition.status === 'active')) }}
      className="flex-shrink-0"
      title={definition.status === 'active' ? 'Disable this check (all instances)' : 'Enable this check'}
    >
      {definition.status === 'active'
        ? <ToggleRight className="w-6 h-6 text-green-500 hover:text-green-600" />
        : <ToggleLeft className="w-6 h-6 text-gray-300 hover:text-gray-400" />}
    </button>
  )
}

// ── Target group summary — where this definition is actually applied ────────
// Groups instances by table and, within each table, breaks down how many
// columns/multi-column groups/cross-table refs it's applied to. Built purely
// from RuleInstance.scope + .target_config already returned by listInstances
// — no new backend field needed, this data is already stored per instance.

interface TableTargetGroup {
  key: string
  label: string          // "DB.SCHEMA.TABLE" or "Global (all tables)"
  columnCount: number     // distinct columns targeted via scope='column'
  multiColumnCount: number
  crossTableCount: number
  tableLevelCount: number
}

function summarizeTargetGroups(instances: RuleInstance[]): TableTargetGroup[] {
  const groups = new Map<string, TableTargetGroup>()
  for (const inst of instances) {
    const isGlobal = inst.database_name === '*' || !inst.table_name
    const key = isGlobal
      ? '__global__'
      : `${inst.database_name}.${inst.schema_name}.${inst.table_name}`
    const label = isGlobal
      ? 'Global (all tables)'
      : `${inst.schema_name}.${inst.table_name}`

    if (!groups.has(key)) {
      groups.set(key, { key, label, columnCount: 0, multiColumnCount: 0, crossTableCount: 0, tableLevelCount: 0 })
    }
    const g = groups.get(key)!
    if (inst.scope === 'column') g.columnCount += 1
    else if (inst.scope === 'multi_column') g.multiColumnCount += 1
    else if (inst.scope === 'cross_table') g.crossTableCount += 1
    else g.tableLevelCount += 1
  }
  return Array.from(groups.values()).sort((a, b) => a.label.localeCompare(b.label))
}

function TargetGroupChip({ group }: { group: TableTargetGroup }) {
  const parts: string[] = []
  if (group.columnCount > 0) parts.push(`${group.columnCount} column${group.columnCount !== 1 ? 's' : ''}`)
  if (group.multiColumnCount > 0) parts.push(`${group.multiColumnCount} multi-column`)
  if (group.crossTableCount > 0) parts.push(`${group.crossTableCount} cross-table`)
  if (group.tableLevelCount > 0) parts.push(`${group.tableLevelCount} table-level`)

  return (
    <span className="inline-flex items-center gap-1 text-xs font-medium bg-gray-100 text-gray-700 px-2.5 py-1 rounded-full border border-gray-200">
      <Database className="w-3 h-3 text-gray-400" />
      <span className="font-mono">{group.label}</span>
      <span className="text-gray-400">({parts.join(', ') || '0'})</span>
    </span>
  )
}

// ── Instances view ────────────────────────────────────────────────────────────

function InstancesView({ definition, onBack }: { definition: RuleDefinition; onBack: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ['rule-definition-instances', definition.id],
    queryFn: () => ruleLibraryApi.listInstances(definition.id).then(r => r.data),
  })

  const targetGroups = useMemo(() => summarizeTargetGroups(data?.instances ?? []), [data])

  return (
    <div className="space-y-6">
      <button onClick={onBack} className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-800">
        <ArrowLeft className="w-4 h-4" /> Back to Rule Library
      </button>

      <div className={`bg-white rounded-xl shadow overflow-hidden`}>
        <div className={`px-6 py-4 flex items-center gap-2 border-b ${CATEGORY_COLORS[definition.category] ?? 'bg-gray-50 text-gray-700 border-gray-200'}`}>
          <DefinitionToggle definition={definition} />
          {CATEGORY_ICONS[definition.category] ?? <ShieldCheck className="w-4 h-4" />}
          <span className="text-base font-semibold">{definition.name}</span>
          <CheckKindBadge checkKind={definition.check_kind} />
          {definition.source === 'claude' && (
            <span className="text-xs bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded font-medium flex items-center gap-1">
              <Sparkles className="w-3 h-3" /> AI-proposed
            </span>
          )}
          <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${(STATUS_STYLES[definition.status] ?? STATUS_STYLES.active).pill}`}>
            {(STATUS_STYLES[definition.status] ?? STATUS_STYLES.active).label}
          </span>
          <span className="ml-auto text-xs opacity-70">
            {definition.instance_count} instance{definition.instance_count !== 1 ? 's' : ''} · {definition.approval_count} approved
          </span>
        </div>
        <div className="px-6 py-4">
          <p className="text-sm text-gray-600">{definition.description}</p>
          {definition.template_shape && (
            <p className="text-xs text-gray-400 mt-2 font-mono">shape: {definition.template_shape}</p>
          )}
          {targetGroups.length > 0 && (
            <div className="mt-3 pt-3 border-t border-gray-100">
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Applied to</p>
              <div className="flex flex-wrap gap-2">
                {targetGroups.map(g => <TargetGroupChip key={g.key} group={g} />)}
              </div>
            </div>
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
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [showModal, setShowModal] = useState(false)
  const [form, setForm] = useState<RuleCreatePayload>(emptyForm())
  const [formError, setFormError] = useState('')
  // AI generation state
  const [aiPrompt, setAiPrompt] = useState('')
  const [aiOwner, setAiOwner] = useState('')
  const [generated, setGenerated] = useState<GeneratedRule | null>(null)
  const [aiStep, setAiStep] = useState<'prompt' | 'preview'>('prompt')
  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ['rule-definitions'],
    queryFn: () => ruleLibraryApi.listDefinitions({}).then(r => r.data),
    staleTime: 30_000,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['rule-definitions'] })
    queryClient.invalidateQueries({ queryKey: ['rules-stats'] })
  }

  const resetModal = () => {
    setShowModal(false)
    setForm(emptyForm())
    setFormError('')
    setGenerated(null)
    setAiPrompt('')
    setAiOwner('')
    setAiStep('prompt')
  }

  const createMutation = useMutation({
    mutationFn: (payload: RuleCreatePayload) => rulesApi.create(payload),
    onSuccess: () => { invalidate(); resetModal() },
    onError: (err: any) => setFormError(err?.response?.data?.detail || 'Failed to create rule'),
  })

  const generateMutation = useMutation({
    mutationFn: () => rulesApi.generate(aiPrompt, aiOwner).then(r => r.data),
    onSuccess: (result) => {
      setGenerated(result)
      setAiStep('preview')
      // Pre-fill the form with generated values so user can still edit
      setForm({
        code: result.code,
        name: result.name,
        description: result.description,
        category: result.category,
        severity: result.severity,
        applies_to: result.applies_to,
        owner: aiOwner,
        created_by: '',
        jira_ticket: '',
        rule_config: {},
        is_active: false,
      })
    },
  })

  const toggleAppliesTo = (type: string) =>
    setForm(f => ({
      ...f,
      applies_to: f.applies_to.includes(type)
        ? f.applies_to.filter(t => t !== type)
        : [...f.applies_to, type],
    }))

  const handleCreate = () => {
    if (!form.code.trim()) return setFormError('Rule code is required')
    if (!form.name.trim()) return setFormError('Rule name is required')
    if (!form.description.trim()) return setFormError('Description is required')
    if (!form.owner.trim()) return setFormError('Owner is required')
    if (form.applies_to.length === 0) return setFormError('Select at least one asset type')
    const codeClean = form.code.trim().toUpperCase().replace(/\s+/g, '_')
    createMutation.mutate({ ...form, code: codeClean })
  }

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
      {/* Title + Add Rule */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Rule Library</h1>
          <p className="mt-1 text-gray-500">
            Check definitions (concepts) and the specific table/column instances applying them.
          </p>
        </div>
        <button
          onClick={() => { setShowModal(true); setFormError('') }}
          className="flex items-center gap-2 px-4 py-2.5 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700 transition-colors flex-shrink-0"
        >
          <Plus className="w-4 h-4" /> Add Rule
        </button>
      </div>

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
        Object.entries(grouped).map(([category, definitions]) => {
          const isCollapsed = collapsed[category] ?? false
          return (
          <div key={category} className="bg-white rounded-xl shadow overflow-hidden">
            <div
              onClick={() => setCollapsed(c => ({ ...c, [category]: !isCollapsed }))}
              className={`px-6 py-3 flex items-center gap-2 border-b cursor-pointer select-none ${CATEGORY_COLORS[category] ?? 'bg-gray-50 text-gray-700 border-gray-200'}`}
            >
              {isCollapsed ? <ChevronRight className="w-4 h-4 flex-shrink-0" /> : <ChevronDown className="w-4 h-4 flex-shrink-0" />}
              {CATEGORY_ICONS[category] ?? <ShieldCheck className="w-4 h-4" />}
              <span className="text-sm font-semibold">{cap(category)}</span>
              <span className="ml-auto text-xs opacity-70">{definitions.length} definition{definitions.length !== 1 ? 's' : ''}</span>
            </div>

            {!isCollapsed && (
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
                    <div className="mt-0.5"><DefinitionToggle definition={d} /></div>
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
            )}
          </div>
          )
        })
      )}

      {/* ── Add Rule Modal (AI-powered, ported from the old Rules.tsx) ────────── */}
      {showModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-xl overflow-hidden">

            {/* Header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 bg-gradient-to-r from-primary-50 to-purple-50">
              <div>
                <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                  <Sparkles className="w-5 h-5 text-purple-500" />
                  {aiStep === 'prompt' ? 'Add Rule with AI' : 'Review AI-Generated Rule'}
                </h2>
                <p className="text-xs text-gray-500 mt-0.5">
                  {aiStep === 'prompt'
                    ? 'Describe what you want in plain English — Claude will create the rule structure'
                    : 'Edit any field, then submit for approval'}
                </p>
              </div>
              <button onClick={resetModal} className="text-gray-400 hover:text-gray-600 p-1 rounded">
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="px-6 py-5 space-y-4 max-h-[75vh] overflow-y-auto">

              {/* ── Step 1: Prompt ── */}
              {aiStep === 'prompt' && (
                <>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-2">
                      Describe the rule you want
                    </label>
                    <textarea
                      rows={4}
                      value={aiPrompt}
                      onChange={e => setAiPrompt(e.target.value)}
                      placeholder={
                        "Examples:\n" +
                        "• Customer ID should never be null in the orders table\n" +
                        "• Status column should only have values: PENDING, ACTIVE, CLOSED\n" +
                        "• Every fact table should have a created_date column"
                      }
                      className="w-full px-3 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
                    />
                    <p className="text-xs text-gray-400 mt-1">
                      Be as specific as you like — column names, allowed values, business context all help.
                    </p>
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      Your name / team <span className="text-gray-400 text-xs font-normal">(owner of this rule)</span>
                    </label>
                    <input
                      type="text"
                      value={aiOwner}
                      onChange={e => setAiOwner(e.target.value)}
                      placeholder="e.g. data-governance-team or your name"
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
                    />
                  </div>

                  {generateMutation.isError && (
                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-800">
                      {(generateMutation.error as any)?.response?.data?.detail || 'AI generation failed. Try again.'}
                    </div>
                  )}
                </>
              )}

              {/* ── Step 2: Preview + edit ── */}
              {aiStep === 'preview' && generated && (
                <>
                  {/* Duplicate warning — shown when AI or similarity check finds a match */}
                  {generated.duplicate_of && (
                    <div className="bg-amber-50 border border-amber-300 rounded-lg px-4 py-3 flex items-start gap-2">
                      <span className="text-amber-500 text-base flex-shrink-0">⚠️</span>
                      <div>
                        <p className="text-sm font-semibold text-amber-900">Similar rule already exists</p>
                        <p className="text-xs text-amber-800 mt-0.5">
                          <span className="font-mono font-bold">{generated.duplicate_of.code}</span>
                          {' — '}{generated.duplicate_of.name}
                        </p>
                        <p className="text-xs text-amber-700 mt-1">
                          Review the existing rule before submitting. If your requirement is genuinely different, edit the fields below and proceed.
                        </p>
                      </div>
                    </div>
                  )}

                  {/* AI rationale banner */}
                  <div className="bg-purple-50 border border-purple-100 rounded-lg px-4 py-3">
                    <p className="text-xs text-purple-800">
                      <span className="font-semibold">AI Rationale: </span>{generated.rationale}
                    </p>
                  </div>

                  {formError && (
                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-800">{formError}</div>
                  )}

                  {/* Code */}
                  <div>
                    <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                      Rule Code
                    </label>
                    <input type="text"
                      value={form.code}
                      onChange={e => setForm(f => ({ ...f, code: e.target.value.toUpperCase().replace(/\s+/g, '_') }))}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary-500"
                    />
                  </div>

                  {/* Name */}
                  <div>
                    <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                      Name
                    </label>
                    <input type="text"
                      value={form.name}
                      onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
                    />
                  </div>

                  {/* Description */}
                  <div>
                    <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                      Description
                    </label>
                    <textarea rows={3}
                      value={form.description}
                      onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary-500"
                    />
                  </div>

                  {/* Category + Severity */}
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Category</label>
                      <select value={form.category} onChange={e => setForm(f => ({ ...f, category: e.target.value }))}
                        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500">
                        {CATEGORIES.map(c => <option key={c} value={c}>{cap(c)}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Severity</label>
                      <select value={form.severity} onChange={e => setForm(f => ({ ...f, severity: e.target.value }))}
                        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500">
                        {Object.keys(SEVERITY_COLORS).map(s => <option key={s} value={s}>{cap(s)}</option>)}
                      </select>
                    </div>
                  </div>

                  {/* Applies to */}
                  <div>
                    <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Applies To</label>
                    <div className="flex gap-2">
                      {ASSET_TYPES.map(t => (
                        <button key={t} type="button" onClick={() => toggleAppliesTo(t)}
                          className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors ${
                            form.applies_to.includes(t)
                              ? 'bg-primary-600 text-white border-primary-600'
                              : 'bg-white text-gray-600 border-gray-300 hover:border-primary-400'
                          }`}>{t}</button>
                      ))}
                    </div>
                  </div>

                  {/* Owner + Jira */}
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                        Owner <span className="text-red-500">*</span>
                      </label>
                      <input type="text" placeholder="team or person"
                        value={form.owner ?? ''}
                        onChange={e => setForm(f => ({ ...f, owner: e.target.value }))}
                        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                        Jira Ticket
                      </label>
                      <input type="text" placeholder="e.g. DQ-123"
                        value={form.jira_ticket ?? ''}
                        onChange={e => setForm(f => ({ ...f, jira_ticket: e.target.value }))}
                        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary-500"
                      />
                    </div>
                  </div>
                </>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 bg-gray-50">
              {aiStep === 'prompt' ? (
                <>
                  <p className="text-xs text-gray-400 flex items-center gap-1">
                    <Sparkles className="w-3.5 h-3.5 text-purple-400" /> Powered by Claude
                  </p>
                  <div className="flex gap-3">
                    <button
                      onClick={() => { setShowModal(false); setAiPrompt(''); setAiOwner(''); setAiStep('prompt') }}
                      className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => generateMutation.mutate()}
                      disabled={!aiPrompt.trim() || generateMutation.isPending}
                      className="flex items-center gap-2 px-5 py-2 text-sm font-medium text-white bg-purple-600 rounded-lg hover:bg-purple-700 disabled:opacity-50"
                    >
                      {generateMutation.isPending
                        ? <><Loader2 className="w-4 h-4 animate-spin" />Generating...</>
                        : <><Sparkles className="w-4 h-4" />Generate Rule</>
                      }
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <button
                    onClick={() => { setAiStep('prompt'); setGenerated(null); setFormError('') }}
                    className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-800"
                  >
                    <RefreshCw className="w-3.5 h-3.5" /> Regenerate
                  </button>
                  <div className="flex gap-3">
                    <button
                      onClick={resetModal}
                      className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={handleCreate}
                      disabled={createMutation.isPending}
                      className="flex items-center gap-2 px-5 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50"
                    >
                      {createMutation.isPending
                        ? <><Loader2 className="w-4 h-4 animate-spin" />Submitting...</>
                        : <><CheckCircle2 className="w-4 h-4" />Submit for Approval</>
                      }
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function RuleLibrary() {
  const [selectedDefinition, setSelectedDefinition] = useState<RuleDefinition | null>(null)

  return (
    <div className="space-y-6">
      {selectedDefinition ? (
        <InstancesView definition={selectedDefinition} onBack={() => setSelectedDefinition(null)} />
      ) : (
        <DefinitionsView onSelect={setSelectedDefinition} />
      )}
    </div>
  )
}

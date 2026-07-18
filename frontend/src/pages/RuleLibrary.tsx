import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ruleLibraryApi, rulesApi } from '../api/client'
import RuleChatPanel from './RuleChatPanel'
import { fmtIST } from '../utils/dates'
import type { RuleDefinition, RuleInstance, Rule } from '../api/client'
import {
  ShieldCheck, FileText, Database, Tag, Filter, Search, X,
  ArrowLeft, ChevronDown, ChevronRight, Code2, Sparkles, Layers,
  CheckCircle2, XCircle, AlertTriangle, Clock, Hash, GitBranch,
  ToggleLeft, ToggleRight, Plus, ExternalLink, Loader2,
} from 'lucide-react'

// ── Constants (mirrors Rules.tsx's palette so both pages feel like one system) ─


const CATEGORY_COLORS: Record<string, string> = {
  security: 'bg-red-100 text-red-800 border-red-200',
  data_quality: 'bg-orange-100 text-orange-800 border-orange-200',
  schema: 'bg-blue-100 text-blue-800 border-blue-200',
  naming: 'bg-purple-100 text-purple-800 border-purple-200',
  documentation: 'bg-yellow-100 text-yellow-800 border-yellow-200',
  ownership: 'bg-green-100 text-green-800 border-green-200',
  performance: 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200 border-gray-200 dark:border-gray-700',
}

const SEVERITY_COLORS: Record<string, string> = {
  critical: 'bg-red-100 text-red-800',
  high: 'bg-orange-100 text-orange-800',
  medium: 'bg-yellow-100 text-yellow-800',
  low: 'bg-blue-100 text-blue-800',
  info: 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200',
}

const STATUS_STYLES: Record<string, { pill: string; label: string }> = {
  active: { pill: 'bg-green-100 text-green-700', label: 'Active' },
  proposed: { pill: 'bg-yellow-100 text-yellow-700', label: 'Proposed' },
  pending: { pill: 'bg-yellow-100 text-yellow-700', label: 'Pending' },
  disabled: { pill: 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-300', label: 'Disabled' },
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
    <span className="text-xs bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 px-1.5 py-0.5 rounded font-medium flex items-center gap-1">
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
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 flex items-center justify-between">
      <div>
        <p className="text-sm font-medium text-gray-500 dark:text-gray-400">{title}</p>
        <p className="mt-1 text-3xl font-bold text-gray-900 dark:text-gray-100">{value}</p>
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
  const queryClient = useQueryClient()

  const { data: executionsData } = useQuery({
    queryKey: ['rule-instance-executions', instance.id],
    queryFn: () => ruleLibraryApi.listExecutions(instance.id).then(r => r.data),
    enabled: expanded,
  })

  // A newly-added rule lands as a PENDING instance (proposed definition). These
  // approve/reject the instance via the existing endpoints — approve also flips
  // the proposed definition to active server-side (POST /rules/{id}/approve).
  const isPending = instance.status === 'pending'
  const refetchAfterReview = () => {
    queryClient.invalidateQueries({ queryKey: ['rule-definition-instances', instance.definition_id] })
    queryClient.invalidateQueries({ queryKey: ['rule-definitions'] })
    queryClient.invalidateQueries({ queryKey: ['rules-stats'] })
  }
  const approveMutation = useMutation({
    mutationFn: () => rulesApi.approve(instance.id),
    onSuccess: refetchAfterReview,
  })
  const rejectMutation = useMutation({
    mutationFn: (reason: string) => rulesApi.reject(instance.id, reason),
    onSuccess: refetchAfterReview,
  })
  const handleReject = () => {
    const reason = window.prompt('Reason for rejecting this rule?')
    if (reason && reason.trim()) rejectMutation.mutate(reason.trim())
  }
  const reviewing = approveMutation.isPending || rejectMutation.isPending

  const statusStyle = STATUS_STYLES[instance.status] ?? STATUS_STYLES.active

  return (
    <div className="px-6 py-4">
      <div className="flex items-start gap-4">
        <div className="flex-shrink-0 mt-0.5 cursor-pointer" onClick={() => setExpanded(e => !e)}>
          {expanded ? <ChevronDown className="w-4 h-4 text-gray-400 dark:text-gray-500" /> : <ChevronRight className="w-4 h-4 text-gray-400 dark:text-gray-500" />}
        </div>
        <div className="flex-1 min-w-0 cursor-pointer" onClick={() => setExpanded(e => !e)}>
          <div className="flex flex-wrap items-center gap-2 mb-1">
            <span className={`text-xs font-semibold px-2 py-0.5 rounded border ${SEVERITY_COLORS[instance.severity] ?? ''}`}>
              {instance.severity.toUpperCase()}
            </span>
            <span className="text-sm font-semibold text-gray-900 dark:text-gray-100 font-mono">
              {instance.database_name}.{instance.schema_name}.{instance.table_name}
            </span>
            <span className="text-xs text-gray-500 dark:text-gray-400">{formatTarget(instance.scope, instance.target_config)}</span>
          </div>
          {instance.rationale && (
            <p className="text-sm text-gray-600 dark:text-gray-300">{instance.rationale}</p>
          )}
          {/* Review provenance — who approved/rejected and when */}
          {instance.status === 'active' && instance.approved_by && (
            <p className="text-xs text-green-700 dark:text-green-300 mt-1">
              ✓ Approved by <span className="font-medium">{instance.approved_by}</span>
              {instance.approved_at ? ` · ${fmtIST(instance.approved_at)}` : ''}
            </p>
          )}
          {instance.rejection_reason && (
            <p className="text-xs text-red-600 dark:text-red-300 bg-red-50 dark:bg-red-950/40 px-2 py-1 rounded mt-1 inline-block">
              ✗ Rejected{instance.rejected_by ? ` by ${instance.rejected_by}` : ''}: {instance.rejection_reason}
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
          {isPending && (
            <div className="flex items-center gap-2">
              <button
                onClick={e => { e.stopPropagation(); approveMutation.mutate() }}
                disabled={reviewing}
                className="flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-lg bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
                title="Approve — activate this rule"
              >
                {approveMutation.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />} Approve
              </button>
              <button
                onClick={e => { e.stopPropagation(); handleReject() }}
                disabled={reviewing}
                className="flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-lg border border-red-300 dark:border-red-500/40 text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-950/40 disabled:opacity-50"
                title="Reject this rule with a reason"
              >
                {rejectMutation.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <XCircle className="w-3 h-3" />} Reject
              </button>
            </div>
          )}
          {(approveMutation.isError || rejectMutation.isError) && (
            <span className="text-[10px] text-red-600 dark:text-red-400">
              {((approveMutation.error || rejectMutation.error) as any)?.response?.data?.detail || 'Action failed'}
            </span>
          )}
        </div>
      </div>

      {expanded && (
        <div className="mt-3 ml-8 space-y-3">
          {instance.rule_sql ? (
            <div>
              <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1 flex items-center gap-1">
                <Code2 className="w-3.5 h-3.5" /> SQL
              </p>
              <pre className="text-xs bg-gray-900 dark:bg-black text-green-400 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap border border-transparent dark:border-gray-700">
                {instance.rule_sql}
              </pre>
            </div>
          ) : (
            <p className="text-xs text-gray-400 dark:text-gray-500">No SQL rendered for this instance (python_handler check).</p>
          )}

          <div>
            <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1 flex items-center gap-1">
              <Clock className="w-3.5 h-3.5" /> Recent runs
            </p>
            {executionsData && executionsData.executions.length > 0 ? (
              <div className="flex items-center gap-2 flex-wrap">
                {executionsData.executions.map(e => (
                  <span key={e.id} className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400" title={e.executed_at}>
                    {executionDot(e.status)}
                    {new Date(e.executed_at).toLocaleDateString()}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-400 dark:text-gray-500">No execution history yet.</p>
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
    onMutate: async (is_active: boolean) => {
      await queryClient.cancelQueries({ queryKey: ['rule-definitions'] })
      const previous = queryClient.getQueryData(['rule-definitions'])
      queryClient.setQueriesData({ queryKey: ['rule-definitions'] }, (old: any) => {
        if (!old) return old
        const newStatus = is_active ? 'active' : 'disabled'
        if (Array.isArray(old))
          return old.map((d: any) => d.id === definition.id ? { ...d, status: newStatus } : d)
        if (old.items)
          return { ...old, items: old.items.map((d: any) => d.id === definition.id ? { ...d, status: newStatus } : d) }
        return old
      })
      return { previous }
    },
    onError: (_err, _vars, context: any) => {
      if (context?.previous) queryClient.setQueryData(['rule-definitions'], context.previous)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['rule-definitions'] })
      queryClient.invalidateQueries({ queryKey: ['rules-stats'] })
    },
  })

  const isPending = toggleMutation.isPending
  const optimisticActive = isPending
    ? !(definition.status === 'active')
    : definition.status === 'active'

  if (!canToggle) return <div className="w-6 flex-shrink-0" />

  return (
    <button
      onClick={e => { e.stopPropagation(); toggleMutation.mutate(!(definition.status === 'active')) }}
      className={`flex-shrink-0 transition-opacity ${isPending ? 'opacity-60' : ''}`}
      title={optimisticActive ? 'Disable this check (all instances)' : 'Enable this check'}
      disabled={isPending}
    >
      {optimisticActive
        ? <ToggleRight className="w-6 h-6 text-green-500 hover:text-green-600" />
        : <ToggleLeft className="w-6 h-6 text-gray-300 dark:text-gray-600 hover:text-gray-400 dark:hover:text-gray-500" />}
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
    <span className="inline-flex items-center gap-1 text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 px-2.5 py-1 rounded-full border border-gray-200 dark:border-gray-600">
      <Database className="w-3 h-3 text-gray-400 dark:text-gray-400" />
      <span className="font-mono">{group.label}</span>
      <span className="text-gray-400 dark:text-gray-400">({parts.join(', ') || '0'})</span>
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
      <button onClick={onBack} className="flex items-center gap-1.5 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200">
        <ArrowLeft className="w-4 h-4" /> Back to Rule Library
      </button>

      <div className={`bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden`}>
        <div className={`px-6 py-4 flex items-center gap-2 border-b ${CATEGORY_COLORS[definition.category] ?? 'bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-200 border-gray-200 dark:border-gray-700'}`}>
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
          <p className="text-sm text-gray-600 dark:text-gray-300">{definition.description}</p>
          {definition.template_shape && (
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-2 font-mono">shape: {definition.template_shape}</p>
          )}
          {targetGroups.length > 0 && (
            <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700">
              <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">Applied to</p>
              <div className="flex flex-wrap gap-2">
                {targetGroups.map(g => <TargetGroupChip key={g.key} group={g} />)}
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
        {isLoading ? (
          <div className="p-12 text-center text-gray-400 dark:text-gray-500">Loading instances…</div>
        ) : !data || data.instances.length === 0 ? (
          <div className="p-12 text-center text-gray-400 dark:text-gray-500">No instances of this definition yet.</div>
        ) : (
          <div className="divide-y divide-gray-100 dark:divide-gray-700">
            {data.instances.map(instance => (
              <InstanceRow key={instance.id} instance={instance} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Pending Review section ────────────────────────────────────────────────────
// Surfaces every PENDING rule instance at the top of the library with inline
// Approve/Reject, so proposed rules never have to be hunted for inside each
// definition's detail view. Driven by the flat rule-view (rulesApi.list), which
// carries the instance id that /approve and /reject act on.

function PendingReviewRow({ rule, onReviewed }: { rule: Rule; onReviewed: () => void }) {
  const approveMutation = useMutation({
    mutationFn: () => rulesApi.approve(rule.id),
    onSuccess: onReviewed,
  })
  const rejectMutation = useMutation({
    mutationFn: (reason: string) => rulesApi.reject(rule.id, reason),
    onSuccess: onReviewed,
  })
  const reviewing = approveMutation.isPending || rejectMutation.isPending
  const handleReject = () => {
    const reason = window.prompt(`Reason for rejecting "${rule.name}"?`)
    if (reason && reason.trim()) rejectMutation.mutate(reason.trim())
  }

  return (
    <div className="flex items-center gap-3 px-4 py-3">
      <span className={`text-xs font-semibold px-2 py-0.5 rounded border ${SEVERITY_COLORS[rule.severity] ?? ''}`}>
        {rule.severity.toUpperCase()}
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{rule.name}</p>
          <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-300">{rule.code}</span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300">{cap(rule.category)}</span>
        </div>
        {rule.description && (
          <p className="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">{rule.description}</p>
        )}
        <p className="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5">
          Owner: <span className="text-gray-600 dark:text-gray-300">{rule.owner || '—'}</span>
          {rule.created_by ? <> · Added by: <span className="text-gray-600 dark:text-gray-300">{rule.created_by}</span></> : null}
          {rule.created_at ? <> · {fmtIST(rule.created_at)}</> : null}
          {rule.applies_to?.length ? <> · Applies to: {rule.applies_to.join(', ')}</> : null}
        </p>
      </div>
      <div className="flex items-center gap-2 flex-shrink-0">
        <button
          onClick={() => approveMutation.mutate()}
          disabled={reviewing}
          className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded-lg bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
        >
          {approveMutation.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />} Approve
        </button>
        <button
          onClick={handleReject}
          disabled={reviewing}
          className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded-lg border border-red-300 dark:border-red-500/40 text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-950/40 disabled:opacity-50"
        >
          {rejectMutation.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <XCircle className="w-3 h-3" />} Reject
        </button>
      </div>
    </div>
  )
}

function PendingReviewSection() {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['rules-pending'],
    queryFn: () => rulesApi.list({ status: 'pending' }).then(r => r.data),
    staleTime: 15_000,
  })
  // Only rules created via "Add Rule" (definition source='user') — NOT AI/workflow
  // proposals, which are reviewed in the workflow rule-review flow instead.
  const pending = (data?.rules ?? []).filter(r => r.source === 'user')
  const onReviewed = () => {
    queryClient.invalidateQueries({ queryKey: ['rules-pending'] })
    queryClient.invalidateQueries({ queryKey: ['rule-definitions'] })
    queryClient.invalidateQueries({ queryKey: ['rules-stats'] })
  }
  if (pending.length === 0) return null

  return (
    <div className="bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800/50 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-amber-200 dark:border-amber-800/50 flex items-center gap-2">
        <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400" />
        <span className="text-sm font-semibold text-amber-800 dark:text-amber-200">
          {pending.length} manually-added rule{pending.length !== 1 ? 's' : ''} awaiting review
        </span>
      </div>
      <div className="divide-y divide-amber-200/60 dark:divide-amber-800/40">
        {pending.map(rule => (
          <PendingReviewRow key={rule.id} rule={rule} onReviewed={onReviewed} />
        ))}
      </div>
    </div>
  )
}

// ── Definitions view ──────────────────────────────────────────────────────────

function DefinitionsView({ onSelect, highlightId }: { onSelect: (d: RuleDefinition) => void; highlightId?: string | null }) {
  const [categoryFilter, setCategoryFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [search, setSearch] = useState('')
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [showChatPanel, setShowChatPanel] = useState(false)
  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ['rule-definitions'],
    queryFn: () => ruleLibraryApi.listDefinitions({}).then(r => r.data),
    staleTime: 30_000,
  })

  // Scroll highlighted definition into view once data loads
  useEffect(() => {
    if (!highlightId || !data) return
    const el = document.getElementById(`def-${highlightId}`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [highlightId, data])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['rule-definitions'] })
    queryClient.invalidateQueries({ queryKey: ['rules-stats'] })
    queryClient.invalidateQueries({ queryKey: ['rules-pending'] })
  }

  // Categories actually present in the library, not the full fixed enum —
  // an unused category (e.g. no "ownership" rules exist yet) shouldn't
  // clutter the filter with an option that always returns zero results.
  const availableCategories = useMemo(() => {
    const present = new Set((data?.definitions ?? []).map(d => d.category).filter(Boolean))
    return Array.from(present).sort()
  }, [data])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (data?.definitions ?? []).filter(d => {
      if (categoryFilter && d.category !== categoryFilter) return false
      if (sourceFilter === 'ai' && d.source !== 'claude') return false
      if (sourceFilter === 'existing' && d.source === 'claude') return false
      if (q) return (
        d.name.toLowerCase().includes(q) ||
        d.description.toLowerCase().includes(q) ||
        d.category.toLowerCase().includes(q) ||
        (d.template_shape ?? '').toLowerCase().includes(q)
      )
      return true
    })
  }, [data, categoryFilter, sourceFilter, search])

  const grouped = filtered.reduce<Record<string, RuleDefinition[]>>((acc, d) => {
    const cat = d.category || 'other'
    acc[cat] = acc[cat] ? [...acc[cat], d] : [d]
    return acc
  }, {})

  const totalInstances = (data?.definitions ?? []).reduce((sum, d) => sum + d.instance_count, 0)
  const anyFilter = search || categoryFilter || sourceFilter

  return (
    <div className="space-y-6">
      {/* Title + Add Rule */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Rule Library</h1>
          <p className="mt-1 text-gray-500 dark:text-gray-300">
            Check definitions (concepts) and the specific table/column instances applying them.
          </p>
        </div>
        <button
          onClick={() => setShowChatPanel(true)}
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

      {/* Pending review — surfaced up top so proposed rules are one click to approve/reject */}
      <PendingReviewSection />

      {/* Search + Filters */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-4 space-y-3">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 dark:text-gray-400 pointer-events-none" />
          <input
            type="text"
            placeholder="Search by name, description, or template shape…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full pl-9 pr-9 py-2.5 border border-gray-200 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100 rounded-lg text-sm focus:outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
          />
          {search && (
            <button onClick={() => setSearch('')} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 dark:text-gray-400 hover:text-gray-600">
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

        <div className="flex flex-wrap gap-3">
          <select value={categoryFilter} onChange={e => setCategoryFilter(e.target.value)}
            className="px-3 py-2 border border-gray-200 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100 rounded-lg text-sm focus:outline-none focus:border-primary-500">
            <option value="">All Categories</option>
            {availableCategories.map(c => <option key={c} value={c}>{cap(c)}</option>)}
          </select>

          <select value={sourceFilter} onChange={e => setSourceFilter(e.target.value)}
            className="px-3 py-2 border border-gray-200 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100 rounded-lg text-sm focus:outline-none focus:border-primary-500">
            <option value="">All Sources</option>
            <option value="ai">AI-proposed</option>
            <option value="existing">Existing</option>
          </select>

          {anyFilter && (
            <button
              onClick={() => { setSearch(''); setCategoryFilter(''); setSourceFilter('') }}
              className="flex items-center gap-1 px-3 py-2 text-sm text-gray-500 dark:text-gray-300 hover:text-gray-700 border border-dashed border-gray-300 dark:border-gray-600 rounded-lg"
            >
              <X className="w-3.5 h-3.5" /> Clear
            </button>
          )}
        </div>
      </div>

      {/* Definitions grouped by category */}
      {isLoading ? (
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-12 text-center text-gray-400 dark:text-gray-500">Loading definitions…</div>
      ) : Object.keys(grouped).length === 0 ? (
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow p-12 text-center text-gray-400 dark:text-gray-500">No definitions match the current filters.</div>
      ) : (
        Object.entries(grouped).map(([category, definitions]) => {
          const isCollapsed = collapsed[category] ?? false
          return (
          <div key={category} className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
            <div
              onClick={() => setCollapsed(c => ({ ...c, [category]: !isCollapsed }))}
              className={`px-6 py-3 flex items-center gap-2 border-b cursor-pointer select-none ${CATEGORY_COLORS[category] ?? 'bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-200 border-gray-200 dark:border-gray-700'}`}
            >
              {isCollapsed ? <ChevronRight className="w-4 h-4 flex-shrink-0" /> : <ChevronDown className="w-4 h-4 flex-shrink-0" />}
              {CATEGORY_ICONS[category] ?? <ShieldCheck className="w-4 h-4" />}
              <span className="text-sm font-semibold">{cap(category)}</span>
              <span className="ml-auto text-xs opacity-70">{definitions.length} definition{definitions.length !== 1 ? 's' : ''}</span>
            </div>

            {!isCollapsed && (
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {definitions
                .slice()
                .sort((a, b) => b.instance_count - a.instance_count)
                .map(d => (
                  <div
                    key={d.id}
                    id={`def-${d.id}`}
                    onClick={() => onSelect(d)}
                    className={`px-6 py-4 flex items-start gap-4 hover:bg-gray-50 dark:hover:bg-gray-700/40 cursor-pointer transition-colors ${highlightId === d.id ? 'ring-2 ring-inset ring-primary-500 bg-primary-100 dark:bg-primary-900/60' : ''}`}
                  >
                    <div className="mt-0.5"><DefinitionToggle definition={d} /></div>
                    <div className="flex-1 min-w-0">
                      <div className="flex flex-wrap items-center gap-2 mb-1">
                        <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">{d.name}</span>
                        <CheckKindBadge checkKind={d.check_kind} />
                        {d.source === 'claude' && (
                          <span className="text-xs bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded font-medium flex items-center gap-1">
                            <Sparkles className="w-3 h-3" /> AI
                          </span>
                        )}
                        {d.template_shape && (
                          <span className="text-xs font-mono text-gray-400 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">{d.template_shape}</span>
                        )}
                      </div>
                      <p className="text-sm text-gray-600 dark:text-gray-300 leading-relaxed">{d.description}</p>
                      <div className="flex flex-wrap items-center gap-3 text-xs text-gray-400 dark:text-gray-400 mt-2">
                        <span className="flex items-center gap-1"><Hash className="w-3 h-3" /> {d.instance_count} instance{d.instance_count !== 1 ? 's' : ''}</span>
                        <span className="flex items-center gap-1"><GitBranch className="w-3 h-3" /> {d.approval_count} approved</span>
                      </div>
                    </div>
                    <div className="flex flex-col items-end gap-2 flex-shrink-0">
                      <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${(STATUS_STYLES[d.status] ?? STATUS_STYLES.active).pill}`}>
                        {(STATUS_STYLES[d.status] ?? STATUS_STYLES.active).label}
                      </span>
                      <ChevronRight className="w-4 h-4 text-gray-300 dark:text-gray-600" />
                    </div>
                  </div>
                ))}
            </div>
            )}
          </div>
          )
        })
      )}

      <RuleChatPanel
        isOpen={showChatPanel}
        onClose={() => setShowChatPanel(false)}
        onRuleCreated={invalidate}
      />
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function RuleLibrary() {
  const [selectedDefinition, setSelectedDefinition] = useState<RuleDefinition | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const highlightId = searchParams.get('highlight')

  // When navigated to with ?highlight=<id>, clear the param after a moment
  useEffect(() => {
    if (highlightId) {
      const t = setTimeout(() => setSearchParams({}, { replace: true }), 3000)
      return () => clearTimeout(t)
    }
  }, [highlightId, setSearchParams])

  return (
    <div className="space-y-6">
      {selectedDefinition ? (
        <InstancesView definition={selectedDefinition} onBack={() => setSelectedDefinition(null)} />
      ) : (
        <DefinitionsView onSelect={setSelectedDefinition} highlightId={highlightId} />
      )}
    </div>
  )
}

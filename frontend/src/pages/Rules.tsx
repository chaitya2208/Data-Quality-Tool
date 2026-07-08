import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { rulesApi } from '../api/client'
import type { Rule, RuleCreatePayload, GeneratedRule } from '../api/client'
import {
  ShieldCheck, FileText, Database, Tag, Filter,
  ToggleLeft, ToggleRight, Plus, Search, X, User,
  GitBranch, Clock, CheckCircle, XCircle, Ticket, ExternalLink,
  Sparkles, Loader2, RefreshCw
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'

// ── Constants ────────────────────────────────────────────────────────────────

const CATEGORIES  = ['documentation','ownership','schema','naming','data_quality','security','performance']
const ASSET_TYPES = ['table','column','schema','database']

const CATEGORY_COLORS: Record<string,string> = {
  security:     'bg-red-100 text-red-800 border-red-200',
  data_quality: 'bg-orange-100 text-orange-800 border-orange-200',
  schema:       'bg-blue-100 text-blue-800 border-blue-200',
  naming:       'bg-purple-100 text-purple-800 border-purple-200',
  documentation:'bg-yellow-100 text-yellow-800 border-yellow-200',
  ownership:    'bg-green-100 text-green-800 border-green-200',
  performance:  'bg-gray-100 text-gray-800 border-gray-200',
}

const SEVERITY_COLORS: Record<string,string> = {
  critical:'bg-red-100 text-red-800',
  high:    'bg-orange-100 text-orange-800',
  medium:  'bg-yellow-100 text-yellow-800',
  low:     'bg-blue-100 text-blue-800',
  info:    'bg-gray-100 text-gray-800',
}

const STATUS_STYLES: Record<string, { pill: string; label: string }> = {
  active:   { pill: 'bg-green-100 text-green-700',  label: 'Active'    },
  pending:  { pill: 'bg-yellow-100 text-yellow-700',label: 'Pending'   },
  disabled: { pill: 'bg-gray-100 text-gray-500',    label: 'Disabled'  },
  rejected: { pill: 'bg-red-100 text-red-700',      label: 'Rejected'  },
}

const SEVERITY_ORDER = ['critical','high','medium','low','info']

const CATEGORY_ICONS: Record<string, React.ReactNode> = {
  security:     <ShieldCheck className="w-4 h-4" />,
  documentation:<FileText    className="w-4 h-4" />,
  schema:       <Database    className="w-4 h-4" />,
  naming:       <Tag         className="w-4 h-4" />,
  ownership:    <ShieldCheck className="w-4 h-4" />,
  data_quality: <Filter      className="w-4 h-4" />,
}

function cap(s: string) {
  return s.replace(/_/g,' ').replace(/\b\w/g, c => c.toUpperCase())
}

const emptyForm = (): RuleCreatePayload => ({
  code:'', name:'', description:'',
  category:'schema', severity:'medium',
  applies_to:['table'], rule_config:{},
  is_active:false,   // starts pending/inactive
  owner:'', created_by:'', jira_ticket:'',
})

// ── Component ────────────────────────────────────────────────────────────────

export default function Rules() {
  const [categoryFilter, setCategoryFilter] = useState('')
  const [severityFilter, setSeverityFilter] = useState('')
  const [statusFilter,   setStatusFilter]   = useState('')
  const [search,         setSearch]         = useState('')
  const [showModal,      setShowModal]       = useState(false)
  const [form,           setForm]            = useState<RuleCreatePayload>(emptyForm())
  const [formError,      setFormError]       = useState('')
  // AI generation state
  const [aiPrompt,       setAiPrompt]        = useState('')
  const [aiOwner,        setAiOwner]         = useState('')
  const [generated,      setGenerated]       = useState<GeneratedRule | null>(null)
  const [aiStep,         setAiStep]          = useState<'prompt' | 'preview'>('prompt')
  const [rejectingId,    setRejectingId]     = useState<string|null>(null)
  const [rejectReason,   setRejectReason]    = useState('')
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const { data, isLoading } = useQuery({
    queryKey: ['rules-all'],
    queryFn: () => rulesApi.list({ limit: 500 } as any).then(r => r.data),
    staleTime: 30_000,
  })

  const { data: stats } = useQuery({
    queryKey: ['rules-stats'],
    queryFn: () => rulesApi.stats().then(r => r.data),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['rules-all'] })
    queryClient.invalidateQueries({ queryKey: ['rules-stats'] })
  }

  const toggleMutation  = useMutation({
    mutationFn: ({ id, is_active }: { id:string; is_active:boolean }) => rulesApi.toggle(id, is_active),
    onSuccess: invalidate,
  })
  const approveMutation = useMutation({
    mutationFn: (id: string) => rulesApi.approve(id),
    onSuccess: invalidate,
  })
  const rejectMutation  = useMutation({
    mutationFn: ({ id, reason }: { id:string; reason:string }) => rulesApi.reject(id, reason),
    onSuccess: () => { invalidate(); setRejectingId(null); setRejectReason('') },
  })
  const createMutation  = useMutation({
    mutationFn: (payload: RuleCreatePayload) => rulesApi.create(payload),
    onSuccess: () => {
      invalidate()
      setShowModal(false)
      setForm(emptyForm())
      setFormError('')
      setGenerated(null)
      setAiPrompt('')
      setAiOwner('')
      setAiStep('prompt')
    },
    onError: (err: any) => setFormError(err?.response?.data?.detail || 'Failed to create rule'),
  })

  const generateMutation = useMutation({
    mutationFn: () => rulesApi.generate(aiPrompt, aiOwner).then(r => r.data),
    onSuccess: (result) => {
      setGenerated(result)
      setAiStep('preview')
      // Pre-fill the form with generated values so user can still edit
      setForm({
        code:        result.code,
        name:        result.name,
        description: result.description,
        category:    result.category,
        severity:    result.severity,
        applies_to:  result.applies_to,
        owner:       aiOwner,
        created_by:  '',
        jira_ticket: '',
        rule_config: {},
        is_active:   false,
      })
    },
  })

  // ── Client-side filter + search ───────────────────────────────────────────
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (data?.rules ?? []).filter(r => {
      if (categoryFilter && r.category !== categoryFilter) return false
      if (severityFilter && r.severity !== severityFilter) return false
      if (statusFilter   && (r.status ?? 'active') !== statusFilter) return false
      if (q) return (
        r.name.toLowerCase().includes(q)          ||
        r.code.toLowerCase().includes(q)          ||
        r.description.toLowerCase().includes(q)   ||
        r.category.toLowerCase().includes(q)      ||
        (r.owner ?? '').toLowerCase().includes(q) ||
        (r.created_by ?? '').toLowerCase().includes(q) ||
        (r.jira_ticket ?? '').toLowerCase().includes(q)
      )
      return true
    })
  }, [data, categoryFilter, severityFilter, statusFilter, search])

  const pendingRules = filtered.filter(r => (r.status ?? 'active') === 'pending')
  const otherRules   = filtered.filter(r => (r.status ?? 'active') !== 'pending')

  const grouped = otherRules.reduce<Record<string, Rule[]>>((acc, r) => {
    const cat = r.category || 'other'
    acc[cat] = acc[cat] ? [...acc[cat], r] : [r]
    return acc
  }, {})

  // ── Helpers ───────────────────────────────────────────────────────────────
  const toggleAppliesTo = (type: string) =>
    setForm(f => ({
      ...f,
      applies_to: f.applies_to.includes(type)
        ? f.applies_to.filter(t => t !== type)
        : [...f.applies_to, type],
    }))

  const handleCreate = () => {
    if (!form.code.trim())        return setFormError('Rule code is required')
    if (!form.name.trim())        return setFormError('Rule name is required')
    if (!form.description.trim()) return setFormError('Description is required')
    if (!form.owner.trim())       return setFormError('Owner is required')
    if (form.applies_to.length === 0) return setFormError('Select at least one asset type')
    const codeClean = form.code.trim().toUpperCase().replace(/\s+/g, '_')
    createMutation.mutate({ ...form, code: codeClean })
  }

  const anyFilter = search || categoryFilter || severityFilter || statusFilter

  return (
    <div className="space-y-6">

      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Rules</h1>
          <p className="mt-1 text-gray-500">
            {data?.total ?? 0} rules total
            {stats?.pending ? (
              <span className="ml-2 inline-flex items-center gap-1 text-yellow-600 font-medium">
                <Clock className="w-3.5 h-3.5" /> {stats.pending} pending approval
              </span>
            ) : null}
          </p>
        </div>
        <button
          onClick={() => { setShowModal(true); setFormError('') }}
          className="flex items-center gap-2 px-4 py-2.5 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700 transition-colors"
        >
          <Plus className="w-4 h-4" /> Add Rule
        </button>
      </div>

      {/* Search + Filters */}
      <div className="bg-white rounded-xl shadow p-4 space-y-3">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
          <input
            type="text"
            placeholder="Search by name, code, description, owner, creator, or Jira ticket…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full pl-9 pr-9 py-2.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
          />
          {search && (
            <button onClick={() => setSearch('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
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

          <select value={severityFilter} onChange={e => setSeverityFilter(e.target.value)}
            className="px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary-500">
            <option value="">All Severities</option>
            {SEVERITY_ORDER.map(s => <option key={s} value={s}>{cap(s)}</option>)}
          </select>

          <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
            className="px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-primary-500">
            <option value="">All Statuses</option>
            <option value="active">Active</option>
            <option value="pending">Pending</option>
            <option value="disabled">Disabled</option>
            <option value="rejected">Rejected</option>
          </select>

          {anyFilter && (
            <button
              onClick={() => { setSearch(''); setCategoryFilter(''); setSeverityFilter(''); setStatusFilter('') }}
              className="flex items-center gap-1 px-3 py-2 text-sm text-gray-500 hover:text-gray-700 border border-dashed border-gray-300 rounded-lg"
            >
              <X className="w-3.5 h-3.5" /> Clear
            </button>
          )}
        </div>
      </div>

      {/* ── Pending Approval Queue ─────────────────────────────────────────── */}
      {pendingRules.length > 0 && (
        <div className="bg-yellow-50 border-2 border-yellow-200 rounded-xl overflow-hidden">
          <div className="px-6 py-3 flex items-center gap-2 bg-yellow-100 border-b border-yellow-200">
            <Clock className="w-4 h-4 text-yellow-700" />
            <span className="text-sm font-semibold text-yellow-800">
              Pending Approval ({pendingRules.length})
            </span>
            <span className="ml-auto text-xs text-yellow-600">
              Review and approve or reject these rules before they run on scans
            </span>
          </div>
          <div className="divide-y divide-yellow-100">
            {pendingRules.map(rule => (
              <div key={rule.id} className="px-6 py-4 flex items-start gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-2 mb-1">
                    <span className={`text-xs font-semibold px-2 py-0.5 rounded border ${SEVERITY_COLORS[rule.severity] ?? ''}`}>
                      {rule.severity.toUpperCase()}
                    </span>
                    <span className="text-sm font-semibold text-gray-900">{rule.name}</span>
                    <span className="text-xs font-mono text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">{rule.code}</span>
                  </div>
                  <p className="text-sm text-gray-600 mb-2">{rule.description}</p>
                  <div className="flex flex-wrap gap-3 text-xs text-gray-500">
                    {rule.applies_to.map(t => (
                      <span key={t} className="bg-gray-100 px-2 py-0.5 rounded">{t}</span>
                    ))}
                    <span className="flex items-center gap-1"><User className="w-3 h-3" /> Owner: <strong>{rule.owner}</strong></span>
                    {rule.created_by && <span className="flex items-center gap-1"><User className="w-3 h-3" /> By: {rule.created_by}</span>}
                    {rule.jira_ticket && (
                      <span className="flex items-center gap-1 text-blue-600">
                        <Ticket className="w-3 h-3" /> {rule.jira_ticket}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    onClick={() => approveMutation.mutate(rule.id)}
                    disabled={approveMutation.isPending}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 disabled:opacity-50"
                  >
                    <CheckCircle className="w-4 h-4" /> Approve
                  </button>
                  <button
                    onClick={() => { setRejectingId(rule.id); setRejectReason('') }}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-white text-red-600 text-sm font-medium border border-red-300 rounded-lg hover:bg-red-50"
                  >
                    <XCircle className="w-4 h-4" /> Reject
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Reject reason modal */}
      {rejectingId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-2xl w-full max-w-md mx-4 p-6 space-y-4">
            <h3 className="text-lg font-semibold text-gray-900">Reject Rule</h3>
            <textarea
              rows={3}
              placeholder="Reason for rejection (required)…"
              value={rejectReason}
              onChange={e => setRejectReason(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:border-red-400 resize-none"
            />
            <div className="flex gap-3 justify-end">
              <button onClick={() => setRejectingId(null)}
                className="px-4 py-2 text-sm text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50">
                Cancel
              </button>
              <button
                onClick={() => { if (rejectReason.trim()) rejectMutation.mutate({ id: rejectingId, reason: rejectReason }) }}
                disabled={!rejectReason.trim() || rejectMutation.isPending}
                className="px-4 py-2 text-sm text-white bg-red-600 rounded-lg hover:bg-red-700 disabled:opacity-50"
              >
                {rejectMutation.isPending ? 'Rejecting…' : 'Confirm Reject'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Rules grouped by category ─────────────────────────────────────── */}
      {isLoading ? (
        <div className="bg-white rounded-xl shadow p-12 text-center text-gray-400">Loading rules…</div>
      ) : Object.keys(grouped).length === 0 && pendingRules.length === 0 ? (
        <div className="bg-white rounded-xl shadow p-12 text-center text-gray-400">No rules match the current filters.</div>
      ) : (
        Object.entries(grouped).map(([category, rules]) => (
          <div key={category} className="bg-white rounded-xl shadow overflow-hidden">
            <div className={`px-6 py-3 flex items-center gap-2 border-b ${CATEGORY_COLORS[category] ?? 'bg-gray-50 text-gray-700 border-gray-200'}`}>
              {CATEGORY_ICONS[category] ?? <ShieldCheck className="w-4 h-4" />}
              <span className="text-sm font-semibold">{cap(category)}</span>
              <span className="ml-auto text-xs opacity-70">{rules.length} rule{rules.length !== 1 ? 's' : ''}</span>
            </div>

            <div className="divide-y divide-gray-100">
              {rules
                .slice()
                .sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity))
                .map(rule => {
                  const statusStyle = STATUS_STYLES[rule.status ?? 'active'] ?? STATUS_STYLES.active
                  return (
                    <div key={rule.id}
                      className={`px-6 py-4 flex items-start gap-4 transition-colors ${
                        rule.is_active ? 'hover:bg-gray-50' : 'opacity-60 bg-gray-50/40'
                      }`}
                    >
                      {/* Toggle — only for active/disabled rules */}
                      {(rule.status === 'active' || rule.status === 'disabled' || !rule.status) ? (
                        <button
                          onClick={() => toggleMutation.mutate({ id: rule.id, is_active: !rule.is_active })}
                          className="flex-shrink-0 mt-0.5"
                          title={rule.is_active ? 'Disable rule' : 'Enable rule'}
                        >
                          {rule.is_active
                            ? <ToggleRight className="w-6 h-6 text-green-500 hover:text-green-600" />
                            : <ToggleLeft  className="w-6 h-6 text-gray-300 hover:text-gray-400" />}
                        </button>
                      ) : (
                        <div className="w-6 flex-shrink-0 mt-0.5" />
                      )}

                      <div className="flex-1 min-w-0">
                        <div className="flex flex-wrap items-center gap-2 mb-1">
                          <span className={`text-xs font-semibold px-2 py-0.5 rounded border ${SEVERITY_COLORS[rule.severity] ?? ''}`}>
                            {rule.severity.toUpperCase()}
                          </span>
                          <span className="text-sm font-semibold text-gray-900">{rule.name}</span>
                          <span className="text-xs font-mono text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">{rule.code}</span>
                        </div>

                        <p className="text-sm text-gray-600 leading-relaxed mb-2">{rule.description}</p>

                        {/* Rejected reason */}
                        {rule.status === 'rejected' && rule.rejection_reason && (
                          <p className="text-xs text-red-600 bg-red-50 px-3 py-1.5 rounded mb-2">
                            ✗ Rejected: {rule.rejection_reason}
                          </p>
                        )}

                        <div className="flex flex-wrap items-center gap-3 text-xs text-gray-400">
                          {rule.applies_to.map(t => (
                            <span key={t} className="bg-gray-100 text-gray-500 px-2 py-0.5 rounded">{t}</span>
                          ))}
                          <span className="flex items-center gap-1">
                            <User className="w-3 h-3" /> {rule.owner}
                          </span>
                          {rule.created_by && (
                            <span className="flex items-center gap-1">
                              <User className="w-3 h-3 opacity-60" /> by {rule.created_by}
                            </span>
                          )}
                          {rule.jira_ticket && (
                            <span className="flex items-center gap-1 text-blue-500">
                              <Ticket className="w-3 h-3" /> {rule.jira_ticket}
                            </span>
                          )}
                          <span className="flex items-center gap-1">
                            <GitBranch className="w-3 h-3" /> v{rule.version ?? 1}
                          </span>
                        </div>
                      </div>

                      <div className="flex flex-col items-end gap-2 flex-shrink-0">
                        <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${statusStyle.pill}`}>
                          {statusStyle.label}
                        </span>
                        {rule.status === 'active' && (
                          <button
                            onClick={() => navigate(`/findings?rule_code=${encodeURIComponent(rule.code)}`)}
                            className="flex items-center gap-1 text-xs text-primary-600 hover:text-primary-800 font-medium"
                            title="View findings for this rule"
                          >
                            <ExternalLink className="w-3 h-3" /> Findings
                          </button>
                        )}
                      </div>
                    </div>
                  )
                })}
            </div>
          </div>
        ))
      )}

      {/* ── Add Rule Modal (AI-powered) ────────────────────────────────────── */}
      {showModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-xl overflow-hidden">

            {/* Header */}
            <div className="flex items-center justify-between px-6 py-4 border-b bg-gradient-to-r from-primary-50 to-purple-50">
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
              <button
                onClick={() => {
                  setShowModal(false); setForm(emptyForm()); setFormError('')
                  setGenerated(null); setAiPrompt(''); setAiOwner(''); setAiStep('prompt')
                }}
                className="text-gray-400 hover:text-gray-600 p-1 rounded"
              >
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
                        {SEVERITY_ORDER.map(s => <option key={s} value={s}>{cap(s)}</option>)}
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
            <div className="flex items-center justify-between px-6 py-4 border-t bg-gray-50">
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
                      onClick={() => { setShowModal(false); setForm(emptyForm()); setFormError(''); setGenerated(null); setAiPrompt(''); setAiOwner(''); setAiStep('prompt') }}
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
                        : <><CheckCircle className="w-4 h-4" />Submit for Approval</>
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

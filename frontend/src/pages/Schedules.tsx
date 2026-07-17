import { useState, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { fmtIST } from '../utils/dates'
import {
  schedulesApi, workflowsApi, assetsApi,
  type Schedule, type ScheduleCreatePayload, type ScheduleCadence, type WorkflowScope,
} from '../api/client'
import { useConnection } from '../ConnectionContext'
import {
  Clock, Play, Pencil, Trash2, X, Save, Loader2, Plus, Search,
  Database, AlertTriangle, CheckCircle2, PauseCircle, PlayCircle,
} from 'lucide-react'

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December']

// ── Searchable combobox (copied from SavedWorkflows) ────────────────────────────

function Combobox({
  value, onChange, options, placeholder, loading, disabled, error,
}: {
  value: string
  onChange: (v: string) => void
  options: string[]
  placeholder: string
  loading?: boolean
  disabled?: boolean
  error?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState(value)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => { setQuery(value) }, [value])
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const filtered = options.filter(o => o.toLowerCase().includes(query.toLowerCase()))
  const select = (v: string) => { onChange(v); setQuery(v); setOpen(false) }

  return (
    <div ref={ref} className="relative">
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400 pointer-events-none" />
        <input
          value={query}
          onChange={e => { setQuery(e.target.value); onChange(e.target.value); setOpen(true) }}
          onFocus={() => setOpen(true)}
          disabled={disabled}
          placeholder={loading ? 'Loading…' : error ? 'Failed to load' : placeholder}
          className={`w-full text-sm border rounded-lg pl-8 pr-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100 disabled:opacity-50 disabled:cursor-not-allowed ${
            error ? 'border-red-300' : 'border-gray-300 dark:border-gray-600'
          } focus:ring-2 focus:ring-primary-500 focus:border-transparent`}
        />
        {loading && <Loader2 className="absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 animate-spin text-gray-400" />}
      </div>
      {open && !loading && filtered.length > 0 && (
        <ul className="absolute z-50 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-48 overflow-y-auto">
          {filtered.map(o => (
            <li
              key={o}
              onMouseDown={() => select(o)}
              className={`px-3 py-2 text-sm cursor-pointer hover:bg-primary-50 dark:hover:bg-primary-900/30 ${
                o === value ? 'bg-primary-50 dark:bg-primary-900/30 font-medium text-primary-700 dark:text-primary-300' : 'text-gray-800 dark:text-gray-200'
              }`}
            >
              {o}
            </li>
          ))}
        </ul>
      )}
      {open && !loading && filtered.length === 0 && query && (
        <div className="absolute z-50 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg px-3 py-2 text-xs text-gray-400">
          No matches — press Enter to use "{query}" anyway
        </div>
      )}
    </div>
  )
}

// ── Cadence summary helper ──────────────────────────────────────────────────────

function cadenceSummary(s: Schedule | ScheduleFormState): string {
  const t = s.time_of_day || '00:00'
  switch (s.cadence) {
    case 'daily':   return `Daily at ${t}`
    case 'weekly':  return `Weekly on ${DAYS[s.day_of_week ?? 0]} at ${t}`
    case 'monthly': return `Monthly on day ${s.day_of_month ?? 1} at ${t}`
    case 'yearly':  return `Yearly on ${MONTHS[(s.month_of_year ?? 1) - 1]} ${s.day_of_month ?? 1} at ${t}`
    case 'custom':  return `Every ${s.interval_value ?? 1} ${s.interval_unit ?? 'days'}`
    default:        return s.cadence
  }
}

function scopeSummary(s: Schedule): string {
  if (s.scope === 'database') return `Database · ${s.database}`
  if (s.scope === 'schema')   return `Schema · ${s.database}.${s.schema_name}`
  return `Table · ${s.database}.${s.schema_name}.${s.table}`
}

// ── Create / Edit modal ─────────────────────────────────────────────────────────

interface ScheduleFormState {
  name: string
  scope: WorkflowScope
  database: string
  schema_name: string
  table: string
  workflow_template_id: string
  cadence: ScheduleCadence
  time_of_day: string
  day_of_week: number
  day_of_month: number
  month_of_year: number
  interval_value: number
  interval_unit: string
}

function ScheduleModal({
  existing, onClose,
}: {
  existing: Schedule | null
  onClose: () => void
}) {
  const qc = useQueryClient()
  const { selectedId } = useConnection()

  const [form, setForm] = useState<ScheduleFormState>({
    name: existing?.name ?? '',
    scope: existing?.scope ?? 'table',
    database: existing?.database ?? '',
    schema_name: existing?.schema_name ?? '',
    table: existing?.table ?? '',
    workflow_template_id: existing?.workflow_template_id ?? '',
    cadence: existing?.cadence ?? 'daily',
    time_of_day: existing?.time_of_day ?? '03:00',
    day_of_week: existing?.day_of_week ?? 0,
    day_of_month: existing?.day_of_month ?? 1,
    month_of_year: existing?.month_of_year ?? 1,
    interval_value: existing?.interval_value ?? 6,
    interval_unit: existing?.interval_unit ?? 'hours',
  })
  const set = <K extends keyof ScheduleFormState>(k: K, v: ScheduleFormState[K]) =>
    setForm(f => ({ ...f, [k]: v }))

  const { data: dbData, isFetching: dbLoading } = useQuery({
    queryKey: ['databases', selectedId],
    queryFn: () => assetsApi.discoverDatabases(selectedId).then(r => r.data),
    staleTime: 5 * 60_000,
  })
  const { data: schemaData, isFetching: schemaLoading, isError: schemaError } = useQuery({
    queryKey: ['schemas', selectedId, form.database],
    queryFn: () => assetsApi.discoverSchemas(form.database, selectedId).then(r => r.data),
    enabled: !!form.database,
    staleTime: 5 * 60_000,
    retry: false,
  })
  const { data: tableData, isFetching: tableLoading, isError: tableError } = useQuery({
    queryKey: ['tables', selectedId, form.database, form.schema_name],
    queryFn: () => assetsApi.discoverTables(form.database, form.schema_name, selectedId).then(r => r.data),
    enabled: !!form.database && !!form.schema_name && form.scope === 'table',
    staleTime: 5 * 60_000,
    retry: false,
  })
  const { data: workflows = [] } = useQuery({
    queryKey: ['workflows'],
    queryFn: () => workflowsApi.list().then(r => r.data),
  })

  const databases = dbData?.databases ?? []
  const schemas   = schemaData?.schemas ?? []
  const tables    = tableData?.tables ?? []

  const scopeValid = !!form.database && (
    form.scope === 'database' ||
    (form.scope === 'schema' && !!form.schema_name) ||
    (form.scope === 'table' && !!form.schema_name && !!form.table)
  )
  const canSave = !!form.name.trim() && scopeValid

  // Picking a saved workflow auto-fills the scope from where it was created.
  // Fields stay editable. Older workflows (no stored origin) leave scope as-is
  // so the user fills it manually. Clearing back to "AI pipeline" leaves the
  // current scope untouched.
  const onPickWorkflow = (workflowId: string) => {
    const wf = workflows.find(w => w.id === workflowId)
    setForm(f => {
      const next = { ...f, workflow_template_id: workflowId }
      if (wf && wf.origin_database) {
        next.scope = wf.origin_scope ?? 'table'
        next.database = wf.origin_database
        next.schema_name = wf.origin_schema ?? ''
        next.table = wf.origin_table ?? ''
      }
      return next
    })
  }

  const buildPayload = (): ScheduleCreatePayload => ({
    name: form.name.trim(),
    connection_id: selectedId,
    scope: form.scope,
    database: form.database,
    schema_name: form.scope !== 'database' ? form.schema_name : undefined,
    table: form.scope === 'table' ? form.table : undefined,
    workflow_template_id: form.workflow_template_id || null,
    cadence: form.cadence,
    time_of_day: form.cadence !== 'custom' ? form.time_of_day : undefined,
    day_of_week: form.cadence === 'weekly' ? form.day_of_week : undefined,
    day_of_month: (form.cadence === 'monthly' || form.cadence === 'yearly') ? form.day_of_month : undefined,
    month_of_year: form.cadence === 'yearly' ? form.month_of_year : undefined,
    interval_value: form.cadence === 'custom' ? form.interval_value : undefined,
    interval_unit: form.cadence === 'custom' ? form.interval_unit : undefined,
  })

  const saveMutation = useMutation({
    mutationFn: () => existing
      ? schedulesApi.update(existing.id, buildPayload())
      : schedulesApi.create(buildPayload()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['schedules'] })
      onClose()
    },
  })

  const labelCls = 'block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1'
  const inputCls = 'w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            {existing ? 'Edit Schedule' : 'New Schedule'}
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-3">
          <div>
            <label className={labelCls}>Name</label>
            <input value={form.name} onChange={e => set('name', e.target.value)}
              placeholder="e.g. Nightly analytics DB scan" className={inputCls} />
          </div>

          {/* Scope */}
          <div>
            <label className={labelCls}>Scope</label>
            <select value={form.scope}
              onChange={e => set('scope', e.target.value as WorkflowScope)} className={inputCls}>
              <option value="table">Single table</option>
              <option value="schema">All tables in schema</option>
              <option value="database">All tables in database</option>
            </select>
          </div>

          <div>
            <label className={labelCls}>Database</label>
            <Combobox
              value={form.database}
              onChange={v => setForm(f => ({ ...f, database: v, schema_name: '', table: '' }))}
              options={databases} placeholder="Search databases…" loading={dbLoading} />
          </div>

          {(form.scope === 'table' || form.scope === 'schema') && (
            <div>
              <label className={labelCls}>Schema</label>
              <Combobox
                value={form.schema_name}
                onChange={v => setForm(f => ({ ...f, schema_name: v, table: '' }))}
                options={schemas} placeholder={form.database ? 'Search schemas…' : 'Select a database first'}
                loading={schemaLoading} disabled={!form.database} error={schemaError} />
            </div>
          )}

          {form.scope === 'table' && (
            <div>
              <label className={labelCls}>Table</label>
              <Combobox
                value={form.table} onChange={v => set('table', v)}
                options={tables} placeholder={form.schema_name ? 'Search tables…' : 'Select a schema first'}
                loading={tableLoading} disabled={!form.schema_name} error={tableError} />
            </div>
          )}

          {/* Saved workflow (optional) */}
          <div>
            <label className={labelCls}>Saved workflow (optional)</label>
            <select value={form.workflow_template_id}
              onChange={e => onPickWorkflow(e.target.value)} className={inputCls}>
              <option value="">AI pipeline (rule intelligence)</option>
              {workflows.map(w => <option key={w.id} value={w.id}>{w.label}</option>)}
            </select>
            <p className="text-[11px] text-gray-400 mt-1">
              Leave as "AI pipeline" to generate rules each run, or pick a saved workflow to apply a fixed rule set.
              Picking a workflow auto-fills its scope (you can still change it).
            </p>
          </div>

          {/* Cadence */}
          <div>
            <label className={labelCls}>Cadence</label>
            <select value={form.cadence}
              onChange={e => set('cadence', e.target.value as ScheduleCadence)} className={inputCls}>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Monthly</option>
              <option value="yearly">Yearly</option>
              <option value="custom">Custom interval</option>
            </select>
          </div>

          {/* Cadence-specific fields */}
          {form.cadence === 'custom' ? (
            <div className="flex gap-2">
              <div className="flex-1">
                <label className={labelCls}>Every</label>
                <input type="number" min={1} value={form.interval_value}
                  onChange={e => set('interval_value', Math.max(1, parseInt(e.target.value) || 1))}
                  className={inputCls} />
              </div>
              <div className="flex-1">
                <label className={labelCls}>Unit</label>
                <select value={form.interval_unit}
                  onChange={e => set('interval_unit', e.target.value)} className={inputCls}>
                  <option value="hours">Hours</option>
                  <option value="days">Days</option>
                </select>
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className={labelCls}>Time of day</label>
                <input type="time" value={form.time_of_day}
                  onChange={e => set('time_of_day', e.target.value)} className={inputCls} />
              </div>
              {form.cadence === 'weekly' && (
                <div>
                  <label className={labelCls}>Day of week</label>
                  <select value={form.day_of_week}
                    onChange={e => set('day_of_week', parseInt(e.target.value))} className={inputCls}>
                    {DAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
                  </select>
                </div>
              )}
              {(form.cadence === 'monthly' || form.cadence === 'yearly') && (
                <div>
                  <label className={labelCls}>Day of month</label>
                  <input type="number" min={1} max={31} value={form.day_of_month}
                    onChange={e => set('day_of_month', Math.min(31, Math.max(1, parseInt(e.target.value) || 1)))}
                    className={inputCls} />
                </div>
              )}
              {form.cadence === 'yearly' && (
                <div>
                  <label className={labelCls}>Month</label>
                  <select value={form.month_of_year}
                    onChange={e => set('month_of_year', parseInt(e.target.value))} className={inputCls}>
                    {MONTHS.map((m, i) => <option key={i} value={i + 1}>{m}</option>)}
                  </select>
                </div>
              )}
            </div>
          )}

          <p className="text-xs text-primary-600 dark:text-primary-400 bg-primary-50 dark:bg-primary-900/20 rounded-lg px-3 py-2">
            {cadenceSummary(form)}
          </p>
        </div>

        {saveMutation.isError && (
          <p className="mt-3 text-xs text-red-600">
            {(saveMutation.error as any)?.response?.data?.detail || 'Save failed'}
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">Cancel</button>
          <button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending || !canSave}
            className="flex items-center gap-2 px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
          >
            {saveMutation.isPending
              ? <><Loader2 className="w-4 h-4 animate-spin" />Saving...</>
              : <><Save className="w-4 h-4" />{existing ? 'Save Changes' : 'Create Schedule'}</>}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Schedules() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [modalOpen, setModalOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<Schedule | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)

  const { data: schedules = [], isLoading } = useQuery({
    queryKey: ['schedules'],
    queryFn: () => schedulesApi.list().then(r => r.data),
    refetchInterval: 30_000,
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: ['schedules'] })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => schedulesApi.delete(id),
    onSuccess: () => { invalidate(); setDeleteConfirm(null) },
  })
  const toggleMutation = useMutation({
    mutationFn: (id: string) => schedulesApi.toggle(id),
    onSuccess: invalidate,
  })
  const runNowMutation = useMutation({
    mutationFn: (id: string) => schedulesApi.runNow(id),
    onSuccess: () => { invalidate(); navigate('/workflow') },
  })

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Schedules</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Run workflows automatically on a daily, weekly, monthly, yearly, or custom cadence
          </p>
        </div>
        <button
          onClick={() => { setEditTarget(null); setModalOpen(true) }}
          className="flex items-center gap-2 px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700"
        >
          <Plus className="w-4 h-4" />
          New Schedule
        </button>
      </div>

      <div className="flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg px-3 py-2">
        <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
        <span>
          Scheduled runs require an active Snowflake session. If the backend's SSO login has expired,
          a run will be marked failed — restart the backend to re-authenticate.
        </span>
      </div>

      {isLoading && (
        <div className="flex justify-center py-12">
          <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
        </div>
      )}

      {!isLoading && schedules.length === 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-12 text-center">
          <Clock className="w-10 h-10 text-gray-300 mx-auto mb-3" />
          <p className="text-sm font-medium text-gray-500 dark:text-gray-400">No schedules yet</p>
          <p className="text-xs text-gray-400 mt-1">
            Create a schedule to run a workflow automatically on a cadence.
          </p>
        </div>
      )}

      <div className="grid gap-4">
        {schedules.map(s => (
          <div key={s.id} className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-5">
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100 truncate">{s.name}</h3>
                  {s.enabled
                    ? <span className="text-[11px] px-2 py-0.5 rounded-full bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300">Active</span>
                    : <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400">Paused</span>}
                </div>

                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2 text-xs text-gray-500 dark:text-gray-400">
                  <span className="flex items-center gap-1"><Clock className="w-3.5 h-3.5" />{cadenceSummary(s)}</span>
                  <span className="flex items-center gap-1"><Database className="w-3.5 h-3.5" />{scopeSummary(s)}</span>
                  {s.workflow_template_id && <span className="text-primary-500">Saved workflow</span>}
                </div>

                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2 text-xs">
                  {s.next_run_at && (
                    <span className="text-gray-500 dark:text-gray-400">
                      Next: {fmtIST(s.next_run_at)}
                    </span>
                  )}
                  {s.last_run_at && (
                    <span className="flex items-center gap-1 text-gray-400">
                      {s.last_status === 'error'
                        ? <AlertTriangle className="w-3.5 h-3.5 text-red-500" />
                        : <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />}
                      Last: {fmtIST(s.last_run_at)}
                    </span>
                  )}
                </div>

                {s.last_status === 'error' && s.last_error && (
                  <p className="mt-2 text-xs text-red-600 dark:text-red-400 truncate">{s.last_error}</p>
                )}
              </div>

              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  onClick={() => runNowMutation.mutate(s.id)}
                  disabled={runNowMutation.isPending}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
                >
                  <Play className="w-3.5 h-3.5" />Run now
                </button>
                <button
                  onClick={() => toggleMutation.mutate(s.id)}
                  className="p-1.5 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
                  title={s.enabled ? 'Pause schedule' : 'Resume schedule'}
                >
                  {s.enabled ? <PauseCircle className="w-4 h-4" /> : <PlayCircle className="w-4 h-4" />}
                </button>
                <button
                  onClick={() => { setEditTarget(s); setModalOpen(true) }}
                  className="p-1.5 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
                  title="Edit schedule"
                >
                  <Pencil className="w-4 h-4" />
                </button>
                <button
                  onClick={() => setDeleteConfirm(s.id)}
                  className="p-1.5 text-gray-400 hover:text-red-500 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
                  title="Delete schedule"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>

            {deleteConfirm === s.id && (
              <div className="mt-3 flex items-center gap-3 p-3 bg-red-50 dark:bg-red-950/30 rounded-lg border border-red-200 dark:border-red-800">
                <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                <p className="text-xs text-red-700 dark:text-red-400 flex-1">Delete "{s.name}"? This cannot be undone.</p>
                <button onClick={() => setDeleteConfirm(null)} className="text-xs text-gray-500 hover:text-gray-700 px-2">Cancel</button>
                <button
                  onClick={() => deleteMutation.mutate(s.id)}
                  disabled={deleteMutation.isPending}
                  className="text-xs px-3 py-1 bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
                >
                  {deleteMutation.isPending ? 'Deleting...' : 'Delete'}
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      {modalOpen && (
        <ScheduleModal
          existing={editTarget}
          onClose={() => { setModalOpen(false); setEditTarget(null) }}
        />
      )}
    </div>
  )
}

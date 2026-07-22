import { useState, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { workflowsApi, agentRunsApi, assetsApi, ruleLibraryApi, type WorkflowTemplate, type RulePattern } from '../api/client'
import { ScheduleModal } from './Schedules'
import { useConnection } from '../ConnectionContext'
import {
  BookOpen, Play, Pencil, Trash2, X, Save,
  ChevronDown, ChevronRight, Database, AlertTriangle, Loader2,
  Plus, Search, Clock,
} from 'lucide-react'

// ── Searchable combobox ───────────────────────────────────────────────────────

function Combobox({
  value,
  onChange,
  options,
  placeholder,
  loading,
  disabled,
  error,
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

  // Sync internal query when value changes externally (e.g. reset)
  useEffect(() => { setQuery(value) }, [value])

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const filtered = options.filter(o => o.toLowerCase().includes(query.toLowerCase()))

  const select = (v: string) => {
    onChange(v)
    setQuery(v)
    setOpen(false)
  }

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

// ── Run modal ─────────────────────────────────────────────────────────────────

function RunModal({
  workflow,
  onClose,
}: {
  workflow: WorkflowTemplate
  onClose: () => void
}) {
  const navigate = useNavigate()
  const { selectedId } = useConnection()

  const hasOrigin = !!(workflow.origin_database && workflow.origin_schema && workflow.origin_table)
  // Default to re-running on the exact origin — that's the common case.
  // "different" lets the user pick another target for the rare cross-table apply.
  const [mode, setMode] = useState<'origin' | 'different'>(hasOrigin ? 'origin' : 'different')
  const [scope, setScope] = useState<'table' | 'schema' | 'database'>('table')
  const [database, setDatabase] = useState('')
  const [schemaName, setSchemaName] = useState('')
  const [table, setTable] = useState('')

  // Effective target the run will use.
  const effDatabase   = mode === 'origin' ? (workflow.origin_database  ?? '') : database
  const effSchemaName = mode === 'origin' ? (workflow.origin_schema    ?? '') : schemaName
  const effTable      = mode === 'origin' ? (workflow.origin_table     ?? '') : table
  const effScope: 'table' | 'schema' | 'database' = mode === 'origin' ? 'table' : scope

  const { data: dbData, isFetching: dbLoading } = useQuery({
    queryKey: ['databases', selectedId],
    queryFn: () => assetsApi.discoverDatabases(selectedId).then(r => r.data),
    enabled: mode === 'different',
    staleTime: 5 * 60_000,
  })
  const { data: schemaData, isFetching: schemaLoading, isError: schemaError } = useQuery({
    queryKey: ['schemas', selectedId, database],
    queryFn: () => assetsApi.discoverSchemas(database, selectedId).then(r => r.data),
    enabled: mode === 'different' && !!database,
    staleTime: 5 * 60_000,
    retry: false,
  })
  const { data: tableData, isFetching: tableLoading, isError: tableError } = useQuery({
    queryKey: ['tables', selectedId, database, schemaName],
    queryFn: () => assetsApi.discoverTables(database, schemaName, selectedId).then(r => r.data),
    enabled: mode === 'different' && !!database && !!schemaName && scope === 'table',
    staleTime: 5 * 60_000,
    retry: false,
  })

  const databases = dbData?.databases ?? []
  const schemas   = schemaData?.schemas ?? []
  const tables    = tableData?.tables ?? []

  const canRun = mode === 'origin'
    ? hasOrigin
    : !!database && (
        scope === 'database' ||
        (scope === 'schema' && !!schemaName) ||
        (scope === 'table' && !!schemaName && !!table)
      )

  const runMutation = useMutation({
    mutationFn: () =>
      agentRunsApi.startBatch({
        scope: effScope,
        database: effDatabase,
        schema_name: effSchemaName || undefined,
        table: effTable || undefined,
        connection_id: selectedId,
        workflow_template_id: workflow.id,
      }),
    onSuccess: (res) => {
      // Pass the freshly-created run id so AgentWorkflow lands on the live
      // run instead of appearing idle (its two active-run discovery paths
      // are ?run_id=… and the dq_active_run_id localStorage entry — writing
      // the URL param is enough here).
      const firstRunId = res?.data?.runs?.[0]?.id
      navigate(firstRunId ? `/workflow?run_id=${firstRunId}` : '/workflow')
      onClose()
    },
  })

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-md p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            Run "{workflow.label}"
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X className="w-4 h-4" />
          </button>
        </div>

        <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
          {workflow.pattern_count} rule pattern{workflow.pattern_count !== 1 ? 's' : ''} will be applied.
          Column-scoped rules are skipped if the column doesn't exist on the target table.
        </p>

        {hasOrigin && (
          <div className="mb-4 space-y-2">
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">Target</label>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setMode('origin')}
                className={`text-left px-3 py-2 rounded-lg border text-xs ${
                  mode === 'origin'
                    ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/30 text-primary-800 dark:text-primary-200'
                    : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50'
                }`}
              >
                <div className="font-medium">Re-run on origin table</div>
                <div className="mt-0.5 font-mono text-[11px] break-all">
                  {workflow.origin_database}.{workflow.origin_schema}.{workflow.origin_table}
                </div>
              </button>
              <button
                type="button"
                onClick={() => setMode('different')}
                className={`text-left px-3 py-2 rounded-lg border text-xs ${
                  mode === 'different'
                    ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/30 text-primary-800 dark:text-primary-200'
                    : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50'
                }`}
              >
                <div className="font-medium">Different target</div>
                <div className="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
                  Pick another table, schema, or database.
                </div>
              </button>
            </div>
          </div>
        )}

        {mode === 'different' && (
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Scope</label>
              <select
                value={scope}
                onChange={e => setScope(e.target.value as any)}
                className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100"
              >
                <option value="table">Single table</option>
                <option value="schema">All tables in schema</option>
                <option value="database">All tables in database</option>
              </select>
            </div>

            <div>
              <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Database</label>
              <Combobox
                value={database}
                onChange={v => { setDatabase(v); setSchemaName(''); setTable('') }}
                options={databases}
                placeholder="Search databases…"
                loading={dbLoading}
              />
            </div>

            {(scope === 'table' || scope === 'schema') && (
              <div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Schema</label>
                <Combobox
                  value={schemaName}
                  onChange={v => { setSchemaName(v); setTable('') }}
                  options={schemas}
                  placeholder={database ? 'Search schemas…' : 'Select a database first'}
                  loading={schemaLoading}
                  disabled={!database}
                  error={schemaError}
                />
              </div>
            )}

            {scope === 'table' && (
              <div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Table</label>
                <Combobox
                  value={table}
                  onChange={setTable}
                  options={tables}
                  placeholder={schemaName ? 'Search tables…' : 'Select a schema first'}
                  loading={tableLoading}
                  disabled={!schemaName}
                  error={tableError}
                />
              </div>
            )}
          </div>
        )}

        {runMutation.isError && (
          <p className="mt-3 text-xs text-red-600">
            {(runMutation.error as any)?.response?.data?.detail || 'Failed to start run'}
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">
            Cancel
          </button>
          <button
            onClick={() => runMutation.mutate()}
            disabled={runMutation.isPending || !canRun}
            className="flex items-center gap-2 px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
          >
            {runMutation.isPending
              ? <><Loader2 className="w-4 h-4 animate-spin" />Starting...</>
              : <><Play className="w-4 h-4" />Run Workflow</>
            }
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Create modal ──────────────────────────────────────────────────────────────

function CreateModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [label, setLabel] = useState('')
  const [description, setDescription] = useState('')
  const [patterns, setPatterns] = useState<RulePattern[]>([])
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [addSearch, setAddSearch] = useState('')

  const { data: defsData, isFetching: defsLoading } = useQuery({
    queryKey: ['rule-definitions-active'],
    queryFn: () => ruleLibraryApi.listDefinitions({ status: 'active' }).then(r => r.data),
    staleTime: 60_000,
    enabled: addOpen,
  })
  const allDefs = defsData?.definitions ?? []
  const filteredDefs = allDefs.filter(d =>
    d.name.toLowerCase().includes(addSearch.toLowerCase()) ||
    d.category.toLowerCase().includes(addSearch.toLowerCase())
  )
  const alreadyAdded = new Set(patterns.map(p => p.definition_id))

  const addPattern = (def: typeof allDefs[number]) => {
    if (alreadyAdded.has(def.id)) return
    setPatterns(p => [...p, {
      definition_id: def.id,
      definition_name: def.name,
      scope: def.allowed_scopes?.[0] ?? 'table',
      target_config: {},
      threshold_config: {},
      severity: def.default_severity,
      template_shape: def.template_shape,
      rationale: def.description,
    }])
  }

  const removePattern = (idx: number) => {
    setPatterns(p => p.filter((_, i) => i !== idx))
    if (expandedIdx === idx) setExpandedIdx(null)
  }

  const createMutation = useMutation({
    mutationFn: () => workflowsApi.create({ label, description, rule_patterns: patterns }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workflows'] })
      onClose()
    },
  })

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">Create Workflow</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-3 mb-5">
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Label</label>
            <input
              value={label}
              onChange={e => setLabel(e.target.value)}
              placeholder="e.g. PII Audit Workflow"
              className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Description</label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              rows={2}
              placeholder="Optional description…"
              className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100"
            />
          </div>
        </div>

        <div className="mb-4">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">
              Rule Patterns ({patterns.length})
            </h4>
            <button
              onClick={() => setAddOpen(o => !o)}
              className="flex items-center gap-1 text-xs px-2.5 py-1 bg-primary-50 dark:bg-primary-900/30 text-primary-700 dark:text-primary-300 border border-primary-200 dark:border-primary-700 rounded-lg hover:bg-primary-100 dark:hover:bg-primary-900/50"
            >
              <Plus className="w-3.5 h-3.5" />
              Add Rule
            </button>
          </div>

          {addOpen && (
            <div className="mb-3 border border-primary-200 dark:border-primary-700 rounded-lg overflow-hidden">
              <div className="px-3 py-2 bg-primary-50 dark:bg-primary-900/20 border-b border-primary-200 dark:border-primary-700 flex items-center gap-2">
                <Search className="w-3.5 h-3.5 text-primary-500 flex-shrink-0" />
                <input
                  autoFocus
                  value={addSearch}
                  onChange={e => setAddSearch(e.target.value)}
                  placeholder="Search rule definitions…"
                  className="flex-1 text-sm bg-transparent outline-none text-gray-800 dark:text-gray-200 placeholder-gray-400"
                />
                {defsLoading && <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-400" />}
              </div>
              <div className="max-h-52 overflow-y-auto">
                {filteredDefs.length === 0 && !defsLoading && (
                  <p className="text-xs text-gray-400 text-center py-4">No matching rules</p>
                )}
                {filteredDefs.map(def => {
                  const added = alreadyAdded.has(def.id)
                  return (
                    <div
                      key={def.id}
                      onClick={() => !added && addPattern(def)}
                      className={`flex items-center gap-3 px-3 py-2.5 border-b border-gray-100 dark:border-gray-700 last:border-0 ${
                        added ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50'
                      }`}
                    >
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{def.name}</p>
                        <p className="text-xs text-gray-400 truncate">{def.category} · {def.description.slice(0, 80)}{def.description.length > 80 ? '…' : ''}</p>
                      </div>
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium flex-shrink-0 ${severityClass(def.default_severity)}`}>
                        {def.default_severity}
                      </span>
                      {added
                        ? <span className="text-xs text-gray-400 flex-shrink-0">added</span>
                        : <Plus className="w-3.5 h-3.5 text-primary-500 flex-shrink-0" />
                      }
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {patterns.length === 0 && !addOpen && (
            <p className="text-xs text-gray-400 py-4 text-center">
              No patterns yet — click "Add Rule" to build your workflow.
            </p>
          )}

          <div className="space-y-2">
            {patterns.map((p, idx) => (
              <div key={idx} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                <div
                  className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50"
                  onClick={() => setExpandedIdx(expandedIdx === idx ? null : idx)}
                >
                  {expandedIdx === idx
                    ? <ChevronDown className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
                    : <ChevronRight className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
                  }
                  <span className="text-sm font-medium text-gray-800 dark:text-gray-200 flex-1 truncate">
                    {p.definition_name}
                  </span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${severityClass(p.severity)}`}>
                    {p.severity}
                  </span>
                  <span className="text-xs text-gray-400">{p.scope}</span>
                  <button
                    onClick={e => { e.stopPropagation(); removePattern(idx) }}
                    className="ml-1 text-gray-400 hover:text-red-500 flex-shrink-0"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
                {expandedIdx === idx && (
                  <div className="px-4 py-3 bg-gray-50 dark:bg-gray-900/40 border-t border-gray-100 dark:border-gray-700 text-xs text-gray-600 dark:text-gray-400 space-y-1">
                    {p.template_shape && <div><span className="font-medium">Shape:</span> {p.template_shape}</div>}
                    {p.target_config && Object.keys(p.target_config).length > 0 && (
                      <div><span className="font-medium">Target:</span> {JSON.stringify(p.target_config)}</div>
                    )}
                    {p.threshold_config && Object.keys(p.threshold_config).length > 0 && (
                      <div><span className="font-medium">Threshold:</span> {JSON.stringify(p.threshold_config)}</div>
                    )}
                    {p.rationale && <div><span className="font-medium">Rationale:</span> {p.rationale}</div>}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {createMutation.isError && (
          <p className="mb-3 text-xs text-red-600">
            {(createMutation.error as any)?.response?.data?.detail || 'Create failed'}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">Cancel</button>
          <button
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending || !label.trim()}
            className="flex items-center gap-2 px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
          >
            {createMutation.isPending
              ? <><Loader2 className="w-4 h-4 animate-spin" />Creating...</>
              : <><Save className="w-4 h-4" />Create Workflow</>
            }
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Edit modal ────────────────────────────────────────────────────────────────

function EditModal({
  workflow,
  onClose,
}: {
  workflow: WorkflowTemplate
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [label, setLabel] = useState(workflow.label)
  const [description, setDescription] = useState(workflow.description || '')
  const [patterns, setPatterns] = useState<RulePattern[]>(workflow.rule_patterns)
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [addSearch, setAddSearch] = useState('')

  const { data: defsData, isFetching: defsLoading } = useQuery({
    queryKey: ['rule-definitions-active'],
    queryFn: () => ruleLibraryApi.listDefinitions({ status: 'active' }).then(r => r.data),
    staleTime: 60_000,
    enabled: addOpen,
  })
  const allDefs = defsData?.definitions ?? []
  const filteredDefs = allDefs.filter(d =>
    d.name.toLowerCase().includes(addSearch.toLowerCase()) ||
    d.category.toLowerCase().includes(addSearch.toLowerCase())
  )

  const alreadyAdded = new Set(patterns.map(p => p.definition_id))

  const addPattern = (def: typeof allDefs[number]) => {
    if (alreadyAdded.has(def.id)) return
    setPatterns(p => [...p, {
      definition_id: def.id,
      definition_name: def.name,
      scope: def.allowed_scopes?.[0] ?? 'table',
      target_config: {},
      threshold_config: {},
      severity: def.default_severity,
      template_shape: def.template_shape,
      rationale: def.description,
    }])
  }

  const removePattern = (idx: number) => {
    setPatterns(p => p.filter((_, i) => i !== idx))
    if (expandedIdx === idx) setExpandedIdx(null)
  }

  const saveMutation = useMutation({
    mutationFn: () =>
      workflowsApi.update(workflow.id, { label, description, rule_patterns: patterns }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workflows'] })
      onClose()
    },
  })

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">Edit Workflow</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-3 mb-5">
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Label</label>
            <input
              value={label}
              onChange={e => setLabel(e.target.value)}
              className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Description</label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              rows={2}
              className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 dark:text-gray-100"
            />
          </div>
        </div>

        {/* Current patterns */}
        <div className="mb-4">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">
              Rule Patterns ({patterns.length})
            </h4>
            <button
              onClick={() => setAddOpen(o => !o)}
              className="flex items-center gap-1 text-xs px-2.5 py-1 bg-primary-50 dark:bg-primary-900/30 text-primary-700 dark:text-primary-300 border border-primary-200 dark:border-primary-700 rounded-lg hover:bg-primary-100 dark:hover:bg-primary-900/50"
            >
              <Plus className="w-3.5 h-3.5" />
              Add Rule
            </button>
          </div>

          {/* Add rule picker */}
          {addOpen && (
            <div className="mb-3 border border-primary-200 dark:border-primary-700 rounded-lg overflow-hidden">
              <div className="px-3 py-2 bg-primary-50 dark:bg-primary-900/20 border-b border-primary-200 dark:border-primary-700 flex items-center gap-2">
                <Search className="w-3.5 h-3.5 text-primary-500 flex-shrink-0" />
                <input
                  autoFocus
                  value={addSearch}
                  onChange={e => setAddSearch(e.target.value)}
                  placeholder="Search rule definitions…"
                  className="flex-1 text-sm bg-transparent outline-none text-gray-800 dark:text-gray-200 placeholder-gray-400"
                />
                {defsLoading && <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-400" />}
              </div>
              <div className="max-h-52 overflow-y-auto">
                {filteredDefs.length === 0 && !defsLoading && (
                  <p className="text-xs text-gray-400 text-center py-4">No matching rules</p>
                )}
                {filteredDefs.map(def => {
                  const added = alreadyAdded.has(def.id)
                  return (
                    <div
                      key={def.id}
                      onClick={() => !added && addPattern(def)}
                      className={`flex items-center gap-3 px-3 py-2.5 border-b border-gray-100 dark:border-gray-700 last:border-0 ${
                        added
                          ? 'opacity-40 cursor-not-allowed'
                          : 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50'
                      }`}
                    >
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{def.name}</p>
                        <p className="text-xs text-gray-400 truncate">{def.category} · {def.description.slice(0, 80)}{def.description.length > 80 ? '…' : ''}</p>
                      </div>
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium flex-shrink-0 ${severityClass(def.default_severity)}`}>
                        {def.default_severity}
                      </span>
                      {added
                        ? <span className="text-xs text-gray-400 flex-shrink-0">added</span>
                        : <Plus className="w-3.5 h-3.5 text-primary-500 flex-shrink-0" />
                      }
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {patterns.length === 0 && !addOpen && (
            <p className="text-xs text-gray-400 py-4 text-center">
              No patterns — click "Add Rule" to build your workflow.
            </p>
          )}

          <div className="space-y-2">
            {patterns.map((p, idx) => (
              <div key={idx} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                <div
                  className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50"
                  onClick={() => setExpandedIdx(expandedIdx === idx ? null : idx)}
                >
                  {expandedIdx === idx
                    ? <ChevronDown className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
                    : <ChevronRight className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
                  }
                  <span className="text-sm font-medium text-gray-800 dark:text-gray-200 flex-1 truncate">
                    {p.definition_name}
                  </span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${severityClass(p.severity)}`}>
                    {p.severity}
                  </span>
                  <span className="text-xs text-gray-400">{p.scope}</span>
                  {p.target_config?.column && (
                    <span className="text-xs text-gray-500 font-mono">{p.target_config.column}</span>
                  )}
                  <button
                    onClick={e => { e.stopPropagation(); removePattern(idx) }}
                    className="ml-1 text-gray-400 hover:text-red-500 flex-shrink-0"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
                {expandedIdx === idx && (
                  <div className="px-4 py-3 bg-gray-50 dark:bg-gray-900/40 border-t border-gray-100 dark:border-gray-700 text-xs text-gray-600 dark:text-gray-400 space-y-1">
                    {p.template_shape && <div><span className="font-medium">Shape:</span> {p.template_shape}</div>}
                    {p.target_config && Object.keys(p.target_config).length > 0 && (
                      <div><span className="font-medium">Target:</span> {JSON.stringify(p.target_config)}</div>
                    )}
                    {p.threshold_config && Object.keys(p.threshold_config).length > 0 && (
                      <div><span className="font-medium">Threshold:</span> {JSON.stringify(p.threshold_config)}</div>
                    )}
                    {p.rationale && <div><span className="font-medium">Rationale:</span> {p.rationale}</div>}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {saveMutation.isError && (
          <p className="mb-3 text-xs text-red-600">
            {(saveMutation.error as any)?.response?.data?.detail || 'Save failed'}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">Cancel</button>
          <button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending || !label.trim()}
            className="flex items-center gap-2 px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
          >
            {saveMutation.isPending
              ? <><Loader2 className="w-4 h-4 animate-spin" />Saving...</>
              : <><Save className="w-4 h-4" />Save Changes</>
            }
          </button>
        </div>
      </div>
    </div>
  )
}

function severityClass(s: string) {
  switch (s) {
    case 'critical': return 'bg-red-100 text-red-700'
    case 'high':     return 'bg-orange-100 text-orange-700'
    case 'medium':   return 'bg-yellow-100 text-yellow-700'
    default:         return 'bg-gray-100 text-gray-600'
  }
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function SavedWorkflows() {
  const qc = useQueryClient()
  const [runTarget, setRunTarget] = useState<WorkflowTemplate | null>(null)
  const [editTarget, setEditTarget] = useState<WorkflowTemplate | null>(null)
  const [scheduleTarget, setScheduleTarget] = useState<WorkflowTemplate | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)

  const { data: workflows = [], isLoading } = useQuery({
    queryKey: ['workflows'],
    queryFn: () => workflowsApi.list().then(r => r.data),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => workflowsApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workflows'] })
      setDeleteConfirm(null)
    },
  })

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Saved Workflows</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Reusable sets of approved rules you can run on any table or schema
          </p>
        </div>
        <button
          onClick={() => setCreateOpen(true)}
          className="flex items-center gap-2 px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
        >
          <Plus className="w-4 h-4" />
          Create Workflow
        </button>
      </div>

      {isLoading && (
        <div className="flex justify-center py-12">
          <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
        </div>
      )}

      {!isLoading && workflows.length === 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-12 text-center">
          <BookOpen className="w-10 h-10 text-gray-300 mx-auto mb-3" />
          <p className="text-sm font-medium text-gray-500 dark:text-gray-400">No saved workflows yet</p>
          <p className="text-xs text-gray-400 mt-1">
            After reviewing and approving rules in the Workflow page, click "Save as Workflow" to create one.
          </p>
        </div>
      )}

      <div className="grid gap-4">
        {workflows.map(wf => (
          <div
            key={wf.id}
            className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-5"
          >
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1 min-w-0">
                <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100 truncate">
                  {wf.label}
                </h3>
                {wf.description && (
                  <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">
                    {wf.description}
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-xs text-gray-400">
                  <span className="flex items-center gap-1">
                    <Database className="w-3.5 h-3.5" />
                    {wf.pattern_count} rule pattern{wf.pattern_count !== 1 ? 's' : ''}
                  </span>
                  {wf.origin_database && (
                    <span
                      className="font-mono text-gray-500 dark:text-gray-300 break-all"
                      title="Table this workflow was created from"
                    >
                      {wf.origin_database}
                      {wf.origin_schema ? `.${wf.origin_schema}` : ''}
                      {wf.origin_table ? `.${wf.origin_table}` : ''}
                    </span>
                  )}
                  <span>Saved {new Date(wf.created_at).toLocaleDateString()}</span>
                  {wf.created_by && <span>by {wf.created_by}</span>}
                </div>

                {/* Pattern preview pills */}
                <div className="flex flex-wrap gap-1.5 mt-3">
                  {wf.rule_patterns.slice(0, 6).map((p, i) => (
                    <span
                      key={i}
                      className="inline-flex items-center gap-1 text-xs bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 rounded-full px-2 py-0.5"
                    >
                      <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                        p.severity === 'critical' ? 'bg-red-500' :
                        p.severity === 'high'     ? 'bg-orange-500' :
                        p.severity === 'medium'   ? 'bg-yellow-500' : 'bg-gray-400'
                      }`} />
                      {p.definition_name}
                      {p.target_config?.column && (
                        <span className="text-gray-400 font-mono">· {p.target_config.column}</span>
                      )}
                    </span>
                  ))}
                  {wf.rule_patterns.length > 6 && (
                    <span className="text-xs text-gray-400 px-1 py-0.5">
                      +{wf.rule_patterns.length - 6} more
                    </span>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  onClick={() => setRunTarget(wf)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                >
                  <Play className="w-3.5 h-3.5" />
                  Run
                </button>
                <button
                  onClick={() => setScheduleTarget(wf)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-primary-700 dark:text-primary-300 bg-primary-50 dark:bg-primary-900/30 border border-primary-200 dark:border-primary-800 rounded-lg hover:bg-primary-100 dark:hover:bg-primary-900/50 transition-colors"
                  title="Create a schedule that runs this workflow on a cadence"
                >
                  <Clock className="w-3.5 h-3.5" />
                  Schedule
                </button>
                <button
                  onClick={() => setEditTarget(wf)}
                  className="p-1.5 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
                  title="Edit workflow"
                >
                  <Pencil className="w-4 h-4" />
                </button>
                <button
                  onClick={() => setDeleteConfirm(wf.id)}
                  className="p-1.5 text-gray-400 hover:text-red-500 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
                  title="Delete workflow"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>

            {/* Delete confirm inline */}
            {deleteConfirm === wf.id && (
              <div className="mt-3 flex items-center gap-3 p-3 bg-red-50 dark:bg-red-950/30 rounded-lg border border-red-200 dark:border-red-800">
                <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                <p className="text-xs text-red-700 dark:text-red-400 flex-1">
                  Delete "{wf.label}"? This cannot be undone.
                </p>
                <button
                  onClick={() => setDeleteConfirm(null)}
                  className="text-xs text-gray-500 hover:text-gray-700 px-2"
                >
                  Cancel
                </button>
                <button
                  onClick={() => deleteMutation.mutate(wf.id)}
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

      {runTarget && <RunModal workflow={runTarget} onClose={() => setRunTarget(null)} />}
      {editTarget && <EditModal workflow={editTarget} onClose={() => setEditTarget(null)} />}
      {createOpen && <CreateModal onClose={() => setCreateOpen(false)} />}
      {scheduleTarget && (
        <ScheduleModal
          existing={null}
          prefillWorkflowId={scheduleTarget.id}
          onClose={() => setScheduleTarget(null)}
        />
      )}
    </div>
  )
}

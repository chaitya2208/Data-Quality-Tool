import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { findingsApi, assetsApi, rulesApi } from '../api/client'
import { AlertCircle, Filter, X, Database, Sparkles, ShieldCheck } from 'lucide-react'

export default function Findings() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate    = useNavigate()
  const queryClient = useQueryClient()

  // ── Filters (initialised from URL params) ──────────────────────────────────
  const [severityFilter, setSeverityFilter] = useState(searchParams.get('severity') || '')
  const [statusFilter,   setStatusFilter]   = useState(searchParams.get('status')   || '')
  const [tableFilter,    setTableFilter]    = useState(searchParams.get('table')    || '')
  const [ruleFilter,     setRuleFilter]     = useState(searchParams.get('rule_code')|| '')
  // scan_id filter: pre-applied when navigating from Workflow "Fix Issues"
  const [scanIdFilter,   setScanIdFilter]   = useState(searchParams.get('scan_id')  || '')

  // URL-param helpers (table_name + database from Dashboard drill-down)
  const urlTableName = searchParams.get('table_name') || ''
  const urlDatabase  = searchParams.get('database')   || ''

  const [selectedFindings, setSelectedFindings] = useState<string[]>([])

  // Sync URL params → filter state once on mount
  useEffect(() => {
    if (urlTableName) setTableFilter('__table_name__' + urlTableName + '__db__' + urlDatabase)
    const urlRule   = searchParams.get('rule_code')
    const urlScanId = searchParams.get('scan_id')
    if (urlRule)   setRuleFilter(urlRule)
    if (urlScanId) setScanIdFilter(urlScanId)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Data fetching ──────────────────────────────────────────────────────────
  const { data: allFindings, isLoading } = useQuery({
    queryKey: ['findings', severityFilter, statusFilter, scanIdFilter],
    queryFn: () => findingsApi.list({
      severity: severityFilter || undefined,
      status:   statusFilter   || undefined,
      scan_id:  scanIdFilter   || undefined,
      limit:    5000,
    }).then(r => r.data),
  })

  const { data: assetsData } = useQuery({
    queryKey: ['assets', 'tables'],
    queryFn: () => assetsApi.list({ asset_type: 'table' }).then(r => r.data),
  })

  const { data: rulesData } = useQuery({
    queryKey: ['rules-all'],
    queryFn: () => rulesApi.list({ is_active: true, limit: 500 } as any).then(r => r.data),
    staleTime: 60_000,
  })

  // ── Unique tables from findings ────────────────────────────────────────────
  const uniqueTables = useMemo(() => {
    if (!allFindings?.findings) return []
    const map = new Map<string, { fqn: string; table_name: string; database_name: string; schema_name: string }>()
    allFindings.findings.forEach(f => {
      if (f.context?.table_name) {
        const key = `${f.context.database_name}.${f.context.schema_name}.${f.context.table_name}`
        if (!map.has(key)) map.set(key, {
          fqn: key,
          table_name:    f.context.table_name,
          database_name: f.context.database_name,
          schema_name:   f.context.schema_name,
        })
      }
    })
    return Array.from(map.values()).sort((a, b) => a.table_name.localeCompare(b.table_name))
  }, [allFindings])

  // ── Unique rule codes that appear in findings ──────────────────────────────
  const uniqueRuleCodes = useMemo(() => {
    if (!allFindings?.findings) return []
    const seen = new Set<string>()
    allFindings.findings.forEach(f => {
      const code = f.context?.rule_code
      if (code) seen.add(code)
    })
    return Array.from(seen).sort()
  }, [allFindings])

  // ── Client-side filtering ──────────────────────────────────────────────────
  const data = useMemo(() => {
    if (!allFindings) return allFindings

    const filtered = allFindings.findings.filter(f => {
      // Table filter — two modes:
      // 1. FQN mode (from findings filter dropdown)
      // 2. table_name+database mode (from Dashboard drill-down)
      if (tableFilter) {
        if (tableFilter.startsWith('__table_name__')) {
          const parts   = tableFilter.split('__db__')
          const tName   = parts[0].replace('__table_name__', '')
          const dbName  = parts[1] || ''
          if (f.context?.table_name !== tName) return false
          if (dbName && f.context?.database_name !== dbName) return false
        } else {
          const fqn = `${f.context?.database_name}.${f.context?.schema_name}.${f.context?.table_name}`
          if (fqn !== tableFilter) return false
        }
      }
      // Rule filter
      if (ruleFilter && f.context?.rule_code !== ruleFilter) return false
      return true
    })

    return { total: allFindings.total, findings: filtered }
  }, [allFindings, tableFilter, ruleFilter])

  // ── Derived display label for active table filter ──────────────────────────
  const activeTableLabel = useMemo(() => {
    if (!tableFilter) return ''
    if (tableFilter.startsWith('__table_name__')) {
      const parts = tableFilter.split('__db__')
      return parts[0].replace('__table_name__', '')
    }
    return uniqueTables.find(t => t.fqn === tableFilter)?.table_name || tableFilter
  }, [tableFilter, uniqueTables])

  // ── Mutations ──────────────────────────────────────────────────────────────
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: any }) => findingsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['findings'] })
      queryClient.invalidateQueries({ queryKey: ['findings-stats'] })
    },
  })

  const handleSelectAll = () =>
    setSelectedFindings(
      selectedFindings.length === data?.findings.length
        ? []
        : data?.findings.map(f => f.id) || []
    )

  const handleSelectFinding = (id: string) =>
    setSelectedFindings(prev =>
      prev.includes(id) ? prev.filter(fid => fid !== id) : [...prev, id]
    )

  const handleGetAIFixes = () => {
    // Encode current URL so AIFix "Back" button returns here with all filters intact
    const returnTo = encodeURIComponent(window.location.pathname + window.location.search)
    navigate(`/ai-fix?findings=${selectedFindings.join(',')}&return_to=${returnTo}`)
  }

  const clearAll = () => {
    setSeverityFilter(''); setStatusFilter(''); setTableFilter(''); setRuleFilter(''); setScanIdFilter('')
    setSearchParams({})
  }

  const anyFilter = severityFilter || statusFilter || tableFilter || ruleFilter || scanIdFilter

  // ── Colour helpers ─────────────────────────────────────────────────────────
  const sevColor = (s: string) => ({
    critical: 'bg-red-100 text-red-800 border-red-200',
    high:     'bg-orange-100 text-orange-800 border-orange-200',
    medium:   'bg-yellow-100 text-yellow-800 border-yellow-200',
    low:      'bg-blue-100 text-blue-800 border-blue-200',
  }[s] ?? 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200 border-gray-200 dark:border-gray-700')

  const stColor = (s: string) => ({
    detected:  'bg-red-50 text-red-700 border-red-200',
    validated: 'bg-yellow-50 text-yellow-700 border-yellow-200',
    assigned:  'bg-blue-50 text-blue-700 border-blue-200',
    resolved:  'bg-green-50 text-green-700 border-green-200',
  }[s] ?? 'bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-200 border-gray-200 dark:border-gray-700')

  return (
    <div className="space-y-6">

      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold text-gray-900 dark:text-gray-100">Findings</h1>
          <p className="mt-2 text-gray-600 dark:text-gray-300">
            {anyFilter ? (
              <>Showing <span className="font-semibold text-gray-900 dark:text-gray-100">{data?.findings.length ?? 0}</span> of{' '}
              <span className="font-semibold text-gray-900 dark:text-gray-100">{allFindings?.total ?? 0}</span> quality issues</>
            ) : (
              <><span className="font-semibold text-gray-900 dark:text-gray-100">{data?.total ?? 0}</span> quality issues detected</>
            )}
            {selectedFindings.length > 0 && (
              <span className="ml-2 text-primary-600 font-semibold">({selectedFindings.length} selected)</span>
            )}
          </p>
        </div>
        {selectedFindings.length > 0 && (
          <button onClick={handleGetAIFixes}
            className="flex items-center px-6 py-3 bg-gradient-to-r from-purple-600 to-blue-600 text-white font-semibold rounded-lg hover:from-purple-700 hover:to-blue-700 shadow-lg transition-all">
            <Sparkles className="w-5 h-5 mr-2" />
            Get AI Fixes ({selectedFindings.length})
          </button>
        )}
      </div>

      {/* ── Filters ──────────────────────────────────────────────────────────── */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4 space-y-3">
        <div className="flex items-center gap-2 mb-1">
          <Filter className="w-4 h-4 text-gray-400 dark:text-gray-400" />
          <span className="text-sm font-medium text-gray-600 dark:text-gray-300">Filters</span>
          {anyFilter && (
            <button onClick={clearAll}
              className="ml-auto flex items-center gap-1 text-xs text-gray-500 dark:text-gray-300 hover:text-gray-800 px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700">
              <X className="w-3 h-3" /> Clear all
            </button>
          )}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">

          {/* Table */}
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-200 mb-1">
              Table ({uniqueTables.length})
            </label>
            <select value={tableFilter.startsWith('__table_name__') ? tableFilter : tableFilter}
              onChange={e => { setTableFilter(e.target.value); setSearchParams({}) }}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 focus:border-transparent">
              <option value="">All Tables</option>
              {uniqueTables.map(t => (
                <option key={t.fqn} value={t.fqn}>{t.table_name}</option>
              ))}
            </select>
          </div>

          {/* Rule */}
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-200 mb-1 flex items-center gap-1">
              <ShieldCheck className="w-3 h-3" /> Rule ({uniqueRuleCodes.length} active)
            </label>
            <select value={ruleFilter} onChange={e => setRuleFilter(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 focus:border-transparent">
              <option value="">All Rules</option>
              {uniqueRuleCodes.map(code => {
                const rule = rulesData?.rules.find(r => r.code === code)
                return (
                  <option key={code} value={code}>
                    {rule?.name ?? code}
                  </option>
                )
              })}
            </select>
          </div>

          {/* Severity */}
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-200 mb-1">Severity</label>
            <select value={severityFilter} onChange={e => setSeverityFilter(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 focus:border-transparent">
              <option value="">All Severities</option>
              <option value="critical">Critical</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
              <option value="info">Info</option>
            </select>
          </div>

          {/* Status */}
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-200 mb-1">Status</label>
            <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 focus:border-transparent">
              <option value="">All Statuses</option>
              <option value="detected">Detected</option>
              <option value="validated">Validated</option>
              <option value="assigned">Assigned</option>
              <option value="resolved">Resolved</option>
            </select>
          </div>
        </div>

        {/* Active filter pills */}
        {anyFilter && (
          <div className="flex flex-wrap gap-2 pt-1">
            {scanIdFilter && (
              <span className="inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-medium bg-indigo-100 text-indigo-800">
                🔀 Workflow scan
                <button onClick={() => { setScanIdFilter(''); setSearchParams({}) }} className="ml-1 hover:text-indigo-900"><X className="w-3 h-3" /></button>
              </span>
            )}
            {tableFilter && (
              <span className="inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-medium bg-primary-100 text-primary-800">
                <Database className="w-3 h-3" /> {activeTableLabel}
                <button onClick={() => { setTableFilter(''); setSearchParams({}) }} className="ml-1 hover:text-primary-900"><X className="w-3 h-3" /></button>
              </span>
            )}
            {ruleFilter && (
              <span className="inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-medium bg-purple-100 text-purple-800">
                <ShieldCheck className="w-3 h-3" />
                {rulesData?.rules.find(r => r.code === ruleFilter)?.name ?? ruleFilter}
                <button onClick={() => setRuleFilter('')} className="ml-1 hover:text-purple-900"><X className="w-3 h-3" /></button>
              </span>
            )}
            {severityFilter && (
              <span className="inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-medium bg-orange-100 text-orange-800">
                {severityFilter}
                <button onClick={() => setSeverityFilter('')} className="ml-1 hover:text-orange-900"><X className="w-3 h-3" /></button>
              </span>
            )}
            {statusFilter && (
              <span className="inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-medium bg-blue-100 text-blue-800">
                {statusFilter}
                <button onClick={() => setStatusFilter('')} className="ml-1 hover:text-blue-900"><X className="w-3 h-3" /></button>
              </span>
            )}
          </div>
        )}
      </div>

      {/* Bulk select bar */}
      {data?.findings && data.findings.length > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4 flex items-center justify-between">
          <label className="flex items-center cursor-pointer">
            <input type="checkbox"
              checked={selectedFindings.length === data.findings.length}
              onChange={handleSelectAll}
              className="w-4 h-4 text-primary-600 border-gray-300 dark:border-gray-600 rounded focus:ring-primary-500" />
            <span className="ml-2 text-sm font-medium text-gray-700 dark:text-gray-200">
              Select All ({data.findings.length})
            </span>
          </label>
          {selectedFindings.length > 0 && (
            <div className="flex items-center gap-3">
              <span className="text-sm text-gray-600 dark:text-gray-300">{selectedFindings.length} selected</span>
              <button onClick={() => setSelectedFindings([])} className="text-sm text-gray-600 dark:text-gray-300 hover:text-gray-900">Clear</button>
            </div>
          )}
        </div>
      )}

      {/* Findings list */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
        {isLoading ? (
          <div className="p-12 text-center">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-600 mx-auto mb-4" />
            <p className="text-gray-500 dark:text-gray-300">Loading findings…</p>
          </div>
        ) : data?.findings.length === 0 ? (
          <div className="p-12 text-center">
            <AlertCircle className="w-16 h-16 text-gray-300 mx-auto mb-4" />
            {anyFilter ? (
              <>
                <p className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">No findings match your filters</p>
                <p className="text-sm text-gray-500 dark:text-gray-300 mb-4">Try adjusting or clearing the filters above</p>
                <button onClick={clearAll} className="px-4 py-2 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700">
                  Clear Filters
                </button>
              </>
            ) : (
              <>
                <p className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">No quality issues found yet</p>
                <p className="text-sm text-gray-500 dark:text-gray-300 mb-4">Scan a table to discover data quality issues</p>
                <button onClick={() => navigate('/scanner')} className="px-4 py-2 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700">
                  Go to Scanner
                </button>
              </>
            )}
          </div>
        ) : (
          <div className="divide-y divide-gray-200 dark:divide-gray-700">
            {data?.findings.map(finding => (
              <div key={finding.id} className="p-6 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors">
                <div className="flex items-start gap-4">
                  <div className="flex-shrink-0 pt-1">
                    <input type="checkbox"
                      checked={selectedFindings.includes(finding.id)}
                      onChange={() => handleSelectFinding(finding.id)}
                      className="w-5 h-5 text-primary-600 border-gray-300 dark:border-gray-600 rounded focus:ring-primary-500 cursor-pointer" />
                  </div>

                  <div className="flex-1 min-w-0">
                    {/* Badges row */}
                    <div className="flex flex-wrap items-center gap-2 mb-2">
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border ${sevColor(finding.severity)}`}>
                        {finding.severity.toUpperCase()}
                      </span>
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border ${stColor(finding.status)}`}>
                        {finding.status}
                      </span>
                      {/* Rule code chip — clickable to filter */}
                      {finding.context?.rule_code && (
                        <button
                          onClick={() => setRuleFilter(finding.context.rule_code)}
                          className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium border transition-colors ${
                            ruleFilter === finding.context.rule_code
                              ? 'bg-purple-200 text-purple-900 border-purple-300'
                              : 'bg-purple-50 text-purple-700 border-purple-200 hover:bg-purple-100'
                          }`}
                          title="Filter by this rule"
                        >
                          <ShieldCheck className="w-3 h-3" />
                          {rulesData?.rules.find(r => r.code === finding.context.rule_code)?.name ?? finding.context.rule_code}
                        </button>
                      )}
                    </div>

                    {/* Table badge */}
                    {finding.context?.table_name && (
                      <div className="mb-2">
                        <button
                          onClick={() => setTableFilter(
                            `${finding.context.database_name}.${finding.context.schema_name}.${finding.context.table_name}`
                          )}
                          className="inline-flex items-center px-2.5 py-0.5 rounded text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 border border-gray-300 dark:border-gray-600 hover:bg-gray-200 transition-colors"
                          title="Filter by this table"
                        >
                          <Database className="w-3 h-3 mr-1" />
                          {finding.context.table_name}
                        </button>
                      </div>
                    )}

                    <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100 mb-1">{finding.title}</h3>
                    <p className="text-sm text-gray-600 dark:text-gray-300 mb-3">{finding.description}</p>

                    <div className="flex flex-wrap gap-x-6 gap-y-2 text-xs text-gray-500 dark:text-gray-300">
                      {finding.context?.fqn && (
                        <span className="font-mono bg-gray-50 dark:bg-gray-900 px-2 py-1 rounded border border-gray-200 dark:border-gray-700">
                          {finding.context.fqn}
                        </span>
                      )}
                      <span className="flex items-center">
                        <span className="text-gray-400 dark:text-gray-400 mr-1">Detected:</span>
                        {new Date(finding.detected_at).toLocaleString()}
                      </span>
                    </div>
                  </div>

                  <div className="flex-shrink-0">
                    {finding.status === 'detected' && (
                      <button
                        onClick={() => updateMutation.mutate({ id: finding.id, data: { status: 'validated' } })}
                        className="px-3 py-1.5 text-sm font-medium text-primary-700 bg-primary-50 rounded-lg hover:bg-primary-100">
                        Validate
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

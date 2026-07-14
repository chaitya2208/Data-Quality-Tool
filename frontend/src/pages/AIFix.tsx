import { useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { findingsApi, aiApi } from '../api/client'
import {
  Sparkles, CheckCircle, XCircle, Loader2, Copy,
  AlertTriangle, ArrowLeft, Check, Server, ShieldCheck, ChevronDown, RefreshCw
} from 'lucide-react'

export default function AIFix() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const findingIds = searchParams.get('findings')?.split(',') || []
  const returnTo = searchParams.get('return_to')
    ? decodeURIComponent(searchParams.get('return_to')!)
    : '/findings'

  const [selectedWarehouse, setSelectedWarehouse] = useState('')
  const [selectedRole, setSelectedRole] = useState('')
  const [approvedFixes, setApprovedFixes] = useState<string[]>([])
  const [executedFixes, setExecutedFixes] = useState<string[]>([])
  const [copiedSQL, setCopiedSQL] = useState<string | null>(null)
  // Per-finding edited SQL. Keyed by finding id; when a key exists it overrides
  // the AI's recommended sql_query so the user can tweak before executing.
  const [editedSql, setEditedSql] = useState<Record<string, string>>({})

  // Single call — served from backend startup cache, instant
  const { data: sfContext, isLoading: loadingContext } = useQuery({
    queryKey: ['sf-context'],
    queryFn: () => aiApi.getContext().then(res => res.data),
    staleTime: Infinity,
  })

  const warehouses = sfContext?.warehouses ?? []
  const roles      = sfContext?.roles ?? []
  const loadingWarehouses = loadingContext
  const loadingRoles      = loadingContext

  // Auto-defaults: first running warehouse, current role
  const effectiveWarehouse = selectedWarehouse ||
    warehouses.find(w => w.state === 'STARTED')?.name ||
    warehouses[0]?.name || ''

  const effectiveRole = selectedRole ||
    roles.find(r => r.is_current)?.name ||
    roles[0]?.name || ''

  // Fetch finding details
  const { data: findingsData, isLoading: loadingFindings } = useQuery({
    queryKey: ['findings-for-fix', findingIds],
    queryFn: async () =>
      Promise.all(findingIds.map(id => findingsApi.get(id).then(r => r.data))),
    enabled: findingIds.length > 0,
  })

  // Fetch AI recommendations — Claude calls; can take 10-30s for multiple findings
  const {
    data: recommendations,
    isLoading: loadingRecommendations,
    isError: recsError,
    error: recsErrorObj,
  } = useQuery({
    queryKey: ['ai-recommendations', findingIds],
    queryFn: () => aiApi.getRecommendations(findingIds).then(r => r.data),
    enabled: findingIds.length > 0,
    retry: 1,
    staleTime: 5 * 60 * 1000, // cache for 5 min — avoid re-calling Claude on re-render
  })

  const isLoading = loadingFindings || loadingRecommendations

  // Fast source-type lookup — resolves immediately from scan/connection data,
  // no Claude call. Drives whether to show Snowflake role/warehouse or the
  // Postgres connection pill before recommendations even load.
  const { data: sourceTypeData } = useQuery({
    queryKey: ['ai-source-type', findingIds],
    queryFn: () => aiApi.getSourceType(findingIds).then(r => r.data),
    enabled: findingIds.length > 0,
    staleTime: Infinity,
  })

  const isPostgres     = sourceTypeData?.source_type === 'postgres'
  const connectionName = sourceTypeData?.connection_name ?? recommendations?.[0]?.connection_name ?? ''
  const connectionUser = sourceTypeData?.connection_user ?? recommendations?.[0]?.connection_user ?? ''

  const executeMutation = useMutation({
    mutationFn: ({ findingId, sqlQuery }: { findingId: string; sqlQuery: string }) =>
      isPostgres
        ? aiApi.executeSQL(findingId, sqlQuery)
        : aiApi.executeSQL(findingId, sqlQuery, effectiveWarehouse, effectiveRole),
    onSuccess: (_, variables) => {
      setExecutedFixes(prev => [...prev, variables.findingId])
      queryClient.invalidateQueries({ queryKey: ['findings'] })
      queryClient.invalidateQueries({ queryKey: ['findings-stats'] })
    },
  })

  const getRecommendation = (id: string) =>
    recommendations?.find(r => r.finding_id === id)

  const handleCopySQL = (sql: string) => {
    navigator.clipboard.writeText(sql)
    setCopiedSQL(sql)
    setTimeout(() => setCopiedSQL(null), 2000)
  }

  const handleReject = (findingId: string) => {
    const remaining = findingIds.filter(id => id !== findingId)
    if (remaining.length === 0) navigate(returnTo)
    else navigate(`/ai-fix?findings=${remaining.join(',')}&return_to=${encodeURIComponent(returnTo)}`)
  }

  // Postgres needs no Snowflake context; Snowflake needs a role + warehouse.
  const canExecute = isPostgres || (!!effectiveWarehouse && !!effectiveRole)

  if (loadingFindings) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[60vh] gap-3">
        <Loader2 className="w-10 h-10 animate-spin text-primary-600" />
        <p className="text-gray-600 dark:text-gray-300 font-medium">Loading findings...</p>
      </div>
    )
  }

  if (!findingsData || findingsData.length === 0) {
    return (
      <div className="max-w-4xl mx-auto py-12 text-center">
        <AlertTriangle className="w-16 h-16 text-gray-400 dark:text-gray-400 mx-auto mb-4" />
        <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-2">No Findings Selected</h2>
        <p className="text-gray-600 dark:text-gray-300 mb-6">Please select findings from the Findings page.</p>
        <button onClick={() => navigate('/findings')}
          className="px-6 py-3 bg-primary-600 text-white rounded-lg hover:bg-primary-700">
          Go to Findings
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-6">

      {/* ── Header ── */}
      <div>
        <button onClick={() => navigate(returnTo)}
          className="flex items-center text-gray-500 dark:text-gray-300 hover:text-gray-900 text-sm mb-3">
          <ArrowLeft className="w-4 h-4 mr-1" /> Back to Findings
        </button>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-gray-900 dark:text-gray-100">AI-Powered Fixes</h1>
            <p className="mt-1 text-gray-500 dark:text-gray-300">
              {findingsData.length} finding{findingsData.length !== 1 ? 's' : ''} selected for review
            </p>
          </div>

          {/* Role + Warehouse dropdowns — Snowflake only, hidden until source type resolves */}
          {sourceTypeData && !isPostgres && (
          <div className="flex flex-wrap items-end gap-3">
            {/* Role dropdown */}
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-gray-500 dark:text-gray-300 flex items-center gap-1">
                <ShieldCheck className="w-3 h-3 text-purple-500" /> Role
              </label>
              <div className="relative">
                {loadingRoles ? (
                  <div className="flex items-center gap-2 px-3 py-2 border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 text-sm text-gray-400 dark:text-gray-400 w-40">
                    <Loader2 className="w-4 h-4 animate-spin" /> Loading...
                  </div>
                ) : (
                  <select
                    value={effectiveRole}
                    onChange={e => setSelectedRole(e.target.value)}
                    className="appearance-none w-40 sm:w-52 pl-3 pr-8 py-2 border-2 border-purple-200 rounded-lg bg-white dark:bg-gray-800 text-sm font-medium text-gray-900 dark:text-gray-100 focus:outline-none focus:border-purple-500 focus:ring-1 focus:ring-purple-500 cursor-pointer"
                  >
                    {roles?.map(role => (
                      <option key={role.name} value={role.name}>
                        {role.name}{role.is_current ? ' ★' : ''}
                      </option>
                    ))}
                  </select>
                )}
                <ChevronDown className="absolute right-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 dark:text-gray-400 pointer-events-none" />
              </div>
            </div>

            {/* Warehouse dropdown */}
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-gray-500 dark:text-gray-300 flex items-center gap-1">
                <Server className="w-3 h-3 text-primary-500" /> Warehouse
              </label>
              <div className="relative">
                {loadingWarehouses ? (
                  <div className="flex items-center gap-2 px-3 py-2 border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 text-sm text-gray-400 dark:text-gray-400 w-40">
                    <Loader2 className="w-4 h-4 animate-spin" /> Loading...
                  </div>
                ) : (
                  <select
                    value={effectiveWarehouse}
                    onChange={e => setSelectedWarehouse(e.target.value)}
                    className="appearance-none w-40 sm:w-56 pl-3 pr-8 py-2 border-2 border-primary-200 rounded-lg bg-white dark:bg-gray-800 text-sm font-medium text-gray-900 dark:text-gray-100 focus:outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500 cursor-pointer"
                  >
                    {warehouses?.map(wh => (
                      <option key={wh.name} value={wh.name}>
                        {wh.name} {wh.state === 'STARTED' ? '●' : '○'}
                      </option>
                    ))}
                  </select>
                )}
                <ChevronDown className="absolute right-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 dark:text-gray-400 pointer-events-none" />
              </div>
            </div>
          </div>
          )}

          {/* Postgres/RDS: show which connection fixes run on */}
          {isPostgres && (
            <div className="flex flex-col gap-1 items-end">
              <label className="text-xs font-medium text-gray-500 dark:text-gray-300 flex items-center gap-1">
                <Server className="w-3 h-3 text-primary-500" /> Runs on
              </label>
              <span className="inline-flex items-center gap-1.5 px-3 py-2 border-2 border-primary-200 dark:border-primary-500/40 rounded-lg bg-white dark:bg-gray-800 text-sm font-medium text-gray-900 dark:text-gray-100">
                {connectionName || 'Postgres connection'}
                {connectionUser && <span className="text-gray-400 dark:text-gray-400">· {connectionUser}</span>}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Active context pill — Snowflake role+warehouse */}
      {sourceTypeData && !isPostgres && canExecute && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="flex items-center gap-1.5 bg-purple-50 border border-purple-200 text-purple-800 px-3 py-1.5 rounded-full font-medium">
            <ShieldCheck className="w-3.5 h-3.5" /> {effectiveRole}
          </span>
          <span className="text-gray-400 dark:text-gray-400">+</span>
          <span className="flex items-center gap-1.5 bg-blue-50 border border-blue-200 text-blue-800 px-3 py-1.5 rounded-full font-medium">
            <Server className="w-3.5 h-3.5" /> {effectiveWarehouse}
            {warehouses?.find(w => w.name === effectiveWarehouse)?.state === 'SUSPENDED' && (
              <span className="text-yellow-600 ml-1">(will resume)</span>
            )}
          </span>
          <span className="text-gray-400 dark:text-gray-400 italic hidden sm:inline">— all fixes will run with this context</span>
        </div>
      )}

      {/* Postgres/RDS context pill */}
      {isPostgres && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="flex items-center gap-1.5 bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-500/40 text-blue-800 dark:text-blue-300 px-3 py-1.5 rounded-full font-medium">
            <Server className="w-3.5 h-3.5" /> {connectionName || 'Postgres/RDS'}
            {connectionUser && <span className="opacity-80">· {connectionUser}</span>}
          </span>
          <span className="text-gray-400 dark:text-gray-400 italic hidden sm:inline">— fixes run on this connection</span>
        </div>
      )}

      {sourceTypeData && !isPostgres && !canExecute && !loadingWarehouses && !loadingRoles && (
        <div className="bg-yellow-50 dark:bg-yellow-950/40 border border-yellow-200 dark:border-yellow-500/40 rounded-lg p-3 text-sm text-yellow-800 dark:text-yellow-300">
          ⚠️ Could not load warehouse or role from Snowflake. Check your connection.
        </div>
      )}

      {/* Info banner */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start gap-3">
        <Sparkles className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
        <p className="text-sm text-blue-800 dark:text-blue-300">
          <span className="font-semibold">Review each fix carefully before approving.</span>{' '}
          The AI generates SQL based on the detected issue. Once you execute, the fix runs{' '}
          {isPostgres
            ? <>on <span className="font-medium">{connectionName || 'the Postgres connection'}</span>{connectionUser ? <> as <span className="font-medium">{connectionUser}</span></> : null}</>
            : <>in Snowflake under the role and warehouse selected above</>}
          , and the finding is marked resolved.
        </p>
      </div>

      {/* ── Recommendations loading / error banner ── */}
      {loadingRecommendations && (
        <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 flex items-center gap-3">
          <Loader2 className="w-5 h-5 animate-spin text-blue-600 flex-shrink-0" />
          <div>
            <p className="text-sm font-semibold text-blue-900">Claude is generating recommendations...</p>
            <p className="text-xs text-blue-700 mt-0.5">
              This can take 10–30 seconds. Finding cards will appear below once ready.
            </p>
          </div>
        </div>
      )}

      {recsError && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-center gap-3">
          <AlertTriangle className="w-5 h-5 text-red-500 flex-shrink-0" />
          <div>
            <p className="text-sm font-semibold text-red-900">Failed to generate recommendations</p>
            <p className="text-xs text-red-700 mt-0.5">
              {(recsErrorObj as any)?.response?.data?.detail || 'Check backend logs for details.'}
            </p>
          </div>
        </div>
      )}

      {/* ── Finding cards ── */}
      <div className="space-y-6">
        {findingsData.map((finding) => {
          const rec = getRecommendation(finding.id)
          const isApproved = approvedFixes.includes(finding.id)
          const isExecuting = executeMutation.isPending && executeMutation.variables?.findingId === finding.id
          const isExecuted = executedFixes.includes(finding.id)
          const originalSql = rec?.sql_query ?? ''
          const sql = editedSql[finding.id] ?? originalSql
          const isEdited = editedSql[finding.id] !== undefined && editedSql[finding.id] !== originalSql

          return (
            <div key={finding.id}
              className={`bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden transition-all ${
                isExecuted ? 'ring-2 ring-green-300 opacity-80' : ''
              }`}
            >
              {/* Card header */}
              <div className="bg-gray-50 dark:bg-gray-900 px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">{finding.title}</h3>
                  <div className="flex flex-wrap items-center gap-2 mt-2">
                    <span className="text-xs font-mono bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 px-2 py-0.5 rounded text-gray-600 dark:text-gray-300 truncate max-w-sm">
                      {finding.context?.fqn}
                    </span>
                    <span className={`text-xs px-2 py-0.5 rounded font-medium ${
                      finding.severity === 'high' ? 'bg-orange-100 text-orange-800' :
                      finding.severity === 'medium' ? 'bg-yellow-100 text-yellow-800' :
                      'bg-blue-100 text-blue-800'
                    }`}>
                      {finding.severity.toUpperCase()}
                    </span>
                  </div>
                </div>
                {isExecuted && (
                  <span className="flex items-center gap-1 text-green-700 text-xs font-medium bg-green-100 px-3 py-1 rounded-full flex-shrink-0">
                    <CheckCircle className="w-3.5 h-3.5" /> Resolved
                  </span>
                )}
              </div>

              {/* Card body */}
              <div className="p-6 space-y-4">

                {/* ── Recommendation pending / error / loaded ── */}
                {!rec && loadingRecommendations && (
                  <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-300">
                    <Loader2 className="w-4 h-4 animate-spin text-purple-500" />
                    Asking Claude for a fix recommendation...
                  </div>
                )}

                {!rec && !loadingRecommendations && (
                  <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 rounded-lg p-3">
                    <AlertTriangle className="w-4 h-4 text-yellow-500" />
                    No recommendation generated for this finding.
                  </div>
                )}

                {rec && (
                  <>
                    {/* Explanation + confidence */}
                    <div className="flex items-start justify-between gap-6">
                      <div className="flex-1">
                        <div className="flex items-center gap-1.5 mb-1.5">
                          <Sparkles className="w-4 h-4 text-purple-500" />
                          <span className="text-sm font-semibold text-gray-800 dark:text-gray-200">AI Recommendation</span>
                          {rec.from_cache ? (
                            <span className="text-xs bg-green-50 border border-green-200 text-green-700 px-1.5 py-0.5 rounded-full font-medium">
                              ⚡ cached
                            </span>
                          ) : rec.source === 'cortex' ? (
                            <span className="text-xs bg-blue-50 border border-blue-200 text-blue-700 px-1.5 py-0.5 rounded-full font-medium">
                              ❄️ Cortex
                            </span>
                          ) : rec.source === 'claude' ? (
                            <span className="text-xs bg-purple-50 border border-purple-200 text-purple-700 px-1.5 py-0.5 rounded-full font-medium">
                              ✦ Claude
                            </span>
                          ) : null}
                        </div>
                        <p className="text-sm text-gray-600 dark:text-gray-300 leading-relaxed">{rec.explanation}</p>
                      </div>
                      <div className="text-center flex-shrink-0">
                        <p className="text-xs text-gray-400 dark:text-gray-400 mb-1">Confidence</p>
                        <p className={`text-3xl font-bold leading-none ${
                          rec.confidence >= 90 ? 'text-green-600' :
                          rec.confidence >= 70 ? 'text-yellow-500' : 'text-red-500'
                        }`}>
                          {rec.confidence}%
                        </p>
                      </div>
                    </div>

                    {/* SQL block — editable in place */}
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-gray-700 dark:text-gray-200">Suggested SQL Fix</span>
                          {isEdited && (
                            <span className="text-xs bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-700 text-amber-700 dark:text-amber-300 px-1.5 py-0.5 rounded-full font-medium">
                              edited
                            </span>
                          )}
                          {!isExecuted && (
                            <span className="text-xs text-gray-400 dark:text-gray-500">— editable</span>
                          )}
                        </div>
                        <div className="flex items-center gap-1">
                          {isEdited && !isExecuted && (
                            <button
                              onClick={() => setEditedSql(p => { const n = { ...p }; delete n[finding.id]; return n })}
                              className="flex items-center gap-1 text-xs text-gray-400 dark:text-gray-400 hover:text-gray-700 px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                              title="Revert to the AI-recommended SQL"
                            >
                              <RefreshCw className="w-3.5 h-3.5" />Reset
                            </button>
                          )}
                          <button onClick={() => handleCopySQL(sql)}
                            className="flex items-center gap-1 text-xs text-gray-400 dark:text-gray-400 hover:text-gray-700 px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors">
                            {copiedSQL === sql
                              ? <><Check className="w-3.5 h-3.5 text-green-500" /><span className="text-green-600">Copied!</span></>
                              : <><Copy className="w-3.5 h-3.5" />Copy</>
                            }
                          </button>
                        </div>
                      </div>
                      <textarea
                        value={sql}
                        onChange={e => setEditedSql(p => ({ ...p, [finding.id]: e.target.value }))}
                        readOnly={isExecuted}
                        spellCheck={false}
                        rows={Math.min(Math.max(sql.split('\n').length, 3), 18)}
                        className="w-full bg-gray-900 text-green-400 p-4 rounded-lg text-sm font-mono leading-relaxed resize-y border border-gray-700 focus:outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500 disabled:opacity-70"
                      />
                    </div>

                    {/* Impact */}
                    {rec.impact && (
                      <div className="flex items-start gap-2 bg-yellow-50 border border-yellow-200 rounded-lg px-4 py-2.5">
                        <AlertTriangle className="w-4 h-4 text-yellow-500 mt-0.5 flex-shrink-0" />
                        <p className="text-sm text-yellow-800">
                          <span className="font-medium">Impact: </span>{rec.impact}
                        </p>
                      </div>
                    )}
                  </>
                )}

                {/* Action buttons — only shown when rec is available */}
                {rec && (
                  <div className="flex items-center gap-3 pt-1">
                    {!isApproved && !isExecuted && (
                      <>
                        <button
                          onClick={() => setApprovedFixes(p => [...p, finding.id])}
                          disabled={!canExecute}
                          className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-green-600 text-white font-medium rounded-lg hover:bg-green-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                          <CheckCircle className="w-4 h-4" /> Approve Fix
                        </button>
                        <button
                          onClick={() => handleReject(finding.id)}
                          className="flex items-center justify-center gap-2 px-4 py-2.5 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 font-medium rounded-lg hover:bg-gray-200 transition-colors"
                        >
                          <XCircle className="w-4 h-4" /> Reject
                        </button>
                      </>
                    )}

                    {isApproved && !isExecuting && !isExecuted && (
                      <button
                        onClick={() => executeMutation.mutate({ findingId: finding.id, sqlQuery: sql })}
                        className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-primary-600 text-white font-medium rounded-lg hover:bg-primary-700 transition-colors"
                      >
                        <Server className="w-4 h-4" />
                        {isPostgres
                          ? <>Execute Fix{connectionName ? <> on <span className="font-bold">{connectionName}</span></> : null}</>
                          : <>Execute as <span className="font-bold">{effectiveRole}</span> on <span className="font-bold">{effectiveWarehouse}</span></>}
                      </button>
                    )}

                    {isExecuting && (
                      <div className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-primary-50 dark:bg-primary-900/40 text-primary-700 dark:text-primary-300 font-medium rounded-lg">
                        <Loader2 className="w-4 h-4 animate-spin" />
                        {isPostgres ? `Running on ${connectionName || 'Postgres'}...` : `Running on ${effectiveWarehouse}...`}
                      </div>
                    )}

                    {isExecuted && (
                      <div className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-300 font-medium rounded-lg">
                        <CheckCircle className="w-4 h-4" />
                        Executed — finding resolved
                      </div>
                    )}
                  </div>
                )}

                {/* Execution error */}
                {executeMutation.isError && executeMutation.variables?.findingId === finding.id && (
                  <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-800">
                    ❌ {(executeMutation.error as any)?.response?.data?.detail || 'Execution failed. Check backend logs.'}
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

import { Fragment, useEffect, useRef, useState } from 'react'
import { useNavigate, useLocation, Navigate } from 'react-router-dom'
import './App.css'
import AlertsPage from './AlertsPage'
import DashboardPage from './DashboardPage'
import ExecutionHistoryPage from './ExecutionHistoryPage'
import SettingsPage from './SettingsPage'
import TableHealthPage from './TableHealthPage'

const API_BASE = ''

function ConnectionStatus() {
  const [status, setStatus] = useState(null)
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState(null)
  const connectingRef = useRef(false)

  function checkStatus() {
    fetch(`${API_BASE}/api/connection/status`)
      .then((res) => res.json())
      .then(setStatus)
      .catch(() => setStatus({ connected: false }))
  }

  useEffect(() => {
    checkStatus()
  }, [])

  function connect() {
    // Prevent multiple in-flight requests — each one opens an SSO browser tab
    if (connectingRef.current) return
    connectingRef.current = true
    setConnecting(true)
    setError(null)
    fetch(`${API_BASE}/api/connection/connect`, { method: 'POST' })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then(setStatus)
      .catch((err) => setError(err.message))
      .finally(() => {
        connectingRef.current = false
        setConnecting(false)
      })
  }

  if (status?.connected) {
    return (
      <div className="status status-ok">
        <strong>{status.USER}</strong><br />
        {status.ROLE} · {status.WAREHOUSE}
      </div>
    )
  }

  return (
    <div className="conn-panel">
      {error && <div className="status status-error" style={{ marginBottom: 8 }}>{error}</div>}
      <button className="conn-connect-btn" onClick={connect} disabled={connecting}>
        {connecting ? 'Opening browser…' : 'Connect to Snowflake'}
      </button>
      {connecting && <p className="muted" style={{ marginTop: 6, fontSize: 11 }}>Complete the SSO login in your browser</p>}
    </div>
  )
}

function DatabaseExplorer({
  onRulesRecommended,
  onSchemaScanned,
  onScanStart,
  onScanEnd,
  // Scan-progress state lifted to App so it survives page navigation
  progressLogs, setProgressLogs,
  logsVisible, setLogsVisible,
  lastScanIds, setLastScanIds,
  lastTableErrors, setLastTableErrors,
  tableScanMap, setTableScanMap,
  scanTableList, setScanTableList,
  currentTableName, setCurrentTableName,
  pollRef,
}) {
  const [databases, setDatabases] = useState([])
  const [selectedDb, setSelectedDb] = useState(null)
  const [schemas, setSchemas] = useState([])
  const [selectedSchema, setSelectedSchema] = useState(null)
  const [tables, setTables] = useState([])
  const [selectedTable, setSelectedTable] = useState(null)
  const [columns, setColumns] = useState([])
  const [profile, setProfile] = useState(null)
  const [loading, setLoading] = useState({})
  const [error, setError] = useState(null)
  const [scanScope, setScanScope] = useState('table') // 'table' | 'schema' | 'tables' | 'database'
  const [selectedTableNames, setSelectedTableNames] = useState([])
  const [dbPreview, setDbPreview] = useState(null)
  const [dbPreviewLoading, setDbPreviewLoading] = useState(false)

  function toggleSelectedTable(name) {
    setSelectedTableNames((names) =>
      names.includes(name) ? names.filter((n) => n !== name) : [...names, name]
    )
  }

  function stopProgressPolling() {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  function startScan() {
    setLoading((s) => ({ ...s, recommend: true }))
    onScanStart?.()
  }

  function endScan() {
    stopProgressPolling()
    setLoading((s) => ({ ...s, recommend: false }))
    onScanEnd?.()
  }

  // Single-table scans: poll latest-scan for live logs (no scan_id until POST resolves)
  function startProgressPolling(dbName, schemaName, tableName, knownTables = []) {
    setProgressLogs([])
    setLogsVisible(true)
    setLastScanIds(null)
    setTableScanMap({})
    setScanTableList(knownTables)
    setCurrentTableName(knownTables.length === 1 ? knownTables[0] : null)
    stopProgressPolling()
    const tableParam = tableName ? `?table_name=${tableName}` : ''
    pollRef.current = setInterval(() => {
      fetch(`${API_BASE}/api/databases/${dbName}/schemas/${schemaName}/latest-scan${tableParam}`)
        .then((res) => res.json())
        .then((data) => {
          if (!data.scan_id) return
          if (data.target_table) setCurrentTableName(data.target_table)
          return fetch(`${API_BASE}/api/scans/${data.scan_id}/logs`)
            .then((res) => res.json())
            .then(setProgressLogs)
        })
        .catch(() => {})
    }, 2000)
  }

  // Multi-table scans: consume the NDJSON stream. Each "started" event sets
  // currentTableName immediately; each "completed" event delivers the real
  // scan_id for that table right away — no polling or snapshot hacks needed.
  async function runStreamingScan(url, fetchOptions, knownTables) {
    setProgressLogs([])
    setLogsVisible(true)
    setLastScanIds(null)
    setTableScanMap({})
    setScanTableList(knownTables)
    setCurrentTableName(null)
    startScan()

    const allScanIds = []
    const allErrors = []

    try {
      const res = await fetch(url, fetchOptions)
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `HTTP ${res.status}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() // keep incomplete last line
        for (const line of lines) {
          if (!line.trim()) continue
          try {
            const event = JSON.parse(line)
            if (event.event === 'started') {
              setCurrentTableName(event.table_name)
            } else if (event.event === 'completed') {
              const entry = { scan_id: event.scan_id, rule_count: event.rule_count ?? 0, error: event.error }
              setTableScanMap((prev) => ({ ...prev, [event.table_name]: entry }))
              setCurrentTableName(null)
              if (event.scan_id) allScanIds.push(event.scan_id)
              if (event.error) allErrors.push({ table_name: event.table_name, error: event.error })
            }
          } catch {
            // malformed line — skip
          }
        }
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLastScanIds(allScanIds)
      setLastTableErrors(allErrors)
      endScan()
      if (allScanIds.length > 0) onSchemaScanned(allScanIds, allErrors)
    }
  }

  // pollRef cleanup is handled in App (since pollRef is lifted there)

  useEffect(() => {
    setLoading((s) => ({ ...s, databases: true }))
    fetch(`${API_BASE}/api/databases`)
      .then((res) => res.json())
      .then(setDatabases)
      .catch((err) => setError(err.message))
      .finally(() => setLoading((s) => ({ ...s, databases: false })))
  }, [])

  function selectDatabase(dbName) {
    if (loading.profile) return
    setSelectedDb(dbName)
    setSelectedSchema(null)
    setSelectedTable(null)
    setSchemas([])
    setTables([])
    setColumns([])
    setSelectedTableNames([])
    setDbPreview(null)
    setError(null)
    setLoading((s) => ({ ...s, schemas: true }))
    fetch(`${API_BASE}/api/databases/${dbName}/schemas`)
      .then((res) => res.json())
      .then(setSchemas)
      .catch((err) => setError(err.message))
      .finally(() => setLoading((s) => ({ ...s, schemas: false })))
  }

  function selectSchema(schemaName) {
    if (loading.profile) return
    setSelectedSchema(schemaName)
    setSelectedTable(null)
    setTables([])
    setColumns([])
    setSelectedTableNames([])
    setError(null)
    setLoading((s) => ({ ...s, tables: true }))
    fetch(`${API_BASE}/api/databases/${selectedDb}/schemas/${schemaName}/tables`)
      .then((res) => res.json())
      .then(setTables)
      .catch((err) => setError(err.message))
      .finally(() => setLoading((s) => ({ ...s, tables: false })))
  }

  function selectTable(tableName) {
    if (loading.profile) return
    setSelectedTable(tableName)
    setColumns([])
    setProfile(null)
    setError(null)
    setLoading((s) => ({ ...s, columns: true }))
    fetch(
      `${API_BASE}/api/databases/${selectedDb}/schemas/${selectedSchema}/tables/${tableName}/columns`
    )
      .then((res) => res.json())
      .then(setColumns)
      .catch((err) => setError(err.message))
      .finally(() => setLoading((s) => ({ ...s, columns: false })))
  }

  function runProfile() {
    const profilingTable = selectedTable
    setError(null)
    setLoading((s) => ({ ...s, profile: true }))
    fetch(
      `${API_BASE}/api/databases/${selectedDb}/schemas/${selectedSchema}/tables/${profilingTable}/profile`,
      { method: 'POST' }
    )
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        // Table switching is blocked while profiling, but guard anyway: only
        // apply this result if it's still for the table currently selected.
        if (profilingTable === selectedTable) setProfile(data)
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading((s) => ({ ...s, profile: false })))
  }

  function runRecommendRules() {
    const targetTable = selectedTable
    setError(null)
    startScan()
    startProgressPolling(selectedDb, selectedSchema, targetTable, [targetTable])
    fetch(
      `${API_BASE}/api/databases/${selectedDb}/schemas/${selectedSchema}/tables/${targetTable}/recommend-rules`,
      { method: 'POST' }
    )
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        setLastScanIds([data.scan_id])
        setTableScanMap({ [targetTable]: { scan_id: data.scan_id, rule_count: data.recommended_rules?.length ?? null, error: null } })
        onRulesRecommended(data.scan_id, data.table_classification ?? null)
      })
      .catch((err) => setError(err.message))
      .finally(endScan)
  }

  function runSchemaScan() {
    setError(null)
    runStreamingScan(
      `${API_BASE}/api/databases/${selectedDb}/schemas/${selectedSchema}/recommend-rules/stream`,
      { method: 'POST' },
      tables.map(t => t.name),
    )
  }

  function runSelectedTablesScan() {
    setError(null)
    runStreamingScan(
      `${API_BASE}/api/databases/${selectedDb}/schemas/${selectedSchema}/recommend-rules-selected/stream`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ table_names: selectedTableNames }) },
      selectedTableNames,
    )
  }

  function loadDatabasePreview() {
    setDbPreviewLoading(true)
    setError(null)
    fetch(`${API_BASE}/api/databases/${selectedDb}/scan-preview`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setDbPreview)
      .catch((err) => setError(err.message))
      .finally(() => setDbPreviewLoading(false))
  }

  function runDatabaseScan() {
    // Full-database scan can trigger a full-table-scan profiling pass over
    // every table in every schema (no sampling yet -- see
    // docs/deferred-and-future-work.md #4), which for a database with a
    // large table can mean a multi-hour run -- confirm against the real
    // row-count preview before starting, rather than letting one click
    // silently kick off something that big.
    const totalRows = dbPreview?.total_rows ?? 0
    const confirmed = window.confirm(
      `This will scan ${dbPreview?.total_tables ?? '?'} tables across ` +
        `${dbPreview?.schemas.length ?? '?'} schemas (~${totalRows.toLocaleString()} rows total) ` +
        `in ${selectedDb}. Large tables are fully scanned (no sampling yet) and this can take a long ` +
        `time. Continue?`
    )
    if (!confirmed) return

    setError(null)
    startScan()
    startProgressPolling(selectedDb, selectedSchema || dbPreview?.schemas[0]?.schema_name, null, [])
    fetch(`${API_BASE}/api/databases/${selectedDb}/recommend-rules`, { method: 'POST' })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        const allTables = data.schemas.flatMap((s) =>
          s.tables.map((t) => ({ ...t, schema_name: s.schema_name }))
        )
        const scanIds = allTables.filter((t) => t.scan_id).map((t) => t.scan_id)
        const tableErrors = allTables.filter((t) => t.error)
        const schemaErrors = data.schemas.filter((s) => s.error).map((s) => ({
          table_name: `(schema ${s.schema_name})`,
          error: s.error,
        }))
        const allErrors = [...tableErrors, ...schemaErrors]
        const map = {}
        allTables.forEach((t) => { map[`${t.schema_name}.${t.table_name}`] = { scan_id: t.scan_id, rule_count: t.rule_count ?? 0, error: t.error } })
        setLastScanIds(scanIds)
        setLastTableErrors(allErrors)
        setTableScanMap(map)
        onSchemaScanned(scanIds, allErrors)
      })
      .catch((err) => setError(err.message))
      .finally(endScan)
  }

  function runScan() {
    if (scanScope === 'table') runRecommendRules()
    else if (scanScope === 'schema') runSchemaScan()
    else if (scanScope === 'tables') runSelectedTablesScan()
    else if (scanScope === 'database') runDatabaseScan()
  }

  return (
    <div className="explorer">
      {error && <div className="status status-error">{error}</div>}
      {loading.profile && (
        <div className="status status-pending">
          Profiling in progress — switching database/schema/table is disabled until it finishes.
        </div>
      )}
      {logsVisible && (
        <ScanProgressPanel
          logs={progressLogs}
          running={loading.recommend}
          tableScanMap={tableScanMap}
          scanTableList={scanTableList}
          currentTableName={currentTableName}
          onDismiss={() => setLogsVisible(false)}
          onViewResults={lastScanIds ? () => {
            if (lastScanIds.length === 1) onRulesRecommended(lastScanIds[0])
            else onSchemaScanned(lastScanIds, lastTableErrors)
          } : null}
        />
      )}
      {!logsVisible && lastScanIds && !loading.recommend && (
        <div className="scan-complete-banner">
          <span>Scan complete.</span>
          <button
            className="scan-progress-view-results"
            onClick={() => {
              if (lastScanIds.length === 1) onRulesRecommended(lastScanIds[0])
              else onSchemaScanned(lastScanIds, lastTableErrors)
            }}
          >
            View recommended rules →
          </button>
        </div>
      )}

      <div className="explorer-columns">
        <ExplorerColumn
          title="Databases"
          items={databases.map((d) => d.name)}
          selected={selectedDb}
          onSelect={selectDatabase}
          loading={loading.databases}
          disabled={loading.profile || loading.recommend}
          disabledReason="Busy — profiling or recommending rules"
        />
        <ExplorerColumn
          title="Schemas"
          items={schemas.map((s) => s.name)}
          selected={selectedSchema}
          onSelect={selectSchema}
          loading={loading.schemas}
          disabled={!selectedDb || loading.profile || loading.recommend}
          disabledReason={
            loading.profile || loading.recommend ? 'Busy' : 'Select from the left first'
          }
        />
        <ExplorerColumn
          title="Tables"
          items={tables.map((t) => t.name)}
          selected={selectedTable}
          onSelect={selectTable}
          loading={loading.tables}
          disabled={!selectedSchema || loading.profile || loading.recommend}
          disabledReason={
            loading.profile || loading.recommend ? 'Busy' : 'Select from the left first'
          }
        />
      </div>

      <ScanPanel
        dbSelected={!!selectedDb}
        schemaSelected={!!selectedSchema}
        tableSelected={!!selectedTable}
        tables={tables}
        selectedTableNames={selectedTableNames}
        onToggleSelectedTable={toggleSelectedTable}
        scanScope={scanScope}
        onScopeChange={(scope) => {
          setScanScope(scope)
          if (scope === 'database' && !dbPreview) loadDatabasePreview()
        }}
        onRunScan={runScan}
        dbPreview={dbPreview}
        dbPreviewLoading={dbPreviewLoading}
        running={loading.recommend}
        disabled={loading.profile}
      />

      <TableProfilePanel tableName={selectedTable} profile={profile} loading={loading.profile} profileColumns={columns.length} />

      <ColumnsPanel
        tableName={selectedTable}
        tableInfo={tables.find((t) => t.name === selectedTable) ?? null}
        columns={columns}
        loading={loading.columns}
        onProfile={runProfile}
        profiling={loading.profile}
      />
    </div>
  )
}

const SCAN_SCOPE_OPTIONS = [
  { value: 'table', label: 'This table' },
  { value: 'schema', label: 'This schema' },
  { value: 'tables', label: 'Selected tables' },
  { value: 'database', label: 'Full database' },
]

const SCAN_SCOPE_BUTTON_LABEL = {
  table: 'Recommend rules',
  schema: 'Recommend rules for schema',
  tables: 'Recommend rules for selected tables',
  database: 'Recommend rules for database',
}

function ScanPanel({
  dbSelected,
  schemaSelected,
  tableSelected,
  tables,
  selectedTableNames,
  onToggleSelectedTable,
  scanScope,
  onScopeChange,
  onRunScan,
  dbPreview,
  dbPreviewLoading,
  running,
  disabled,
}) {
  if (!dbSelected) return null

  const canRun =
    (scanScope === 'schema' && schemaSelected) ||
    (scanScope === 'table' && tableSelected) ||
    (scanScope === 'tables' && selectedTableNames.length > 0) ||
    (scanScope === 'database' && !!dbPreview)

  return (
    <div className="scan-scope-panel">
      <h3>Scan scope</h3>
      <div className="scan-scope-options">
        {SCAN_SCOPE_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            className={`scan-scope-option ${scanScope === opt.value ? 'active' : ''}`}
            onClick={() => onScopeChange(opt.value)}
            disabled={(opt.value !== 'database' && !schemaSelected) || disabled || running}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {scanScope === 'tables' && (
        <div className="scan-scope-table-picker">
          {tables.length === 0 && <p className="muted">No tables in this schema.</p>}
          {tables.map((t) => (
            <label key={t.name} className="scan-scope-table-checkbox">
              <input
                type="checkbox"
                checked={selectedTableNames.includes(t.name)}
                onChange={() => onToggleSelectedTable(t.name)}
                disabled={disabled || running}
              />
              {t.name}
            </label>
          ))}
        </div>
      )}

      {scanScope === 'database' && (
        <div className="scan-scope-db-preview">
          {dbPreviewLoading && <p className="muted">Loading row counts across all schemas...</p>}
          {dbPreview && (
            <p className="muted">
              {dbPreview.total_tables} tables across {dbPreview.schemas.length} schemas (~
              {dbPreview.total_rows.toLocaleString()} rows total). Large tables are fully scanned
              (no sampling yet) — you'll be asked to confirm before this runs.
            </p>
          )}
        </div>
      )}

      <button className="profile-button" onClick={onRunScan} disabled={!canRun || disabled || running}>
        {running ? 'Recommending...' : SCAN_SCOPE_BUTTON_LABEL[scanScope]}
      </button>
      {scanScope === 'table' && !tableSelected && (
        <p className="muted">Select a table above first.</p>
      )}
      {scanScope === 'schema' && !schemaSelected && (
        <p className="muted">Select a schema above first.</p>
      )}
      {scanScope === 'tables' && selectedTableNames.length === 0 && (
        <p className="muted">Check at least one table above.</p>
      )}
    </div>
  )
}

// Maps STEP_NAME + STATUS from LOGS.AGENT_RUN_LOGS into the exact
// milestone wording the ask specified, so the timeline reads as plain
// English rather than raw enum values.
const PROGRESS_STEP_LABELS = {
  METADATA_DISCOVERY: { STARTED: 'Metadata discovery started', COMPLETED: 'Metadata completed' },
  PROFILING: { STARTED: 'Profiling started', COMPLETED: 'Profiling completed' },
  RULE_RECOMMENDATION: { STARTED: 'Recommending rules...', COMPLETED: 'Rules recommended' },
  SQL_GENERATION: { STARTED: 'Generating SQL...', COMPLETED: 'SQL generated' },
  SQL_VALIDATION: { STARTED: 'Validating SQL...', COMPLETED: 'SQL validated' },
  RULE_TEST_EXECUTION: { STARTED: 'Testing rules...', COMPLETED: 'Testing completed' },
  AWAITING_APPROVAL: { COMPLETED: 'Awaiting approval' },
}

// Pipeline steps keyed by step_name (matches log.step_name from dq_workflow_graph.py)
const PIPELINE_STEPS = [
  { key: 'METADATA_DISCOVERY',  label: 'Metadata discovery' },
  { key: 'PROFILING',           label: 'Profiling' },
  { key: 'PII_CLASSIFICATION',  label: 'PII classification' },
  { key: 'RULE_RECOMMENDATION', label: 'Rule recommendation' },
  { key: 'SQL_GENERATION',      label: 'SQL generation' },
  { key: 'SQL_VALIDATION',      label: 'SQL validation' },
  { key: 'RULE_TEST_EXECUTION', label: 'Rule test execution' },
]

function stepStatus(stepLogs) {
  if (!stepLogs || stepLogs.length === 0) return 'pending'
  const s = stepLogs.map(l => l.status)
  if (s.includes('FAILED')) return 'failed'
  // Check completed before running: STARTED+COMPLETED together means done.
  if (s.every(x => x === 'COMPLETED' || x === 'SKIPPED')) return 'completed'
  if (s.includes('STARTED')) return 'running'
  return 'running'
}

function overallStatus(byStep) {
  const statuses = PIPELINE_STEPS.map(s => stepStatus(byStep[s.key]))
  if (statuses.includes('failed'))    return 'failed'
  if (statuses.includes('running'))   return 'running'
  if (statuses.includes('completed')) {
    if (statuses.every(s => s === 'completed' || s === 'pending')) return 'completed'
    return 'running'
  }
  return 'pending'
}

// Group logs by step_name — matches PIPELINE_STEPS keys
function groupLogsByStep(logs) {
  const map = {}
  for (const log of logs) {
    const key = log.step_name || 'UNKNOWN'
    if (!map[key]) map[key] = []
    map[key].push(log)
  }
  return map
}

// Single-table pipeline view (vertical timeline)
function TablePipelineView({ byStep, expanded, onToggleStep }) {
  return (
    <div className="spw-timeline">
      {PIPELINE_STEPS.map((step) => {
        const status = stepStatus(byStep[step.key])
        const stepLogs = byStep[step.key] || []
        const isOpen = expanded === step.key
        return (
          <div key={step.key} className={`spw-step spw-step-${status}`}>
            <button
              className="spw-step-row"
              onClick={() => stepLogs.length > 0 && onToggleStep(step.key)}
              disabled={stepLogs.length === 0}
            >
              <span className={`spw-step-dot spw-dot-${status}`} />
              <span className="spw-step-label">{step.label}</span>
              <span className={`spw-step-badge spw-badge-${status}`}>
                {status === 'running' ? 'Running…' : status === 'completed' ? 'Done' : status === 'failed' ? 'Failed' : '—'}
              </span>
              {stepLogs.length > 0 && (
                <span className="spw-step-chevron">{isOpen ? '▾' : '▸'}</span>
              )}
            </button>
            {isOpen && stepLogs.length > 0 && (
              <ul className="spw-step-logs">
                {stepLogs.map(log => (
                  <li key={log.log_id} className={`spw-log-item spw-log-${log.status?.toLowerCase()}`}>
                    <span className="spw-log-step">{log.step_name}</span>
                    <span className="spw-log-msg">{log.message || log.status}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )
      })}
    </div>
  )
}

// One table card in a multi-table scan
function TableScanCard({ tableName, scanId, ruleCount, tableError, isCurrent, running, liveLogs }) {
  const [fetchedLogs, setFetchedLogs] = useState([])
  const [loadingLogs, setLoadingLogs] = useState(false)
  const [open, setOpen] = useState(isCurrent)
  const [expandedStep, setExpandedStep] = useState(null)

  function toggleStep(key) {
    setExpandedStep(prev => prev === key ? null : key)
  }

  const prevIsCurrent = useRef(false)
  const prevLiveLogs = useRef(null)

  // Track the last non-null liveLogs separately. isCurrent and liveLogs both
  // change in the same render (parent sets isCurrent=false and liveLogs=null
  // simultaneously), so by the time this effect runs liveLogs is already null.
  // prevLiveLogs captures the last real logs before they were cleared.
  useEffect(() => {
    if (liveLogs && liveLogs.length > 0) prevLiveLogs.current = liveLogs
  }, [liveLogs])

  useEffect(() => {
    if (isCurrent) {
      setOpen(true)
    } else if (prevIsCurrent.current && !isCurrent) {
      // Table just finished — snapshot the last known live logs so the card
      // doesn't go blank while waiting for the POST to return with the scanId
      if (prevLiveLogs.current && prevLiveLogs.current.length > 0) {
        setFetchedLogs(prevLiveLogs.current)
      }
    }
    prevIsCurrent.current = isCurrent
  }, [isCurrent])

  // Fetch final logs once scanId is available (replaces the snapshot)
  useEffect(() => {
    if (!scanId || isCurrent) return
    fetch(`${API_BASE}/api/scans/${scanId}/logs`)
      .then(r => r.json())
      .then(setFetchedLogs)
      .catch(() => {})
  }, [scanId, isCurrent])

  // While current: use live polled logs directly (scanId may not be known yet)
  // After done: use logs fetched by scanId (or snapshot from prevLiveLogs)
  const logs = isCurrent ? (liveLogs ?? []) : fetchedLogs
  const hasLiveLogs = isCurrent && logs.length > 0
  const byStep = groupLogsByStep(logs)
  // If not current and no scanId yet but we have fetched/snapshotted logs,
  // derive status from those logs rather than showing 'pending'.
  const status = isCurrent ? (logs.length > 0 ? overallStatus(byStep) || 'running' : 'running')
               : logs.length > 0 ? (overallStatus(byStep) || 'pending')
               :                   'pending'

  return (
    <div className={`spw-table-card spw-card-${status}`}>
      <button className="spw-table-row" onClick={() => setOpen(o => !o)}>
        <span className={`spw-card-dot spw-dot-${status}`} />
        <span className="spw-table-name">{tableName}</span>
        {!isCurrent && !scanId && <span className="spw-table-meta">pending</span>}
        {tableError && <span className="spw-table-meta spw-table-error" title={tableError}>error</span>}
        {ruleCount != null && status === 'completed' && (
          <span className="spw-table-rules">{ruleCount} rule{ruleCount !== 1 ? 's' : ''}</span>
        )}
        {(isCurrent || scanId) && status !== 'pending' && status !== 'completed' && (
          <span className={`spw-step-badge spw-badge-${status}`}>
            {status === 'running' ? 'Running…' : status === 'failed' ? 'Failed' : '—'}
          </span>
        )}
        <span className="spw-step-chevron">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="spw-table-body">
          {isCurrent && logs.length === 0 ? (
            <p className="muted" style={{ fontSize: 12, padding: '8px 0 4px' }}>Starting…</p>
          ) : !isCurrent && !scanId && logs.length === 0 ? (
            <p className="muted" style={{ fontSize: 12, padding: '8px 0 4px' }}>Waiting to start…</p>
          ) : loadingLogs ? (
            <p className="muted" style={{ fontSize: 12, padding: '8px 0 4px' }}>Loading…</p>
          ) : logs.length === 0 ? (
            <p className="muted" style={{ fontSize: 12, padding: '8px 0 4px' }}>No logs yet.</p>
          ) : (
            <TablePipelineView byStep={byStep} expanded={expandedStep} onToggleStep={toggleStep} />
          )}
        </div>
      )}
    </div>
  )
}

function ScanProgressPanel({ logs, running, tableScanMap, scanTableList, currentTableName, onDismiss, onViewResults }) {
  const [expandedStep, setExpandedStep] = useState(null)

  function toggleStep(agent) {
    setExpandedStep(prev => prev === agent ? null : agent)
  }

  const byStep = groupLogsByStep(logs)
  // Use scanTableList (known at scan start) as the authoritative table list;
  // fall back to tableScanMap keys (populated after POST returns) if needed.
  const tables = scanTableList && scanTableList.length > 0
    ? scanTableList
    : Object.keys(tableScanMap)
  const isMultiTable = tables.length > 1

  return (
    <div className="scan-progress-panel">
      <div className="scan-progress-header">
        <span className="scan-progress-title">
          {running
            ? isMultiTable
              ? `Scanning ${tables.length} tables…`
              : 'Scan in progress…'
            : tables.length > 0
              ? `${tables.length} table${tables.length !== 1 ? 's' : ''} scanned`
              : 'Scan complete'}
        </span>
        {!running && (
          <div style={{ display: 'flex', gap: 8 }}>
            {onViewResults && (
              <button className="scan-progress-view-results" onClick={onViewResults}>
                View results →
              </button>
            )}
            <button className="scan-progress-dismiss" onClick={onDismiss}>
              Dismiss
            </button>
          </div>
        )}
      </div>

      {isMultiTable ? (
        // Multi-table: show each table as a collapsible card
        <div className="spw-table-list">
          {tables.map(tableName => {
            const entry = tableScanMap[tableName]
            const scanId = entry?.scan_id ?? null
            const ruleCount = entry?.rule_count ?? null
            const tableError = entry?.error ?? null
            const isCurrent = running && (currentTableName ? tableName === currentTableName : tableName === tables[0])
            return (
              <TableScanCard
                key={tableName}
                tableName={tableName}
                scanId={scanId}
                ruleCount={ruleCount}
                tableError={tableError}
                isCurrent={isCurrent}
                running={running}
                liveLogs={isCurrent ? logs : null}
              />
            )
          })}
        </div>
      ) : (
        // Single table: show pipeline timeline directly
        <>
          {logs.length === 0 ? (
            <p className="muted" style={{ fontSize: 13, margin: '4px 0 2px' }}>Waiting for the agent to start…</p>
          ) : (
            <TablePipelineView byStep={byStep} expanded={expandedStep} onToggleStep={toggleStep} />
          )}
        </>
      )}
    </div>
  )
}

function ExplorerColumn({
  title,
  items,
  selected,
  onSelect,
  loading,
  disabled,
  disabledReason = 'Select from the left first',
}) {
  return (
    <div className={`explorer-panel ${disabled ? 'disabled' : ''}`}>
      <h3>{title}</h3>
      {loading && <p className="muted">Loading...</p>}
      {!loading && disabled && <p className="muted">{disabledReason}</p>}
      {!loading && !disabled && items.length === 0 && (
        <p className="muted">No items found</p>
      )}
      <ul>
        {items.map((name) => (
          <li key={name}>
            <button
              className={name === selected ? 'active' : ''}
              onClick={() => onSelect(name)}
            >
              {name}
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function formatBytes(bytes) {
  if (bytes == null) return '—'
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`
}

function ColumnsPanel({ tableName, tableInfo, columns, loading, onProfile, profiling }) {
  if (!tableName) return null
  return (
    <div className="columns-panel">
      <div className="columns-panel-header">
        <h3>{tableName}</h3>
        <div className="columns-panel-actions">
          <button className="profile-button" onClick={onProfile} disabled={profiling}>
            {profiling ? 'Profiling...' : 'Profile this table'}
          </button>
        </div>
      </div>

      {tableInfo && (
        <div className="table-meta-strip">
          <div className="table-meta-item">
            <span className="table-meta-label">Rows</span>
            <span className="table-meta-value">
              {tableInfo.row_count != null ? tableInfo.row_count.toLocaleString() : '—'}
            </span>
          </div>
          <div className="table-meta-item">
            <span className="table-meta-label">Size</span>
            <span className="table-meta-value">{formatBytes(tableInfo.bytes)}</span>
          </div>
          <div className="table-meta-item">
            <span className="table-meta-label">Type</span>
            <span className="table-meta-value">{tableInfo.kind ?? '—'}</span>
          </div>
          {tableInfo.owner && (
            <div className="table-meta-item">
              <span className="table-meta-label">Owner</span>
              <span className="table-meta-value">{tableInfo.owner}</span>
            </div>
          )}
          {tableInfo.comment && (
            <div className="table-meta-item table-meta-comment">
              <span className="table-meta-label">Comment</span>
              <span className="table-meta-value">{tableInfo.comment}</span>
            </div>
          )}
        </div>
      )}

      {loading && <p className="muted">Loading columns...</p>}
      {!loading && (
        <table>
          <thead>
            <tr>
              <th>Column</th>
              <th>Data Type</th>
              <th>Nullable</th>
              <th>Primary Key</th>
            </tr>
          </thead>
          <tbody>
            {columns.map((c) => (
              <tr key={c.column_name}>
                <td>{c.column_name}</td>
                <td>{c.data_type}</td>
                <td>{c.is_nullable ? 'Yes' : 'No'}</td>
                <td>{c.primary_key ? 'Yes' : 'No'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function formatValue(value) {
  if (value === null || value === undefined) return '—'
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value)
  return text.length > 60 ? text.slice(0, 60) + '…' : text
}

function TopValuesCell({ topValues }) {
  if (!topValues || topValues.length === 0) return '—'
  return (
    <ul className="top-values-list">
      {topValues.map((tv, i) => (
        <li key={i}>
          {formatValue(tv.value)} <span className="muted">({tv.count})</span>
        </li>
      ))}
    </ul>
  )
}

function ProfileProgressBar({ tableName, columnCount }) {
  const [pct, setPct] = useState(0)

  useEffect(() => {
    // Estimate ~3s per column, cap total at 120s. Animate smoothly to ~90%
    // then stall — the last jump to 100% happens when the panel unmounts.
    const totalMs = Math.min(Math.max((columnCount || 5) * 3000, 8000), 120000)
    const intervalMs = 300
    const step = (intervalMs / totalMs) * 90 // advance to 90% max
    setPct(0)
    const id = setInterval(() => {
      setPct((p) => {
        const next = p + step
        if (next >= 90) { clearInterval(id); return 90 }
        return next
      })
    }, intervalMs)
    return () => clearInterval(id)
  }, [tableName, columnCount])

  return (
    <div className="profile-panel profile-panel-progress">
      <div className="profile-progress-header">
        <span className="profile-progress-title">Profiling {tableName}</span>
        <span className="profile-progress-pct">{Math.round(pct)}%</span>
      </div>
      <div className="profile-progress-track">
        <div
          className="profile-progress-fill"
          style={{ width: `${pct}%`, transition: 'width 0.3s linear' }}
        />
      </div>
      <p className="muted" style={{ marginTop: 8 }}>
        Running per-column stats: null %, distinct count, min/max, top values
        {columnCount ? ` across ${columnCount} columns` : ''}…
      </p>
    </div>
  )
}

function TableProfilePanel({ tableName, profile, loading, profileColumns }) {
  if (!tableName) return null
  if (loading) {
    return <ProfileProgressBar tableName={tableName} columnCount={profileColumns} />
  }
  if (!profile) return null

  return (
    <div className="profile-panel">
      <h3>Table Profile — {tableName}</h3>
      <p className="muted">
        {profile.table.row_count.toLocaleString()} rows · {profile.table.column_count} columns
      </p>
      <table>
        <thead>
          <tr>
            <th>Column</th>
            <th>Data Type</th>
            <th>Null %</th>
            <th>Distinct</th>
            <th>Min</th>
            <th>Max</th>
            <th>Top Values</th>
          </tr>
        </thead>
        <tbody>
          {profile.columns.map((c) => (
            <tr key={c.column_name}>
              <td>{c.column_name}</td>
              <td>{c.data_type}</td>
              <td>{c.null_percentage}%</td>
              <td>{c.distinct_count.toLocaleString()}</td>
              <td>{formatValue(c.min_value)}</td>
              <td>{formatValue(c.max_value)}</td>
              <td>
                <TopValuesCell topValues={c.top_values} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function SampleFailedRows({ sample }) {
  if (!sample) return null
  const { rows, note, evidence } = sample
  return (
    <div className="sample-failed-rows">
      {rows && rows.length > 0 && (
        <table className="rules-table">
          <thead>
            <tr>
              {Object.keys(rows[0]).map((col) => (
                <th key={col}>{col}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {Object.keys(rows[0]).map((col) => (
                  <td key={col}>{row[col] === null || row[col] === undefined ? '—' : String(row[col])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {note && <p className="muted">{note}</p>}
      {evidence && (
        <ul className="evidence-list">
          {Object.entries(evidence).map(([key, value]) => (
            <li key={key}>
              {key}: {value === null || value === undefined ? '—' : String(value)}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// SQL trust-level label for one rule (docs/rules-architecture.md §5.5/§6.4):
// distinguishes template-rendered (TRUSTED), Claude-drafted (not yet
// independently validated by a human), vs. legacy rows with neither signal
// (definition_id/is_new_definition predate this feature — render nothing
// rather than guess).
function sqlTrustLabel(rule) {
  if (!rule.generated_sql) return null
  if (rule.is_new_definition) return { text: 'Claude draft (not yet validated)', variant: 'claude-draft' }
  if (rule.definition_id) return { text: 'Template SQL', variant: 'template' }
  if (rule.rule_fingerprint === 'source:claude') return { text: 'Claude draft (not yet validated)', variant: 'claude-draft' }
  if (rule.rule_fingerprint === 'source:template' || rule.rule_fingerprint === 'source:governance') {
    return { text: 'Template SQL', variant: 'template' }
  }
  return null
}

function RuleDetailRow({ rule, onCancel, loading }) {
  const trust = sqlTrustLabel(rule)
  return (
    <tr className="rule-detail-row">
      <td colSpan={13}>
        <div className="rule-detail">
          {loading ? (
            <div className="rule-detail-loading">
              <span className="rule-detail-spinner" />
              Loading details…
            </div>
          ) : <>
          {(rule.scope || rule.target_config) && (
            <div className="rule-detail-scope-row">
              {rule.scope && (
                <span className={`scope-badge scope-${rule.scope.toLowerCase().replace('_', '-')}`}>{rule.scope}</span>
              )}
              {rule.target_config && Object.keys(rule.target_config).length > 0 && (
                <span className="rule-detail-target">
                  {rule.scope === 'CROSS_TABLE'
                    ? `${rule.target_config.column} → ${[rule.target_config.ref_table, rule.target_config.ref_column].filter(Boolean).join('.')}`
                    : rule.scope === 'MULTI_COLUMN'
                    ? (rule.target_config.columns || []).join(', ')
                    : rule.scope === 'CONDITIONAL'
                    ? `${rule.target_config.column} when ${rule.target_config.when_column} ${rule.target_config.when_operator} ${rule.target_config.when_value}`
                    : rule.target_config.column || JSON.stringify(rule.target_config)}
                </span>
              )}
            </div>
          )}
          {rule.is_new_definition && rule.proposed_definition && (
            <div className="rule-detail-new-def">
              <strong>Will create new rule type:</strong>{' '}
              <span className="new-def-badge">new type</span>
              <div className="rule-detail-new-def-body">
                <div><span className="detail-label">Name:</span> {rule.proposed_definition.name}</div>
                {rule.proposed_definition.category && <div><span className="detail-label">Category:</span> {rule.proposed_definition.category}</div>}
                {rule.proposed_definition.description && <div><span className="detail-label">Description:</span> {rule.proposed_definition.description}</div>}
                {rule.proposed_definition.check_logic && <div><span className="detail-label">Check logic:</span> {rule.proposed_definition.check_logic}</div>}
              </div>
            </div>
          )}
          <p>
            <strong>Description:</strong> {rule.description || '—'}
          </p>
          <p>
            <strong>Reason:</strong> {rule.reason || '—'}
          </p>
          <p className="explanation-block">
            <strong>Business explanation:</strong> {rule.business_explanation || '—'}
          </p>
          <p className="explanation-block">
            <strong>Business impact:</strong> {rule.business_impact || '—'}
          </p>
          <p className="explanation-block">
            <strong>False-positive risk:</strong> {rule.false_positive_risk || '—'}
          </p>
          <div>
            <strong>Evidence:</strong>{' '}
            {rule.evidence && rule.evidence.length > 0 ? (
              <ul className="evidence-list">
                {rule.evidence.map((e, i) => (
                  <li key={i}>{typeof e === 'string' ? e : JSON.stringify(e)}</li>
                ))}
              </ul>
            ) : (
              '—'
            )}
          </div>
          <p>
            <strong>Threshold config:</strong>{' '}
            {rule.threshold_config ? JSON.stringify(rule.threshold_config) : '—'}
          </p>
          <p>
            <strong>Generated SQL:</strong>{' '}
            {trust && <span className={`sql-trust-badge sql-trust-${trust.variant}`}>{trust.text}</span>}
          </p>
          <pre className="rule-sql">{rule.generated_sql || '(no SQL — not template-expressible)'}</pre>
          <p>
            <strong>Test result:</strong>{' '}
            {rule.test_result
              ? `would_pass=${rule.test_result.would_pass}, failed=${rule.test_result.failed_count ?? '—'}, total=${rule.test_result.total_count ?? '—'}, failure%=${rule.test_result.failure_percentage ?? '—'}`
              : '—'}
          </p>
          {rule.test_result?.sample_failed_rows && (
            <div>
              <strong>Sample failed rows:</strong>
              <SampleFailedRows sample={rule.test_result.sample_failed_rows} />
            </div>
          )}
          <button className="link-button" onClick={onCancel}>
            Close details
          </button>
          </>}
        </div>
      </td>
    </tr>
  )
}

function RuleEditForm({ rule, onCancel, onSaved, setError }) {
  // Inline JSON-textarea form for threshold_config -- a pragmatic MVP
  // simplification (flagged): a real approval screen would have typed
  // fields per threshold key, but every rule_type's threshold_config shape
  // differs (accepted_values / pattern / min_value / max_age_hours / ...),
  // and building a schema-aware form per rule_type is real future work, not
  // this task's scope.
  const [severity, setSeverity] = useState(rule.severity)
  const [thresholdText, setThresholdText] = useState(
    rule.threshold_config ? JSON.stringify(rule.threshold_config, null, 2) : ''
  )
  const [sql, setSql] = useState(rule.generated_sql || '')
  const [saving, setSaving] = useState(false)

  function save() {
    let threshold_config
    try {
      threshold_config = thresholdText.trim() ? JSON.parse(thresholdText) : null
    } catch {
      setError('Threshold config is not valid JSON')
      return
    }
    setSaving(true)
    setError(null)
    fetch(`${API_BASE}/api/rules/${rule.rule_id}/edit`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ severity, threshold_config, generated_sql: sql }),
    })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then((updated) => onSaved(updated))
      .catch((err) => setError(err.message))
      .finally(() => setSaving(false))
  }

  return (
    <tr className="rule-detail-row">
      <td colSpan={13}>
        <div className="rule-detail rule-edit-form">
          <label>
            Severity
            <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
              <option value="CRITICAL">CRITICAL</option>
              <option value="WARNING">WARNING</option>
              <option value="INFO">INFO</option>
            </select>
          </label>
          <label>
            Threshold config (JSON)
            <textarea
              rows={4}
              value={thresholdText}
              onChange={(e) => setThresholdText(e.target.value)}
            />
          </label>
          <label>
            Generated SQL
            <textarea rows={6} value={sql} onChange={(e) => setSql(e.target.value)} />
          </label>
          <div className="rule-edit-actions">
            <button className="approve-button" onClick={save} disabled={saving}>
              {saving ? 'Saving...' : 'Save & revalidate'}
            </button>
            <button className="link-button" onClick={onCancel} disabled={saving}>
              Cancel
            </button>
          </div>
        </div>
      </td>
    </tr>
  )
}

function ScanLogsPanel({ ids }) {
  const [open, setOpen] = useState(false)
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(false)

  function fetchLogs() {
    if (!ids || ids.length === 0) return
    setLoading(true)
    Promise.all(
      ids.map((id) =>
        fetch(`${API_BASE}/api/scans/${id}/logs`).then((r) => r.json()).then((rows) =>
          rows.map((row) => ({ ...row, scan_id: id }))
        )
      )
    )
      .then((results) => setLogs(results.flat()))
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  function toggle() {
    if (!open && logs.length === 0) fetchLogs()
    setOpen((v) => !v)
  }

  if (!ids || ids.length === 0) return null

  return (
    <div className="scan-logs-panel">
      <button className="scan-logs-toggle" onClick={toggle}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points={open ? '18 15 12 9 6 15' : '6 9 12 15 18 9'} />
        </svg>
        {open ? 'Hide' : 'View'} scan logs
        <span className="scan-logs-badge">{ids.length} scan{ids.length !== 1 ? 's' : ''}</span>
      </button>
      {open && (
        <div className="scan-logs-body">
          {loading && <p className="muted">Loading logs…</p>}
          {!loading && logs.length === 0 && <p className="muted">No log entries found for this scan.</p>}
          {!loading && logs.length > 0 && (
            <ul className="scan-progress-list">
              {logs.map((log) => {
                const label =
                  PROGRESS_STEP_LABELS[log.step_name]?.[log.status] ||
                  `${log.step_name} — ${log.status}`
                return (
                  <li key={log.log_id} className={`scan-progress-item scan-progress-${log.status.toLowerCase()}`}>
                    <span className="scan-progress-label">{log.message && log.status === 'COMPLETED' && log.message.toLowerCase().includes('failed') ? log.message : label}</span>
                    {log.message && log.status === 'FAILED' && (
                      <span className="scan-progress-detail muted">{log.message}</span>
                    )}
                    {ids.length > 1 && (
                      <span className="scan-progress-detail muted">scan {log.scan_id.slice(0, 8)}</span>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

// A column-header cell with a built-in dropdown that filters the table by
// that column's values. Native <select> so its option list renders in the
// browser's own layer (not clipped by .table-card's overflow-x: auto), and
// so it's the exact same control as the DB/Schema/Table/Column filters above.
function ColumnFilterHeader({ label, value, options, onChange, sticky }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [open])

  if (options.length === 0) {
    return <th className={sticky ? 'col-sticky-right' : undefined}>{label}</th>
  }

  return (
    <th className={sticky ? 'col-sticky-right' : undefined}>
      <div className={`col-filter-head${value ? ' active' : ''}`} ref={ref}>
        <button
          className={`col-filter-btn${value ? ' active' : ''}`}
          onClick={() => setOpen((o) => !o)}
          title={value ? `Filtering: ${value}` : `Filter by ${label.toLowerCase()}`}
        >
          {value || label}
          <svg className="col-filter-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
        {open && (
          <div className="col-filter-dropdown">
            <button
              className={`col-filter-option${!value ? ' selected' : ''}`}
              onClick={() => { onChange(''); setOpen(false) }}
            >
              All {label.toLowerCase()}s
            </button>
            {options.map((o) => (
              <button
                key={o}
                className={`col-filter-option${value === o ? ' selected' : ''}`}
                onClick={() => { onChange(o); setOpen(false) }}
              >
                {o}
              </button>
            ))}
          </div>
        )}
      </div>
    </th>
  )
}

// Renders the "Target" cell — what a rule applies to, scope-aware.
// COLUMN rules: single column name (clickable filter)
// MULTI_COLUMN: comma-joined list
// TABLE: "— (table-level)"
// CROSS_TABLE: "col → ref_table.ref_col"
// CONDITIONAL: "col when other_col op val"
// Legacy (no scope field): falls back to column_name
function RuleScopeTarget({ rule, onFilterColumn }) {
  const { scope, target_config: tc, column_name } = rule
  if (!scope || scope === 'COLUMN') {
    const col = tc?.column || column_name
    if (!col) return <span className="muted-cell">—</span>
    return (
      <button className="table-filter-link" title="Click to filter by this column" onClick={() => onFilterColumn(col)}>
        {col}
      </button>
    )
  }
  if (scope === 'MULTI_COLUMN') {
    const cols = tc?.columns || []
    return <span className="scope-target-multi">{cols.join(', ') || '—'}</span>
  }
  if (scope === 'TABLE') {
    return <span className="muted-cell">table-level</span>
  }
  if (scope === 'CROSS_TABLE') {
    const ref = [tc?.ref_table, tc?.ref_column].filter(Boolean).join('.')
    return <span className="scope-target-cross">{tc?.column || column_name}{ref ? ` → ${ref}` : ''}</span>
  }
  if (scope === 'CONDITIONAL') {
    return (
      <span className="scope-target-cond" title={`when ${tc?.when_column} ${tc?.when_operator} ${tc?.when_value}`}>
        {tc?.column || column_name}
        <span className="scope-target-cond-hint"> (conditional)</span>
      </span>
    )
  }
  return <span className="muted-cell">{column_name || '—'}</span>
}

function SortableHeader({ label, sortKey, activeSortKey, sortDir, onSort }) {
  const active = activeSortKey === sortKey
  return (
    <th>
      <button className={`col-sort-btn${active ? ' active' : ''}`} onClick={() => onSort(sortKey)}>
        {label}
        <span className="col-sort-icon">
          {active ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}
        </span>
      </button>
    </th>
  )
}

function RecommendedRulesPage({ tableErrors, tableClassification }) {
  // Scan context lives in the URL (?scan_id=... repeated for multi-table scans).
  // Navigating away and back preserves it; clicking "Recommended" in the sidebar
  // with no query params naturally shows the all-rules view.
  const location = useLocation()
  const urlScanIds = new URLSearchParams(location.search).getAll('scan_id')
  const scanIds = urlScanIds.length > 1 ? urlScanIds : null
  const scanId  = urlScanIds.length === 1 ? urlScanIds[0] : null
  const [rules, setRules] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [expandedRuleId, setExpandedRuleId] = useState(null)
  const [expandedMode, setExpandedMode] = useState(null) // 'details' | 'edit'
  const [actioningRuleId, setActioningRuleId] = useState(null)
  const [sortKey, setSortKey] = useState(null)   // 'confidence' | 'priority' | 'failed_count'
  const [sortDir, setSortDir] = useState('desc') // 'asc' | 'desc'

  // Groups (docs/rules-architecture.md §6.4): display/approval convenience
  // only, no effect on execution. groupsById is fetched once per loadRules()
  // call (unscoped -- the page doesn't reliably know one database/schema/
  // table to filter by in every mode, e.g. the no-scan summary landing) and
  // keyed by group_id for O(1) lookup per rule row. expandedGroupIds is a
  // Set of group_ids currently expanded to show member rows.
  const [groupsById, setGroupsById] = useState({})
  const [expandedGroupIds, setExpandedGroupIds] = useState(() => new Set())
  const [groupActioningId, setGroupActioningId] = useState(null)

  function toggleGroupExpanded(groupId) {
    setExpandedGroupIds((prev) => {
      const next = new Set(prev)
      if (next.has(groupId)) next.delete(groupId)
      else next.add(groupId)
      return next
    })
  }

  function bulkApprove(groupId) {
    setGroupActioningId(groupId)
    setError(null)
    fetch(`${API_BASE}/api/rules/bulk-approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_id: groupId }),
    })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then((result) => {
        if (result.skipped && result.skipped.length > 0) {
          setError(
            `${result.approved.length} approved, ${result.skipped.length} skipped ` +
            `(no valid SQL yet): ${result.skipped.map((s) => s.reason).join('; ')}`
          )
        }
        loadRules({ silent: true })
      })
      .catch((err) => setError(err.message))
      .finally(() => setGroupActioningId(null))
  }

  function bulkReject(groupId) {
    const reason = window.prompt('Rejection reason for the whole group (optional):') || null
    setGroupActioningId(groupId)
    setError(null)
    fetch(`${API_BASE}/api/rules/bulk-reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_id: groupId, reason }),
    })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then(() => loadRules({ silent: true }))
      .catch((err) => setError(err.message))
      .finally(() => setGroupActioningId(null))
  }

  function toggleSort(key) {
    if (sortKey === key) {
      if (sortDir === 'desc') {
        setSortDir('asc')
      } else {
        setSortKey(null)
        setSortDir('desc')
      }
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }
  // Full-detail cache for the no-scan landing view: that path loads a
  // lightweight summary (see loadRules) which omits generated_sql / evidence /
  // threshold_config / test_result, so those are lazy-fetched per rule the
  // first time a row is expanded for Details or Edit.
  const [detailCache, setDetailCache] = useState({})
  const [detailLoadingId, setDetailLoadingId] = useState(null)

  // Filters. Every per-column filter is a single-value string ('' = all) so
  // it maps directly onto a header-cell <select>. Cascading location filters
  // (database -> schema -> table -> column) each scope the options below them.
  const [search, setSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [filterSeverity, setFilterSeverity] = useState('')
  const [filterTestStatus, setFilterTestStatus] = useState('')
  const [filterSource, setFilterSource] = useState('')       // 'claude' | 'template' | ''
  const [filterRuleType, setFilterRuleType] = useState('')
  const [filterDatabase, setFilterDatabase] = useState('')
  const [filterSchema, setFilterSchema] = useState('')
  const [filterTable, setFilterTable] = useState('')
  const [filterColumn, setFilterColumn] = useState('')
  const [filterScope, setFilterScope] = useState('')

  // Selecting a level clears everything below it, so the cascade never shows
  // a stale child selection that doesn't belong to the new parent.
  function selectDatabase(v) { setFilterDatabase(v); setFilterSchema(''); setFilterTable(''); setFilterColumn('') }
  function selectSchema(v) { setFilterSchema(v); setFilterTable(''); setFilterColumn('') }
  function selectTable(v) { setFilterTable(v); setFilterColumn('') }
  function clearFilters() {
    setSearch(''); setFilterStatus(''); setFilterSeverity('')
    setFilterTestStatus(''); setFilterSource(''); setFilterRuleType('')
    setFilterDatabase(''); setFilterSchema(''); setFilterTable(''); setFilterColumn('')
    setFilterScope('')
  }
  const hasActiveFilters = search || filterStatus || filterSeverity ||
    filterTestStatus || filterSource || filterRuleType ||
    filterDatabase || filterSchema || filterTable || filterColumn || filterScope

  // Schema scan: one scan_id per table (no parent/child scan grouping
  // column exists in storage -- see main.py's recommend_rules_for_schema())
  // so this page fetches every table's scan and concatenates the rules.
  // Single-table scan (the original flow): just scanId, or no scan at all
  // (unfiltered "all recommendations" -- see deferred-and-future-work.md
  // #21 for why that path is slow/fragile at real data volume).
  const ids = scanIds && scanIds.length > 0 ? scanIds : scanId ? [scanId] : null

  // No-scan landing view ("Recommended" in the sidebar with no scan in hand):
  // the unfiltered GET /api/rules/recommended returns full rows across every
  // scan and hangs 60-80s then 500s at real data volume behind this network's
  // proxy TLS (docs/deferred-and-future-work.md #21) -- which is why the page
  // used to sit on "Loading..." forever. Use the lightweight summary endpoint
  // instead; full per-rule detail is lazy-loaded on expand (see loadRuleDetail).
  // Always summary mode — the full /api/rules/recommended endpoint returns
  // large VARIANT payloads that trigger Snowflake S3 streaming and timeout
  // behind the corporate proxy. Detail is lazy-loaded on expand.
  const summaryMode = true

  // silent=true (used after approve/reject/bulk-approve/bulk-reject) refetches
  // without blanking the table -- rows stay on screen and are swapped for
  // fresh ones once the response lands, so deciding on several rules in a
  // row doesn't feel like repeated full-page reloads. A real dataset change
  // (mount, navigating to a different scan) still shows the loading state.
  function loadRules({ silent = false } = {}) {
    if (!silent) setLoading(true)
    setError(null)
    if (!silent) setDetailCache({})
    const fetches = ids
      ? ids.map((id) => fetch(`${API_BASE}/api/rules/recommended/summary?scan_id=${id}`).then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          return res.json()
        }))
      : [fetch(`${API_BASE}/api/rules/recommended/summary`).then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          return res.json()
        })]
    Promise.all(fetches)
      .then((results) => setRules(results.flat()))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))

    // Groups are fetched unscoped -- this page has no single database/
    // schema/table it can reliably filter by across every mode (schema
    // scan, no-scan summary landing, etc.) -- and merged client-side by
    // suggested_group_id. A failure here degrades gracefully: rows with a
    // suggested_group_id just render ungrouped rather than blocking the
    // whole page (groups are a display convenience, never load-bearing).
    fetch(`${API_BASE}/api/rules/groups`)
      .then((res) => (res.ok ? res.json() : []))
      .then((groups) => setGroupsById(Object.fromEntries(groups.map((g) => [g.group_id, g]))))
      .catch(() => setGroupsById({}))
  }

  // Fetch one rule's full detail (SQL/evidence/threshold/test_result) on
  // demand for the summary view's Details/Edit expansion, then merge it into
  // the row so the detail panels and the edit form have every field.
  function loadRuleDetail(ruleId) {
    if (detailCache[ruleId]) return Promise.resolve(detailCache[ruleId])
    setDetailLoadingId(ruleId)
    return fetch(`${API_BASE}/api/rules/recommended/${ruleId}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((full) => {
        setDetailCache((c) => ({ ...c, [ruleId]: full }))
        setRules((rs) => rs.map((r) => (r.rule_id === ruleId ? { ...r, ...full } : r)))
        return full
      })
      .catch((err) => setError(err.message))
      .finally(() => setDetailLoadingId(null))
  }

  // Use the raw URL search string as the dependency — scanIds is a new array
  // every render so using it directly would cause an infinite reload loop.
  useEffect(() => {
    loadRules()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.search])

  function closeExpanded() {
    setExpandedRuleId(null)
    setExpandedMode(null)
  }

  function approve(ruleId) {
    setActioningRuleId(ruleId)
    setError(null)
    fetch(`${API_BASE}/api/rules/${ruleId}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then(() => loadRules({ silent: true }))
      .catch((err) => setError(err.message))
      .finally(() => setActioningRuleId(null))
  }

  function reject(ruleId) {
    const reason = window.prompt('Rejection reason (optional):') || null
    setActioningRuleId(ruleId)
    setError(null)
    fetch(`${API_BASE}/api/rules/${ruleId}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then(() => loadRules({ silent: true }))
      .catch((err) => setError(err.message))
      .finally(() => setActioningRuleId(null))
  }

  function onEditSaved(updatedRule) {
    setRules((rs) => rs.map((r) => (r.rule_id === updatedRule.rule_id ? updatedRule : r)))
    closeExpanded()
  }

  if (loading) return <p className="muted">Loading recommended rules...</p>

  const headerSuffix =
    scanIds && scanIds.length > 0
      ? ` — schema scan (${scanIds.length} table${scanIds.length === 1 ? '' : 's'})`
      : scanId
      ? ` — scan ${scanId.slice(0, 8)}`
      : ''

  // Derive filter option sets from loaded rules.
  const uniqSorted = (fn, source = rules) => [...new Set(source.map(fn).filter(Boolean))].sort()
  // Cascade: each level's dropdown lists only values reachable given the
  // levels selected above it.
  const databases = uniqSorted((r) => r.database_name)
  const schemas = uniqSorted(
    (r) => r.schema_name,
    rules.filter((r) => !filterDatabase || r.database_name === filterDatabase)
  )
  const tables = uniqSorted(
    (r) => r.table_name,
    rules.filter((r) =>
      (!filterDatabase || r.database_name === filterDatabase) &&
      (!filterSchema || r.schema_name === filterSchema))
  )
  const columns = uniqSorted(
    (r) => r.column_name,
    rules.filter((r) =>
      (!filterDatabase || r.database_name === filterDatabase) &&
      (!filterSchema || r.schema_name === filterSchema) &&
      (!filterTable || r.table_name === filterTable))
  )
  // Per-column dropdown option sets, derived from the loaded rules.
  const ruleTypes = uniqSorted((r) => r.rule_type)
  const severities = ['CRITICAL', 'WARNING', 'INFO'].filter((s) => rules.some((r) => r.severity === s))
  const testStatuses = ['PASSED', 'FAILED', 'ERROR', 'PENDING'].filter((s) => rules.some((r) => r.test_status === s))
  const statuses = ['PENDING', 'APPROVED', 'REJECTED'].filter((s) => rules.some((r) => r.approval_status === s))
  const sourceLabelOf = (r) =>
    r.rule_fingerprint === 'source:governance' ? 'Governance'
      : r.rule_fingerprint === 'source:claude' ? 'Claude' : 'Template'
  const sources = uniqSorted(sourceLabelOf)
  const scopes = ['COLUMN', 'MULTI_COLUMN', 'TABLE', 'CROSS_TABLE', 'CONDITIONAL'].filter((s) =>
    rules.some((r) => r.scope === s)
  )

  const filteredRules = rules.filter((rule) => {
    if (search && !rule.rule_name?.toLowerCase().includes(search.toLowerCase()) &&
        !rule.rule_type?.toLowerCase().includes(search.toLowerCase())) return false
    if (filterStatus && rule.approval_status !== filterStatus) return false
    if (filterSeverity && rule.severity !== filterSeverity) return false
    if (filterTestStatus && rule.test_status !== filterTestStatus) return false
    if (filterSource && sourceLabelOf(rule) !== filterSource) return false
    if (filterRuleType && rule.rule_type !== filterRuleType) return false
    if (filterDatabase && rule.database_name !== filterDatabase) return false
    if (filterSchema && rule.schema_name !== filterSchema) return false
    if (filterTable && rule.table_name !== filterTable) return false
    if (filterColumn && rule.column_name !== filterColumn) return false
    if (filterScope && rule.scope !== filterScope) return false
    return true
  })
  const visibleRules = sortKey
    ? [...filteredRules].sort((a, b) => {
        const av = a[sortKey] ?? -Infinity
        const bv = b[sortKey] ?? -Infinity
        return sortDir === 'asc' ? av - bv : bv - av
      })
    : filteredRules

  // Partition into grouped (suggested_group_id truthy AND the group is one
  // this page knows about via groupsById) vs. ungrouped (§6.4: "Ungrouped
  // view: Individual instances exactly as today"). A rule whose
  // suggested_group_id doesn't resolve to a fetched group (e.g. the groups
  // fetch failed, or the group belongs to a table this page's filters have
  // hidden) falls back to rendering ungrouped rather than disappearing.
  const groupOrder = []
  const groupMembers = {}
  const ungroupedRules = []
  for (const rule of visibleRules) {
    const groupId = rule.suggested_group_id
    if (groupId && groupsById[groupId]) {
      if (!groupMembers[groupId]) {
        groupMembers[groupId] = []
        groupOrder.push(groupId)
      }
      groupMembers[groupId].push(rule)
    } else {
      ungroupedRules.push(rule)
    }
  }

  // One row-rendering path shared by ungrouped rows and expanded group
  // members (§6.4: bulk approve is equivalent to individually approving
  // each, not a replacement for the per-instance controls -- so a group
  // member keeps its own Approve/Reject/Edit/Details exactly like an
  // ungrouped row).
  function renderRuleRow(rule) {
    const isPending = rule.approval_status === 'PENDING'
    const isExpandedDetails = expandedRuleId === rule.rule_id && expandedMode === 'details'
    const isExpandedEdit = expandedRuleId === rule.rule_id && expandedMode === 'edit'
    const isNew = ids && ids.includes(rule.scan_id)
    return (
      <Fragment key={rule.rule_id}>
        <tr className={!isPending ? 'rule-decided' : isNew ? 'rule-new' : ''}>
          <td>
            {isNew && <span className="new-badge">new</span>}
            {rule.is_new_definition && (
              <span className="new-def-badge" title="Approving will create a new rule type in the library">new type</span>
            )}
            {rule.rule_name}
          </td>
          <td>
            <button
              className="table-filter-link"
              title="Click to filter by this table"
              onClick={() => selectTable(rule.table_name)}
            >
              {rule.table_name}
            </button>
          </td>
          <td>
            <RuleScopeTarget rule={rule} onFilterColumn={setFilterColumn} />
          </td>
          <td>
            <button
              className="table-filter-link"
              title="Click to filter by this rule type"
              onClick={() => setFilterRuleType(rule.rule_type)}
            >
              {rule.rule_type}
            </button>
          </td>
          <td>
            {(() => {
              const fp = rule.rule_fingerprint
              const variant = fp === 'source:governance' ? 'governance' : fp === 'source:claude' ? 'claude' : 'template'
              const label = fp === 'source:governance' ? 'Governance' : fp === 'source:claude' ? 'Claude' : 'Template'
              return (
                <span
                  className={`source-badge source-${variant} badge-filterable`}
                  title="Click to filter by source"
                  onClick={() => setFilterSource(label)}
                >{label}</span>
              )
            })()}
          </td>
          <td>
            <span
              className={`test-status-badge test-status-${rule.test_status?.toLowerCase()} badge-filterable`}
              title="Click to filter by test status"
              onClick={() => setFilterTestStatus(rule.test_status)}
            >
              {rule.test_status}
            </span>
          </td>
          <td>
            <span
              className={`severity-badge severity-${rule.severity?.toLowerCase()} badge-filterable`}
              title="Click to filter by severity"
              onClick={() => setFilterSeverity(rule.severity)}
            >
              {rule.severity}
            </span>
          </td>
          <td>{rule.confidence}</td>
          <td>{rule.priority}</td>
          <td>
            <span
              className={`approval-badge approval-${rule.approval_status?.toLowerCase()} badge-filterable`}
              title="Click to filter by status"
              onClick={() => setFilterStatus(rule.approval_status)}
            >
              {rule.approval_status}
            </span>
          </td>
          <td>{rule.failed_count ?? '—'}</td>
          <td className="muted" style={{ whiteSpace: 'nowrap', fontSize: 11 }}>{formatTimestamp(rule.created_at)}</td>
          <td className="col-sticky-right">
            <div className="rule-actions-cell">
              <button
                className="approve-button"
                disabled={!isPending || actioningRuleId === rule.rule_id}
                onClick={() => approve(rule.rule_id)}
                title={
                  rule.validation_status !== 'PASSED'
                    ? 'Rule has no valid SQL yet — edit it first'
                    : undefined
                }
              >
                Approve
              </button>
              <button
                className="reject-button"
                disabled={!isPending || actioningRuleId === rule.rule_id}
                onClick={() => reject(rule.rule_id)}
              >
                Reject
              </button>
              <button
                className="link-button"
                disabled={!isPending || detailLoadingId === rule.rule_id}
                onClick={() => {
                  // The edit form seeds its fields from the rule at
                  // mount, so in summary mode the full detail
                  // (SQL/threshold) must be merged in before the
                  // form opens -- otherwise it seeds from blanks.
                  if (summaryMode) {
                    loadRuleDetail(rule.rule_id).then(() => {
                      setExpandedRuleId(rule.rule_id)
                      setExpandedMode('edit')
                    })
                  } else {
                    setExpandedRuleId(rule.rule_id)
                    setExpandedMode('edit')
                  }
                }}
              >
                Edit
              </button>
              <button
                className="link-button"
                disabled={detailLoadingId === rule.rule_id}
                onClick={() => {
                  const next = isExpandedDetails ? null : 'details'
                  if (next && summaryMode) loadRuleDetail(rule.rule_id)
                  setExpandedRuleId(rule.rule_id)
                  setExpandedMode(next)
                }}
              >
                {isExpandedDetails
                  ? 'Hide'
                  : detailLoadingId === rule.rule_id
                  ? 'Loading…'
                  : 'Details'}
              </button>
            </div>
          </td>
        </tr>
        {isExpandedDetails && (
          <RuleDetailRow rule={rule} onCancel={closeExpanded} loading={detailLoadingId === rule.rule_id} />
        )}
        {isExpandedEdit && (
          <RuleEditForm
            rule={rule}
            onCancel={closeExpanded}
            onSaved={onEditSaved}
            setError={setError}
          />
        )}
      </Fragment>
    )
  }

  return (
    <div className="rules-page">
      {error && <div className="status status-error">{error}</div>}
      {tableErrors && tableErrors.length > 0 && (
        <div className="status status-error">
          {tableErrors.length} table{tableErrors.length === 1 ? '' : 's'} failed during the schema
          scan:
          <ul>
            {tableErrors.map((t) => (
              <li key={t.table_name}>
                {t.table_name}: {t.error}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="rules-page-header">
        <h2>Recommended Rules{headerSuffix}</h2>
        <button className="link-button" onClick={loadRules}>
          Refresh
        </button>
      </div>
      <ScanLogsPanel ids={ids} />
      {tableClassification && (
        <div className="classification-banner">
          <span className="classification-badge">
            Claude classified this table as:{' '}
            <strong className="classification-type">{tableClassification.table_type?.toUpperCase()}</strong>
            {tableClassification.confidence != null && (
              <span className="classification-confidence"> ({Math.round(tableClassification.confidence * 100)}% confident)</span>
            )}
            {tableClassification.reasoning && (
              <span className="classification-reasoning"> — {tableClassification.reasoning}</span>
            )}
          </span>
        </div>
      )}

      {!ids && rules.length > 0 && (
        <div className="rules-all-pending-banner">
          Showing all pending rules across all scans. Run a new scan from the Explorer to see scan-specific results.
        </div>
      )}
      {rules.length === 0 && !loading && <p className="muted">No recommended rules yet.</p>}
      {rules.length > 0 && (
        <>
          <div className="rules-filter-bar">
            <div className="rules-filter-search">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
              </svg>
              <input
                className="rules-search-input"
                placeholder="Search rules…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>

            <div className="rules-filter-divider" />

            {databases.length > 0 && (
              <div className="rules-filter-group">
                <span className="rules-filter-label">Database</span>
                <select
                  className="rules-filter-select"
                  value={filterDatabase}
                  onChange={(e) => selectDatabase(e.target.value)}
                >
                  <option value="">All databases</option>
                  {databases.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
              </div>
            )}

            {schemas.length > 0 && (
              <div className="rules-filter-group">
                <span className="rules-filter-label">Schema</span>
                <select
                  className="rules-filter-select"
                  value={filterSchema}
                  onChange={(e) => selectSchema(e.target.value)}
                >
                  <option value="">All schemas</option>
                  {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
            )}

            {tables.length > 0 && (
              <div className="rules-filter-group">
                <span className="rules-filter-label">Table</span>
                <select
                  className="rules-filter-select"
                  value={filterTable}
                  onChange={(e) => selectTable(e.target.value)}
                >
                  <option value="">All tables</option>
                  {tables.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
            )}

            {columns.length > 0 && (
              <div className="rules-filter-group">
                <span className="rules-filter-label">Column</span>
                <select
                  className="rules-filter-select"
                  value={filterColumn}
                  onChange={(e) => setFilterColumn(e.target.value)}
                >
                  <option value="">All columns</option>
                  {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
            )}

            <div className="rules-filter-summary">
              <span>{visibleRules.length} of {rules.length}</span>
              {hasActiveFilters && (
                <button className="rules-filter-clear" onClick={clearFilters}>Clear</button>
              )}
            </div>
          </div>

          {visibleRules.length === 0 ? (
            <div className="rules-empty-filtered">
              <p className="muted">No rules match the current filters.</p>
              <button className="link-button" onClick={clearFilters}>Clear filters</button>
            </div>
          ) : (
          <div className="table-card">
          <table className="rules-table">
            <thead>
              <tr>
                <th>Rule name</th>
                <ColumnFilterHeader label="Table" value={filterTable} options={tables} onChange={selectTable} />
                <ColumnFilterHeader label="Target" value={filterColumn} options={columns} onChange={setFilterColumn} />
                <ColumnFilterHeader label="Scope" value={filterScope} options={scopes} onChange={setFilterScope} />
                <ColumnFilterHeader label="Rule type" value={filterRuleType} options={ruleTypes} onChange={setFilterRuleType} />
                <ColumnFilterHeader label="Source" value={filterSource} options={sources} onChange={setFilterSource} />
                <ColumnFilterHeader label="Test" value={filterTestStatus} options={testStatuses} onChange={setFilterTestStatus} />
                <ColumnFilterHeader label="Severity" value={filterSeverity} options={severities} onChange={setFilterSeverity} />
                <SortableHeader label="Confidence" sortKey="confidence" activeSortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableHeader label="Priority" sortKey="priority" activeSortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <ColumnFilterHeader label="Status" value={filterStatus} options={statuses} onChange={setFilterStatus} />
                <SortableHeader label="Failed" sortKey="failed_count" activeSortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <th>Scanned</th>
                <th className="col-sticky-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {groupOrder.map((groupId) => {
                const group = groupsById[groupId]
                const members = groupMembers[groupId]
                const isExpanded = expandedGroupIds.has(groupId)
                const pendingMembers = members.filter((m) => m.approval_status === 'PENDING')
                const avgConfidence =
                  members.reduce((sum, m) => sum + (m.confidence || 0), 0) / members.length
                return (
                  <Fragment key={groupId}>
                    <tr className="rule-group-header-row">
                      <td colSpan={13}>
                        <div className="rule-group-header">
                          <button
                            className="rule-group-toggle"
                            onClick={() => toggleGroupExpanded(groupId)}
                            title={isExpanded ? 'Collapse group' : 'Expand group'}
                          >
                            <span className={`rule-group-chevron${isExpanded ? ' expanded' : ''}`}>▶</span>
                            <strong>{group.name}</strong>
                          </button>
                          <span className="rule-group-meta">
                            {members.length} instance{members.length === 1 ? '' : 's'} · avg confidence{' '}
                            {avgConfidence.toFixed(2)} · {group.scope_level}
                          </span>
                          <div className="rule-group-actions">
                            <button
                              className="approve-button"
                              disabled={pendingMembers.length === 0 || groupActioningId === groupId}
                              onClick={() => bulkApprove(groupId)}
                              title="Approve every pending instance in this group with valid SQL"
                            >
                              Approve all
                            </button>
                            <button
                              className="reject-button"
                              disabled={pendingMembers.length === 0 || groupActioningId === groupId}
                              onClick={() => bulkReject(groupId)}
                            >
                              Reject all
                            </button>
                          </div>
                        </div>
                      </td>
                    </tr>
                    {isExpanded && members.map((rule) => renderRuleRow(rule))}
                  </Fragment>
                )
              })}
              {ungroupedRules.map((rule) => renderRuleRow(rule))}
            </tbody>
          </table>
          </div>
          )}
        </>
      )}
    </div>
  )
}

function formatTimestamp(value) {
  if (!value) return 'Never run'
  // Snowflake TIMESTAMP_NTZ comes back as a naive ISO string (no zone) --
  // displayed as-is rather than re-interpreted through the browser's local
  // zone, since it's already the app DB's own clock.
  return value.replace('T', ' ').slice(0, 19)
}

function ActiveRulesPage({ initialTable }) {
  const [rules, setRules] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [runningRuleId, setRunningRuleId] = useState(null)
  const [runningAll, setRunningAll] = useState(false)
  const [runAllSummary, setRunAllSummary] = useState(null)
  const [filterTable, setFilterTable] = useState(initialTable || '')

  // silent=true (used after run/run-all) refetches without blanking the
  // table -- rows stay on screen and are swapped for fresh ones once the
  // response lands, so running several rules in a row doesn't feel like
  // repeated full-page reloads. Mount still shows the loading state, since
  // that's a genuinely first-time data fetch.
  function loadRules({ silent = false } = {}) {
    if (!silent) setLoading(true)
    setError(null)
    fetch(`${API_BASE}/api/rules/active`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setRules)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    loadRules()
  }, [])

  function runNow(ruleId) {
    setRunningRuleId(ruleId)
    setError(null)
    fetch(`${API_BASE}/api/rules/${ruleId}/run`, { method: 'POST' })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      // Re-fetch the whole list rather than patching one row in place --
      // last_run_status/failed_count/last_run_at all come from a join this
      // component doesn't otherwise have the pieces to reconstruct locally.
      .then(() => loadRules({ silent: true }))
      .catch((err) => setError(err.message))
      .finally(() => setRunningRuleId(null))
  }

  function runAll() {
    setRunningAll(true)
    setRunAllSummary(null)
    setError(null)
    fetch(`${API_BASE}/api/rules/run-all`, { method: 'POST' })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then((data) => {
        const counts = { PASSED: 0, FAILED: 0, ERROR: 0, SKIPPED: 0 }
        data.results.forEach((r) => {
          counts[r.status] = (counts[r.status] || 0) + 1
        })
        setRunAllSummary({ total: data.results.length, counts })
        loadRules({ silent: true })
      })
      .catch((err) => setError(err.message))
      .finally(() => setRunningAll(false))
  }

  if (loading) return <p className="muted">Loading active rules...</p>

  return (
    <div className="rules-page">
      {error && <div className="status status-error">{error}</div>}
      <div className="rules-page-header">
        <h2>Active Rules</h2>
        <div className="rules-page-header-actions">
          <button className="approve-button" onClick={runAll} disabled={runningAll || rules.length === 0}>
            {runningAll ? 'Running rules...' : 'Run Rules'}
          </button>
          <button className="link-button" onClick={loadRules}>
            Refresh
          </button>
        </div>
      </div>
      {runAllSummary && (
        <div className="status status-ok">
          Ran {runAllSummary.total} rule{runAllSummary.total === 1 ? '' : 's'}: {runAllSummary.counts.PASSED || 0}{' '}
          passed, {runAllSummary.counts.FAILED || 0} failed, {runAllSummary.counts.ERROR || 0} error,{' '}
          {runAllSummary.counts.SKIPPED || 0} skipped.
        </div>
      )}
      {rules.length === 0 && <p className="muted">No approved rules yet.</p>}
      {rules.length > 0 && (() => {
        const tables = [...new Set(rules.map(r => r.table_name).filter(Boolean))].sort()
        const visible = filterTable ? rules.filter(r => r.table_name === filterTable) : rules
        return (<>
        {filterTable && (
          <div className="rules-filter-bar">
            <span className="rules-filter-label">Filtered to table:</span>
            <strong style={{fontSize:13}}>{filterTable}</strong>
            <button className="rules-filter-clear" onClick={() => setFilterTable('')}>Clear</button>
            <div className="rules-filter-summary"><span>{visible.length} of {rules.length}</span></div>
          </div>
        )}
        <div className="table-card">
        <table className="rules-table">
          <thead>
            <tr>
              <th>Rule name</th>
              <ColumnFilterHeader label="Table" value={filterTable} options={tables} onChange={setFilterTable} />
              <th>Column</th>
              <th>Active</th>
              <th>Severity</th>
              <th>Rule SQL</th>
              <th>Schedule</th>
              <th>Last run status</th>
              <th>Last run</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((rule) => (
              <tr key={rule.rule_id} className={!rule.is_active ? 'rule-decided' : ''}>
                <td>{rule.rule_name}</td>
                <td>{rule.table_name}</td>
                <td>{rule.column_name || '—'}</td>
                <td>
                  <span className={`active-badge active-${rule.is_active ? 'yes' : 'no'}`}>
                    {rule.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                <td>
                  <span className={`severity-badge severity-${rule.severity?.toLowerCase()}`}>
                    {rule.severity}
                  </span>
                </td>
                <td>
                  <pre className="rule-sql rule-sql-compact">{rule.rule_sql}</pre>
                </td>
                <td className="muted">
                  {/* Scheduling is MVP2 (mvp-scope.md) -- schedule_config exists on
                      the table/API but nothing ever sets it yet, so this is always
                      the placeholder branch today. */}
                  {rule.schedule_config ? JSON.stringify(rule.schedule_config) : 'Manual only (MVP1)'}
                </td>
                <td>
                  {rule.last_run_status ? (
                    <>
                      <span
                        className={`test-status-badge test-status-${rule.last_run_status.toLowerCase()}`}
                      >
                        {rule.last_run_status}
                      </span>
                      {rule.last_run_failed_count != null && (
                        <span className="muted">
                          {' '}
                          ({rule.last_run_failed_count}/{rule.last_run_total_count})
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="test-status-badge test-status-pending">Never run</span>
                  )}
                </td>
                <td className="muted">{formatTimestamp(rule.last_run_at)}</td>
                <td>
                  <button
                    className="approve-button"
                    disabled={runningRuleId === rule.rule_id}
                    onClick={() => runNow(rule.rule_id)}
                  >
                    {runningRuleId === rule.rule_id ? 'Running...' : 'Run now'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        </>)
      })()}
    </div>
  )
}

// ─── Rule Library Page ────────────────────────────────────────────────────────

function RuleLibraryPage() {
  const [definitions, setDefinitions] = useState([])
  const [groups, setGroups] = useState([])
  const [instances, setInstances] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [expandedDefId, setExpandedDefId] = useState(null)
  const [expandedGroupId, setExpandedGroupId] = useState(null)
  const [defFilter, setDefFilter] = useState('')   // search definitions
  const [statusFilter, setStatusFilter] = useState('')  // ACTIVE | DISABLED | ''
  const [togglingDefId, setTogglingDefId] = useState(null)

  // silent=true (used after toggling a definition's status) refetches
  // without blanking the grid -- existing cards stay on screen and are
  // swapped for fresh ones once the response lands, same pattern as
  // RecommendedRulesPage/ActiveRulesPage/SettingsPage.
  function load({ silent = false } = {}) {
    if (!silent) setLoading(true)
    setError(null)
    Promise.all([
      fetch(`${API_BASE}/api/rules/definitions`).then(r => r.json()),
      fetch(`${API_BASE}/api/rules/groups`).then(r => r.json()),
      fetch(`${API_BASE}/api/rules/active`).then(r => r.json()),
    ])
      .then(([defs, grps, inst]) => {
        setDefinitions(Array.isArray(defs) ? defs : [])
        setGroups(Array.isArray(grps) ? grps : [])
        setInstances(Array.isArray(inst) ? inst : [])
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  // Disable/re-enable a definition (docs/rules-architecture.md §6.3): a
  // DISABLED definition is excluded from list_rule_definitions(status="ACTIVE"),
  // which is the exact call agents/rule_recommendation_agent.py uses to
  // build both the deterministic-skill lookup table and Claude's library
  // context -- so disabling here genuinely stops future scans from
  // proposing new instances of this check, on this or any table. Existing
  // already-approved RULE_INSTANCES keep running either way; this only
  // affects what gets *suggested* going forward.
  function toggleDefinitionStatus(def) {
    const nextStatus = def.status === 'DISABLED' ? 'ACTIVE' : 'DISABLED'
    setTogglingDefId(def.definition_id)
    setError(null)
    fetch(`${API_BASE}/api/rules/definitions/${def.definition_id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: nextStatus }),
    })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then(() => load({ silent: true }))
      .catch((err) => setError(err.message))
      .finally(() => setTogglingDefId(null))
  }

  if (loading) return <p className="muted">Loading rule library…</p>

  const visibleDefs = definitions.filter(d => {
    if (statusFilter && d.status !== statusFilter) return false
    if (defFilter && !d.name.toLowerCase().includes(defFilter.toLowerCase()) &&
        !d.category?.toLowerCase().includes(defFilter.toLowerCase())) return false
    return true
  })

  // Group instances by definition_id for the per-definition count
  const instancesByDef = {}
  for (const inst of instances) {
    if (inst.definition_id) {
      instancesByDef[inst.definition_id] = (instancesByDef[inst.definition_id] || 0) + 1
    }
  }

  const sourceVariant = s => s === 'SYSTEM' ? 'template' : s === 'CLAUDE' ? 'claude' : 'governance'

  return (
    <div className="lib-page">
      {error && <div className="status status-error">{error}</div>}

      {/* ── Layer 1: Definitions ── */}
      <section className="lib-section">
        <div className="lib-section-header">
          <div className="lib-layer-tag">Layer 1</div>
          <h3>Rule Definitions</h3>
          <span className="lib-count">{definitions.length} definitions</span>
          <div className="lib-section-controls">
            <div className="rules-filter-search" style={{minWidth: 200}}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
              </svg>
              <input className="rules-search-input" placeholder="Search definitions…" value={defFilter} onChange={e => setDefFilter(e.target.value)} />
            </div>
            <select className="rules-filter-select" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
              <option value="">All statuses</option>
              <option value="ACTIVE">Active</option>
              <option value="DISABLED">Disabled</option>
            </select>
          </div>
        </div>
        <div className="lib-def-grid">
          {visibleDefs.map(def => {
            const isOpen = expandedDefId === def.definition_id
            const liveCount = instancesByDef[def.definition_id] || 0
            return (
              <div key={def.definition_id} className={`lib-def-card${def.status === 'DISABLED' ? ' disabled' : ''}`}>
                <div className="lib-def-card-header" onClick={() => setExpandedDefId(isOpen ? null : def.definition_id)}>
                  <div className="lib-def-card-title">
                    <span className={`source-badge source-${sourceVariant(def.source)}`}>{def.source}</span>
                    <strong>{def.name}</strong>
                    {def.status === 'DISABLED' && <span className="lib-disabled-tag">disabled</span>}
                  </div>
                  <div className="lib-def-card-meta">
                    <span className="lib-meta-chip">{def.category}</span>
                    {def.allowed_scopes?.map(s => (
                      <span key={s} className={`scope-badge scope-${s.toLowerCase().replace('_', '-')}`}>{s}</span>
                    ))}
                    <span className="lib-meta-stat">{liveCount} live · {def.approval_count ?? 0} approved</span>
                    <span className="lib-def-chevron">{isOpen ? '▲' : '▼'}</span>
                  </div>
                </div>
                {isOpen && (
                  <div className="lib-def-card-body">
                    {def.description && <p className="lib-def-desc">{def.description}</p>}
                    {def.check_logic && (
                      <div className="lib-def-row"><span className="detail-label">Check logic:</span> <span>{def.check_logic}</span></div>
                    )}
                    {def.default_severity && (
                      <div className="lib-def-row"><span className="detail-label">Default severity:</span> <span className={`severity-badge severity-${def.default_severity.toLowerCase()}`}>{def.default_severity}</span></div>
                    )}
                    {def.default_threshold_config && (
                      <div className="lib-def-row"><span className="detail-label">Default threshold:</span> <code className="lib-inline-code">{JSON.stringify(def.default_threshold_config)}</code></div>
                    )}
                    {def.sql_template && (
                      <div className="lib-def-sql-block">
                        <span className="detail-label">SQL template</span>
                        <pre className="rule-sql">{def.sql_template}</pre>
                      </div>
                    )}
                    {def.created_at && (
                      <div className="lib-def-row lib-def-footer-row">
                        <span className="muted">Created {formatTimestamp(def.created_at)}{def.created_by ? ` by ${def.created_by}` : ''}</span>
                        <span className="muted lib-def-id">ID: {def.definition_id.slice(0, 8)}…</span>
                      </div>
                    )}
                    <div className="lib-def-row lib-def-action-row">
                      <button
                        className={def.status === 'DISABLED' ? 'approve-button' : 'reject-button'}
                        disabled={togglingDefId === def.definition_id}
                        onClick={(e) => {
                          e.stopPropagation()
                          toggleDefinitionStatus(def)
                        }}
                        title={
                          def.status === 'DISABLED'
                            ? 'Re-enable — future scans will start suggesting this check again'
                            : 'Disable — future scans will stop suggesting this check on any table. Already-approved instances keep running.'
                        }
                      >
                        {togglingDefId === def.definition_id
                          ? (def.status === 'DISABLED' ? 'Enabling…' : 'Disabling…')
                          : (def.status === 'DISABLED' ? 'Re-enable' : 'Disable')}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
          {visibleDefs.length === 0 && <p className="muted">No definitions match.</p>}
        </div>
      </section>

      {/* ── Layer 2a: Groups ── */}
      {groups.length > 0 && (
        <section className="lib-section">
          <div className="lib-section-header">
            <div className="lib-layer-tag">Layer 2</div>
            <h3>Rule Groups</h3>
            <span className="lib-count">{groups.length} groups</span>
          </div>
          <div className="table-card">
            <table className="rules-table">
              <thead>
                <tr>
                  <th>Group name</th>
                  <th>Definition</th>
                  <th>Scope level</th>
                  <th>Database</th>
                  <th>Schema</th>
                  <th>Table</th>
                  <th>Created</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {groups.map(group => {
                  const def = definitions.find(d => d.definition_id === group.definition_id)
                  const isOpen = expandedGroupId === group.group_id
                  return (
                    <Fragment key={group.group_id}>
                      <tr>
                        <td><strong>{group.name}</strong>{group.description && <div className="lib-group-desc">{group.description}</div>}</td>
                        <td>{def ? <span className="lib-def-link">{def.name}</span> : <span className="muted">{group.definition_id?.slice(0, 8)}…</span>}</td>
                        <td><span className={`scope-badge scope-${group.scope_level?.toLowerCase().replace('_', '-')}`}>{group.scope_level}</span></td>
                        <td className="muted">{group.database_name}</td>
                        <td className="muted">{group.schema_name}</td>
                        <td className="muted">{group.table_name || '—'}</td>
                        <td className="muted" style={{whiteSpace:'nowrap',fontSize:11}}>{formatTimestamp(group.created_at)}</td>
                        <td>
                          <button className="link-button" onClick={() => setExpandedGroupId(isOpen ? null : group.group_id)}>
                            {isOpen ? 'Hide' : 'Instances'}
                          </button>
                        </td>
                      </tr>
                      {isOpen && <GroupInstancesRow groupId={group.group_id} colSpan={8} />}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* ── Layer 2b: All Instances (Active Rules) ── */}
      <section className="lib-section">
        <div className="lib-section-header">
          <div className="lib-layer-tag">Layer 2</div>
          <h3>Rule Instances</h3>
          <span className="lib-count">{instances.length} active instances</span>
        </div>
        {instances.length === 0
          ? <p className="muted">No active rule instances yet.</p>
          : (
          <div className="table-card">
            <table className="rules-table">
              <thead>
                <tr>
                  <th>Rule name</th>
                  <th>Scope</th>
                  <th>Target</th>
                  <th>Table</th>
                  <th>Severity</th>
                  <th>Last run</th>
                  <th>Last status</th>
                  <th>Failed</th>
                  <th>Approved</th>
                </tr>
              </thead>
              <tbody>
                {instances.map(inst => (
                  <tr key={inst.rule_id} className={inst.is_active ? '' : 'rule-decided'}>
                    <td>
                      {!inst.is_active && <span className="lib-inactive-tag">inactive</span>}
                      {inst.rule_name}
                    </td>
                    <td>{inst.scope
                      ? <span className={`scope-badge scope-${inst.scope.toLowerCase().replace('_', '-')}`}>{inst.scope}</span>
                      : '—'}
                    </td>
                    <td><RuleScopeTarget rule={inst} onFilterColumn={() => {}} /></td>
                    <td className="muted">{inst.table_name}</td>
                    <td><span className={`severity-badge severity-${inst.severity?.toLowerCase()}`}>{inst.severity}</span></td>
                    <td className="muted" style={{whiteSpace:'nowrap',fontSize:11}}>{inst.last_run_at ? formatTimestamp(inst.last_run_at) : '—'}</td>
                    <td>{inst.last_run_status
                      ? <span className={`test-status-badge test-status-${inst.last_run_status.toLowerCase()}`}>{inst.last_run_status}</span>
                      : '—'}
                    </td>
                    <td>{inst.last_run_failed_count ?? '—'}</td>
                    <td className="muted" style={{whiteSpace:'nowrap',fontSize:11}}>{formatTimestamp(inst.approved_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}

function GroupInstancesRow({ groupId, colSpan }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch(`${API_BASE}/api/rules/groups/${groupId}/instances`)
      .then(r => r.json())
      .then(setData)
      .catch(() => setData({ pending: [], approved: [] }))
      .finally(() => setLoading(false))
  }, [groupId])

  return (
    <tr className="rule-detail-row">
      <td colSpan={colSpan}>
        <div className="rule-detail lib-group-instances">
          {loading ? (
            <div className="rule-detail-loading"><span className="rule-detail-spinner" /> Loading instances…</div>
          ) : (
            <>
              {data?.pending?.length > 0 && (
                <div>
                  <p className="detail-label">Pending ({data.pending.length})</p>
                  <ul className="lib-instance-list">
                    {data.pending.map(r => <li key={r.rule_id}><span className="approval-badge approval-pending">PENDING</span> {r.rule_name} — {r.table_name}</li>)}
                  </ul>
                </div>
              )}
              {data?.approved?.length > 0 && (
                <div>
                  <p className="detail-label">Approved ({data.approved.length})</p>
                  <ul className="lib-instance-list">
                    {data.approved.map(r => <li key={r.rule_id}><span className="approval-badge approval-approved">APPROVED</span> {r.rule_name} — {r.table_name}</li>)}
                  </ul>
                </div>
              )}
              {!data?.pending?.length && !data?.approved?.length && <p className="muted">No instances.</p>}
            </>
          )}
        </div>
      </td>
    </tr>
  )
}

// ─── Scans Page ───────────────────────────────────────────────────────────────

function ScansPage() {
  const [scans, setScans] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [openScanId, setOpenScanId] = useState(null)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')

  function load() {
    setLoading(true)
    setError(null)
    fetch(`${API_BASE}/api/scans`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(data => setScans(Array.isArray(data) ? data : []))
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  if (loading) return <p className="muted">Loading scans…</p>

  const statuses = [...new Set(scans.map(s => s.status).filter(Boolean))].sort()
  const visible = scans.filter(s => {
    if (statusFilter && s.status !== statusFilter) return false
    if (search) {
      const q = search.toLowerCase()
      return (s.scan_name || '').toLowerCase().includes(q) ||
             (s.target_table || '').toLowerCase().includes(q) ||
             (s.target_schema || '').toLowerCase().includes(q) ||
             (s.target_database || '').toLowerCase().includes(q)
    }
    return true
  })

  const statusVariant = s => {
    if (s === 'COMPLETED') return 'passed'
    if (s === 'FAILED') return 'failed'
    if (s === 'RUNNING') return 'running'
    return 'pending'
  }

  return (
    <div className="scans-page">
      {error && <div className="status status-error">{error}</div>}
      <div className="rules-page-header">
        <span className="lib-count">{scans.length} scans</span>
        <button className="link-button" onClick={load}>Refresh</button>
      </div>

      <div className="rules-filter-bar">
        <div className="rules-filter-search">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          <input className="rules-search-input" placeholder="Search by name, table, schema…" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div className="rules-filter-divider" />
        {statuses.length > 0 && (
          <div className="rules-filter-group">
            <span className="rules-filter-label">Status</span>
            <select className="rules-filter-select" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
              <option value="">All</option>
              {statuses.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        )}
        <div className="rules-filter-summary">
          <span>{visible.length} of {scans.length}</span>
          {(search || statusFilter) && <button className="rules-filter-clear" onClick={() => { setSearch(''); setStatusFilter('') }}>Clear</button>}
        </div>
      </div>

      {visible.length === 0
        ? <p className="muted">No scans match.</p>
        : (
        <div className="table-card">
          <table className="rules-table">
            <thead>
              <tr>
                <th>Scan name</th>
                <th>Status</th>
                <th>Database</th>
                <th>Schema</th>
                <th>Table</th>
                <th>Progress</th>
                <th>Started</th>
                <th>Ended</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visible.map(scan => {
                const isOpen = openScanId === scan.scan_id
                return (
                  <Fragment key={scan.scan_id}>
                    <tr>
                      <td>
                        <div className="scan-name">{scan.scan_name || scan.scan_id.slice(0, 8)}</div>
                        <div className="scan-id-sub">{scan.scan_id.slice(0, 8)}…</div>
                      </td>
                      <td>
                        <span className={`test-status-badge test-status-${statusVariant(scan.status)}`}>{scan.status}</span>
                        {scan.error_message && (
                          <div className="scan-error-hint" title={scan.error_message}>⚠ error</div>
                        )}
                      </td>
                      <td className="muted">{scan.target_database}</td>
                      <td className="muted">{scan.target_schema}</td>
                      <td className="muted">{scan.target_table || <span className="muted">schema-level</span>}</td>
                      <td>
                        {scan.progress_percentage != null
                          ? <div className="scan-progress-bar"><div className="scan-progress-fill" style={{width: `${scan.progress_percentage}%`}} /></div>
                          : '—'}
                        {scan.current_step && <div className="scan-step-hint">{scan.current_step}</div>}
                      </td>
                      <td className="muted" style={{whiteSpace:'nowrap',fontSize:11}}>{formatTimestamp(scan.started_at)}</td>
                      <td className="muted" style={{whiteSpace:'nowrap',fontSize:11}}>{scan.ended_at ? formatTimestamp(scan.ended_at) : '—'}</td>
                      <td>
                        <button className="link-button" onClick={() => setOpenScanId(isOpen ? null : scan.scan_id)}>
                          {isOpen ? 'Hide' : 'Details'}
                        </button>
                      </td>
                    </tr>
                    {isOpen && <ScanDetailRow scan={scan} />}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function ScanDetailRow({ scan }) {
  const [logs, setLogs] = useState([])
  const [rules, setRules] = useState([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState('logs') // 'logs' | 'rules'

  useEffect(() => {
    Promise.all([
      fetch(`${API_BASE}/api/scans/${scan.scan_id}/logs`).then(r => r.json()).catch(() => []),
      fetch(`${API_BASE}/api/rules/recommended?scan_id=${scan.scan_id}`).then(r => r.json()).catch(() => []),
    ]).then(([l, r]) => {
      setLogs(Array.isArray(l) ? l : [])
      setRules(Array.isArray(r) ? r : [])
    }).finally(() => setLoading(false))
  }, [scan.scan_id])

  const logStatusIcon = s => s === 'COMPLETED' ? '✓' : s === 'FAILED' ? '✗' : s === 'RUNNING' ? '…' : '·'
  const logStatusClass = s => s === 'COMPLETED' ? 'log-ok' : s === 'FAILED' ? 'log-fail' : s === 'RUNNING' ? 'log-run' : ''

  return (
    <tr className="rule-detail-row">
      <td colSpan={9}>
        <div className="rule-detail scan-detail-panel">
          {scan.error_message && (
            <div className="status status-error" style={{marginBottom:8}}>{scan.error_message}</div>
          )}
          <div className="scan-detail-tabs">
            <button className={`scan-tab${tab === 'logs' ? ' active' : ''}`} onClick={() => setTab('logs')}>
              Agent logs {!loading && `(${logs.length})`}
            </button>
            <button className={`scan-tab${tab === 'rules' ? ' active' : ''}`} onClick={() => setTab('rules')}>
              Recommended rules {!loading && `(${rules.length})`}
            </button>
          </div>
          {loading ? (
            <div className="rule-detail-loading"><span className="rule-detail-spinner" /> Loading…</div>
          ) : tab === 'logs' ? (
            logs.length === 0
              ? <p className="muted">No logs for this scan.</p>
              : (
              <div className="scan-log-list">
                {logs.map(log => (
                  <div key={log.log_id} className="scan-log-entry">
                    <span className={`scan-log-icon ${logStatusClass(log.status)}`}>{logStatusIcon(log.status)}</span>
                    <span className="scan-log-agent">{log.agent_name}</span>
                    <span className="scan-log-step">{log.step_name}</span>
                    <span className="scan-log-msg">{log.message}</span>
                    <span className="scan-log-time">{formatTimestamp(log.logged_at)}</span>
                  </div>
                ))}
              </div>
            )
          ) : (
            rules.length === 0
              ? <p className="muted">No rules recommended in this scan.</p>
              : (
              <table className="rules-table scan-rules-mini">
                <thead>
                  <tr><th>Rule name</th><th>Type</th><th>Scope</th><th>Target</th><th>Severity</th><th>Status</th><th>Test</th></tr>
                </thead>
                <tbody>
                  {rules.map(r => (
                    <tr key={r.rule_id} className={r.approval_status !== 'PENDING' ? 'rule-decided' : ''}>
                      <td>{r.rule_name}</td>
                      <td>{r.rule_type}</td>
                      <td>{r.scope ? <span className={`scope-badge scope-${r.scope.toLowerCase().replace('_','-')}`}>{r.scope}</span> : '—'}</td>
                      <td><RuleScopeTarget rule={r} onFilterColumn={() => {}} /></td>
                      <td><span className={`severity-badge severity-${r.severity?.toLowerCase()}`}>{r.severity}</span></td>
                      <td><span className={`approval-badge approval-${r.approval_status?.toLowerCase()}`}>{r.approval_status}</span></td>
                      <td><span className={`test-status-badge test-status-${r.test_status?.toLowerCase()}`}>{r.test_status}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </td>
    </tr>
  )
}

const NAV = [
  {
    section: 'Overview',
    items: [
      {
        id: 'dashboard',
        path: '/dashboard',
        label: 'Dashboard',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>
          </svg>
        ),
      },
    ],
  },
  {
    section: 'Discover',
    items: [
      {
        id: 'explorer',
        path: '/explorer',
        label: 'Data Explorer',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/>
          </svg>
        ),
      },
    ],
  },
  {
    section: 'Rules',
    items: [
      {
        id: 'rules',
        path: '/recommended-rules',
        label: 'Recommended',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>
          </svg>
        ),
      },
      {
        id: 'active',
        path: '/active-rules',
        label: 'Active Rules',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
          </svg>
        ),
      },
      {
        id: 'library',
        path: '/rule-library',
        label: 'Rule Library',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 19.5A2.5 2.5 0 016.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/>
          </svg>
        ),
      },
    ],
  },
  {
    section: 'Monitor',
    items: [
      {
        id: 'alerts',
        path: '/alerts',
        label: 'Alerts',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
          </svg>
        ),
      },
      {
        id: 'health',
        path: '/table-health',
        label: 'Table Health',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
        ),
      },
      {
        id: 'history',
        path: '/run-history',
        label: 'Run History',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
          </svg>
        ),
      },
      {
        id: 'scans',
        path: '/scan-history',
        label: 'Scan History',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
        ),
      },
    ],
  },
  {
    section: 'Configure',
    items: [
      {
        id: 'settings',
        path: '/schedules',
        label: 'Schedules',
        icon: (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 010 14.14M4.93 4.93a10 10 0 000 14.14"/>
          </svg>
        ),
      },
    ],
  },
]

const PAGE_TITLES = {
  '/dashboard':         'Dashboard',
  '/explorer':          'Data Explorer',
  '/recommended-rules': 'Recommended Rules',
  '/active-rules':      'Active Rules',
  '/rule-library':      'Rule Library',
  '/alerts':            'Alerts',
  '/table-health':      'Table Health',
  '/run-history':       'Run History',
  '/scan-history':      'Scan History',
  '/schedules':         'Schedules',
}

function App() {
  const navigate = useNavigate()
  const location = useLocation()
  const page = location.pathname === '/' ? '/dashboard' : location.pathname

  const [activeTableErrors, setActiveTableErrors] = useState(null)
  const [activeTableClassification, setActiveTableClassification] = useState(null)
  const [activeTableFilter, setActiveTableFilter] = useState(null)

  // Scan-progress state lifted here so it survives page navigation
  const [progressLogs, setProgressLogs] = useState([])
  const [logsVisible, setLogsVisible] = useState(false)
  const [lastScanIds, setLastScanIds] = useState(null)
  const [lastTableErrors, setLastTableErrors] = useState([])
  const [tableScanMap, setTableScanMap] = useState({})
  const [scanTableList, setScanTableList] = useState([])
  const [currentTableName, setCurrentTableName] = useState(null)
  const [scanRunning, setScanRunning] = useState(false)
  const pollRef = useRef(null)

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  const ID_TO_PATH = {
    dashboard: '/dashboard', explorer: '/explorer',
    rules: '/recommended-rules', active: '/active-rules',
    library: '/rule-library', alerts: '/alerts',
    health: '/table-health', history: '/run-history',
    scans: '/scan-history', settings: '/schedules',
  }

  function navTo(idOrPath) {
    navigate(idOrPath.startsWith('/') ? idOrPath : (ID_TO_PATH[idOrPath] ?? '/' + idOrPath))
  }

  function navigateToTable(tableName, destination) {
    setActiveTableFilter(tableName)
    navTo(destination)
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="sidebar-brand-icon">
            <span /><span /><span />
          </div>
          <div className="sidebar-brand-name">
            DQ Platform
            <small>Agentic</small>
          </div>
        </div>

        <nav className="sidebar-nav">
          {NAV.map((group) => (
            <Fragment key={group.section}>
              <div className="sidebar-section">{group.section}</div>
              {group.items.map((item) => (
                <button
                  key={item.id}
                  className={`sidebar-nav-btn${page === item.path ? ' active' : ''}`}
                  onClick={() => {
                    setActiveTableFilter(null)
                    navigate(item.path)
                  }}
                >
                  {item.icon}
                  {item.label}
                </button>
              ))}
            </Fragment>
          ))}
        </nav>

        {scanRunning && page !== '/explorer' && (
          <button
            className="sidebar-scan-pill"
            onClick={() => navigate('/explorer')}
          >
            <span className="sidebar-scan-dot" />
            Scan running…
          </button>
        )}
        <div className="sidebar-footer">
          <ConnectionStatus />
        </div>
      </aside>

      <div className={`main-content${page === '/dashboard' ? ' main-content--fill' : ''}`}>
        <div className="page-header">
          <h2>{PAGE_TITLES[page]}</h2>
        </div>
        <div className="page-body">
          {page === '/dashboard' && (
            <DashboardPage onNavigate={navTo} />
          )}
          {/* Always mounted — scan state must survive navigation */}
          <div style={{ display: page === '/explorer' ? 'contents' : 'none' }}>
            <DatabaseExplorer
              onRulesRecommended={(scanId, tableClassification) => {
                setActiveTableClassification(tableClassification ?? null)
                navigate(`/recommended-rules?scan_id=${scanId}`)
              }}
              onSchemaScanned={(scanIds, tableErrors) => {
                setActiveTableErrors(tableErrors)
                setActiveTableClassification(null)
                const p = new URLSearchParams()
                scanIds.forEach(id => p.append('scan_id', id))
                navigate(`/recommended-rules?${p.toString()}`)
              }}
              onScanStart={() => setScanRunning(true)}
              onScanEnd={() => setScanRunning(false)}
              progressLogs={progressLogs} setProgressLogs={setProgressLogs}
              logsVisible={logsVisible} setLogsVisible={setLogsVisible}
              lastScanIds={lastScanIds} setLastScanIds={setLastScanIds}
              lastTableErrors={lastTableErrors} setLastTableErrors={setLastTableErrors}
              tableScanMap={tableScanMap} setTableScanMap={setTableScanMap}
              scanTableList={scanTableList} setScanTableList={setScanTableList}
              currentTableName={currentTableName} setCurrentTableName={setCurrentTableName}
              pollRef={pollRef}
            />
          </div>
          {page === '/recommended-rules' && (
            <RecommendedRulesPage
              tableErrors={activeTableErrors}
              tableClassification={activeTableClassification}
            />
          )}
          {page === '/active-rules' && <ActiveRulesPage initialTable={activeTableFilter} />}
          {page === '/rule-library' && <RuleLibraryPage />}
          {page === '/alerts' && <AlertsPage initialTable={activeTableFilter} />}
          {page === '/run-history' && <ExecutionHistoryPage />}
          {page === '/table-health' && <TableHealthPage onNavigate={navigateToTable} />}
          {page === '/scan-history' && <ScansPage />}
          {page === '/schedules' && <SettingsPage />}
          {page === '/' && <Navigate to="/dashboard" replace />}
        </div>
      </div>
    </div>
  )
}

export default App

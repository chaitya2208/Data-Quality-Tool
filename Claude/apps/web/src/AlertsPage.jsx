import { Fragment, useEffect, useState } from 'react'

const API_BASE = ''

function formatTimestamp(value) {
  if (!value) return '—'
  // Snowflake TIMESTAMP_NTZ comes back as a naive ISO string (no zone) --
  // displayed as-is rather than re-interpreted through the browser's local
  // zone, since it's already the app DB's own clock (same convention as
  // ActiveRulesPage's formatTimestamp in App.jsx).
  return value.replace('T', ' ').slice(0, 19)
}

function todayLocalDate() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function StatTile({ label, value, tone }) {
  return (
    <div className={`stat-tile ${tone ? `stat-tile-${tone}` : ''}`}>
      <div className="stat-tile-label">{label}</div>
      <div className="stat-tile-value">{value}</div>
    </div>
  )
}

function SummaryTiles({ summary }) {
  if (!summary) return null
  return (
    <div className="stat-tile-row">
      <StatTile label="Total open alerts" value={summary.total_open_alerts} />
      <StatTile label="Critical alerts" value={summary.critical_alerts} tone="critical" />
      <StatTile label="Warning alerts" value={summary.warning_alerts} tone="warning" />
      <StatTile label="Failed rules today" value={summary.failed_rules_today} />
      <StatTile label="Tables affected" value={summary.tables_affected} />
    </div>
  )
}

const EMPTY_FILTERS = {
  database_name: '',
  schema_name: '',
  table_name: '',
  severity: '',
  status: '',
  date: '',
}

function FilterBar({ filters, onChange, onApply, onClear }) {
  return (
    <div className="alert-filter-bar">
      <input
        placeholder="Database"
        value={filters.database_name}
        onChange={(e) => onChange({ ...filters, database_name: e.target.value })}
      />
      <input
        placeholder="Schema"
        value={filters.schema_name}
        onChange={(e) => onChange({ ...filters, schema_name: e.target.value })}
      />
      <input
        placeholder="Table"
        value={filters.table_name}
        onChange={(e) => onChange({ ...filters, table_name: e.target.value })}
      />
      <select
        value={filters.severity}
        onChange={(e) => onChange({ ...filters, severity: e.target.value })}
      >
        <option value="">All severities</option>
        <option value="CRITICAL">CRITICAL</option>
        <option value="WARNING">WARNING</option>
        <option value="INFO">INFO</option>
      </select>
      <select
        value={filters.status}
        onChange={(e) => onChange({ ...filters, status: e.target.value })}
      >
        <option value="">All statuses</option>
        <option value="OPEN">OPEN</option>
        <option value="ACCEPTED">ACCEPTED</option>
        <option value="FALSE_POSITIVE">FALSE_POSITIVE</option>
        <option value="RESOLVED">RESOLVED</option>
      </select>
      <input
        type="date"
        value={filters.date}
        max={todayLocalDate()}
        onChange={(e) => onChange({ ...filters, date: e.target.value })}
      />
      <button className="approve-button" onClick={onApply}>
        Apply
      </button>
      <button className="link-button" onClick={onClear}>
        Clear
      </button>
    </div>
  )
}

function SampleFailedRows({ sample }) {
  if (!sample) return <p className="muted">—</p>
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

function AlertDetailRow({ alert, onCancel }) {
  // list_alerts() (what the main table is built from) doesn't carry
  // violation_samples -- only get_alert() (the single-alert route) does,
  // to avoid an N+1 query on the dashboard's list view. So this detail row
  // fetches its own full record on expand, same pattern as RuleEditForm's
  // own-fetch-on-demand in App.jsx.
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    fetch(`${API_BASE}/api/alerts/${alert.alert_id}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setLoading(false))
  }, [alert.alert_id])

  return (
    <tr className="rule-detail-row">
      <td colSpan={9}>
        <div className="rule-detail">
          <p className="explanation-block">
            <strong>Business explanation:</strong> {alert.business_explanation || '—'}
          </p>
          <p className="explanation-block">
            <strong>Business impact:</strong> {alert.business_impact || '—'}
          </p>
          <p className="explanation-block">
            <strong>False-positive risk:</strong> {alert.false_positive_risk || '—'}
          </p>
          <div>
            <strong>Sample failed rows:</strong>
            {loading ? (
              <p className="muted">Loading...</p>
            ) : (
              <SampleFailedRows sample={detail?.violation_samples} />
            )}
          </div>
          <button className="link-button" onClick={onCancel}>
            Close details
          </button>
        </div>
      </td>
    </tr>
  )
}

function AlertsPage({ initialTable }) {
  const initFilters = initialTable ? { ...EMPTY_FILTERS, table_name: initialTable } : EMPTY_FILTERS
  const [summary, setSummary] = useState(null)
  const [alerts, setAlerts] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filters, setFilters] = useState(initFilters)
  const [appliedFilters, setAppliedFilters] = useState(initFilters)
  const [actioningAlertId, setActioningAlertId] = useState(null)
  const [expandedAlertId, setExpandedAlertId] = useState(null)

  function loadSummary() {
    fetch(`${API_BASE}/api/alerts/summary`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setSummary)
      .catch((err) => setError(err.message))
  }

  // silent=true (used after accept/reject/false-positive) refetches without
  // blanking the table -- the existing rows stay on screen and are simply
  // swapped for fresh ones when the response lands. A real query change
  // (initial mount, Apply/Clear filters) still shows the loading state,
  // since that's a genuinely new result set the user is waiting on, not a
  // background refresh after an action they already saw complete.
  function loadAlerts(activeFilters, { silent = false } = {}) {
    if (!silent) setLoading(true)
    setError(null)
    const params = new URLSearchParams()
    Object.entries(activeFilters).forEach(([key, value]) => {
      if (value) params.set(key, value)
    })
    const query = params.toString()
    fetch(`${API_BASE}/api/alerts${query ? `?${query}` : ''}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setAlerts)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }

  function loadAll(activeFilters, opts) {
    loadSummary()
    loadAlerts(activeFilters, opts)
  }

  useEffect(() => {
    loadAll(EMPTY_FILTERS)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function applyFilters() {
    setAppliedFilters(filters)
    loadAlerts(filters)
  }

  function clearFilters() {
    setFilters(EMPTY_FILTERS)
    setAppliedFilters(EMPTY_FILTERS)
    loadAlerts(EMPTY_FILTERS)
  }

  function updateStatus(alertId, action) {
    setActioningAlertId(alertId)
    setError(null)
    fetch(`${API_BASE}/api/alerts/${alertId}/${action}`, { method: 'POST' })
      .then((res) => {
        if (!res.ok) return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        return res.json()
      })
      .then(() => loadAll(appliedFilters, { silent: true }))
      .catch((err) => setError(err.message))
      .finally(() => setActioningAlertId(null))
  }

  return (
    <div className="rules-page">
      {error && <div className="status status-error">{error}</div>}

      <SummaryTiles summary={summary} />

      <div className="rules-page-header">
        <h2>Recent Alerts</h2>
        <button className="link-button" onClick={() => loadAll(appliedFilters)}>
          Refresh
        </button>
      </div>

      <FilterBar filters={filters} onChange={setFilters} onApply={applyFilters} onClear={clearFilters} />

      {loading && <p className="muted">Loading alerts...</p>}
      {!loading && alerts.length === 0 && <p className="muted">No alerts match these filters.</p>}
      {!loading && alerts.length > 0 && (
        <div className="table-card">
        <table className="rules-table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Table</th>
              <th>Severity</th>
              <th>Status</th>
              <th>Failed count</th>
              <th>Failure %</th>
              <th>Created at</th>
              <th colSpan={3}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {alerts.map((alert) => {
              const isOpen = alert.status === 'OPEN'
              const isExpanded = expandedAlertId === alert.alert_id
              return (
                <Fragment key={alert.alert_id}>
                  <tr className={!isOpen ? 'rule-decided' : ''}>
                    <td>{alert.title}</td>
                    <td>
                      {alert.database_name}.{alert.schema_name}.{alert.table_name}
                      {alert.column_name ? `.${alert.column_name}` : ''}
                    </td>
                    <td>
                      <span className={`severity-badge severity-${alert.severity?.toLowerCase()}`}>
                        {alert.severity}
                      </span>
                    </td>
                    <td>
                      <span className={`alert-status-badge alert-status-${alert.status?.toLowerCase()}`}>
                        {alert.status}
                      </span>
                    </td>
                    <td>{alert.failed_count ?? '—'}</td>
                    <td>{alert.failure_percentage != null ? `${alert.failure_percentage}%` : '—'}</td>
                    <td className="muted">{formatTimestamp(alert.created_at)}</td>
                    <td>
                      <button
                        className="approve-button"
                        disabled={!isOpen || actioningAlertId === alert.alert_id}
                        onClick={() => updateStatus(alert.alert_id, 'accept')}
                      >
                        Accept
                      </button>
                    </td>
                    <td>
                      <button
                        className="reject-button"
                        disabled={!isOpen || actioningAlertId === alert.alert_id}
                        onClick={() => updateStatus(alert.alert_id, 'false-positive')}
                      >
                        False positive
                      </button>
                    </td>
                    <td>
                      <button
                        className="link-button"
                        onClick={() => setExpandedAlertId(isExpanded ? null : alert.alert_id)}
                      >
                        {isExpanded ? 'Hide details' : 'View details'}
                      </button>
                    </td>
                  </tr>
                  {isExpanded && (
                    <AlertDetailRow alert={alert} onCancel={() => setExpandedAlertId(null)} />
                  )}
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

export default AlertsPage

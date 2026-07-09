import { useEffect, useState } from 'react'

const API_BASE = ''

function formatTimestamp(value) {
  if (!value) return '—'
  // Snowflake TIMESTAMP_NTZ comes back as a naive ISO string (no zone) --
  // displayed as-is rather than re-interpreted through the browser's local
  // zone, same convention as AlertsPage/App.jsx's own formatTimestamp.
  return value.replace('T', ' ').slice(0, 19)
}

function formatDuration(seconds) {
  if (seconds == null) return '—'
  return `${seconds}s`
}

function ExecutionHistoryPage() {
  const [runs, setRuns] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [statusFilter, setStatusFilter] = useState('')

  function loadRuns(status) {
    setLoading(true)
    setError(null)
    const query = status ? `?status=${status}` : ''
    fetch(`${API_BASE}/api/rules/execution-history${query}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setRuns)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    loadRuns(statusFilter)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function applyStatusFilter(value) {
    setStatusFilter(value)
    loadRuns(value)
  }

  return (
    <div className="rules-page">
      {error && <div className="status status-error">{error}</div>}

      <div className="rules-page-header">
        <h2>Rule Execution History</h2>
        <button className="link-button" onClick={() => loadRuns(statusFilter)}>
          Refresh
        </button>
      </div>

      <div className="alert-filter-bar">
        <select value={statusFilter} onChange={(e) => applyStatusFilter(e.target.value)}>
          <option value="">All statuses</option>
          <option value="PASSED">PASSED</option>
          <option value="FAILED">FAILED</option>
          <option value="ERROR">ERROR</option>
          <option value="SKIPPED">SKIPPED</option>
        </select>
      </div>

      {loading && <p className="muted">Loading execution history...</p>}
      {!loading && runs.length === 0 && <p className="muted">No rule runs yet.</p>}
      {!loading && runs.length > 0 && (
        <div className="table-card">
        <table className="rules-table">
          <thead>
            <tr>
              <th>Rule name</th>
              <th>Table</th>
              <th>Status</th>
              <th>Failed count</th>
              <th>Failure %</th>
              <th>Duration</th>
              <th>Last run time</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.execution_id}>
                <td>{run.rule_name || '—'}</td>
                <td>
                  {run.database_name
                    ? `${run.database_name}.${run.schema_name}.${run.table_name}${
                        run.column_name ? `.${run.column_name}` : ''
                      }`
                    : '—'}
                </td>
                <td>
                  <span className={`test-status-badge test-status-${run.status?.toLowerCase()}`}>
                    {run.status}
                  </span>
                </td>
                <td>{run.failed_count ?? '—'}</td>
                <td>{run.failure_percentage != null ? `${run.failure_percentage}%` : '—'}</td>
                <td>{formatDuration(run.duration_seconds)}</td>
                <td className="muted">{formatTimestamp(run.started_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  )
}

export default ExecutionHistoryPage

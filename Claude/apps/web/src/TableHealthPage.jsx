import { useEffect, useState } from 'react'

const API_BASE = ''

function formatTimestamp(value) {
  if (!value) return 'Never scanned'
  // Snowflake TIMESTAMP_NTZ comes back as a naive ISO string (no zone) --
  // displayed as-is, same convention as every other page's formatTimestamp.
  return value.replace('T', ' ').slice(0, 19)
}

// Same three-tier thresholds as the CSS classes (dq-score-good/warning/
// critical) -- >=90 good, 70-89 warning, <70 critical. Matches this app's
// existing severity-badge tiers (CRITICAL/WARNING/INFO) in spirit, applied
// to a continuous score instead of a discrete severity.
function scoreTone(score) {
  if (score == null) return null
  if (score >= 90) return 'good'
  if (score >= 70) return 'warning'
  return 'critical'
}

function DqScoreCell({ score }) {
  if (score == null) {
    return <span className="muted">No active rules</span>
  }
  const tone = scoreTone(score)
  return (
    <div className="dq-score-cell">
      <div className="dq-score-bar-track">
        <div
          className={`dq-score-bar-fill dq-score-${tone}`}
          style={{ width: `${score}%` }}
        />
      </div>
      <span className={`dq-score-label dq-score-${tone}`}>{score}%</span>
    </div>
  )
}

function TableHealthPage({ onNavigate }) {
  const [tables, setTables] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  function loadHealth() {
    setLoading(true)
    setError(null)
    fetch(`${API_BASE}/api/tables/health`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setTables)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    loadHealth()
  }, [])

  return (
    <div className="rules-page">
      {error && <div className="status status-error">{error}</div>}
      <div className="rules-page-header">
        <h2>Table Health</h2>
        <button className="link-button" onClick={loadHealth}>
          Refresh
        </button>
      </div>

      {loading && <p className="muted">Loading table health...</p>}
      {!loading && tables.length === 0 && (
        <p className="muted">No tables have approved rules yet.</p>
      )}
      {!loading && tables.length > 0 && (
        <div className="table-card">
        <table className="rules-table">
          <thead>
            <tr>
              <th>Table</th>
              <th>Active rules</th>
              <th>Passed</th>
              <th>Failed</th>
              <th>Open alerts</th>
              <th>DQ score</th>
              <th>Last scan time</th>
            </tr>
          </thead>
          <tbody>
            {tables.map((t) => (
              <tr key={`${t.database_name}.${t.schema_name}.${t.table_name}`}>
                <td>
                  {t.database_name}.{t.schema_name}.{t.table_name}
                </td>
                <td>
                  {onNavigate && t.total_active_rules > 0
                    ? <button className="table-filter-link" onClick={() => onNavigate(t.table_name, 'active')}>{t.total_active_rules}</button>
                    : t.total_active_rules}
                </td>
                <td>{t.passed_rules}</td>
                <td>{t.failed_rules}</td>
                <td>
                  {onNavigate && t.open_alerts > 0
                    ? <button className="table-filter-link" onClick={() => onNavigate(t.table_name, 'alerts')}>{t.open_alerts}</button>
                    : t.open_alerts}
                </td>
                <td>
                  <DqScoreCell score={t.dq_score} />
                </td>
                <td className="muted">{formatTimestamp(t.last_scan_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  )
}

export default TableHealthPage

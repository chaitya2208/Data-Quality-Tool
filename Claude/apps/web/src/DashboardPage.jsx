import { useEffect, useState } from 'react'
import './DashboardPage.css'

const API_BASE = ''

function fmt(value) {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 16)
}

function scoreColor(score) {
  if (score == null) return '#94a3b8'
  if (score >= 90) return '#16a34a'
  if (score >= 70) return '#d97706'
  return '#dc2626'
}

function scoreTone(score) {
  if (score == null) return 'neutral'
  if (score >= 90) return 'good'
  if (score >= 70) return 'warn'
  return 'bad'
}

// ─── Overall score ring ───────────────────────────────────────────────────────

function ScoreRing({ score }) {
  const pct = score ?? 0
  const r = 38
  const circ = 2 * Math.PI * r
  const dash = (pct / 100) * circ
  const color = scoreColor(score)
  const label =
    score == null ? 'No data' : score >= 90 ? 'Healthy' : score >= 70 ? 'Degraded' : 'At risk'

  return (
    <div className="score-ring-wrap">
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r={r} fill="none" stroke="#e2e8f0" strokeWidth="8" />
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circ}`}
          strokeDashoffset={circ / 4}
          style={{ transition: 'stroke-dasharray 0.6s ease' }}
        />
      </svg>
      <div className="score-ring-inner">
        <div className="score-ring-value" style={{ color }}>
          {score != null ? `${score}%` : '—'}
        </div>
        <div className="score-ring-label">{label}</div>
      </div>
    </div>
  )
}

// ─── KPI tile ─────────────────────────────────────────────────────────────────

function KpiTile({ label, value, tone, sub }) {
  return (
    <div className={`dash-kpi dash-kpi-${tone ?? 'neutral'}`}>
      <div className="dash-kpi-value">{value ?? '—'}</div>
      <div className="dash-kpi-label">{label}</div>
      {sub && <div className="dash-kpi-sub">{sub}</div>}
    </div>
  )
}

// ─── Section header ───────────────────────────────────────────────────────────

function SectionHead({ title, action, onAction }) {
  return (
    <div className="dash-section-head">
      <span className="dash-section-title">{title}</span>
      {action && (
        <button className="dash-section-link" onClick={onAction}>
          {action}
        </button>
      )}
    </div>
  )
}

// ─── Table health list ────────────────────────────────────────────────────────

function HealthList({ tables }) {
  if (!tables.length)
    return <p className="dash-empty">No tables with active rules yet.</p>

  const sorted = [...tables].sort((a, b) => (a.dq_score ?? 101) - (b.dq_score ?? 101))
  const shown = sorted.slice(0, 8)

  return (
    <div className="health-list">
      {shown.map((t) => {
        const name = `${t.schema_name}.${t.table_name}`
        const score = t.dq_score
        const tone = scoreTone(score)
        const color = scoreColor(score)
        return (
          <div key={name} className="health-row">
            <div className="health-row-name" title={`${t.database_name}.${name}`}>
              {name}
            </div>
            <div className="health-row-bar">
              <div
                className="health-row-fill"
                style={{ width: `${score ?? 0}%`, background: color }}
              />
            </div>
            <div className={`health-row-score health-score-${tone}`}>
              {score != null ? `${score}%` : '—'}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ─── Alert feed ───────────────────────────────────────────────────────────────

function AlertFeed({ alerts }) {
  if (!alerts.length)
    return <p className="dash-empty">No open alerts — all clear.</p>

  return (
    <div className="alert-feed">
      {alerts.slice(0, 6).map((a) => (
        <div key={a.alert_id} className="alert-feed-row">
          <span className={`feed-dot feed-dot-${a.severity?.toLowerCase()}`} />
          <div className="feed-body">
            <div className="feed-title">{a.title}</div>
            <div className="feed-meta">
              {a.schema_name}.{a.table_name}
              {a.column_name ? `.${a.column_name}` : ''} · {fmt(a.created_at)}
            </div>
          </div>
          <span className={`severity-badge severity-${a.severity?.toLowerCase()}`}>
            {a.severity}
          </span>
        </div>
      ))}
    </div>
  )
}

// ─── Run feed ─────────────────────────────────────────────────────────────────

function RunFeed({ runs }) {
  if (!runs.length)
    return <p className="dash-empty">No rule runs yet.</p>

  return (
    <div className="alert-feed">
      {runs.slice(0, 6).map((r) => (
        <div key={r.execution_id} className="alert-feed-row">
          <span className={`feed-dot feed-dot-${r.status?.toLowerCase()}`} />
          <div className="feed-body">
            <div className="feed-title">{r.rule_name || '—'}</div>
            <div className="feed-meta">
              {r.table_name ? `${r.schema_name}.${r.table_name}` : '—'} · {fmt(r.started_at)}
            </div>
          </div>
          <span className={`test-status-badge test-status-${r.status?.toLowerCase()}`}>
            {r.status}
          </span>
        </div>
      ))}
    </div>
  )
}

// ─── Coverage bar ─────────────────────────────────────────────────────────────

function CoverageBar({ label, value, total, color }) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0
  return (
    <div className="cov-row">
      <div className="cov-label">{label}</div>
      <div className="cov-track">
        <div className="cov-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
      <div className="cov-stat">
        {value}<span className="cov-total">/{total}</span>
      </div>
    </div>
  )
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

// ─── Rules drill-down tree ────────────────────────────────────────────────────

function RulesTree({ rules, onNavigate }) {
  const [openDbs, setOpenDbs] = useState(new Set())
  const [openSchemas, setOpenSchemas] = useState(new Set())
  const [openTables, setOpenTables] = useState(new Set())

  function toggle(setFn, key) {
    setFn(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }

  if (!rules.length) return <p className="dash-empty">No recommended rules yet.</p>

  const tree = {}
  for (const rule of rules) {
    const db = rule.database_name || '(unknown)'
    const sc = rule.schema_name || '(unknown)'
    const tb = rule.table_name || '(unknown)'
    if (!tree[db]) tree[db] = {}
    if (!tree[db][sc]) tree[db][sc] = {}
    if (!tree[db][sc][tb]) tree[db][sc][tb] = []
    tree[db][sc][tb].push(rule)
  }

  return (
    <div className="rules-tree">
      {Object.entries(tree).map(([db, schemas]) => {
        const allDbRules = Object.values(schemas).flatMap(ts => Object.values(ts).flat())
        const dbPending = allDbRules.filter(r => r.approval_status === 'PENDING').length
        const isDbOpen = openDbs.has(db)

        return (
          <div key={db} className="rt-group">
            <button
              className={`rt-row rt-level-db${isDbOpen ? ' rt-open' : ''}`}
              onClick={() => toggle(setOpenDbs, db)}
            >
              <span className="rt-chevron">{isDbOpen ? '▾' : '▸'}</span>
              <span className="rt-label rt-label-db">{db}</span>
              <span className="rt-meta">{Object.keys(schemas).length} schema{Object.keys(schemas).length !== 1 ? 's' : ''}</span>
              <span className="rt-pills">
                <span className="rt-pill rt-pill-total">{allDbRules.length} rules</span>
                {dbPending > 0 && <span className="rt-pill rt-pill-pending">{dbPending} pending</span>}
              </span>
            </button>

            {isDbOpen && Object.entries(schemas).map(([sc, tables]) => {
              const allScRules = Object.values(tables).flat()
              const scPending = allScRules.filter(r => r.approval_status === 'PENDING').length
              const scKey = `${db}::${sc}`
              const isScOpen = openSchemas.has(scKey)

              return (
                <div key={sc} className="rt-group rt-indent-1">
                  <button
                    className={`rt-row rt-level-schema${isScOpen ? ' rt-open' : ''}`}
                    onClick={() => toggle(setOpenSchemas, scKey)}
                  >
                    <span className="rt-chevron">{isScOpen ? '▾' : '▸'}</span>
                    <span className="rt-label rt-label-schema">{sc}</span>
                    <span className="rt-meta">{Object.keys(tables).length} table{Object.keys(tables).length !== 1 ? 's' : ''}</span>
                    <span className="rt-pills">
                      <span className="rt-pill rt-pill-total">{allScRules.length} rules</span>
                      {scPending > 0 && <span className="rt-pill rt-pill-pending">{scPending} pending</span>}
                    </span>
                  </button>

                  {isScOpen && Object.entries(tables).map(([tb, tbRules]) => {
                    const tbPending = tbRules.filter(r => r.approval_status === 'PENDING').length
                    const tbKey = `${scKey}::${tb}`
                    const isTbOpen = openTables.has(tbKey)

                    return (
                      <div key={tb} className="rt-group rt-indent-2">
                        <button
                          className={`rt-row rt-level-table${isTbOpen ? ' rt-open' : ''}`}
                          onClick={() => toggle(setOpenTables, tbKey)}
                        >
                          <span className="rt-chevron">{isTbOpen ? '▾' : '▸'}</span>
                          <span className="rt-label rt-label-table">{tb}</span>
                          <span className="rt-pills">
                            <span className="rt-pill rt-pill-total">{tbRules.length} rules</span>
                            {tbPending > 0 && <span className="rt-pill rt-pill-pending">{tbPending} pending</span>}
                          </span>
                        </button>

                        {isTbOpen && (
                          <div className="rt-rules-list rt-indent-3">
                            {tbRules.map(r => (
                              <div key={r.rule_id} className="rt-rule-row">
                                <span className={`rt-sev-dot rt-sev-${r.severity?.toLowerCase()}`} />
                                <span className="rt-rule-name">{r.rule_name}</span>
                                <span className="rt-rule-type">{r.rule_type}</span>
                                <span className={`rt-approval rt-approval-${r.approval_status?.toLowerCase()}`}>
                                  {r.approval_status}
                                </span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )
            })}
          </div>
        )
      })}
    </div>
  )
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

export default function DashboardPage({ onNavigate }) {
  const [summary, setSummary] = useState(null)
  const [tables, setTables] = useState([])
  const [alerts, setAlerts] = useState([])
  const [runs, setRuns] = useState([])
  const [pendingCount, setPendingCount] = useState(0)
  const [active, setActive] = useState([])
  const [recommended, setRecommended] = useState([])
  const [loading, setLoading] = useState(true)
  const [partialErrors, setPartialErrors] = useState([])

  useEffect(() => {
    setLoading(true)
    setPartialErrors([])
    const failed = new Set()
    const markFailed = (label, reason) => {
      if (!failed.has(label)) { failed.add(label); setPartialErrors((e) => [...e, `${label}: ${reason}`]) }
    }
    const safe = (url, fallback, label) => {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), 20000)
      return fetch(url, { signal: ctrl.signal })
        .then((r) => {
          if (r.ok) return r.json()
          markFailed(label, `server error ${r.status}`)
          return fallback
        })
        .catch((err) => {
          markFailed(label, err.name === 'AbortError' ? 'timed out' : 'unavailable')
          return fallback
        })
        .finally(() => clearTimeout(timer))
    }
    Promise.all([
      safe(`${API_BASE}/api/alerts/summary`, null, 'Alert summary'),
      safe(`${API_BASE}/api/tables/health`, [], 'Table health'),
      safe(`${API_BASE}/api/alerts?status=OPEN&limit=20`, [], 'Open alerts'),
      safe(`${API_BASE}/api/rules/execution-history?limit=20`, [], 'Run history'),
      safe(`${API_BASE}/api/rules/pending-count`, { count: 0 }, 'Pending count'),
      safe(`${API_BASE}/api/rules/active`, [], 'Active rules'),
      safe(`${API_BASE}/api/rules/recommended/summary`, [], 'Recommended rules'),
    ])
      .then(([s, t, a, r, pc, ac, rec]) => {
        setSummary(s)
        setTables(Array.isArray(t) ? t : [])
        setAlerts(Array.isArray(a) ? a : [])
        setRuns(Array.isArray(r) ? r : [])
        setPendingCount(pc?.count ?? 0)
        setActive(Array.isArray(ac) ? ac : [])
        setRecommended(Array.isArray(rec) ? rec : [])
      })
      .finally(() => setLoading(false))
  }, [])

  // Overall DQ score = average of all table scores that have one
  const scoredTables = tables.filter((t) => t.dq_score != null)
  const overallScore =
    scoredTables.length > 0
      ? Math.round(scoredTables.reduce((s, t) => s + t.dq_score, 0) / scoredTables.length)
      : null

  const passedRuns = runs.filter((r) => r.status === 'PASSED').length
  const failedRuns = runs.filter((r) => r.status === 'FAILED' || r.status === 'ERROR').length

  if (loading) {
    return (
      <div className="dash-loading">
        <div className="dash-loading-spinner" />
        <span>Loading dashboard…</span>
      </div>
    )
  }

  return (
    <div className="dashboard">

      {partialErrors.length > 0 && (
        <div className="dash-partial-error">
          Some sections couldn't load: {partialErrors.join(' · ')}
        </div>
      )}

      {/* ── Hero strip ── */}
      <div className="dash-hero">
        <div className="dash-hero-score">
          <ScoreRing score={overallScore} />
          <div className="dash-hero-text">
            <div className="dash-hero-headline">Data Quality Score</div>
            <div className="dash-hero-sub">
              Average across {scoredTables.length} monitored table{scoredTables.length !== 1 ? 's' : ''}
            </div>
            {pendingCount > 0 && (
              <button
                className="dash-hero-cta"
                onClick={() => onNavigate('rules')}
              >
                {pendingCount} rule{pendingCount !== 1 ? 's' : ''} awaiting approval →
              </button>
            )}
          </div>
        </div>

        <div className="dash-kpi-row">
          <KpiTile
            label="Open alerts"
            value={summary?.total_open_alerts}
            tone={summary?.total_open_alerts > 0 ? 'bad' : 'good'}
          />
          <KpiTile
            label="Critical"
            value={summary?.critical_alerts}
            tone={summary?.critical_alerts > 0 ? 'bad' : 'neutral'}
          />
          <KpiTile
            label="Warning"
            value={summary?.warning_alerts}
            tone={summary?.warning_alerts > 0 ? 'warn' : 'neutral'}
          />
          <KpiTile
            label="Failed today"
            value={summary?.failed_rules_today}
            tone={summary?.failed_rules_today > 0 ? 'bad' : 'neutral'}
          />
          <KpiTile
            label="Tables affected"
            value={summary?.tables_affected}
            tone={summary?.tables_affected > 0 ? 'warn' : 'neutral'}
          />
          <KpiTile
            label="Active rules"
            value={active.length}
            tone="neutral"
          />
          <KpiTile
            label="Pending review"
            value={pendingCount}
            tone={pendingCount > 0 ? 'warn' : 'neutral'}
          />
        </div>
      </div>

      {/* ── Main grid ── */}
      <div className="dash-grid">

        {/* Table health — col 1, spans both rows */}
        <div className="dash-card dash-card-tall">
          <SectionHead
            title="Table Health"
            action="View all →"
            onAction={() => onNavigate('health')}
          />
          <HealthList tables={tables} />
        </div>

        {/* Open alerts — col 2, row 1 */}
        <div className="dash-card">
          <SectionHead
            title="Open Alerts"
            action="View all →"
            onAction={() => onNavigate('alerts')}
          />
          <AlertFeed alerts={alerts} />
        </div>

        {/* Coverage — col 3, row 1 */}
        <div className="dash-card">
          <SectionHead title="Rule Coverage" />
          <div className="cov-section">
            <CoverageBar
              label="Tables monitored"
              value={scoredTables.length}
              total={tables.length || scoredTables.length}
              color="#2563eb"
            />
            <CoverageBar
              label="Rules passing"
              value={passedRuns}
              total={runs.length}
              color="#16a34a"
            />
            <CoverageBar
              label="Rules failing"
              value={failedRuns}
              total={runs.length}
              color="#dc2626"
            />
            <CoverageBar
              label="Pending approval"
              value={pendingCount}
              total={pendingCount + active.length}
              color="#d97706"
            />
          </div>
          {pendingCount > 0 && (
            <button
              className="dash-card-action-btn"
              onClick={() => onNavigate('rules')}
            >
              Review {pendingCount} pending rule{pendingCount !== 1 ? 's' : ''}
            </button>
          )}
        </div>

        {/* Recent runs — col 2, row 2 */}
        <div className="dash-card">
          <SectionHead
            title="Recent Rule Runs"
            action="View all →"
            onAction={() => onNavigate('history')}
          />
          <RunFeed runs={runs} />
        </div>

        {/* Rules drill-down — col 3, row 2 */}
        <div className="dash-card">
          <SectionHead
            title="Recommended Rules"
            action="View all →"
            onAction={() => onNavigate('rules')}
          />
          <RulesTree rules={recommended} onNavigate={onNavigate} />
        </div>

      </div>

    </div>
  )
}

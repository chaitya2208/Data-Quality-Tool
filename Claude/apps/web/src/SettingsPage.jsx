import { useEffect, useRef, useState } from 'react'

const API_BASE = ''

function formatTimestamp(value) {
  if (!value) return '—'
  // Snowflake TIMESTAMP_NTZ comes back as a naive ISO string (no zone) --
  // displayed as-is, same convention as every other page's formatTimestamp.
  return value.replace('T', ' ').slice(0, 19)
}

const EMPTY_FORM = {
  schedule_type: 'RULE_EXECUTION',
  target_database: '',
  target_schema: '',
  target_table: '',
  interval_minutes: '',
}

function validateForm(form) {
  if (!form.target_database.trim()) return 'Target database is required.'
  const interval = Number(form.interval_minutes)
  if (!Number.isInteger(interval) || interval <= 0) {
    return 'Interval (minutes) must be a positive whole number.'
  }
  return null
}

function SettingsPage() {
  const [schedules, setSchedules] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [creating, setCreating] = useState(false)
  const [deactivatingId, setDeactivatingId] = useState(null)
  const hasLoadedOnce = useRef(false)

  // Only shows the full-page "Loading..." state on first mount. A refresh
  // after create/deactivate refetches silently -- the existing table stays
  // visible the whole time instead of blanking out and redrawing, so acting
  // on several schedules in a row doesn't feel like repeated full-page
  // reloads. Tracked via a ref (not schedules.length) so a genuinely empty
  // list doesn't re-trigger the blanking loading state on every reload.
  function loadSchedules() {
    if (!hasLoadedOnce.current) setLoading(true)
    setError(null)
    fetch(`${API_BASE}/api/schedules`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setSchedules)
      .catch((err) => setError(err.message))
      .finally(() => {
        hasLoadedOnce.current = true
        setLoading(false)
      })
  }

  useEffect(() => {
    loadSchedules()
  }, [])

  function createSchedule() {
    const validationError = validateForm(form)
    if (validationError) {
      setError(validationError)
      return
    }
    setCreating(true)
    setError(null)
    fetch(`${API_BASE}/api/schedules`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        schedule_type: form.schedule_type,
        target_database: form.target_database,
        target_schema: form.schedule_type === 'RESCAN' ? form.target_schema || null : null,
        target_table: form.schedule_type === 'RESCAN' ? form.target_table || null : null,
        interval_minutes: Number(form.interval_minutes),
      }),
    })
      .then((res) => {
        if (!res.ok) {
          return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        }
        return res.json()
      })
      .then(() => {
        setForm(EMPTY_FORM)
        loadSchedules()
      })
      .catch((err) => setError(err.message))
      .finally(() => setCreating(false))
  }

  function deactivateSchedule(scheduleId) {
    setDeactivatingId(scheduleId)
    setError(null)
    fetch(`${API_BASE}/api/schedules/${scheduleId}/deactivate`, { method: 'POST' })
      .then((res) => {
        if (!res.ok) {
          return res.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${res.status}`)))
        }
        return res.json()
      })
      .then(() => loadSchedules())
      .catch((err) => setError(err.message))
      .finally(() => setDeactivatingId(null))
  }

  return (
    <div className="rules-page">
      {error && <div className="status status-error">{error}</div>}
      <div className="rules-page-header">
        <h2>Settings — Schedules</h2>
        <button className="link-button" onClick={loadSchedules}>
          Refresh
        </button>
      </div>

      <p className="muted">
        Schedules run on the same Snowflake session as your manual actions in this app. If this
        backend process restarts, the first schedule to fire before anyone logs in through the
        browser SSO prompt will hang or fail silently — open any page here once after a restart
        to complete that login before relying on a schedule.
      </p>

      <div className="rule-edit-form">
        <label>
          Schedule type
          <select
            value={form.schedule_type}
            onChange={(e) => setForm({ ...form, schedule_type: e.target.value })}
          >
            <option value="RULE_EXECUTION">Rule execution</option>
            <option value="RESCAN">Rescan</option>
          </select>
        </label>
        <label>
          Target database
          <input
            type="text"
            value={form.target_database}
            onChange={(e) => setForm({ ...form, target_database: e.target.value })}
          />
        </label>
        {form.schedule_type === 'RULE_EXECUTION' && (
          <p className="muted">
            Target database is ignored for Rule Execution — every approved rule re-runs
            regardless of target.
          </p>
        )}
        {form.schedule_type === 'RESCAN' && (
          <>
            <label>
              Target schema (optional)
              <input
                type="text"
                value={form.target_schema}
                onChange={(e) => setForm({ ...form, target_schema: e.target.value })}
              />
            </label>
            <label>
              Target table (optional)
              <input
                type="text"
                value={form.target_table}
                onChange={(e) => setForm({ ...form, target_table: e.target.value })}
              />
            </label>
          </>
        )}
        <label>
          Interval (minutes)
          <input
            type="number"
            min="1"
            value={form.interval_minutes}
            onChange={(e) => setForm({ ...form, interval_minutes: e.target.value })}
          />
        </label>
        <button className="approve-button" onClick={createSchedule} disabled={creating}>
          {creating ? 'Creating...' : 'Create schedule'}
        </button>
      </div>

      {loading && <p className="muted">Loading schedules...</p>}
      {!loading && schedules.length === 0 && <p className="muted">No schedules yet.</p>}
      {!loading && schedules.length > 0 && (
        <div className="table-card">
        <table className="rules-table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Target</th>
              <th>Interval (min)</th>
              <th>Active</th>
              <th>Last run</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {schedules.map((s) => (
              <tr key={s.schedule_id}>
                <td>{s.schedule_type}</td>
                <td>
                  {[s.target_database, s.target_schema, s.target_table].filter(Boolean).join('.') ||
                    '—'}
                </td>
                <td>{s.interval_minutes}</td>
                <td>
                  <span className={`active-badge active-${s.is_active ? 'yes' : 'no'}`}>
                    {s.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                <td className="muted">{formatTimestamp(s.last_run_at)}</td>
                <td className="muted">{formatTimestamp(s.created_at)}</td>
                <td>
                  <button
                    className="reject-button"
                    disabled={!s.is_active || deactivatingId === s.schedule_id}
                    onClick={() => deactivateSchedule(s.schedule_id)}
                  >
                    {deactivatingId === s.schedule_id ? 'Deactivating...' : 'Deactivate'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  )
}

export default SettingsPage

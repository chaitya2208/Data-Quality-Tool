import { useEffect, useState } from 'react';
import { Bell, CheckCircle, XCircle, ChevronRight, Loader2, RefreshCw, Wrench, PlayCircle } from 'lucide-react';
import {
  notificationsApi, proposalsApi, maintenanceApi,
  type Notification, type PendingProposal, type MaintenanceProposal,
} from '../api/client';

export default function Notifications() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [proposals, setProposals] = useState<PendingProposal[]>([]);
  const [maintenance, setMaintenance] = useState<MaintenanceProposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<PendingProposal | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [sweepMsg, setSweepMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [n, p, m] = await Promise.all([
        notificationsApi.list({ limit: 100 }),
        proposalsApi.listPending(200),
        maintenanceApi.listPending(200),
      ]);
      setNotifications(n.data.items);
      setProposals(p.data.items);
      setMaintenance(m.data.items);
    } catch (e: any) {
      setError(e?.message || 'Failed to load notifications');
    } finally {
      setLoading(false);
    }
  }

  async function runSweep() {
    setBusy(true);
    setSweepMsg(null);
    try {
      const r = await maintenanceApi.runSweep();
      setSweepMsg(`Scanned ${r.data.scanned} instances, created ${r.data.proposals_created} proposals.`);
      await load();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Maintenance sweep failed');
    } finally {
      setBusy(false);
    }
  }

  async function approveMaintenance(p: MaintenanceProposal) {
    setBusy(true);
    try {
      await maintenanceApi.approve(p.id);
      await load();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Approve failed');
    } finally {
      setBusy(false);
    }
  }

  async function dismissMaintenance(p: MaintenanceProposal) {
    setBusy(true);
    try {
      await maintenanceApi.dismiss(p.id);
      await load();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Dismiss failed');
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function markAllRead() {
    try {
      await notificationsApi.markAllRead();
      await load();
    } catch (e) {
      console.error(e);
    }
  }

  async function approve(p: PendingProposal) {
    setBusy(true);
    try {
      await proposalsApi.approve(p.id);
      setSelected(null);
      await load();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Approve failed');
    } finally {
      setBusy(false);
    }
  }

  async function reject(p: PendingProposal) {
    setBusy(true);
    try {
      await proposalsApi.reject(p.id, rejectReason);
      setSelected(null);
      setRejectReason('');
      await load();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Reject failed');
    } finally {
      setBusy(false);
    }
  }

  const unread = notifications.filter(n => !n.read_at);

  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Bell className="w-6 h-6 text-primary-600" />
          <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">Notifications</h1>
          {unread.length > 0 && (
            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-primary-100 text-primary-800 dark:bg-primary-900/40 dark:text-primary-300">
              {unread.length} unread
            </span>
          )}
        </div>
        <div className="flex gap-2">
          <button
            onClick={load}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
          >
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>
          {unread.length > 0 && (
            <button
              onClick={markAllRead}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700"
            >
              Mark all read
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-sm text-red-700 dark:text-red-200">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* ── Left: notifications feed ─────────────────────── */}
          <div className="lg:col-span-1 space-y-3">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
              Recent
            </h2>
            {notifications.length === 0 && (
              <p className="text-sm text-gray-500 dark:text-gray-400 italic">No notifications yet.</p>
            )}
            {notifications.map(n => {
              // Match a notification to its proposals so a click opens them.
              // Anomaly notifications carry the source run_id in ref_id; each
              // pending proposal from that run has source_run_id === ref_id.
              const linked = proposals.filter(
                p => (n.kind === 'anomaly_proposals') && p.source_run_id === n.ref_id
              );
              const first = linked[0] || null;
              async function onClick() {
                if (first) setSelected(first);
                if (!n.read_at) {
                  try {
                    await notificationsApi.markRead(n.id);
                  } catch (e) {
                    console.error('markRead failed', e);
                  }
                  await load();
                }
              }
              return (
                <button
                  key={n.id}
                  onClick={onClick}
                  className={`w-full text-left p-3 rounded-lg border transition-colors cursor-pointer ${
                    n.read_at
                      ? 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700/50'
                      : 'border-primary-200 dark:border-primary-800 bg-primary-50 dark:bg-primary-900/20 hover:bg-primary-100 dark:hover:bg-primary-900/30'
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{n.title}</p>
                    {!n.read_at && (
                      <span className="mt-1 w-2 h-2 rounded-full bg-primary-500 flex-shrink-0" />
                    )}
                  </div>
                  {n.body && (
                    <p className="mt-1 text-xs text-gray-600 dark:text-gray-400">{n.body}</p>
                  )}
                  <div className="mt-2 flex items-center justify-between">
                    <p className="text-[11px] text-gray-400">
                      {n.created_at ? new Date(n.created_at).toLocaleString() : ''}
                    </p>
                    {linked.length > 0 ? (
                      <span className="text-[11px] font-medium text-primary-600 dark:text-primary-400 inline-flex items-center gap-0.5">
                        Review {linked.length} <ChevronRight className="w-3 h-3" />
                      </span>
                    ) : (
                      n.kind === 'anomaly_proposals' && (
                        <span className="text-[11px] text-gray-400 italic">
                          Already reviewed
                        </span>
                      )
                    )}
                  </div>
                </button>
              );
            })}
          </div>

          {/* ── Middle: pending proposals list ─────────────────── */}
          <div className="lg:col-span-1 space-y-3">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
              Pending anomaly rules ({proposals.length})
            </h2>
            {proposals.length === 0 && (
              <p className="text-sm text-gray-500 dark:text-gray-400 italic">Nothing pending review.</p>
            )}
            {proposals.map(p => {
              const isSelected = selected?.id === p.id;
              return (
                <button
                  key={p.id}
                  onClick={() => { setSelected(p); setRejectReason(''); }}
                  className={`w-full text-left p-3 rounded-lg border transition-colors ${
                    isSelected
                      ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/20'
                      : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700/50'
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                        {shapeLabel(p.template_shape)} — {p.metric_name || p.column_name || '(table)'}
                      </p>
                      <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                        {p.database_name}.{p.schema_name}.{p.table_name}
                        {p.column_name ? ` · ${p.column_name}` : ''}
                      </p>
                    </div>
                    <ChevronRight className="w-4 h-4 text-gray-400 flex-shrink-0" />
                  </div>
                  {p.severity && (
                    <span className={`mt-2 inline-block px-1.5 py-0.5 rounded text-[10px] font-medium ${severityColor(p.severity)}`}>
                      {p.severity}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          {/* ── Right: detail + actions ─────────────────────── */}
          <div className="lg:col-span-1">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-3">
              Details
            </h2>
            {!selected ? (
              <div className="p-6 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-center text-sm text-gray-500 dark:text-gray-400">
                Select a proposal to review.
              </div>
            ) : (
              <div className="p-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 space-y-3">
                <div>
                  <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Rule shape</p>
                  <p className="text-sm font-mono text-gray-900 dark:text-gray-100">{selected.template_shape}</p>
                </div>
                <div>
                  <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Target</p>
                  <p className="text-sm text-gray-900 dark:text-gray-100">
                    {selected.database_name}.{selected.schema_name}.{selected.table_name}
                    {selected.column_name ? ` · ${selected.column_name}` : ''}
                  </p>
                </div>
                {selected.metric_name && (
                  <div>
                    <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Metric</p>
                    <p className="text-sm text-gray-900 dark:text-gray-100">{selected.metric_name}</p>
                  </div>
                )}
                {selected.rationale && (
                  <div>
                    <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Rationale</p>
                    <p className="text-sm text-gray-700 dark:text-gray-300">{selected.rationale}</p>
                  </div>
                )}
                {selected.threshold_config && Object.keys(selected.threshold_config).length > 0 && (
                  <div>
                    <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Thresholds</p>
                    <pre className="text-xs font-mono bg-gray-50 dark:bg-gray-900 p-2 rounded overflow-x-auto">
                      {JSON.stringify(selected.threshold_config, null, 2)}
                    </pre>
                  </div>
                )}

                <div className="pt-3 border-t border-gray-200 dark:border-gray-700 space-y-2">
                  <button
                    disabled={busy}
                    onClick={() => approve(selected)}
                    className="w-full inline-flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
                  >
                    <CheckCircle className="w-4 h-4" /> Approve
                  </button>
                  <textarea
                    value={rejectReason}
                    onChange={e => setRejectReason(e.target.value)}
                    placeholder="Reject reason (optional — feeds AI memory to prevent re-proposal)"
                    rows={2}
                    className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1.5 bg-white dark:bg-gray-900 dark:text-gray-100"
                  />
                  <button
                    disabled={busy}
                    onClick={() => reject(selected)}
                    className="w-full inline-flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                  >
                    <XCircle className="w-4 h-4" /> Reject
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Maintenance proposals ─────────────────────────────── */}
      {!loading && (
        <div className="mt-10">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <Wrench className="w-5 h-5 text-gray-500" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                Rule maintenance ({maintenance.length})
              </h2>
            </div>
            <button
              onClick={runSweep}
              disabled={busy}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
            >
              <PlayCircle className="w-4 h-4" /> Run sweep
            </button>
          </div>
          {sweepMsg && (
            <p className="mb-3 text-xs text-gray-600 dark:text-gray-400">{sweepMsg}</p>
          )}
          {maintenance.length === 0 ? (
            <p className="text-sm text-gray-500 dark:text-gray-400 italic">
              No maintenance proposals. Run a sweep to look for retire / flapping / superseded / obsolete rules.
            </p>
          ) : (
            <div className="space-y-2">
              {maintenance.map(m => (
                <div
                  key={m.id}
                  className="p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 flex items-start justify-between gap-3"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium ${actionColor(m.action)}`}>
                        {actionLabel(m.action)}
                      </span>
                      <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                        {m.instance_summary?.definition_name || m.instance_id}
                      </p>
                    </div>
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400 truncate">
                      {m.instance_summary
                        ? `${m.instance_summary.database_name}.${m.instance_summary.schema_name}.${m.instance_summary.table_name}`
                        : '(instance not found)'}
                    </p>
                    {m.reason && (
                      <p className="mt-1 text-xs text-gray-700 dark:text-gray-300">{m.reason}</p>
                    )}
                  </div>
                  <div className="flex flex-col gap-1.5 flex-shrink-0">
                    <button
                      disabled={busy}
                      onClick={() => approveMaintenance(m)}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
                    >
                      <CheckCircle className="w-3 h-3" /> Apply
                    </button>
                    <button
                      disabled={busy}
                      onClick={() => dismissMaintenance(m)}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                    >
                      <XCircle className="w-3 h-3" /> Dismiss
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function actionLabel(action: string): string {
  switch (action) {
    case 'retire_candidate': return 'Retire';
    case 'flapping':         return 'Flapping';
    case 'superseded':       return 'Superseded';
    case 'obsolete_target':  return 'Obsolete';
    default: return action;
  }
}

function actionColor(action: string): string {
  switch (action) {
    case 'flapping':        return 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300';
    case 'obsolete_target': return 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300';
    case 'superseded':      return 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300';
    case 'retire_candidate':
    default: return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
  }
}

function shapeLabel(shape: string | null): string {
  switch (shape) {
    case 'metric_anomaly': return 'Metric anomaly';
    case 'metric_relative_change': return 'Relative change';
    case 'category_disappeared': return 'Category disappeared';
    default: return shape || 'Rule';
  }
}

function severityColor(sev: string): string {
  switch (sev.toLowerCase()) {
    case 'critical':
    case 'high':
      return 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300';
    case 'medium':
      return 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300';
    case 'low':
    case 'info':
    default:
      return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
  }
}

import { useEffect, useState, useMemo } from 'react';
import {
  Wrench, PlayCircle, RefreshCw, CheckCircle, XCircle, Loader2,
  ChevronDown, ChevronRight,
} from 'lucide-react';
import { maintenanceApi, type MaintenanceProposal } from '../api/client';

type ActionKey = 'retire_candidate' | 'flapping' | 'superseded' | 'obsolete_target';

const ACTION_ORDER: ActionKey[] = ['obsolete_target', 'flapping', 'superseded', 'retire_candidate'];

const ACTION_META: Record<ActionKey, { label: string; description: string; color: string }> = {
  obsolete_target: {
    label: 'Obsolete target',
    description: 'The referenced table no longer exists — the rule cannot run.',
    color: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  },
  flapping: {
    label: 'Flapping',
    description: 'Rule has repeatedly opened and closed findings — likely mis-tuned.',
    color: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
  },
  superseded: {
    label: 'Superseded',
    description: 'A newer active rule covers the same target — this one is redundant.',
    color: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  },
  retire_candidate: {
    label: 'Retire candidate',
    description: 'No failures in 90 days — consider pausing to reduce noise.',
    color: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200',
  },
};

function actionMeta(action: string) {
  return ACTION_META[action as ActionKey] ?? {
    label: action, description: '', color: 'bg-gray-100 text-gray-700',
  };
}

export default function Maintenance() {
  const [proposals, setProposals] = useState<MaintenanceProposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [sweepMsg, setSweepMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await maintenanceApi.listPending(200);
      setProposals(r.data.items);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to load maintenance proposals');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function runSweep() {
    setBusy(true);
    setSweepMsg(null);
    try {
      const r = await maintenanceApi.runSweep();
      setSweepMsg(
        `Scanned ${r.data.scanned} instance${r.data.scanned === 1 ? '' : 's'}, ` +
        `created ${r.data.proposals_created} proposal${r.data.proposals_created === 1 ? '' : 's'}.`
      );
      await load();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Maintenance sweep failed');
    } finally {
      setBusy(false);
    }
  }

  async function approve(p: MaintenanceProposal) {
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

  async function dismiss(p: MaintenanceProposal) {
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

  const grouped = useMemo(() => {
    const g: Record<string, MaintenanceProposal[]> = {};
    for (const p of proposals) (g[p.action] ??= []).push(p);
    return g;
  }, [proposals]);

  return (
    <div className="max-w-5xl mx-auto">
      <div className="flex items-start justify-between mb-6 gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <Wrench className="w-6 h-6 text-primary-600" />
            <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">Rule maintenance</h1>
            {proposals.length > 0 && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-primary-100 text-primary-800 dark:bg-primary-900/40 dark:text-primary-300">
                {proposals.length} pending
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            MaintenanceAgent scans active rules weekly and flags cleanup candidates.
            Review and apply, or dismiss to suppress the suggestion permanently.
          </p>
        </div>
        <div className="flex gap-2 flex-shrink-0">
          <button
            onClick={load}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
          >
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>
          <button
            onClick={runSweep}
            disabled={busy}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50"
          >
            <PlayCircle className="w-4 h-4" /> Run sweep now
          </button>
        </div>
      </div>

      {sweepMsg && (
        <div className="mb-4 p-3 rounded-lg bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-200 dark:border-emerald-800 text-sm text-emerald-800 dark:text-emerald-200">
          {sweepMsg}
        </div>
      )}
      {error && (
        <div className="mb-4 p-3 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-sm text-red-700 dark:text-red-200">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
        </div>
      ) : proposals.length === 0 ? (
        <div className="p-8 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-center">
          <Wrench className="w-8 h-8 mx-auto text-gray-300 dark:text-gray-600 mb-3" />
          <p className="text-sm text-gray-600 dark:text-gray-300 font-medium mb-1">Nothing to clean up</p>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            The last sweep didn't find any stale, flapping, redundant, or obsolete rules.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {ACTION_ORDER.filter(a => grouped[a]?.length).map(action => {
            const meta = actionMeta(action);
            const items = grouped[action];
            return (
              <div key={action}>
                <div className="flex items-center gap-2 mb-2">
                  <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${meta.color}`}>
                    {meta.label}
                  </span>
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {items.length} · {meta.description}
                  </span>
                </div>
                <div className="space-y-2">
                  {items.map(m => {
                    const isOpen = !!expanded[m.id];
                    const hasEvidence = m.evidence && Object.keys(m.evidence).length > 0;
                    return (
                      <div
                        key={m.id}
                        className="p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0 flex-1">
                            <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                              {m.instance_summary?.definition_name || m.instance_id}
                            </p>
                            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 truncate">
                              {m.instance_summary
                                ? [m.instance_summary.database_name, m.instance_summary.schema_name, m.instance_summary.table_name]
                                    .filter(p => p && p !== 'NONE' && p !== 'null')
                                    .join('.') || '(unknown target)'
                                : '(instance not found)'}
                            </p>
                            {m.reason && (
                              <p className="mt-1 text-xs text-gray-700 dark:text-gray-300">{m.reason}</p>
                            )}
                            {hasEvidence && (
                              <button
                                onClick={() => setExpanded(prev => ({ ...prev, [m.id]: !prev[m.id] }))}
                                className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100"
                              >
                                {isOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                                Evidence
                              </button>
                            )}
                            {isOpen && hasEvidence && (
                              <pre className="mt-1 text-[11px] font-mono bg-gray-50 dark:bg-gray-900 p-2 rounded overflow-x-auto text-gray-700 dark:text-gray-300">
                                {JSON.stringify(m.evidence, null, 2)}
                              </pre>
                            )}
                          </div>
                          <div className="flex flex-col gap-1.5 flex-shrink-0">
                            <button
                              disabled={busy}
                              onClick={() => approve(m)}
                              className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
                            >
                              <CheckCircle className="w-3 h-3" /> Apply
                            </button>
                            <button
                              disabled={busy}
                              onClick={() => dismiss(m)}
                              className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                            >
                              <XCircle className="w-3 h-3" /> Dismiss
                            </button>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

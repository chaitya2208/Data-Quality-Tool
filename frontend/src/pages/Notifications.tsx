import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Bell, Loader2, RefreshCw, CheckCheck, Inbox, ExternalLink,
  Sparkles, Wrench,
} from 'lucide-react';
import { notificationsApi, type Notification } from '../api/client';

// Read-only history of every notification the system has ever emitted.
// Live triage lives elsewhere: anomaly proposals → /rule-library, maintenance
// proposals → /maintenance. This page is here so nothing the bell told you
// gets lost after you dismiss it.

const KIND_META: Record<string, { label: string; icon: React.ReactNode; href: (n: Notification) => string }> = {
  anomaly_proposals: {
    label: 'Anomaly proposal',
    icon: <Sparkles className="w-3.5 h-3.5" />,
    href: n => n.ref_id
      ? `/rule-library?ref=${encodeURIComponent(n.ref_id)}`
      : '/rule-library',
  },
  maintenance_proposals: {
    label: 'Rule maintenance',
    icon: <Wrench className="w-3.5 h-3.5" />,
    href: () => '/maintenance',
  },
};

function kindMeta(kind: string) {
  return KIND_META[kind] ?? {
    label: kind,
    icon: <Bell className="w-3.5 h-3.5" />,
    href: () => '/',
  };
}

export default function Notifications() {
  const [items, setItems] = useState<Notification[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [showUnreadOnly, setShowUnreadOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await notificationsApi.list({ limit: 100, unread_only: showUnreadOnly });
      setItems(r.data.items);
    } catch (e: any) {
      setError(e?.message || 'Failed to load notifications');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [showUnreadOnly]);

  async function markAllRead() {
    setBusy(true);
    try {
      await notificationsApi.markAllRead();
      await load();
    } catch (e: any) {
      setError(e?.message || 'Mark all read failed');
    } finally {
      setBusy(false);
    }
  }

  const unreadCount = items.filter(n => !n.read_at).length;

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex items-start justify-between mb-6 gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <Bell className="w-6 h-6 text-primary-600" />
            <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">Notifications</h1>
            {unreadCount > 0 && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-primary-100 text-primary-800 dark:bg-primary-900/40 dark:text-primary-300">
                {unreadCount} unread
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            History of system alerts. Click a row to jump to the relevant workspace.
          </p>
        </div>
        <div className="flex gap-2 flex-shrink-0">
          <label className="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-200 px-3 py-1.5 border border-gray-300 dark:border-gray-600 rounded-lg">
            <input
              type="checkbox"
              checked={showUnreadOnly}
              onChange={e => setShowUnreadOnly(e.target.checked)}
              className="rounded border-gray-300 text-primary-600 focus:ring-primary-500"
            />
            Unread only
          </label>
          <button
            onClick={load}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
          >
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>
          {unreadCount > 0 && (
            <button
              onClick={markAllRead}
              disabled={busy}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
            >
              <CheckCheck className="w-4 h-4" /> Mark all read
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
      ) : items.length === 0 ? (
        <div className="p-8 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-center">
          <Inbox className="w-8 h-8 mx-auto text-gray-300 dark:text-gray-600 mb-3" />
          <p className="text-sm text-gray-600 dark:text-gray-300 font-medium mb-1">
            {showUnreadOnly ? 'No unread notifications' : 'No notifications yet'}
          </p>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Scheduled scans and the weekly maintenance sweep will land here.
          </p>
        </div>
      ) : (
        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 divide-y divide-gray-100 dark:divide-gray-700 overflow-hidden">
          {items.map(n => {
            const meta = kindMeta(n.kind);
            return (
              <Link
                key={n.id}
                to={meta.href(n)}
                className={`block px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors ${
                  n.read_at ? '' : 'bg-primary-50/40 dark:bg-primary-900/10'
                }`}
              >
                <div className="flex items-start gap-3">
                  <span
                    className={`mt-1.5 w-2 h-2 rounded-full flex-shrink-0 ${
                      n.read_at ? 'bg-transparent' : 'bg-primary-500'
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
                        {meta.icon} {meta.label}
                      </span>
                      <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                        {n.title}
                      </p>
                    </div>
                    {n.body && (
                      <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 line-clamp-2">{n.body}</p>
                    )}
                    <p className="mt-1 text-[11px] text-gray-400">
                      {n.created_at ? new Date(n.created_at).toLocaleString() : ''}
                    </p>
                  </div>
                  <ExternalLink className="w-4 h-4 text-gray-300 dark:text-gray-500 flex-shrink-0 mt-1" />
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}

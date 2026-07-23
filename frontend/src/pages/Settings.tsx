import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { settingsApi, aiApi } from '../api/client'
import { Plug, Info, Palette, Loader2, CheckCircle2, XCircle, Monitor, Sun, Moon, Snowflake, Server, KeyRound } from 'lucide-react'
import Connections from './Connections'
import { useTheme } from '../ThemeContext'
import type { ThemeMode } from '../ThemeContext'

type Tab = 'connections' | 'session' | 'appearance' | 'system'

const TABS: { id: Tab; label: string; icon: any }[] = [
  { id: 'connections', label: 'Connections', icon: Plug },
  { id: 'session',     label: 'Session',     icon: KeyRound },
  { id: 'appearance',  label: 'Appearance',  icon: Palette },
  { id: 'system',      label: 'System',      icon: Info },
]

// ── Appearance tab ──────────────────────────────────────────────────────────
function AppearanceTab() {
  const { mode, setMode } = useTheme()
  const opts: { id: ThemeMode; label: string; icon: any }[] = [
    { id: 'light', label: 'Light', icon: Sun },
    { id: 'dark', label: 'Dark', icon: Moon },
    { id: 'system', label: 'System', icon: Monitor },
  ]
  return (
    <div className="space-y-4 max-w-md">
      <p className="text-sm text-gray-600 dark:text-gray-200">Theme is saved in this browser.</p>
      <div className="grid grid-cols-3 gap-3">
        {opts.map(o => {
          const Icon = o.icon
          const active = mode === o.id
          return (
            <button key={o.id} onClick={() => setMode(o.id)}
              className={`flex flex-col items-center gap-2 rounded-xl border-2 px-3 py-4 transition-all ${
                active ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/20' : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
              }`}>
              <Icon className={`w-6 h-6 ${active ? 'text-primary-600' : 'text-gray-500 dark:text-gray-200'}`} />
              <span className={`text-sm font-medium ${active ? 'text-primary-700 dark:text-primary-300' : 'text-gray-700 dark:text-gray-200'}`}>{o.label}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Session tab: UI-configured default role + warehouse ───────────────────
// These override .env at runtime. Empty = fall back to .env / user default.
function SessionTab() {
  const qc = useQueryClient()
  const settingsQ = useQuery({
    queryKey: ['settings'],
    queryFn: () => settingsApi.get().then(r => r.data),
  })
  const contextQ = useQuery({
    queryKey: ['ai-context'],
    queryFn: () => aiApi.getContext().then(r => r.data),
  })
  const [role, setRole] = useState('')
  const [wh, setWh] = useState('')
  const [saved, setSaved] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [errMsg, setErrMsg] = useState<string | null>(null)

  useEffect(() => {
    if (settingsQ.data) {
      const r = (settingsQ.data as any).default_role?.value ?? ''
      const w = (settingsQ.data as any).default_warehouse?.value ?? ''
      setRole(r); setWh(w)
    }
  }, [settingsQ.data])

  const save = useMutation({
    mutationFn: () => settingsApi.update({ default_role: role, default_warehouse: wh } as any),
    onMutate: () => { setSaved('saving'); setErrMsg(null) },
    onSuccess: () => {
      setSaved('saved')
      qc.invalidateQueries({ queryKey: ['settings'] })
      qc.invalidateQueries({ queryKey: ['ai-context'] })
      setTimeout(() => setSaved('idle'), 2000)
    },
    onError: (e: any) => {
      setSaved('error')
      setErrMsg(e?.response?.data?.detail ?? e?.message ?? 'Save failed')
    },
  })

  if (settingsQ.isLoading || contextQ.isLoading) {
    return <div className="p-6 text-sm text-gray-400 dark:text-gray-300 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />Loading…</div>
  }

  const ctx = contextQ.data
  const rolesList = ctx?.roles ?? []
  const whList = ctx?.warehouses ?? []

  return (
    <div className="max-w-xl space-y-6">
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-sm">
        <div className="text-gray-500 dark:text-gray-300 mb-1">Signed in as</div>
        <div className="font-medium text-gray-900 dark:text-gray-100">{ctx?.user}</div>
        <div className="mt-2 text-xs text-gray-500 dark:text-gray-300">
          Active role: <span className="text-gray-900 dark:text-gray-100 font-medium">{ctx?.current_role || '—'}</span>
        </div>
      </div>

      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-200 mb-1">Default role</label>
          <select value={role} onChange={e => setRole(e.target.value)}
            className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 text-sm">
            <option value="">— use .env default —</option>
            {rolesList.map(r => (
              <option key={r.name} value={r.name}>{r.name}{r.is_default ? ' (default)' : ''}</option>
            ))}
          </select>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-300">Applied to the app-storage session on save. Overrides SNOWFLAKE_ROLE from .env.</p>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-200 mb-1">Default warehouse</label>
          <select value={wh} onChange={e => setWh(e.target.value)}
            className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 text-sm">
            <option value="">— use .env default —</option>
            {whList.map(w => (
              <option key={w.name} value={w.name}>{w.name} · {w.size} · {w.state}</option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-3 pt-2">
          <button onClick={() => save.mutate()} disabled={save.isPending}
            className="rounded-md bg-primary-600 hover:bg-primary-700 disabled:opacity-60 text-white px-4 py-2 text-sm font-medium">
            {save.isPending ? 'Saving…' : 'Save'}
          </button>
          {saved === 'saved' && <span className="text-sm text-green-600 flex items-center gap-1"><CheckCircle2 className="w-4 h-4" />Applied to session</span>}
          {saved === 'error' && <span className="text-sm text-red-600 flex items-center gap-1"><XCircle className="w-4 h-4" />{errMsg}</span>}
        </div>
      </div>
    </div>
  )
}

// ── System info tab ─────────────────────────────────────────────────────────
function SystemTab() {
  const { data, isLoading } = useQuery({
    queryKey: ['system-info'],
    queryFn: () => settingsApi.systemInfo().then(r => r.data),
    refetchInterval: 30_000,
  })
  if (isLoading || !data) {
    return <div className="p-6 text-sm text-gray-400 dark:text-gray-300 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />Loading…</div>
  }
  return (
    <div className="max-w-2xl space-y-5">
      <div className="flex gap-6 text-sm">
        <div><span className="text-gray-500 dark:text-gray-200">Backend</span>{' '}
          <span className="text-green-600 font-medium">{data.backend}</span></div>
        <div><span className="text-gray-500 dark:text-gray-200">Connections</span>{' '}
          <span className="text-gray-900 dark:text-gray-100 font-medium">{data.connections_count}</span></div>
      </div>

      <div className="space-y-3">
        {data.connections.length === 0 && (
          <p className="text-sm text-gray-400 dark:text-gray-300">No connections saved yet.</p>
        )}
        {data.connections.map(c => (
          <div key={c.id} className="rounded-lg border border-gray-200 dark:border-gray-700 p-4">
            <div className="flex items-center justify-between gap-3 mb-2">
              <div className="flex items-center gap-2 min-w-0">
                {c.type === 'snowflake'
                  ? <Snowflake className="w-4 h-4 text-sky-500 flex-shrink-0" />
                  : <Server className="w-4 h-4 text-emerald-500 flex-shrink-0" />}
                <span className="font-semibold text-gray-900 dark:text-gray-100 truncate">{c.name}</span>
                <span className="text-xs text-gray-400 dark:text-gray-300">{c.type}</span>
              </div>
              {c.connected
                ? <span className="flex items-center gap-1 text-xs text-green-600"><CheckCircle2 className="w-3.5 h-3.5" />connected{c.connected_user ? ` · ${c.connected_user}` : ''}</span>
                : <span className="flex items-center gap-1 text-xs text-red-600" title={c.detail ?? ''}><XCircle className="w-3.5 h-3.5" />unreachable</span>}
            </div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-gray-500 dark:text-gray-200">
              {c.host && <div><span className="text-gray-400 dark:text-gray-300">Host:</span> {c.host}</div>}
              {c.database && <div><span className="text-gray-400 dark:text-gray-300">Database:</span> {c.database}</div>}
              {c.username && <div><span className="text-gray-400 dark:text-gray-300">User:</span> {c.username}</div>}
              {c.warehouse && <div><span className="text-gray-400 dark:text-gray-300">Warehouse:</span> {c.warehouse}</div>}
              {c.role && <div><span className="text-gray-400 dark:text-gray-300">Role:</span> {c.role}</div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Settings() {
  const [tab, setTab] = useState<Tab>('connections')

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Settings</h1>
        <p className="mt-1 text-gray-600 dark:text-gray-200">Connections, appearance, and system info.</p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-200 dark:border-gray-700 overflow-x-auto">
        {TABS.map(t => {
          const Icon = t.icon
          const active = tab === t.id
          return (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                active ? 'border-primary-500 text-primary-700 dark:text-primary-300' : 'border-transparent text-gray-500 dark:text-gray-200 hover:text-gray-700 dark:hover:text-gray-200'
              }`}>
              <Icon className="w-4 h-4" />{t.label}
            </button>
          )
        })}
      </div>

      <div>
        {tab === 'connections' && <Connections embedded />}
        {tab === 'session'     && <SessionTab />}
        {tab === 'appearance'  && <AppearanceTab />}
        {tab === 'system'      && <SystemTab />}
      </div>
    </div>
  )
}

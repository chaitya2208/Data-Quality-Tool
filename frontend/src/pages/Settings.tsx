import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { settingsApi } from '../api/client'
import type { SettingsMap } from '../api/client'
import { Plug, SlidersHorizontal, Info, Palette, Loader2, Save, CheckCircle2, XCircle, Monitor, Sun, Moon, Snowflake, Server } from 'lucide-react'
import Connections from './Connections'
import { useTheme } from '../ThemeContext'
import type { ThemeMode } from '../ThemeContext'

type Tab = 'connections' | 'profiling' | 'appearance' | 'system'

const TABS: { id: Tab; label: string; icon: any }[] = [
  { id: 'connections', label: 'Connections', icon: Plug },
  { id: 'profiling',   label: 'Profiling',   icon: SlidersHorizontal },
  { id: 'appearance',  label: 'Appearance',  icon: Palette },
  { id: 'system',      label: 'System',      icon: Info },
]

// ── Profiling preferences tab ───────────────────────────────────────────────
function ProfilingTab() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: () => settingsApi.get().then(r => r.data),
  })
  const [draft, setDraft] = useState<Record<string, number>>({})
  const [saved, setSaved] = useState(false)

  const save = useMutation({
    mutationFn: (updates: Record<string, number>) => settingsApi.update(updates).then(r => r.data),
    onSuccess: (fresh) => {
      qc.setQueryData(['settings'], fresh)
      setDraft({}); setSaved(true); setTimeout(() => setSaved(false), 2000)
    },
  })

  if (isLoading || !data) {
    return <div className="p-6 text-sm text-gray-400 dark:text-gray-300 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />Loading…</div>
  }

  const settings = data as SettingsMap
  const value = (k: string) => (k in draft ? draft[k] : settings[k].value)
  const dirty = Object.keys(draft).length > 0

  return (
    <div className="space-y-5 max-w-2xl">
      <p className="text-sm text-gray-600 dark:text-gray-200">
        Tune how the profiling engine classifies columns and flags anomalies. Changes apply to future profiling runs.
      </p>
      {Object.entries(settings).map(([key, meta]) => (
        <div key={key} className="flex items-start justify-between gap-6 py-3 border-b border-gray-100 dark:border-gray-700">
          <div className="min-w-0">
            <label className="text-sm font-medium text-gray-800 dark:text-gray-200">{meta.label}</label>
            <p className="text-xs text-gray-500 dark:text-gray-200 mt-0.5">{meta.help}</p>
            <p className="text-[11px] text-gray-400 dark:text-gray-300 mt-1">Default {meta.default} · range {meta.min}–{meta.max}</p>
          </div>
          <input
            type="number" step={meta.type === 'float' ? 0.5 : 1} min={meta.min} max={meta.max}
            value={value(key)}
            onChange={e => setDraft(d => ({ ...d, [key]: meta.type === 'float' ? parseFloat(e.target.value) : parseInt(e.target.value, 10) }))}
            className="w-28 flex-shrink-0 px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm"
          />
        </div>
      ))}
      <div className="flex items-center gap-3">
        <button onClick={() => save.mutate(draft)} disabled={!dirty || save.isPending}
          className="flex items-center gap-2 px-4 py-2 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700 disabled:opacity-50">
          {save.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}Save Changes
        </button>
        {saved && <span className="flex items-center gap-1 text-sm text-green-600"><CheckCircle2 className="w-4 h-4" />Saved</span>}
      </div>
    </div>
  )
}

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
        <p className="mt-1 text-gray-600 dark:text-gray-200">Connections, profiling preferences, appearance, and system info.</p>
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
        {tab === 'profiling'   && <ProfilingTab />}
        {tab === 'appearance'  && <AppearanceTab />}
        {tab === 'system'      && <SystemTab />}
      </div>
    </div>
  )
}

import { useState, useEffect, useRef } from 'react'
import { Routes, Route, Link, useLocation, useNavigate } from 'react-router-dom'
import { Home, Database, AlertCircle, GitBranch, Menu, Library, Compass, Plug, Settings as SettingsIcon, Snowflake, Server, BookOpen, History, Clock, Bell, Waypoints, CheckCheck, Inbox, ChevronRight, Wrench } from 'lucide-react'
import Dashboard from './pages/Dashboard'
import Findings from './pages/Findings'
import AgentWorkflow from './pages/AgentWorkflow'
import AIFix from './pages/AIFix'
import RuleLibrary from './pages/RuleLibrary'
import DataExplorer from './pages/DataExplorer'
import Connections from './pages/Connections'
import SettingsPage from './pages/Settings'
import SavedWorkflows from './pages/SavedWorkflows'
import Schedules from './pages/Schedules'
import RunHistory from './pages/RunHistory'
import Notifications from './pages/Notifications'
import Maintenance from './pages/Maintenance'
import MetricDetail from './pages/MetricDetail'
import Lineage from './pages/Lineage'
import { useConnection } from './ConnectionContext'
import { notificationsApi, type Notification } from './api/client'

function NotificationsBell() {
  const navigate = useNavigate()
  const [unread, setUnread] = useState(0)
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<Notification[]>([])
  const [listLoading, setListLoading] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  async function refreshCount() {
    try {
      const r = await notificationsApi.unreadCount()
      setUnread(r.data.unread)
      return true
    } catch {
      return false
    }
  }

  // Poll unread-count with backoff — auth loss / backend hiccups shouldn't
  // hammer the endpoint every minute (each 500 triggers an SSO retry on the
  // backend). Doubles up to a 10-min ceiling; resets on success.
  useEffect(() => {
    let cancelled = false
    let failures = 0
    let timeoutId: ReturnType<typeof setTimeout> | null = null
    async function poll() {
      const ok = await refreshCount()
      if (cancelled) return
      failures = ok ? 0 : Math.min(failures + 1, 6)
      const nextMs = failures === 0 ? 60_000 : Math.min(60_000 * (2 ** failures), 600_000)
      timeoutId = setTimeout(poll, nextMs)
    }
    poll()
    return () => {
      cancelled = true
      if (timeoutId) clearTimeout(timeoutId)
    }
  }, [])

  async function loadList() {
    setListLoading(true)
    setListError(null)
    try {
      const r = await notificationsApi.list({ limit: 20 })
      setItems(r.data.items)
    } catch (e: any) {
      setListError(e?.message || 'Failed to load')
    } finally {
      setListLoading(false)
    }
  }

  // Open/close: fetch fresh list on open, close on outside-click / ESC.
  useEffect(() => {
    if (!open) return
    loadList()
    function onDocClick(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  async function onItemClick(n: Notification) {
    if (!n.read_at) {
      try { await notificationsApi.markRead(n.id) } catch (e) { console.error(e) }
      setItems(prev => prev.map(x => x.id === n.id ? { ...x, read_at: new Date().toISOString() } : x))
      refreshCount()
    }
    // Route to the right workspace by notification kind. Anomaly proposals
    // live in the Rule Library alongside other pending rules; maintenance
    // proposals have their own page; anything else falls back to the history.
    if (n.kind === 'anomaly_proposals' && n.ref_id) {
      navigate(`/rule-library?ref=${encodeURIComponent(n.ref_id)}`)
    } else if (n.kind === 'anomaly_proposals') {
      navigate('/rule-library')
    } else if (n.kind === 'maintenance_proposals') {
      navigate('/maintenance')
    } else {
      navigate('/notifications')
    }
    setOpen(false)
  }

  async function markAllRead() {
    try {
      await notificationsApi.markAllRead()
      setItems(prev => prev.map(x => x.read_at ? x : { ...x, read_at: new Date().toISOString() }))
      refreshCount()
    } catch (e) { console.error(e) }
  }

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen(v => !v)}
        aria-label="Notifications"
        aria-expanded={open}
        className={`relative inline-flex items-center justify-center w-9 h-9 rounded-lg transition-colors ${
          open
            ? 'bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-gray-100'
            : 'text-gray-500 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
        }`}
      >
        <Bell className="w-5 h-5" />
        {unread > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-red-500 text-white text-[10px] font-semibold inline-flex items-center justify-center">
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Notifications"
          className="absolute right-0 mt-2 w-[380px] max-w-[calc(100vw-1.5rem)] rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-2xl z-30 overflow-hidden"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-700">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">Notifications</span>
              {unread > 0 && (
                <span className="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-primary-100 text-primary-800 dark:bg-primary-900/40 dark:text-primary-300">
                  {unread} new
                </span>
              )}
            </div>
            {unread > 0 && (
              <button
                onClick={markAllRead}
                className="inline-flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
              >
                <CheckCheck className="w-3.5 h-3.5" /> Mark all read
              </button>
            )}
          </div>

          <div className="max-h-[420px] overflow-y-auto">
            {listLoading ? (
              <div className="py-10 text-center text-xs text-gray-400">Loading…</div>
            ) : listError ? (
              <div className="p-4 text-xs text-red-600 dark:text-red-300">{listError}</div>
            ) : items.length === 0 ? (
              <div className="py-10 px-4 text-center">
                <Inbox className="w-6 h-6 mx-auto text-gray-300 dark:text-gray-600 mb-2" />
                <p className="text-xs text-gray-500 dark:text-gray-400">You're all caught up.</p>
              </div>
            ) : (
              <ul className="divide-y divide-gray-100 dark:divide-gray-700">
                {items.map(n => (
                  <li key={n.id}>
                    <button
                      onClick={() => onItemClick(n)}
                      className={`w-full text-left px-4 py-3 flex items-start gap-2 transition-colors ${
                        n.read_at
                          ? 'hover:bg-gray-50 dark:hover:bg-gray-700/40'
                          : 'bg-primary-50/60 dark:bg-primary-900/10 hover:bg-primary-50 dark:hover:bg-primary-900/20'
                      }`}
                    >
                      <span
                        className={`mt-1.5 w-2 h-2 rounded-full flex-shrink-0 ${
                          n.read_at ? 'bg-transparent' : 'bg-primary-500'
                        }`}
                      />
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-gray-900 dark:text-gray-100 line-clamp-2">{n.title}</p>
                        {n.body && (
                          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 line-clamp-2">{n.body}</p>
                        )}
                        <p className="mt-1 text-[11px] text-gray-400">
                          {n.created_at ? new Date(n.created_at).toLocaleString() : ''}
                        </p>
                      </div>
                      <ChevronRight className="w-4 h-4 text-gray-300 dark:text-gray-500 flex-shrink-0 mt-1" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="border-t border-gray-100 dark:border-gray-700">
            <Link
              to="/notifications"
              onClick={() => setOpen(false)}
              className="block px-4 py-2.5 text-center text-xs font-medium text-primary-600 dark:text-primary-400 hover:bg-gray-50 dark:hover:bg-gray-700/50"
            >
              View all &amp; review pending
            </Link>
          </div>
        </div>
      )}
    </div>
  )
}

function App() {
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { connections, selectedId, setSelectedId, selected } = useConnection()

  const navigation = [
    { name: 'Dashboard',       href: '/',                icon: Home         },
    { name: 'Data Explorer',   href: '/explorer',        icon: Compass      },
    { name: 'Lineage',         href: '/lineage',         icon: Waypoints    },
    { name: 'Rule Library',    href: '/rule-library',    icon: Library      },
    { name: 'Findings',        href: '/findings',        icon: AlertCircle  },
    { name: 'Workflow',        href: '/workflow',        icon: GitBranch    },
    { name: 'Run History',     href: '/run-history',     icon: History      },
    { name: 'Saved Workflows', href: '/saved-workflows', icon: BookOpen     },
    { name: 'Schedules',       href: '/schedules',       icon: Clock        },
    { name: 'Maintenance',     href: '/maintenance',     icon: Wrench       },
    { name: 'Settings',        href: '/settings',        icon: SettingsIcon },
  ]

  const SidebarContent = ({ onNavClick }: { onNavClick?: () => void }) => (
    <div className="flex flex-col h-full">
      {/* Logo */}
      <div className="flex items-center h-16 px-6 border-b border-gray-200 dark:border-gray-700 flex-shrink-0">
        <Database className="w-8 h-8 text-primary-600 flex-shrink-0" />
        <span className="ml-3 text-xl font-semibold text-gray-900 dark:text-gray-100 truncate">Data Quality</span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-4 py-6 space-y-1 overflow-y-auto">
        {navigation.map((item) => {
          const isActive = location.pathname === item.href
          return (
            <Link
              key={item.name}
              to={item.href}
              onClick={onNavClick}
              className={`flex items-center px-4 py-3 text-sm font-medium rounded-lg transition-colors ${
                isActive
                  ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-700 dark:text-primary-300'
                  : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'
              }`}
            >
              <item.icon className="w-5 h-5 mr-3 flex-shrink-0" />
              {item.name}
            </Link>
          )
        })}
      </nav>

      {/* Footer — active data-source selector */}
      <div className="p-4 border-t border-gray-200 dark:border-gray-700 flex-shrink-0">
        <p className="text-[10px] font-bold uppercase tracking-wider text-gray-400 dark:text-gray-400 mb-1">Data Source</p>
        {connections.length === 0 ? (
          <Link to="/settings" onClick={onNavClick}
            className="flex items-center gap-1.5 text-xs text-primary-600 hover:text-primary-800 dark:text-primary-400">
            <Plug className="w-3.5 h-3.5" /> Add a connection
          </Link>
        ) : (
          <>
            <select
              value={selectedId ?? ''}
              onChange={e => setSelectedId(e.target.value)}
              className="w-full text-sm border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1.5 bg-white dark:bg-gray-800 dark:text-gray-100 focus:ring-2 focus:ring-primary-500"
            >
              {connections.map(c => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
            {selected && (
              <p className="text-xs text-gray-500 dark:text-gray-400 truncate mt-1 flex items-center gap-1">
                {selected.type === 'snowflake'
                  ? <Snowflake className="w-3 h-3 text-sky-500 flex-shrink-0" />
                  : <Server className="w-3 h-3 text-emerald-500 flex-shrink-0" />}
                {selected.type}{selected.host ? ` · ${selected.host}` : ''}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">

      {/* ── Mobile overlay ── */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-20 bg-black/50 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* ── Mobile sidebar drawer ── */}
      <div className={`
        fixed inset-y-0 left-0 z-30 w-64 bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700
        transform transition-transform duration-200 ease-in-out
        lg:hidden
        ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
      `}>
        <SidebarContent onNavClick={() => setSidebarOpen(false)} />
      </div>

      {/* ── Desktop sidebar (always visible ≥ lg) ── */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:left-0 lg:w-64 lg:flex lg:flex-col bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700">
        <SidebarContent />
      </div>

      {/* ── Main content ── */}
      <div className="lg:pl-64 flex flex-col min-h-screen">

        {/* Mobile top bar */}
        <div className="lg:hidden flex items-center justify-between h-14 px-4 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 sticky top-0 z-10">
          <button
            onClick={() => setSidebarOpen(true)}
            className="p-2 rounded-lg text-gray-500 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            aria-label="Open menu"
          >
            <Menu className="w-5 h-5" />
          </button>
          <div className="flex items-center gap-2">
            <Database className="w-5 h-5 text-primary-600" />
            <span className="text-base font-semibold text-gray-900 dark:text-gray-100">Data Quality</span>
          </div>
          <NotificationsBell />
        </div>

        {/* Desktop top bar — just the bell, right-aligned */}
        <div className="hidden lg:flex items-center justify-end h-12 px-6 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 sticky top-0 z-10">
          <NotificationsBell />
        </div>

        <main className="flex-1 p-4 sm:p-6 lg:p-8">
          <Routes>
            <Route path="/"                element={<Dashboard />}     />
            <Route path="/explorer"        element={<DataExplorer />}  />
            <Route path="/connections"     element={<Connections />}   />
            <Route path="/findings"        element={<Findings />}      />
            <Route path="/workflow"        element={<AgentWorkflow />} />
            <Route path="/run-history"     element={<RunHistory />}    />
            <Route path="/ai-fix"          element={<AIFix />}         />
            <Route path="/rule-library"    element={<RuleLibrary />}   />
            <Route path="/saved-workflows" element={<SavedWorkflows />}/>
            <Route path="/schedules"       element={<Schedules />}     />
            <Route path="/settings"        element={<SettingsPage />}  />
            <Route path="/notifications"   element={<Notifications />} />
            <Route path="/maintenance"     element={<Maintenance />}   />
            <Route path="/metrics/:assetId" element={<MetricDetail />} />
            <Route path="/lineage"         element={<Lineage />}       />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default App

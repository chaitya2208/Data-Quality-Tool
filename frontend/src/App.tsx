import { useState } from 'react'
import { Routes, Route, Link, useLocation } from 'react-router-dom'
import { Home, Database, AlertCircle, GitBranch, Menu, Library, Compass, Plug, Settings as SettingsIcon, Snowflake, Server, BookOpen, History, Clock } from 'lucide-react'
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
import { useConnection } from './ConnectionContext'

function App() {
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { connections, selectedId, setSelectedId, selected } = useConnection()

  const navigation = [
    { name: 'Dashboard',       href: '/',                icon: Home         },
    { name: 'Data Explorer',   href: '/explorer',        icon: Compass      },
    { name: 'Rule Library',    href: '/rule-library',    icon: Library      },
    { name: 'Findings',        href: '/findings',        icon: AlertCircle  },
    { name: 'Workflow',        href: '/workflow',        icon: GitBranch    },
    { name: 'Run History',     href: '/run-history',     icon: History      },
    { name: 'Saved Workflows', href: '/saved-workflows', icon: BookOpen     },
    { name: 'Schedules',       href: '/schedules',       icon: Clock        },
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
          {/* Current page name on mobile */}
          <div className="w-8" /> {/* spacer to center title */}
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
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default App

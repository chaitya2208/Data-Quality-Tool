import { useState } from 'react'
import { Routes, Route, Link, useLocation } from 'react-router-dom'
import { Home, Database, AlertCircle, GitBranch, Loader2, CheckCircle, ShieldCheck, Menu, X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { aiApi } from './api/client'
import Dashboard from './pages/Dashboard'
import Assets from './pages/Assets'
import Findings from './pages/Findings'
import AgentWorkflow from './pages/AgentWorkflow'
import AIFix from './pages/AIFix'
import Rules from './pages/Rules'

function App() {
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const { data: sfContext, isLoading: connectingSnowflake } = useQuery({
    queryKey: ['sf-context'],
    queryFn: () => aiApi.getContext().then(res => res.data),
    retry: 3,
    retryDelay: 2000,
    staleTime: Infinity,
  })

  const navigation = [
    { name: 'Dashboard', href: '/',         icon: Home        },
    { name: 'Assets',    href: '/assets',   icon: Database    },
    { name: 'Findings',  href: '/findings', icon: AlertCircle },
    { name: 'Rules',     href: '/rules',    icon: ShieldCheck },
    { name: 'Workflow',  href: '/workflow', icon: GitBranch   },
  ]

  if (connectingSnowflake) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
        <div className="text-center">
          <Loader2 className="w-12 h-12 text-primary-600 animate-spin mx-auto mb-4" />
          <h2 className="text-xl font-semibold text-gray-900 mb-2">Connecting to Snowflake...</h2>
          <p className="text-gray-600 mb-4">Please complete SSO authentication in your browser</p>
          <div className="text-sm text-gray-500 space-y-1">
            <p>✓ Opening browser for SSO login</p>
            <p className="animate-pulse">⏳ Waiting for authentication...</p>
          </div>
        </div>
      </div>
    )
  }

  const SidebarContent = ({ onNavClick }: { onNavClick?: () => void }) => (
    <div className="flex flex-col h-full">
      {/* Logo */}
      <div className="flex items-center h-16 px-6 border-b border-gray-200 flex-shrink-0">
        <Database className="w-8 h-8 text-primary-600 flex-shrink-0" />
        <span className="ml-3 text-xl font-semibold text-gray-900 truncate">Data Quality</span>
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
                  ? 'bg-primary-50 text-primary-700'
                  : 'text-gray-700 hover:bg-gray-100'
              }`}
            >
              <item.icon className="w-5 h-5 mr-3 flex-shrink-0" />
              {item.name}
            </Link>
          )
        })}
      </nav>

      {/* Footer */}
      <div className="p-4 border-t border-gray-200 flex-shrink-0">
        <div className="flex items-center text-xs text-gray-500 mb-1">
          <CheckCircle className="w-3 h-3 text-green-500 mr-1 flex-shrink-0" />
          <span>Snowflake Connected</span>
        </div>
        <p className="text-xs text-gray-400 truncate">{sfContext?.user}</p>
      </div>
    </div>
  )

  return (
    <div className="min-h-screen bg-gray-50">

      {/* ── Mobile overlay ── */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-20 bg-black/50 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* ── Mobile sidebar drawer ── */}
      <div className={`
        fixed inset-y-0 left-0 z-30 w-64 bg-white border-r border-gray-200
        transform transition-transform duration-200 ease-in-out
        lg:hidden
        ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
      `}>
        <SidebarContent onNavClick={() => setSidebarOpen(false)} />
      </div>

      {/* ── Desktop sidebar (always visible ≥ lg) ── */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:left-0 lg:w-64 lg:flex lg:flex-col bg-white border-r border-gray-200">
        <SidebarContent />
      </div>

      {/* ── Main content ── */}
      <div className="lg:pl-64 flex flex-col min-h-screen">

        {/* Mobile top bar */}
        <div className="lg:hidden flex items-center justify-between h-14 px-4 bg-white border-b border-gray-200 sticky top-0 z-10">
          <button
            onClick={() => setSidebarOpen(true)}
            className="p-2 rounded-lg text-gray-500 hover:bg-gray-100 transition-colors"
            aria-label="Open menu"
          >
            <Menu className="w-5 h-5" />
          </button>
          <div className="flex items-center gap-2">
            <Database className="w-5 h-5 text-primary-600" />
            <span className="text-base font-semibold text-gray-900">Data Quality</span>
          </div>
          {/* Current page name on mobile */}
          <div className="w-8" /> {/* spacer to center title */}
        </div>

        <main className="flex-1 p-4 sm:p-6 lg:p-8">
          <Routes>
            <Route path="/"         element={<Dashboard />}     />
            <Route path="/assets"   element={<Assets />}        />
            <Route path="/findings" element={<Findings />}      />
            <Route path="/workflow" element={<AgentWorkflow />} />
            <Route path="/ai-fix"   element={<AIFix />}         />
            <Route path="/rules"    element={<Rules />}         />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default App

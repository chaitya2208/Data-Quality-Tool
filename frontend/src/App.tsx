import { Routes, Route, Link, useLocation } from 'react-router-dom'
import { Home, Database, AlertCircle, GitBranch, Loader2, CheckCircle, ShieldCheck } from 'lucide-react'
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

  // Fetch Snowflake context once — served from backend startup cache, no SSO
  const { data: sfContext, isLoading: connectingSnowflake } = useQuery({
    queryKey: ['sf-context'],
    queryFn: () => aiApi.getContext().then(res => res.data),
    retry: 3,
    retryDelay: 2000,
    staleTime: Infinity,
  })

  const navigation = [
    { name: 'Dashboard', href: '/', icon: Home },
    { name: 'Assets', href: '/assets', icon: Database },
    { name: 'Findings', href: '/findings', icon: AlertCircle },
    { name: 'Rules', href: '/rules', icon: ShieldCheck },
    { name: 'Workflow', href: '/workflow', icon: GitBranch },
  ]

  // Show connection loading screen
  if (connectingSnowflake) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="w-12 h-12 text-primary-600 animate-spin mx-auto mb-4" />
          <h2 className="text-xl font-semibold text-gray-900 mb-2">
            Connecting to Snowflake...
          </h2>
          <p className="text-gray-600 mb-4">
            Please complete SSO authentication in your browser
          </p>
          <div className="text-sm text-gray-500 space-y-1">
            <p>✓ Opening browser for SSO login</p>
            <p className="animate-pulse">⏳ Waiting for authentication...</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Sidebar */}
      <div className="fixed inset-y-0 left-0 w-64 bg-white border-r border-gray-200">
        <div className="flex flex-col h-full">
          {/* Logo */}
          <div className="flex items-center h-16 px-6 border-b border-gray-200">
            <Database className="w-8 h-8 text-primary-600" />
            <span className="ml-3 text-xl font-semibold text-gray-900">
              Data Quality
            </span>
          </div>

          {/* Navigation */}
          <nav className="flex-1 px-4 py-6 space-y-1">
            {navigation.map((item) => {
              const isActive = location.pathname === item.href
              return (
                <Link
                  key={item.name}
                  to={item.href}
                  className={`flex items-center px-4 py-3 text-sm font-medium rounded-lg transition-colors ${
                    isActive
                      ? 'bg-primary-50 text-primary-700'
                      : 'text-gray-700 hover:bg-gray-100'
                  }`}
                >
                  <item.icon className="w-5 h-5 mr-3" />
                  {item.name}
                </Link>
              )
            })}
          </nav>

          {/* Footer */}
          <div className="p-4 border-t border-gray-200">
            <div className="flex items-center text-xs text-gray-500 mb-1">
              <CheckCircle className="w-3 h-3 text-green-500 mr-1" />
              <span>Snowflake Connected</span>
            </div>
            <p className="text-xs text-gray-400">
              {sfContext?.user}
            </p>
          </div>
        </div>
      </div>

      {/* Main content */}
      <div className="pl-64">
        <main className="py-8 px-8">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/assets" element={<Assets />} />
            <Route path="/findings" element={<Findings />} />
            <Route path="/workflow" element={<AgentWorkflow />} />
            <Route path="/ai-fix" element={<AIFix />} />
            <Route path="/rules" element={<Rules />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default App

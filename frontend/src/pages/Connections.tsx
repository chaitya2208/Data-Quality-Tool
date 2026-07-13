import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { connectionsApi } from '../api/client'
import type { ConnectionType, ConnectionCreatePayload } from '../api/client'
import {
  Database, Plus, Loader2, CheckCircle2, XCircle, Trash2, Snowflake, Server, X,
} from 'lucide-react'

const TYPE_META: Record<ConnectionType, { label: string; icon: any; tone: string }> = {
  snowflake: { label: 'Snowflake', icon: Snowflake, tone: 'text-sky-600' },
  postgres:  { label: 'Postgres (RDS)', icon: Server, tone: 'text-emerald-600' },
}

function StatusDot({ connId }: { connId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['conn-status', connId],
    queryFn: () => connectionsApi.status(connId).then(r => r.data),
    staleTime: 30_000,
    retry: false,
  })
  if (isLoading) return <span className="flex items-center gap-1 text-xs text-gray-400 dark:text-gray-400"><Loader2 className="w-3 h-3 animate-spin" />checking</span>
  if (data?.ok) return <span className="flex items-center gap-1 text-xs text-green-600"><CheckCircle2 className="w-3 h-3" />connected{data.user ? ` · ${data.user}` : ''}</span>
  return <span className="flex items-center gap-1 text-xs text-red-600" title={data?.detail ?? ''}><XCircle className="w-3 h-3" />error</span>
}

const EMPTY_FORM: ConnectionCreatePayload = {
  name: '', type: 'postgres', host: '', port: 5432, database: '', schema_name: 'public',
  username: '', secret: '', auth_method: '', extra: {},
}

export default function Connections({ embedded = false }: { embedded?: boolean }) {
  const qc = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<ConnectionCreatePayload>(EMPTY_FORM)
  const [testResult, setTestResult] = useState<Record<string, string>>({})

  const { data, isLoading } = useQuery({
    queryKey: ['connections'],
    queryFn: () => connectionsApi.list().then(r => r.data),
  })

  const createMutation = useMutation({
    mutationFn: (payload: ConnectionCreatePayload) => connectionsApi.create(payload).then(r => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['connections'] })
      setShowForm(false); setForm(EMPTY_FORM)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => connectionsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['connections'] }),
  })

  const testMutation = useMutation({
    mutationFn: (id: string) => connectionsApi.test(id).then(r => r.data),
    onSuccess: (res, id) => {
      setTestResult(prev => ({ ...prev, [id]: res.ok ? `✓ connected${res.user ? ` as ${res.user}` : ''}` : `✗ ${res.detail ?? 'failed'}` }))
      qc.invalidateQueries({ queryKey: ['conn-status', id] })
    },
  })

  const isSnowflake = form.type === 'snowflake'
  const canSubmit = form.name.trim() && form.type && (isSnowflake ? form.host : (form.host && form.database))

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          {!embedded && <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Connections</h1>}
          <p className={embedded ? 'text-gray-600 dark:text-gray-300 dark:text-gray-200' : 'mt-1 text-gray-600 dark:text-gray-300 dark:text-gray-200'}>
            Data sources this tool can profile, scan, and fix. Snowflake and Postgres/RDS supported.
          </p>
        </div>
        <button onClick={() => { setForm(EMPTY_FORM); setShowForm(true) }}
          className="flex items-center gap-2 px-4 py-2 bg-primary-600 text-white text-sm font-medium rounded-lg hover:bg-primary-700 flex-shrink-0">
          <Plus className="w-4 h-4" />Add Connection
        </button>
      </div>

      {/* List */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow overflow-hidden">
        {isLoading ? (
          <div className="p-8 text-sm text-gray-400 dark:text-gray-400 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />Loading…</div>
        ) : !data?.connections.length ? (
          <div className="p-12 text-center">
            <Database className="w-12 h-12 text-gray-200 mx-auto mb-3" />
            <p className="text-gray-900 dark:text-gray-100 font-medium mb-1">No connections yet</p>
            <p className="text-sm text-gray-400 dark:text-gray-400">Add a Snowflake or Postgres/RDS connection to get started.</p>
          </div>
        ) : (
          <ul className="divide-y divide-gray-100 dark:divide-gray-700">
            {data.connections.map(c => {
              const meta = TYPE_META[c.type] ?? TYPE_META.postgres
              const Icon = meta.icon
              return (
                <li key={c.id} className="flex items-center justify-between gap-4 px-6 py-4">
                  <div className="flex items-center gap-3 min-w-0">
                    <Icon className={`w-5 h-5 flex-shrink-0 ${meta.tone}`} />
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{c.name}</p>
                      <p className="text-xs text-gray-400 dark:text-gray-400 truncate">
                        {meta.label} · {c.host ?? '—'}{c.database ? ` / ${c.database}` : ''}
                      </p>
                      {testResult[c.id] && <p className="text-xs mt-0.5 text-gray-500 dark:text-gray-300">{testResult[c.id]}</p>}
                    </div>
                  </div>
                  <div className="flex items-center gap-3 flex-shrink-0">
                    <StatusDot connId={c.id} />
                    <button onClick={() => testMutation.mutate(c.id)} disabled={testMutation.isPending}
                      className="text-xs px-2.5 py-1 border border-gray-300 dark:border-gray-600 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40 disabled:opacity-50">
                      {testMutation.isPending && testMutation.variables === c.id ? 'Testing…' : 'Test'}
                    </button>
                    <button onClick={() => { if (confirm(`Delete connection "${c.name}"?`)) deleteMutation.mutate(c.id) }}
                      className="text-gray-300 hover:text-red-500" title="Delete">
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </div>

      {/* Add form modal */}
      {showForm && (
        <div className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center p-4" onClick={() => setShowForm(false)}>
          <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-lg p-6 space-y-4" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Add Connection</h2>
              <button onClick={() => setShowForm(false)} className="text-gray-400 dark:text-gray-400 hover:text-gray-700"><X className="w-5 h-5" /></button>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="col-span-2 text-xs font-medium text-gray-500 dark:text-gray-300">Name
                <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                  className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm" placeholder="analytics-rds" />
              </label>

              <label className="col-span-2 text-xs font-medium text-gray-500 dark:text-gray-300">Type
                <select value={form.type} onChange={e => {
                    const t = e.target.value as ConnectionType
                    setForm(f => ({ ...f, type: t, port: t === 'postgres' ? 5432 : undefined, auth_method: t === 'snowflake' ? 'externalbrowser' : '' }))
                  }}
                  className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm">
                  <option value="postgres">Postgres (RDS)</option>
                  <option value="snowflake">Snowflake</option>
                </select>
              </label>

              <label className="text-xs font-medium text-gray-500 dark:text-gray-300 col-span-2">{isSnowflake ? 'Account' : 'Host'}
                <input value={form.host ?? ''} onChange={e => setForm(f => ({ ...f, host: e.target.value }))}
                  className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm"
                  placeholder={isSnowflake ? 'xy12345.us-east-1' : 'mydb.abc123.us-east-1.rds.amazonaws.com'} />
              </label>

              {!isSnowflake && (
                <label className="text-xs font-medium text-gray-500 dark:text-gray-300">Port
                  <input type="number" value={form.port ?? 5432} onChange={e => setForm(f => ({ ...f, port: Number(e.target.value) }))}
                    className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm" />
                </label>
              )}

              <label className="text-xs font-medium text-gray-500 dark:text-gray-300">Database
                <input value={form.database ?? ''} onChange={e => setForm(f => ({ ...f, database: e.target.value }))}
                  className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm" placeholder={isSnowflake ? '(optional)' : 'analytics'} />
              </label>

              {!isSnowflake && (
                <label className="text-xs font-medium text-gray-500 dark:text-gray-300">Schema
                  <input value={form.schema_name ?? ''} onChange={e => setForm(f => ({ ...f, schema_name: e.target.value }))}
                    className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm" placeholder="public" />
                </label>
              )}

              <label className="text-xs font-medium text-gray-500 dark:text-gray-300">Username
                <input value={form.username ?? ''} onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
                  className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm" />
              </label>

              <label className="text-xs font-medium text-gray-500 dark:text-gray-300">{isSnowflake ? 'Password (if not SSO)' : 'Password'}
                <input type="password" value={form.secret ?? ''} onChange={e => setForm(f => ({ ...f, secret: e.target.value }))}
                  className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm" />
              </label>

              {isSnowflake && (
                <>
                  <label className="text-xs font-medium text-gray-500 dark:text-gray-300">Warehouse
                    <input value={form.extra?.warehouse ?? ''} onChange={e => setForm(f => ({ ...f, extra: { ...f.extra, warehouse: e.target.value } }))}
                      className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm" />
                  </label>
                  <label className="text-xs font-medium text-gray-500 dark:text-gray-300">Role
                    <input value={form.extra?.role ?? ''} onChange={e => setForm(f => ({ ...f, extra: { ...f.extra, role: e.target.value } }))}
                      className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm" />
                  </label>
                  <label className="col-span-2 text-xs font-medium text-gray-500 dark:text-gray-300">Auth method
                    <select value={form.auth_method ?? 'externalbrowser'} onChange={e => setForm(f => ({ ...f, auth_method: e.target.value }))}
                      className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-lg text-sm">
                      <option value="externalbrowser">externalbrowser (SSO)</option>
                      <option value="password">password</option>
                    </select>
                  </label>
                </>
              )}
            </div>

            {createMutation.isError && (
              <p className="text-xs text-red-600">{(createMutation.error as any)?.response?.data?.detail ?? 'Failed to create connection'}</p>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button onClick={() => setShowForm(false)} className="px-4 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40">Cancel</button>
              <button onClick={() => createMutation.mutate(form)} disabled={!canSubmit || createMutation.isPending}
                className="px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 flex items-center gap-2">
                {createMutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}Save Connection
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

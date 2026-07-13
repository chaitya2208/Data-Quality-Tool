import { createContext, useContext, useState, useEffect, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { connectionsApi } from './api/client'
import type { Connection } from './api/client'

const STORAGE_KEY = 'dq_selected_connection_id'

interface ConnectionCtx {
  connections: Connection[]
  loading: boolean
  selectedId: string | null
  setSelectedId: (id: string | null) => void
  selected: Connection | null
}

const Ctx = createContext<ConnectionCtx>({
  connections: [], loading: false, selectedId: null, setSelectedId: () => {}, selected: null,
})

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const { data, isLoading } = useQuery({
    queryKey: ['connections'],
    queryFn: () => connectionsApi.list().then(r => r.data),
  })
  const connections = data?.connections ?? []

  const [selectedId, setSelectedIdState] = useState<string | null>(() => {
    try { return localStorage.getItem(STORAGE_KEY) } catch { return null }
  })

  const setSelectedId = (id: string | null) => {
    setSelectedIdState(id)
    try {
      if (id) localStorage.setItem(STORAGE_KEY, id)
      else localStorage.removeItem(STORAGE_KEY)
    } catch {}
  }

  // Default to the first connection once loaded (or if the saved one vanished).
  useEffect(() => {
    if (!connections.length) return
    const stillExists = selectedId && connections.some(c => c.id === selectedId)
    if (!stillExists) setSelectedId(connections[0].id)
  }, [connections, selectedId])

  const selected = connections.find(c => c.id === selectedId) ?? null

  return (
    <Ctx.Provider value={{ connections, loading: isLoading, selectedId, setSelectedId, selected }}>
      {children}
    </Ctx.Provider>
  )
}

export function useConnection() {
  return useContext(Ctx)
}

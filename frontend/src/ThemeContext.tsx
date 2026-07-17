import { createContext, useContext, useState, useEffect, type ReactNode } from 'react'

export type ThemeMode = 'light' | 'dark' | 'system'
const STORAGE_KEY = 'dq_theme'

interface ThemeCtx {
  mode: ThemeMode
  setMode: (m: ThemeMode) => void
  resolved: 'light' | 'dark'
}

const Ctx = createContext<ThemeCtx>({ mode: 'system', setMode: () => {}, resolved: 'light' })

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-color-scheme: dark)').matches
}

function apply(resolved: 'light' | 'dark') {
  const root = document.documentElement
  if (resolved === 'dark') root.classList.add('dark')
  else root.classList.remove('dark')
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(() => {
    try { return (localStorage.getItem(STORAGE_KEY) as ThemeMode) || 'system' } catch { return 'system' }
  })

  const resolved: 'light' | 'dark' = mode === 'system' ? (systemPrefersDark() ? 'dark' : 'light') : mode

  useEffect(() => { apply(resolved) }, [resolved])

  // Follow OS changes while in 'system' mode.
  useEffect(() => {
    if (mode !== 'system' || !window.matchMedia) return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => apply(systemPrefersDark() ? 'dark' : 'light')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [mode])

  const setMode = (m: ThemeMode) => {
    setModeState(m)
    try { localStorage.setItem(STORAGE_KEY, m) } catch {}
  }

  return <Ctx.Provider value={{ mode, setMode, resolved }}>{children}</Ctx.Provider>
}

export function useTheme() {
  return useContext(Ctx)
}

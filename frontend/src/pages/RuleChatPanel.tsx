import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { rulesApi, ruleChatSessionsApi } from '../api/client'
import type { ChatMessage, ChatSession, GeneratedRule, ReferencedRule, RuleCreatePayload } from '../api/client'
import {
  X, Send, Sparkles, Loader2, CheckCircle2, RefreshCw, AlertTriangle,
  MessageSquarePlus, PanelLeftOpen, PanelLeftClose, Trash2, ExternalLink,
} from 'lucide-react'

const STORAGE_KEY = 'dq_rule_chat_active_id'

const WELCOME_CONTENT = "Hi! Tell me what data quality rule you'd like to create. I'll ask focused questions to get it right."
const WELCOME: ChatMessage = { role: 'assistant', content: WELCOME_CONTENT }

const CATEGORIES = ['documentation', 'ownership', 'schema', 'naming', 'data_quality', 'security', 'performance']
const ASSET_TYPES = ['table', 'column', 'schema', 'database']
const SEVERITY_LEVELS = ['critical', 'high', 'medium', 'low', 'info']

function cap(s: string) { return s.charAt(0).toUpperCase() + s.slice(1) }

function relativeTime(iso: string) {
  try {
    const diff = Date.now() - new Date(iso).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1) return 'just now'
    if (mins < 60) return `${mins}m ago`
    const hours = Math.floor(mins / 60)
    if (hours < 24) return `${hours}h ago`
    return `${Math.floor(hours / 24)}d ago`
  } catch { return '' }
}

// ── Rule reference card ───────────────────────────────────────────────────────
// NOTE: prop is named "rule" not "ref" — "ref" is a reserved React prop name

function RuleRefCard({ rule, onNavigate }: { rule: ReferencedRule; onNavigate: (id: string) => void }) {
  return (
    <button
      onClick={() => onNavigate(rule.definition_id)}
      className="flex items-center gap-2 w-full text-left px-3 py-2 mt-1 rounded-lg border border-primary-300 dark:border-primary-600 bg-primary-100 dark:bg-primary-800/50 hover:bg-primary-200 dark:hover:bg-primary-700/60 transition-colors"
    >
      <Sparkles className="w-3.5 h-3.5 text-primary-600 dark:text-primary-300 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="text-xs font-mono font-semibold text-primary-800 dark:text-primary-200">{rule.code}</span>
        <span className="text-xs text-gray-700 dark:text-gray-200 ml-1.5">{rule.name}</span>
      </div>
      <span className="text-xs px-1.5 py-0.5 bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-200 rounded border border-gray-200 dark:border-gray-600">{rule.category.replace(/_/g, ' ')}</span>
      <ExternalLink className="w-3 h-3 text-primary-500 dark:text-primary-400 flex-shrink-0" />
    </button>
  )
}

// ── Rule preview card ─────────────────────────────────────────────────────────

function RulePreviewCard({
  rule, onAdd, onRefine, isAdding,
}: {
  rule: GeneratedRule
  onAdd: (form: RuleCreatePayload) => void
  onRefine: () => void
  isAdding: boolean
}) {
  const [form, setForm] = useState<RuleCreatePayload>({
    code: rule.code, name: rule.name, description: rule.description,
    category: rule.category, severity: rule.severity, applies_to: rule.applies_to,
    owner: '', created_by: '', jira_ticket: '', rule_config: {}, is_active: false,
  })
  const [expanded, setExpanded] = useState(false)

  const toggleAppliesTo = (t: string) => setForm(f => ({
    ...f,
    applies_to: f.applies_to.includes(t) ? f.applies_to.filter(x => x !== t) : [...f.applies_to, t],
  }))

  return (
    <div className="mt-2 rounded-xl border border-primary-300 dark:border-primary-700 bg-primary-50 dark:bg-gray-800 overflow-hidden text-sm shadow-sm">
      {/* Header */}
      <div className="px-3 py-2.5 bg-primary-100 dark:bg-primary-900 flex items-center justify-between border-b border-primary-200 dark:border-primary-700">
        <span className="font-semibold text-primary-900 dark:text-primary-100 flex items-center gap-1.5 text-xs">
          <Sparkles className="w-3.5 h-3.5 text-primary-600 dark:text-primary-300" /> Proposed Rule
        </span>
        <button
          onClick={() => setExpanded(e => !e)}
          className="text-xs text-primary-700 dark:text-primary-300 font-medium hover:underline"
        >
          {expanded ? 'Hide editor' : 'Edit fields'}
        </button>
      </div>

      {/* Duplicate warning */}
      {rule.duplicate_of && (
        <div className="mx-3 mt-2.5 flex items-start gap-2 bg-amber-50 dark:bg-amber-900/40 border border-amber-300 dark:border-amber-600 rounded-lg px-2.5 py-2">
          <AlertTriangle className="w-3.5 h-3.5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
          <div>
            <p className="font-semibold text-amber-900 dark:text-amber-200 text-xs">Similar rule exists</p>
            <p className="text-amber-800 dark:text-amber-300 text-xs mt-0.5">
              <span className="font-mono font-bold">{rule.duplicate_of.code}</span> — {rule.duplicate_of.name}
            </p>
          </div>
        </div>
      )}

      {/* Rationale */}
      {rule.rationale && (
        <p className="px-3 pt-2.5 text-xs text-gray-600 dark:text-gray-300 italic">{rule.rationale}</p>
      )}

      {/* Summary view */}
      {!expanded && (
        <div className="px-3 py-2.5 space-y-1.5">
          <div className="flex gap-1.5 flex-wrap">
            <span className="px-2 py-0.5 rounded-full text-xs font-mono bg-gray-200 dark:bg-gray-700 text-gray-800 dark:text-gray-100">{form.code}</span>
            <span className="px-2 py-0.5 rounded-full text-xs bg-blue-100 dark:bg-blue-800 text-blue-900 dark:text-blue-100">{form.category.replace(/_/g, ' ')}</span>
            <span className="px-2 py-0.5 rounded-full text-xs bg-orange-100 dark:bg-orange-800 text-orange-900 dark:text-orange-100">{form.severity}</span>
          </div>
          <p className="font-semibold text-gray-900 dark:text-gray-100 text-xs">{form.name}</p>
          <p className="text-gray-700 dark:text-gray-300 text-xs leading-relaxed">{form.description}</p>
        </div>
      )}

      {/* Editable fields */}
      {expanded && (
        <div className="px-3 py-2.5 space-y-2.5">
          <div>
            <label className="block text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide mb-1">Rule Code</label>
            <input type="text" value={form.code}
              onChange={e => setForm(f => ({ ...f, code: e.target.value.toUpperCase().replace(/\s+/g, '_') }))}
              className="w-full px-2.5 py-1.5 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide mb-1">Name</label>
            <input type="text" value={form.name}
              onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
              className="w-full px-2.5 py-1.5 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide mb-1">Description</label>
            <textarea rows={2} value={form.description}
              onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
              className="w-full px-2.5 py-1.5 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded-lg text-xs resize-none focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide mb-1">Category</label>
              <select value={form.category} onChange={e => setForm(f => ({ ...f, category: e.target.value }))}
                className="w-full px-2 py-1.5 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-primary-500">
                {CATEGORIES.map(c => <option key={c} value={c}>{cap(c)}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide mb-1">Severity</label>
              <select value={form.severity} onChange={e => setForm(f => ({ ...f, severity: e.target.value }))}
                className="w-full px-2 py-1.5 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-primary-500">
                {SEVERITY_LEVELS.map(s => <option key={s} value={s}>{cap(s)}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide mb-1">Applies To</label>
            <div className="flex gap-1.5 flex-wrap">
              {ASSET_TYPES.map(t => (
                <button key={t} type="button" onClick={() => toggleAppliesTo(t)}
                  className={`px-2 py-1 rounded-lg text-xs font-medium border transition-colors ${
                    form.applies_to.includes(t)
                      ? 'bg-primary-600 text-white border-primary-600'
                      : 'bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 border-gray-300 dark:border-gray-500'
                  }`}>
                  {t}
                </button>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide mb-1">Owner *</label>
              <input type="text" placeholder="team / person" value={form.owner ?? ''}
                onChange={e => setForm(f => ({ ...f, owner: e.target.value }))}
                className="w-full px-2.5 py-1.5 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide mb-1">Jira Ticket</label>
              <input type="text" placeholder="DQ-123" value={form.jira_ticket ?? ''}
                onChange={e => setForm(f => ({ ...f, jira_ticket: e.target.value }))}
                className="w-full px-2.5 py-1.5 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
            </div>
          </div>
        </div>
      )}

      {/* Footer: category hint + actions */}
      <div className="px-3 pb-3 pt-1 space-y-2 border-t border-primary-200 dark:border-primary-700 mt-2">
        <p className="text-xs text-gray-600 dark:text-gray-300">
          Will be added to{' '}
          <span className="font-semibold text-gray-900 dark:text-gray-100">
            {form.category.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
          </span>
          {' '}and submitted for approval.
        </p>
        <div className="flex gap-2">
          <button
            onClick={() => onAdd(form)}
            disabled={isAdding}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-primary-600 text-white text-xs font-semibold rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors"
          >
            {isAdding
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" />Adding…</>
              : <><CheckCircle2 className="w-3.5 h-3.5" />Add to Library</>
            }
          </button>
          <button
            onClick={onRefine}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-100 text-xs font-medium rounded-lg border border-gray-300 dark:border-gray-500 hover:bg-gray-50 dark:hover:bg-gray-600 transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5" /> Keep Refining
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Message bubble ────────────────────────────────────────────────────────────

function MessageBubble({
  msg, onAdd, isAdding, onRefine, onNavigate,
}: {
  msg: ChatMessage
  onAdd: (form: RuleCreatePayload) => void
  isAdding: boolean
  onRefine: () => void
  onNavigate: (id: string) => void
}) {
  const isUser = msg.role === 'user'
  return (
    <div>
      <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
        {!isUser && (
          <div className="w-6 h-6 rounded-full bg-purple-100 dark:bg-purple-900 flex items-center justify-center flex-shrink-0 mt-0.5 mr-2">
            <Sparkles className="w-3.5 h-3.5 text-purple-600 dark:text-purple-300" />
          </div>
        )}
        <div className={`max-w-[85%] px-3.5 py-2.5 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
          isUser
            ? 'bg-primary-600 text-white rounded-br-sm'
            : 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-100 rounded-bl-sm'
        }`}>
          {msg.content}
        </div>
      </div>

      {/* Referenced rule cards — prop name is "rule", NOT "ref" */}
      {!isUser && msg.referenced_rules && msg.referenced_rules.length > 0 && (
        <div className="ml-8 space-y-1 mt-1">
          {msg.referenced_rules.map(r => (
            <RuleRefCard key={r.definition_id} rule={r} onNavigate={onNavigate} />
          ))}
        </div>
      )}

      {/* Proposed rule card */}
      {!isUser && msg.proposed_rule && (
        <div className="ml-8">
          <RulePreviewCard
            rule={msg.proposed_rule}
            onAdd={onAdd}
            onRefine={onRefine}
            isAdding={isAdding}
          />
        </div>
      )}
    </div>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────────

interface Props {
  isOpen: boolean
  onClose: () => void
  onRuleCreated: () => void
}

export default function RuleChatPanel({ isOpen, onClose, onRuleCreated }: Props) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  // Track which session ID we've already loaded into localMessages, to avoid
  // overwriting an in-progress conversation when the query re-fetches.
  const loadedSessionRef = useRef<string | null>(null)

  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [activeSessionId, setActiveSessionId] = useState<string | null>(() => {
    try { return localStorage.getItem(STORAGE_KEY) } catch { return null }
  })
  // localMessages is the single source of truth for the UI.
  // Server state is only loaded once per session switch (not on every refetch).
  const [localMessages, setLocalMessages] = useState<ChatMessage[]>([WELCOME])
  const [input, setInput] = useState('')

  const setActive = useCallback((id: string | null) => {
    setActiveSessionId(id)
    try {
      if (id) localStorage.setItem(STORAGE_KEY, id)
      else localStorage.removeItem(STORAGE_KEY)
    } catch {}
  }, [])

  // Session list — only fetch when panel is open
  const { data: sessionsData, refetch: refetchSessions } = useQuery({
    queryKey: ['rule-chat-sessions'],
    queryFn: () => ruleChatSessionsApi.list().then(r => r.data),
    enabled: isOpen,
    staleTime: 15_000,
  })
  const sessions: ChatSession[] = sessionsData?.sessions ?? []

  // Active session — used only for initial load when switching sessions
  const { data: activeSession } = useQuery({
    queryKey: ['rule-chat-session', activeSessionId],
    queryFn: () => ruleChatSessionsApi.get(activeSessionId!).then(r => r.data),
    enabled: isOpen && !!activeSessionId,
    staleTime: 60_000,  // long stale time — we don't want background refetches overwriting UI
  })

  // Sync from server ONLY when we switch to a different session (not on every refetch)
  useEffect(() => {
    if (!activeSessionId) {
      // No active session — show welcome screen
      if (loadedSessionRef.current !== null) {
        loadedSessionRef.current = null
        setLocalMessages([WELCOME])
      }
      return
    }
    // Already loaded this session — don't overwrite in-progress conversation
    if (loadedSessionRef.current === activeSessionId) return
    // New session selected — load from server if data is available
    if (activeSession && activeSession.id === activeSessionId) {
      loadedSessionRef.current = activeSessionId
      const msgs = activeSession.messages as ChatMessage[]
      setLocalMessages(msgs.length > 0 ? msgs : [WELCOME])
    }
  }, [activeSessionId, activeSession])

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [localMessages])

  // Focus input when panel opens
  useEffect(() => {
    if (isOpen) setTimeout(() => inputRef.current?.focus(), 150)
  }, [isOpen])

  // Delete session
  const deleteSessionMutation = useMutation({
    mutationFn: (id: string) => ruleChatSessionsApi.delete(id),
    onSuccess: (_data, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ['rule-chat-sessions'] })
      queryClient.removeQueries({ queryKey: ['rule-chat-session', deletedId] })
      if (activeSessionId === deletedId) {
        loadedSessionRef.current = null
        setActive(null)
        setLocalMessages([WELCOME])
      }
    },
  })

  // AI chat turn
  const chatMutation = useMutation({
    mutationFn: ({ msgs, sessionId }: { msgs: ChatMessage[]; sessionId: string | null }) =>
      rulesApi.chat(
        msgs.map(m => ({ role: m.role, content: m.content })),
        sessionId ?? undefined,
      ).then(r => r.data),
    onSuccess: (data) => {
      const assistantMsg: ChatMessage = {
        role: 'assistant',
        content: data.message,
        proposed_rule: data.proposed_rule ?? undefined,
        referenced_rules: data.referenced_rules?.length > 0 ? data.referenced_rules : undefined,
      }
      setLocalMessages(prev => [...prev, assistantMsg])
      // Refresh the sidebar title (backend may have set it on first message)
      refetchSessions()
      // Do NOT invalidate the session query here — that would re-fetch the server
      // state and trigger the useEffect which would overwrite localMessages.
    },
    onError: () => {
      setLocalMessages(prev => [
        ...prev,
        { role: 'assistant', content: 'Something went wrong on my end. Please try again.' },
      ])
    },
  })

  // Create rule
  const createRuleMutation = useMutation({
    mutationFn: (payload: RuleCreatePayload) => rulesApi.create(payload),
    onSuccess: () => {
      onRuleCreated()
      setLocalMessages(prev => [
        ...prev,
        { role: 'assistant', content: '✓ Rule submitted for approval and added to the library.' },
      ])
    },
    onError: (err: any) => {
      setLocalMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Failed to add rule: ${err?.response?.data?.detail ?? 'unknown error'}` },
      ])
    },
  })

  const startNewChat = () => {
    loadedSessionRef.current = null
    setActive(null)
    setLocalMessages([WELCOME])
    setInput('')
  }

  const switchToSession = (session: ChatSession) => {
    if (session.id === activeSessionId) return
    loadedSessionRef.current = null  // force reload from server
    setActive(session.id)
    setLocalMessages([WELCOME])      // show welcome while server data loads
    setInput('')
  }

  const send = async () => {
    const text = input.trim()
    if (!text || chatMutation.isPending) return

    // Create a backend session on the first message if none exists
    let sessionId = activeSessionId
    if (!sessionId) {
      try {
        const session = await ruleChatSessionsApi.create(text.slice(0, 60)).then(r => r.data)
        sessionId = session.id
        loadedSessionRef.current = session.id  // mark as loaded so effect doesn't reset
        setActive(session.id)
        refetchSessions()
      } catch {
        // proceed without persistence if backend is unreachable
      }
    }

    const userMsg: ChatMessage = { role: 'user', content: text }
    setLocalMessages(prev => [...prev, userMsg])
    setInput('')

    // Build API payload: full local history + new user message, minus the welcome message
    const apiMsgs = [...localMessages, userMsg].filter(
      m => !(m.role === 'assistant' && m.content === WELCOME_CONTENT)
    )
    chatMutation.mutate({ msgs: apiMsgs, sessionId })
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  const handleNavigate = (definitionId: string) => {
    onClose()
    navigate(`/rule-library?highlight=${definitionId}`)
  }

  const handleRefine = (msgIndex: number) => {
    setLocalMessages(prev => [
      ...prev.map((m, i) => i === msgIndex ? { ...m, proposed_rule: undefined } : m),
      { role: 'assistant', content: '• Sure — what would you like to change about the rule?' },
    ])
  }

  return (
    <>
      {isOpen && <div className="fixed inset-0 z-40 bg-black/30" onClick={onClose} />}

      <div
        className={`fixed top-0 right-0 h-full z-50 flex flex-col bg-white dark:bg-gray-900 shadow-2xl border-l border-gray-200 dark:border-gray-700 transition-transform duration-300 ease-in-out ${isOpen ? 'translate-x-0' : 'translate-x-full'}`}
        style={{ width: sidebarOpen ? '640px' : '440px' }}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700 bg-gradient-to-r from-primary-50 to-purple-50 dark:from-gray-800 dark:to-gray-800 flex-shrink-0">
          <div className="flex items-center gap-2">
            <button
              onClick={() => setSidebarOpen(s => !s)}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
              title={sidebarOpen ? 'Hide chat history' : 'Show chat history'}
            >
              {sidebarOpen ? <PanelLeftClose className="w-4 h-4" /> : <PanelLeftOpen className="w-4 h-4" />}
            </button>
            <Sparkles className="w-4 h-4 text-purple-500" />
            <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">Rule AI</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={startNewChat}
              title="New chat"
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            >
              <MessageSquarePlus className="w-4 h-4" />
            </button>
            <button
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        <div className="flex flex-1 min-h-0">
          {/* Sidebar: chat history */}
          {sidebarOpen && (
            <div className="w-[200px] flex-shrink-0 border-r border-gray-200 dark:border-gray-700 flex flex-col bg-gray-50 dark:bg-gray-950">
              <div className="px-3 py-2.5 border-b border-gray-200 dark:border-gray-700 space-y-2">
                <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Chats</p>
                <button
                  onClick={startNewChat}
                  className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs font-semibold text-white bg-primary-600 hover:bg-primary-700 rounded-lg transition-colors shadow-sm"
                >
                  <MessageSquarePlus className="w-3.5 h-3.5" /> New Chat
                </button>
              </div>
              <div className="flex-1 overflow-y-auto py-1.5 space-y-0.5 px-2">
                {sessions.length === 0 && (
                  <p className="text-xs text-gray-400 dark:text-gray-500 px-2 py-3 text-center">No chats yet</p>
                )}
                {sessions.map(s => (
                  <div
                    key={s.id}
                    className={`group relative flex items-start gap-1.5 px-2 py-2 rounded-lg cursor-pointer transition-colors ${activeSessionId === s.id ? 'bg-primary-100 dark:bg-primary-900/40' : 'hover:bg-gray-100 dark:hover:bg-gray-800'}`}
                    onClick={() => switchToSession(s)}
                  >
                    <MessageSquarePlus className="w-3.5 h-3.5 text-gray-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-w-0">
                      <p className="text-xs text-gray-800 dark:text-gray-100 truncate font-medium leading-snug">
                        {s.title || 'New chat'}
                      </p>
                      <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">{relativeTime(s.updated_at)}</p>
                    </div>
                    <button
                      onClick={e => { e.stopPropagation(); deleteSessionMutation.mutate(s.id) }}
                      className="opacity-0 group-hover:opacity-100 p-0.5 text-gray-400 hover:text-red-500 rounded transition-opacity flex-shrink-0 mt-0.5"
                      title="Delete"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Chat area */}
          <div className="flex-1 flex flex-col min-w-0 min-h-0">
            <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
              {localMessages.map((msg, i) => (
                <MessageBubble
                  key={i}
                  msg={msg}
                  onAdd={form => createRuleMutation.mutate(form)}
                  isAdding={createRuleMutation.isPending}
                  onRefine={() => handleRefine(i)}
                  onNavigate={handleNavigate}
                />
              ))}

              {chatMutation.isPending && (
                <div className="flex items-center gap-2">
                  <div className="w-6 h-6 rounded-full bg-purple-100 dark:bg-purple-900 flex items-center justify-center flex-shrink-0">
                    <Sparkles className="w-3.5 h-3.5 text-purple-600 dark:text-purple-300" />
                  </div>
                  <div className="flex gap-1 px-3.5 py-3 bg-gray-100 dark:bg-gray-800 rounded-2xl rounded-bl-sm">
                    <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:0ms]" />
                    <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:150ms]" />
                    <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:300ms]" />
                  </div>
                </div>
              )}
              <div ref={bottomRef} />
            </div>

            {/* Input */}
            <div className="flex-shrink-0 px-4 py-3 border-t border-gray-200 dark:border-gray-700">
              <div className="flex items-end gap-2">
                <textarea
                  ref={inputRef}
                  rows={1}
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="Describe your rule idea… (Enter to send)"
                  className="flex-1 px-3 py-2.5 border border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 rounded-xl text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary-500 max-h-32 overflow-auto"
                  style={{ minHeight: '42px' }}
                />
                <button
                  onClick={send}
                  disabled={!input.trim() || chatMutation.isPending}
                  className="flex-shrink-0 p-2.5 bg-primary-600 text-white rounded-xl hover:bg-primary-700 disabled:opacity-40 transition-colors"
                >
                  <Send className="w-4 h-4" />
                </button>
              </div>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1.5 text-center">Shift+Enter for new line</p>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { mutesApi } from '../api/client'
import type { Finding, Mute } from '../api/client'
import { fmtIST } from '../utils/dates'
import {
  X, BellOff, Bell, RotateCcw, Clock, TableIcon, AlertCircle, Loader2,
} from 'lucide-react'

// Inline sparkline — reused on the card + the drawer.
export function FailCountSparkline({ history }: { history: Finding['fail_history'] }) {
  const points = (history ?? []).filter(h => h.fail_count != null)
  if (points.length === 0) return null
  const w = 120, h = 28, pad = 2
  const max = Math.max(1, ...points.map(p => p.fail_count))
  const step = points.length > 1 ? (w - pad * 2) / (points.length - 1) : 0
  const path = points.map((p, i) => {
    const x = pad + i * step
    const y = pad + (h - pad * 2) * (1 - p.fail_count / max)
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
  }).join(' ')
  const lastX = pad + (points.length - 1) * step
  const lastY = pad + (h - pad * 2) * (1 - points[points.length - 1].fail_count / max)
  return (
    <svg width={w} height={h} className="inline-block align-middle">
      <title>{`Trend of failing rows across last ${points.length} runs (max ${max})`}</title>
      <path d={path} fill="none" stroke="#dc2626" strokeWidth="1.5" />
      <circle cx={lastX} cy={lastY} r="2.5" fill="#dc2626" />
    </svg>
  )
}

// Duration options for a quick mute — matches Monte Carlo / Soda conventions.
const MUTE_OPTIONS = [
  { label: '1 hour',  hours: 1   },
  { label: '24 hours', hours: 24  },
  { label: '7 days',   hours: 168 },
  { label: '30 days',  hours: 720 },
]

export default function FindingDetailDrawer({
  finding, onClose,
}: { finding: Finding; onClose: () => void }) {
  const qc = useQueryClient()
  const [muteOpen, setMuteOpen] = useState(false)
  const [muteReason, setMuteReason] = useState('')

  // Active mute for this (instance, asset) — drives the "Muted" badge + Unmute action.
  const { data: mutes } = useQuery({
    queryKey: ['mutes', finding.instance_id, finding.asset_id],
    queryFn: () => mutesApi.list({
      instance_id: finding.instance_id ?? undefined,
      asset_id: finding.asset_id,
      active_only: true,
    }).then(r => r.data),
    enabled: !!finding.instance_id,
  })
  const activeMute: Mute | undefined = (mutes ?? [])[0]

  const createMute = useMutation({
    mutationFn: (hours: number) => mutesApi.create({
      instance_id: finding.instance_id!,
      asset_id: finding.asset_id,
      duration_hours: hours,
      reason: muteReason || undefined,
    }).then(r => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mutes', finding.instance_id, finding.asset_id] })
      qc.invalidateQueries({ queryKey: ['table-health'] })
      setMuteOpen(false); setMuteReason('')
    },
  })
  const deleteMute = useMutation({
    mutationFn: (id: string) => mutesApi.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mutes', finding.instance_id, finding.asset_id] })
      qc.invalidateQueries({ queryKey: ['table-health'] })
    },
  })

  const history = finding.fail_history ?? []
  const sampleRows: Record<string, any>[] = finding.evidence?.sample_rows ?? []
  const sampleHeaders = sampleRows.length ? Object.keys(sampleRows[0]) : []

  // Metadata-shape rules (PII, generic name, type mismatch, …) have no failing
  // rows — dynamic_rules._finding defaults counts to 1/1 and sample_rows to [].
  // Detect that shape and (a) suppress the misleading "1/1 (100%)" fails tile,
  // (b) render the rule-specific evidence keys instead.
  const CONTRACT_KEYS = new Set(['fail_count', 'total_count', 'sample_rows'])
  const extraEvidence: Array<[string, any]> = Object.entries(finding.evidence ?? {})
    .filter(([k, v]) => !CONTRACT_KEYS.has(k) && v !== null && v !== undefined && v !== '')
  const isMetadataRule =
    sampleRows.length === 0 &&
    (finding.current_total_count ?? 0) <= 1 &&
    (finding.current_fail_count ?? 0) <= 1

  return (
    // Backdrop
    <div className="fixed inset-0 z-50 flex" onClick={onClose}>
      <div className="flex-1 bg-black/30" />
      {/* Drawer */}
      <div
        onClick={e => e.stopPropagation()}
        className="w-full max-w-2xl bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-700 shadow-2xl overflow-y-auto"
      >
        <div className="sticky top-0 bg-white dark:bg-gray-900 border-b border-gray-100 dark:border-gray-700 px-6 py-4 flex items-start gap-3 z-10">
          <div className="flex-1 min-w-0">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">{finding.title}</h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 truncate">{finding.context?.fqn}</p>
          </div>
          <button onClick={onClose} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800" aria-label="Close">
            <X className="w-5 h-5 text-gray-500" />
          </button>
        </div>

        <div className="p-6 space-y-6">
          {/* Lifecycle summary strip */}
          <div className="flex flex-wrap gap-3 text-sm">
            <SummaryTile
              icon={Clock} label="Failing since"
              value={fmtIST(finding.first_detected_at ?? finding.detected_at)}
            />
            {!isMetadataRule && (
              <SummaryTile
                icon={AlertCircle} label="Current fails"
                value={finding.current_fail_count != null && finding.current_total_count != null
                  ? `${finding.current_fail_count.toLocaleString()} / ${finding.current_total_count.toLocaleString()}`
                    + (finding.current_total_count > 0
                        ? ` (${((finding.current_fail_count / finding.current_total_count) * 100).toFixed(1)}%)`
                        : '')
                  : '—'}
                tone={(finding.current_fail_count ?? 0) > 0 ? 'text-red-600' : undefined}
              />
            )}
            <SummaryTile
              icon={RotateCcw} label="Reopened"
              value={(finding.reopened_count ?? 0).toString()}
              tone={(finding.reopened_count ?? 0) > 0 ? 'text-amber-600' : undefined}
            />
            <SummaryTile
              icon={Clock} label="Last seen"
              value={fmtIST(finding.last_seen_at)}
            />
          </div>

          {/* Mute controls */}
          <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-gray-100">
                {activeMute
                  ? <><BellOff className="w-4 h-4 text-amber-600" /> Muted until {fmtIST(activeMute.muted_until)}</>
                  : <><Bell    className="w-4 h-4 text-gray-500" /> Not muted</>}
              </div>
              {activeMute
                ? (
                  <button
                    onClick={() => deleteMute.mutate(activeMute.id)}
                    disabled={deleteMute.isPending}
                    className="px-3 py-1.5 text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200 rounded hover:bg-gray-200"
                  >Unmute</button>
                )
                : (
                  <button
                    onClick={() => setMuteOpen(v => !v)}
                    disabled={!finding.instance_id}
                    className="px-3 py-1.5 text-xs font-medium bg-primary-600 text-white rounded hover:bg-primary-700 disabled:opacity-50"
                    title={!finding.instance_id ? 'Cannot mute — finding has no rule instance' : 'Silence this rule for a window'}
                  >Mute</button>
                )}
            </div>
            {activeMute?.reason && (
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Reason: {activeMute.reason}</p>
            )}
            {muteOpen && !activeMute && (
              <div className="mt-3 space-y-2">
                <input
                  value={muteReason}
                  onChange={e => setMuteReason(e.target.value)}
                  placeholder="Reason (optional)"
                  className="w-full px-3 py-1.5 text-sm border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-800"
                />
                <div className="flex flex-wrap gap-2">
                  {MUTE_OPTIONS.map(o => (
                    <button key={o.hours}
                      onClick={() => createMute.mutate(o.hours)}
                      disabled={createMute.isPending}
                      className="px-3 py-1.5 text-xs font-medium bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded hover:bg-gray-50 disabled:opacity-50"
                    >
                      {createMute.isPending ? <Loader2 className="w-3 h-3 animate-spin inline" /> : o.label}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-gray-400">
                  Scans still run + execute this rule during a mute — no new incident is created, and existing open incidents are frozen (still-failing runs won't reset the clock).
                </p>
              </div>
            )}
          </div>

          {/* Fail history — hidden on metadata-shape rules, where every run is 1/1. */}
          {!isMetadataRule && history.length > 0 && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Run history</h3>
                <FailCountSparkline history={history} />
                <span className="text-xs text-gray-400 ml-auto">{history.length} run{history.length !== 1 ? 's' : ''}</span>
              </div>
              <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
                <table className="text-xs w-full">
                  <thead className="bg-gray-100 dark:bg-gray-800">
                    <tr>
                      <th className="px-3 py-1.5 text-left font-semibold text-gray-600 dark:text-gray-300">When</th>
                      <th className="px-3 py-1.5 text-left font-semibold text-gray-600 dark:text-gray-300">Failing rows</th>
                      <th className="px-3 py-1.5 text-left font-semibold text-gray-600 dark:text-gray-300">Total</th>
                      <th className="px-3 py-1.5 text-left font-semibold text-gray-600 dark:text-gray-300">Event</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-700 bg-white dark:bg-gray-900">
                    {[...history].reverse().map((h, i) => (
                      <tr key={i}>
                        <td className="px-3 py-1.5 text-gray-700 dark:text-gray-300">{fmtIST(h.at)}</td>
                        <td className="px-3 py-1.5 font-mono tabular-nums text-red-600">{h.fail_count.toLocaleString()}</td>
                        <td className="px-3 py-1.5 font-mono tabular-nums text-gray-500">{h.total_count.toLocaleString()}</td>
                        <td className="px-3 py-1.5 text-gray-500">
                          {h.event === 'reopened'
                            ? <span className="inline-flex items-center gap-1 text-amber-700"><RotateCcw className="w-3 h-3" /> reopened</span>
                            : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Evidence — rule-specific key/value pairs (PII match, type mismatch, …). */}
          {extraEvidence.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-2">Evidence</h3>
              <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
                <table className="text-xs w-full">
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-700 bg-white dark:bg-gray-900">
                    {extraEvidence.map(([k, v]) => (
                      <tr key={k}>
                        <td className="px-3 py-1.5 font-semibold text-gray-600 dark:text-gray-300 whitespace-nowrap align-top w-1/3">{k}</td>
                        <td className="px-3 py-1.5 font-mono text-gray-700 dark:text-gray-300 break-all">
                          {Array.isArray(v)
                            ? v.length === 0 ? <span className="text-gray-400 italic">empty</span> : v.map(String).join(', ')
                            : typeof v === 'object'
                              ? JSON.stringify(v)
                              : String(v)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Sample failing rows — full list, not the collapsible short form. */}
          {sampleRows.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-2 flex items-center gap-1.5">
                <TableIcon className="w-4 h-4" />
                Sample failing rows ({sampleRows.length})
              </h3>
              <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
                <table className="text-xs w-full">
                  <thead className="bg-gray-100 dark:bg-gray-800">
                    <tr>
                      {sampleHeaders.map(h => (
                        <th key={h} className="px-3 py-1.5 text-left font-semibold text-gray-600 dark:text-gray-300 whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-700 bg-white dark:bg-gray-900">
                    {sampleRows.map((row, i) => (
                      <tr key={i}>
                        {sampleHeaders.map(h => (
                          <td key={h} className="px-3 py-1.5 font-mono text-gray-700 dark:text-gray-300 whitespace-nowrap max-w-[240px] truncate" title={String(row[h])}>
                            {row[h] === null || row[h] === undefined
                              ? <span className="text-gray-400 italic">null</span>
                              : String(row[h])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function SummaryTile({
  icon: Icon, label, value, tone,
}: { icon: React.ComponentType<{ className?: string }>; label: string; value: string; tone?: string }) {
  return (
    <div className="flex-1 min-w-[10rem] rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2">
      <div className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider text-gray-400 dark:text-gray-400">
        <Icon className="w-3 h-3" />
        {label}
      </div>
      <div className={`text-sm font-semibold tabular-nums ${tone ?? 'text-gray-900 dark:text-gray-100'}`}>{value}</div>
    </div>
  )
}

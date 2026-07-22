import { useMemo, useState, useEffect } from 'react';
import { useParams, useSearchParams, Link, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ResponsiveContainer, ComposedChart, Line, Area, XAxis, YAxis,
  CartesianGrid, Tooltip, Legend,
} from 'recharts';
import {
  ArrowLeft, Activity, AlertTriangle, Loader2, RefreshCw, Save, CheckCircle,
} from 'lucide-react';
import { metricsApi } from '../api/client';
import type { MetricFinding } from '../api/client';

// Anomaly-monitoring detail view: one (asset, metric, [column]) plotted over
// time with the rolling MAD band, breach markers, and threshold controls
// wired to the underlying RULE_INSTANCES.threshold_config.

function fmt(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function shortTs(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function severityColor(sev: string | null | undefined): string {
  switch ((sev || '').toLowerCase()) {
    case 'critical':
    case 'high':   return 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300';
    case 'medium': return 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300';
    case 'low':
    case 'info':
    default:       return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
  }
}

export default function MetricDetail() {
  const { assetId = '' } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const metricName = searchParams.get('metric') || '';
  const columnName = searchParams.get('column') || undefined;
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ['metric-history', assetId, metricName, columnName ?? null],
    queryFn: () => metricsApi.history({
      asset_id: assetId, metric_name: metricName, column_name: columnName,
    }).then(r => r.data),
    enabled: !!(assetId && metricName),
    staleTime: 15_000,
  });

  const data = query.data;
  const [deviations, setDeviations] = useState<number>(3);
  const [pctChange, setPctChange] = useState<number>(25);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!data?.instance) return;
    const tc = data.instance.threshold_config || {};
    if (typeof tc.deviations === 'number') setDeviations(tc.deviations);
    if (typeof tc.max_pct_change === 'number') setPctChange(tc.max_pct_change);
  }, [data?.instance?.id]);

  const chartData = useMemo(() => {
    if (!data) return [];
    const median = data.baseline?.median ?? null;
    const mad = data.baseline?.mad ?? null;
    const band = (median !== null && mad !== null) ? deviations * mad : null;
    return data.snapshots.map(s => ({
      scan_id: s.scan_id,
      captured_at: s.captured_at,
      label: shortTs(s.captured_at),
      value: s.value,
      median,
      upper: (median !== null && band !== null) ? median + band : null,
      lower: (median !== null && band !== null) ? median - band : null,
      // Recharts Area needs two values (lower..upper) as an [] to fill a band.
      band: (median !== null && band !== null) ? [median - band, median + band] : null,
    }));
  }, [data, deviations]);

  const breachScanIds = useMemo(() => {
    return new Set((data?.findings ?? []).map(f => f.scan_id).filter(Boolean));
  }, [data?.findings]);

  const updateThreshold = useMutation({
    mutationFn: (body: { deviations?: number; max_pct_change?: number }) =>
      metricsApi.updateThreshold(data!.instance!.id, body),
    onSuccess: () => {
      setSavedMsg('Threshold saved. Takes effect on next scan.');
      queryClient.invalidateQueries({ queryKey: ['metric-history', assetId, metricName, columnName ?? null] });
      setTimeout(() => setSavedMsg(null), 4000);
    },
  });

  if (!assetId || !metricName) {
    return <div className="p-6 text-sm text-red-600">Missing asset_id or metric.</div>;
  }
  if (query.isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
      </div>
    );
  }
  if (query.isError || !data) {
    return (
      <div className="p-6 text-sm text-red-600">
        Failed to load metric history: {String((query.error as any)?.message || 'unknown error')}
      </div>
    );
  }

  const fqn = `${data.asset.database_name}.${data.asset.schema_name}.${data.asset.table_name}`;
  const currentValue = data.snapshots.length > 0 ? data.snapshots[data.snapshots.length - 1].value : null;
  const baseline = data.baseline;
  const mature = (baseline?.sample_count ?? 0) >= 14;
  const hasInstance = !!data.instance;
  const hasBaseline = !!baseline?.median;

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <button
          onClick={() => navigate(-1)}
          className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100 mb-2"
        >
          <ArrowLeft className="w-3 h-3" /> Back
        </button>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <Activity className="w-6 h-6 text-primary-600" />
              <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">
                {data.metric_name}
                {data.column_name && (
                  <span className="text-gray-400"> · {data.column_name}</span>
                )}
              </h1>
            </div>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400 font-mono">{fqn}</p>
          </div>
          <button
            onClick={() => query.refetch()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
          >
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>
        </div>
      </div>

      {/* Summary strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Latest value" value={fmt(currentValue)} />
        <StatCard label="Baseline median" value={fmt(baseline?.median)} />
        <StatCard label="MAD" value={fmt(baseline?.mad)} />
        <StatCard
          label="Samples"
          value={String(baseline?.sample_count ?? 0)}
          hint={mature ? undefined : 'Need ≥14 to detect anomalies'}
        />
      </div>

      {!hasBaseline && (
        <div className="p-4 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 text-sm text-amber-800 dark:text-amber-200 flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <div>
            <p className="font-medium">Baseline not yet computed.</p>
            <p className="mt-0.5 text-xs">Run more scans and the rolling median + MAD will be populated automatically.</p>
          </div>
        </div>
      )}

      {/* Chart */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-4">
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-3">Metric over time</h2>
        {chartData.length === 0 ? (
          <div className="py-16 text-center text-sm text-gray-500 dark:text-gray-400">
            No snapshots recorded yet for this metric.
          </div>
        ) : (
          <div style={{ width: '100%', height: 320 }}>
            <ResponsiveContainer>
              <ComposedChart data={chartData} margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" opacity={0.5} />
                <XAxis dataKey="label" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip
                  formatter={(v: any, name: string) => [fmt(v as number), name]}
                  contentStyle={{ fontSize: 12 }}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                {hasBaseline && (
                  <Area
                    type="monotone"
                    dataKey="band"
                    fill="#6366f1"
                    fillOpacity={0.12}
                    stroke="none"
                    name={`Baseline ± ${deviations}·MAD`}
                    isAnimationActive={false}
                  />
                )}
                {hasBaseline && (
                  <Line
                    type="monotone" dataKey="median" stroke="#6366f1" strokeDasharray="4 4"
                    dot={false} name="Median" isAnimationActive={false}
                  />
                )}
                <Line
                  type="monotone" dataKey="value" stroke="#059669" strokeWidth={2}
                  dot={(props: any) => {
                    const { cx, cy, payload } = props;
                    const isBreach = payload.scan_id && breachScanIds.has(payload.scan_id);
                    return (
                      <circle
                        cx={cx} cy={cy} r={isBreach ? 5 : 2.5}
                        fill={isBreach ? '#dc2626' : '#059669'}
                        stroke={isBreach ? '#dc2626' : '#059669'}
                      />
                    );
                  }}
                  name="Value" isAnimationActive={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      {/* Threshold editor */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200">Thresholds</h2>
          {savedMsg && (
            <span className="inline-flex items-center gap-1 text-xs text-emerald-700 dark:text-emerald-400">
              <CheckCircle className="w-3.5 h-3.5" /> {savedMsg}
            </span>
          )}
        </div>
        {!hasInstance ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">
            No anomaly rule is monitoring this metric yet. Once a proposal is approved in the Rule Library, its thresholds appear here.
          </p>
        ) : (
          <div className="space-y-4">
            <ThresholdSlider
              label="Deviations from baseline median (MAD-based)"
              hint="Higher = fewer, more extreme flags. 3.0 is Soda's default."
              min={1.0} max={6.0} step={0.1}
              value={deviations} onChange={setDeviations}
            />
            <ThresholdSlider
              label="Max % change vs previous scan"
              hint="Complements MAD detection: catches sudden jumps even when the baseline is noisy."
              min={5} max={200} step={1}
              value={pctChange} onChange={setPctChange}
              unit="%"
            />
            <div className="flex items-center gap-2 pt-2 border-t border-gray-100 dark:border-gray-700">
              <button
                onClick={() => updateThreshold.mutate({ deviations, max_pct_change: pctChange })}
                disabled={updateThreshold.isPending}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50"
              >
                {updateThreshold.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                Save thresholds
              </button>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                Rule instance <code className="font-mono">{data.instance!.id.slice(0, 8)}</code> ·
                {data.instance!.is_active ? ' active' : ' inactive'}
              </span>
            </div>
            {updateThreshold.isError && (
              <p className="text-xs text-red-600 dark:text-red-400">
                {(updateThreshold.error as any)?.response?.data?.detail || 'Save failed'}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Findings list */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-4">
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-3">
          Findings from this metric ({data.findings.length})
        </h2>
        {data.findings.length === 0 ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">No findings recorded — either the metric hasn't breached, or no rule is monitoring it.</p>
        ) : (
          <div className="divide-y divide-gray-100 dark:divide-gray-700">
            {data.findings.map(f => (
              <FindingRow key={f.id} finding={f} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function StatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-3">
      <p className="text-[11px] uppercase tracking-wider text-gray-500 dark:text-gray-400">{label}</p>
      <p className="mt-1 text-xl font-semibold text-gray-900 dark:text-gray-100">{value}</p>
      {hint && <p className="mt-0.5 text-[11px] text-amber-600 dark:text-amber-400">{hint}</p>}
    </div>
  );
}

function ThresholdSlider({
  label, hint, min, max, step, value, onChange, unit = '',
}: {
  label: string; hint?: string;
  min: number; max: number; step: number;
  value: number; onChange: (v: number) => void; unit?: string;
}) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <label className="text-sm font-medium text-gray-700 dark:text-gray-200">{label}</label>
        <span className="text-sm font-mono text-gray-900 dark:text-gray-100">
          {value.toFixed(step < 1 ? 1 : 0)}{unit}
        </span>
      </div>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(Number(e.target.value))}
        className="w-full accent-primary-600"
      />
      {hint && <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">{hint}</p>}
    </div>
  );
}

function FindingRow({ finding }: { finding: MetricFinding }) {
  return (
    <Link
      to={`/findings?id=${encodeURIComponent(finding.id)}`}
      className="flex items-center gap-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/30 px-2 -mx-2 rounded transition-colors"
    >
      {finding.severity && (
        <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${severityColor(finding.severity)}`}>
          {finding.severity.toUpperCase()}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <p className="text-sm text-gray-900 dark:text-gray-100 truncate">{finding.title || '(untitled)'}</p>
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          {finding.detected_at ? new Date(finding.detected_at).toLocaleString() : ''}
          {finding.status ? ` · ${finding.status}` : ''}
        </p>
      </div>
    </Link>
  );
}

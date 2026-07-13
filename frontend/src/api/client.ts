import axios from 'axios';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1';

export const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Types
export interface Finding {
  id: string;
  asset_id: string;
  scan_id: string;
  rule_id: string;
  title: string;
  description: string;
  severity: 'critical' | 'high' | 'medium' | 'low' | 'info';
  status: string;
  context: any;
  evidence: any;
  detected_at: string;
  updated_at: string;
}

export interface Scan {
  id: string;
  asset_id: string;
  scan_type: string;
  status: string;
  started_at: string;
  completed_at: string;
  rules_checked: number;
  findings_count: number;
}

export interface Asset {
  id: string;
  fqn: string;
  asset_type: string;
  database_name: string;
  schema_name: string;
  table_name: string;
  owner: string;
  comment: string;
  row_count: number | null;
  size_bytes: number | null;
  last_scanned_at: string;
}

export interface FindingStats {
  total: number;
  by_status: Record<string, number>;
  by_severity: Record<string, number>;
}

export interface Rule {
  id: string;
  code: string;
  name: string;
  description: string;
  category: string;
  severity: string;
  applies_to: string[];
  is_active: boolean;
  status: 'pending' | 'active' | 'disabled' | 'rejected';
  version: number;
  owner: string;
  created_by: string | null;
  jira_ticket: string | null;
  rejection_reason: string | null;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
  rejected_at: string | null;
}

export interface RuleCreatePayload {
  code: string;
  name: string;
  description: string;
  category: string;
  severity: string;
  applies_to: string[];
  rule_config?: Record<string, unknown>;
  is_active?: boolean;
  owner: string;           // required
  created_by?: string;
  jira_ticket?: string;
}

export interface RuleStats {
  total: number;
  active: number;
  by_category: Record<string, number>;
  by_severity: Record<string, number>;
}

// Health API — health routes are mounted at the server root (NOT under /api/v1),
// so use an absolute URL rather than the /api/v1 `api` instance.
const SERVER_ROOT = API_BASE_URL.replace(/\/api\/v1$/, '');

export interface SnowflakeHealth {
  status: 'connected' | 'disconnected';
  user: string | null;
  role: string | null;
  detail?: string;
}

export const healthApi = {
  check: () => axios.get(`${SERVER_ROOT}/health`),
  checkSnowflake: () => axios.get<SnowflakeHealth>(`${SERVER_ROOT}/health/snowflake`),
};

// Rules API
export interface GeneratedRule {
  code: string;
  name: string;
  description: string;
  category: string;
  severity: string;
  applies_to: string[];
  rationale: string;
  duplicate_of: { code: string; name: string } | null;
}

export const rulesApi = {
  list: (params?: { is_active?: boolean; category?: string; severity?: string; status?: string }) =>
    api.get<{ total: number; rules: Rule[] }>('/rules', { params }),
  stats: () => api.get<RuleStats & { pending: number; by_status: Record<string, number> }>('/rules/stats'),
  toggle: (id: string, is_active: boolean) =>
    api.patch<Rule>(`/rules/${id}`, { is_active }),
  update: (id: string, data: Partial<RuleCreatePayload>) =>
    api.patch<Rule>(`/rules/${id}`, data),
  create: (data: RuleCreatePayload) =>
    api.post<Rule>('/rules', data),
  approve: (id: string) =>
    api.post<Rule>(`/rules/${id}/approve`),
  reject: (id: string, reason: string) =>
    api.post<Rule>(`/rules/${id}/reject`, { reason }),
  generate: (prompt: string, owner?: string) =>
    api.post<GeneratedRule>('/rules/generate', { prompt, owner }),
};

// ── Rule Library: Definitions / Instances / Executions ────────────────────────

export interface RuleDefinition {
  id: string;
  name: string;
  category: string;
  description: string;
  check_kind: 'python_handler' | 'sql_template';
  handler_key: string | null;
  template_shape: string | null;
  sql_template: string | null;
  default_severity: string;
  allowed_scopes: string[];
  source: 'system' | 'claude' | 'user';
  status: 'proposed' | 'active' | 'disabled';
  instance_count: number;
  approval_count: number;
  owner: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface RuleInstance {
  id: string;
  definition_id: string;
  scope: string;
  database_name: string;
  schema_name: string | null;
  table_name: string | null;
  target_config: Record<string, any>;
  threshold_config: Record<string, any> | null;
  severity: string;
  rule_sql: string | null;
  status: string;
  is_active: boolean;
  rationale: string | null;
  rejection_reason: string | null;
  owner: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
  rejected_at: string | null;
}

export interface RuleExecution {
  id: string;
  instance_id: string;
  scan_id: string | null;
  run_id: string | null;
  status: 'passed' | 'failed' | 'error';
  evidence: Record<string, any> | null;
  executed_at: string;
}

export const ruleLibraryApi = {
  listDefinitions: (params?: { status?: string; category?: string; check_kind?: string }) =>
    api.get<{ total: number; definitions: RuleDefinition[] }>('/rules/definitions', { params }),
  getDefinition: (id: string) =>
    api.get<RuleDefinition>(`/rules/definitions/${id}`),
  listInstances: (definitionId: string) =>
    api.get<{ total: number; instances: RuleInstance[] }>(`/rules/definitions/${definitionId}/instances`),
  listExecutions: (instanceId: string) =>
    api.get<{ total: number; executions: RuleExecution[] }>(`/rules/instances/${instanceId}/executions`),
};

// API functions
export interface TableFindingSummary {
  table_name: string;
  total: number;
  by_severity: Record<string, number>;
}

export interface DatabaseFindingSummary {
  database: string;
  total: number;
  tables: TableFindingSummary[];
}

export const findingsApi = {
  list: (params?: any) => api.get<{ total: number; findings: Finding[] }>('/findings', { params }),
  get: (id: string) => api.get<Finding>(`/findings/${id}`),
  update: (id: string, data: any) => api.patch<Finding>(`/findings/${id}`, data),
  stats: () => api.get<FindingStats>('/findings/stats/summary'),
  byDatabase: () => api.get<DatabaseFindingSummary[]>('/findings/stats/by-database'),
};

export const scansApi = {
  list: () => api.get<{ total: number; scans: Scan[] }>('/scans'),
  create: (data: { database: string; schema: string; table: string }) =>
    api.post<Scan>('/scans/table', data),
};

export const assetsApi = {
  list: (params?: any) => api.get<{ total: number; assets: Asset[] }>('/assets', { params }),
  discoverDatabases: (connectionId?: string | null) =>
    api.get<{ databases: string[]; count: number }>('/assets/discover/databases',
      connectionId ? { params: { connection_id: connectionId } } : undefined),
  discoverSchemas: (database: string, connectionId?: string | null) =>
    api.get<{ schemas: string[]; count: number }>(`/assets/discover/schemas/${database}`,
      connectionId ? { params: { connection_id: connectionId } } : undefined),
  discoverTables: (database: string, schema: string, connectionId?: string | null) =>
    api.get<{ tables: string[]; count: number }>(`/assets/discover/tables/${database}/${schema}`,
      connectionId ? { params: { connection_id: connectionId } } : undefined),
};

// ── Data Explorer / Profiling ─────────────────────────────────────────────────

export interface ColumnMeta {
  column_name: string;
  data_type: string;
  is_nullable: boolean;
  primary_key: boolean;
  unique_key: boolean;
  comment: string | null;
}

export interface TopValue {
  value: string | number | null;
  count: number;
}

export type ColumnCategory =
  | 'id' | 'date' | 'amount' | 'measure' | 'status' | 'categorical' | 'email' | 'phone' | 'text';

export interface ColumnProfile {
  column_name: string;
  data_type: string;
  category: ColumnCategory;
  relevant_stats: string[];
  null_count: number | null;
  null_percentage: number | null;
  distinct_count: number | null;
  distinct_pct: number | null;
  duplicate_count: number | null;
  min_value: string | number | null;
  max_value: string | number | null;
  avg_value: string | number | null;
  stddev: string | number | null;
  freshness_days: number | null;
  pattern_match_pct: number | null;
  outlier_hint: boolean | null;
  top_values: TopValue[];
  is_sampled: boolean;
  error?: string;
}

export interface TableInfo {
  name: string;
  row_count: number | null;
  bytes: number | null;
  kind: string | null;
  owner: string | null;
  comment: string | null;
}

export interface TableProfile {
  table: {
    row_count: number;
    column_count: number;
    is_sampled: boolean;
    sample_size: number | null;
    bytes: number | null;
    kind: string | null;
    owner: string | null;
    comment: string | null;
  };
  columns: ColumnProfile[];
  categories: ColumnCategory[];
  category_labels: Record<string, string>;
  category_stats: Record<string, string[]>;
}

const connParam = (connectionId?: string | null) =>
  connectionId ? { params: { connection_id: connectionId } } : undefined;

export const profilingApi = {
  tableInfo: (database: string, schema: string, table: string, connectionId?: string | null) =>
    api.get<TableInfo>(`/profiling/table-info/${database}/${schema}/${table}`, connParam(connectionId)),
  columns: (database: string, schema: string, table: string, connectionId?: string | null) =>
    api.get<{ columns: ColumnMeta[] }>(`/profiling/columns/${database}/${schema}/${table}`, connParam(connectionId)),
  profile: (database: string, schema: string, table: string, connectionId?: string | null) =>
    api.post<TableProfile>(`/profiling/profile/${database}/${schema}/${table}`, null, connParam(connectionId)),
};

// AI API
export interface AIRecommendation {
  finding_id: string;
  explanation: string;
  sql_query: string;
  confidence: number;
  impact: string;
  from_cache: boolean;
  source: string; // cortex | claude | cache | error
}

export interface WarehouseInfo {
  name: string;
  size: string;
  state: string;
}

export interface RoleInfo {
  name: string;
  is_current: boolean;
  is_default: boolean;
}

export interface SnowflakeContext {
  user: string;
  current_role: string;
  roles: RoleInfo[];
  warehouses: WarehouseInfo[];
  databases: string[];
}

// ── Connections (multi-source) ────────────────────────────────────────────────

export type ConnectionType = 'snowflake' | 'postgres';

export interface Connection {
  id: string;
  name: string;
  type: ConnectionType;
  host: string | null;
  port: number | null;
  database: string | null;
  schema_name: string | null;
  username: string | null;
  has_secret: boolean;
  auth_method: string | null;
  extra: Record<string, any> | null;
  is_active: boolean;
  created_at: string;
}

export interface ConnectionCreatePayload {
  name: string;
  type: ConnectionType;
  host?: string;
  port?: number;
  database?: string;
  schema_name?: string;
  username?: string;
  secret?: string;
  auth_method?: string;
  extra?: Record<string, any>;
  is_active?: boolean;
}

export interface ConnectionTestResult {
  ok: boolean;
  user: string | null;
  detail: string | null;
}

// ── Settings ──────────────────────────────────────────────────────────────────

export interface SettingMeta {
  value: number;
  default: number;
  type: 'int' | 'float';
  min: number;
  max: number;
  label: string;
  help: string;
}
export type SettingsMap = Record<string, SettingMeta>;

export interface SystemConnectionInfo {
  id: string;
  name: string;
  type: string;
  host: string | null;
  database: string | null;
  username: string | null;
  warehouse: string | null;
  role: string | null;
  connected: boolean;
  connected_user: string | null;
  detail: string | null;
}

export interface SystemInfo {
  backend: string;
  connections_count: number;
  connections: SystemConnectionInfo[];
}

export const settingsApi = {
  get: () => api.get<SettingsMap>('/settings'),
  update: (updates: Record<string, number>) => api.patch<SettingsMap>('/settings', { updates }),
  systemInfo: () => api.get<SystemInfo>('/settings/system-info'),
};

export const connectionsApi = {
  list: () => api.get<{ total: number; connections: Connection[] }>('/connections'),
  create: (data: ConnectionCreatePayload) => api.post<Connection>('/connections', data),
  update: (id: string, data: Partial<ConnectionCreatePayload>) => api.patch<Connection>(`/connections/${id}`, data),
  remove: (id: string) => api.delete(`/connections/${id}`),
  test: (id: string) => api.post<ConnectionTestResult>(`/connections/${id}/test`),
  status: (id: string) => api.get<ConnectionTestResult>(`/connections/${id}/status`),
};

export const aiApi = {
  getContext: () => api.get<SnowflakeContext>('/ai/context'),
  getWarehouses: () => api.get<WarehouseInfo[]>('/ai/warehouses'),
  getRoles: () => api.get<RoleInfo[]>('/ai/roles'),
  getRecommendations: (findingIds: string[]) =>
    api.post<AIRecommendation[]>('/ai/recommendations', findingIds),
  executeSQL: (findingId: string, sqlQuery: string, warehouse: string, role: string) =>
    api.post('/ai/execute', { finding_id: findingId, sql_query: sqlQuery, warehouse, role }),
};

// ── Agent Workflow Types ──────────────────────────────────────────────────────

export type AgentRunStatus = 'pending' | 'running' | 'awaiting_rule_review' | 'awaiting_fixes' | 'completed' | 'failed';
export type AgentTaskStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped';

export interface AgentTask {
  id: string;
  run_id: string;
  agent_name: string;
  status: AgentTaskStatus;
  started_at: string | null;
  completed_at: string | null;
  output: Record<string, any> | null;
  error_message: string | null;
  duration_seconds: number | null;
}

export interface RuleReviewEntry {
  instance_id: string;
  definition_id: string;
  name: string;
  description: string;
  severity: string;
  original_severity: string;
  reason: string;
  is_new_instance: boolean;
  is_new_definition: boolean;
  source: 'existing' | 'llm' | 'deterministic';
  scope: string;
  target_config: Record<string, any>;
  violated: boolean;
  violation_evidence: string;
}

export interface AgentRun {
  id: string;
  connection_id: string | null;
  batch_id: string | null;
  batch_index: number;
  database: string;
  schema_name: string;
  table: string;
  status: AgentRunStatus;
  scan_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  findings_count: number;
  ai_rules_count: number;
  instance_review_state: {
    active: RuleReviewEntry[];
    skipped: RuleReviewEntry[];
    // Deterministic profiler signals the model never addressed. Freshness has
    // no deterministic backstop, so an omitted freshness signal here means no
    // check was proposed for it — surfaced so the reviewer sees the gap.
    signals_missed?: string[];
    // True when the model's JSON was unparseable even after a retry: "0
    // proposals" should be treated as suspect, not as full coverage.
    parse_failed?: boolean;
  } | null;
  error_message: string | null;
  created_at: string;
  tasks: AgentTask[];
}



export type WorkflowScope = 'table' | 'schema' | 'database';

export interface AgentBatch {
  batch_id: string;
  scope: WorkflowScope;
  database: string;
  schema_name: string | null;
  total: number;
  runs: AgentRun[];
}

export const agentRunsApi = {
  start: (data: { database: string; schema_name: string; table: string; connection_id?: string | null }) =>
    api.post<AgentRun>('/agent/runs', data),
  startBatch: (data: { scope: WorkflowScope; database: string; schema_name?: string; table?: string; connection_id?: string | null }) =>
    api.post<AgentBatch>('/agent/runs/batch', data),
  getBatch: (batchId: string) =>
    api.get<AgentBatch>(`/agent/runs/batch/${batchId}`),
  get: (id: string) =>
    api.get<AgentRun>(`/agent/runs/${id}`),
  list: () =>
    api.get<{ total: number; runs: AgentRun[] }>('/agent/runs'),
  reviewRules: (id: string, data: { active: RuleReviewEntry[]; skipped: RuleReviewEntry[] }) =>
    api.post<AgentRun>(`/agent/runs/${id}/review-rules`, data),
  bulkApprove: (id: string, instanceIds: string[]) =>
    api.post<AgentRun>(`/agent/runs/${id}/review-rules/bulk-approve`, { instance_ids: instanceIds }),
  bulkReject: (id: string, instanceIds: string[], reason?: string) =>
    api.post<AgentRun>(`/agent/runs/${id}/review-rules/bulk-reject`, { instance_ids: instanceIds, reason }),
  runPipeline: (id: string) =>
    api.post(`/agent/runs/${id}/run-pipeline`),
  verify: (id: string) =>
    api.post(`/agent/runs/${id}/verify`),
};

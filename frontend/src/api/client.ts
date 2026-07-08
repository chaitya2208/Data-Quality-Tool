import axios from 'axios';

const API_BASE_URL = 'http://localhost:8000/api/v1';

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

// Health API
export const healthApi = {
  check: () => api.get('/health'),
  checkSnowflake: () => api.get('/health/snowflake'),
};

// Validate API (Phase 4 — Shift-Left DDL Validation)
export interface DDLFinding {
  rule_code: string;
  rule_name: string;
  severity: string;
  title: string;
  description: string;
  column_name: string | null;
}

export interface DDLValidateResponse {
  passed: boolean;
  table_name: string;
  columns_parsed: number;
  rules_checked: number;
  findings_count: number;
  blocked_by: number;
  fail_on: string[];
  findings: DDLFinding[];
}

export const validateApi = {
  ddl: (sql: string, failOn: string[] = ['critical']) =>
    api.post<DDLValidateResponse>('/validate/ddl', { sql, fail_on: failOn }),
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
  discoverDatabases: () => api.get<{ databases: string[]; count: number }>('/assets/discover/databases'),
  discoverSchemas: (database: string) =>
    api.get<{ schemas: string[]; count: number }>(`/assets/discover/schemas/${database}`),
  discoverTables: (database: string, schema: string) =>
    api.get<{ tables: string[]; count: number }>(`/assets/discover/tables/${database}/${schema}`),
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
  code: string;
  name: string;
  description: string;
  severity: string;
  original_severity: string;
  reason: string;
  is_ai_generated: boolean;
  category: string;
  applies_to: string[];
  violated: boolean;
  ai_violation_evidence: string;
}

export interface AgentRun {
  id: string;
  database: string;
  schema_name: string;
  table: string;
  status: AgentRunStatus;
  scan_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  findings_count: number;
  ai_rules_count: number;
  rule_review_state: { active: RuleReviewEntry[]; skipped: RuleReviewEntry[] } | null;
  error_message: string | null;
  created_at: string;
  tasks: AgentTask[];
}



export const agentRunsApi = {
  start: (data: { database: string; schema_name: string; table: string }) =>
    api.post<AgentRun>('/agent/runs', data),
  get: (id: string) =>
    api.get<AgentRun>(`/agent/runs/${id}`),
  list: () =>
    api.get<{ total: number; runs: AgentRun[] }>('/agent/runs'),
  reviewRules: (id: string, data: { active: RuleReviewEntry[]; skipped: RuleReviewEntry[] }) =>
    api.post<AgentRun>(`/agent/runs/${id}/review-rules`, data),
  runPipeline: (id: string) =>
    api.post(`/agent/runs/${id}/run-pipeline`),
  verify: (id: string) =>
    api.post(`/agent/runs/${id}/verify`),
};

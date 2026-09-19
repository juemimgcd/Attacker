/**
 * 后端 Pydantic schema 的 TypeScript 镜像。
 * 只保留前端真实消费的字段；与 app/schemas/* 一一对应。
 */

// ---------- 通用 ----------

export type RiskLevel = "low" | "medium" | "high" | "critical";

export type EvaluationOutcome =
  | "violation"
  | "refused"
  | "safe"
  | "error"
  | "budget_aborted"
  | "not_evaluable"
  | "inconclusive"
  | "policy_denied"
  | "approval_rejected"
  | "loop_aborted";

export type RunStatus =
  | "running"
  | "completed"
  | "failed"
  | "waiting_approval"
  | "paused"
  | "cancelled"
  | "aborted"
  | string;

// ---------- Target ----------

export interface TargetAuth {
  type: string;
  token?: string | null;
  header_name: string;
  token_prefix: string;
}

export interface TargetRequestTemplate {
  body_template: Record<string, unknown>;
}

export interface TargetConfig {
  name: string;
  endpoint: string;
  method: "POST";
  headers: Record<string, string>;
  auth: TargetAuth;
  timeout_seconds: number;
  allow_public_target: boolean;
  provider_instance_id?: string | null;
  refusal_status_codes: number[];
  request_template: TargetRequestTemplate;
}

// ---------- Run 请求 ----------

export interface RunBudget {
  max_cases: number;
  max_target_calls: number;
  max_duration_seconds: number;
  max_response_bytes: number;
}

export interface DeterministicRunRequest {
  target: TargetConfig;
  dataset_path: string;
  case_ids?: string[] | null;
  fixture_evidence_refs?: Record<string, string>;
  budget: RunBudget;
}

export interface AttackPolicy {
  max_steps: number;
  max_target_calls: number;
  max_duration_seconds: number;
  allowed_risk_levels: RiskLevel[];
  allowed_case_ids?: string[] | null;
  approval_risk_levels: RiskLevel[];
  loop_detection_threshold: number;
}

export interface PlannerConfig {
  backend: "deterministic" | "openai_compatible";
  endpoint?: string | null;
  api_key?: string | null;
  provider_id: string;
  model: string;
  timeout_seconds: number;
  temperature: number;
  max_physical_attempts: number;
  prompt_template_version: string;
}

export interface GrayBoxRunRequest {
  target: TargetConfig;
  dataset_path: string;
  case_ids?: string[] | null;
  policy: AttackPolicy;
  planner: PlannerConfig;
  test_principal_refs: string[];
  baseline_run_id?: string | null;
}

export interface StatefulRunRequest {
  profile: "vulnerable" | "hardened" | "regressed";
  dataset_path: string;
  case_ids?: string[] | null;
  target_name: string;
}

export interface SubagentAssignment {
  agent_id: string;
  case_ids: string[];
  planner?: PlannerConfig | null;
}

export interface SubagentRunRequest {
  run: GrayBoxRunRequest;
  subagents: SubagentAssignment[];
  concurrency: number;
  parallel_target_safe: boolean;
}

// ---------- 报告 ----------

export interface RunSummary {
  status: RunStatus;
  total_cases: number;
  completed_cases: number;
  outcomes: Record<string, number>;
  false_positives: number;
  defense_overblocks: number;
  target_calls: number;
  tool_calls: number;
  planner_calls: number;
  planner_tokens: number;
  policy_denials: number;
  approval_requests: number;
  finding_evidence_link_rate: number | null;
  step_outcomes?: Record<string, number>;
}

export interface RunInfo {
  id: string;
  mode: string;
  status: RunStatus;
  started_at?: string;
  completed_at?: string | null;
  total_cases: number;
  completed_cases: number;
  violation_count: number;
  refused_count: number;
  safe_count: number;
  error_count: number;
  budget_aborted_count: number;
  false_positive_count: number;
  defense_overblock_count: number;
  target_call_count: number;
  tool_call_count?: number;
  planner_call_count?: number;
  planner_token_count?: number;
  policy_denied_count?: number;
  [key: string]: unknown;
}

export interface DatasetInfo {
  id: string;
  name: string;
  version: string;
  sha256?: string;
  [key: string]: unknown;
}

export interface TargetInfo {
  id: string;
  name: string;
  endpoint?: string;
  [key: string]: unknown;
}

export interface Finding {
  id: string;
  case_id: string;
  category?: string;
  outcome?: string;
  risk_level: RiskLevel;
  reason?: string;
  evidence_event_ids: string[];
  fingerprint?: string;
  is_control?: boolean;
  created_at?: string;
  [key: string]: unknown;
}

export interface EvidenceEvent {
  id: string;
  event_type: string;
  step_id?: string | null;
  operation_id?: string;
  sequence?: number;
  evidence: Record<string, unknown>;
  created_at?: string;
  [key: string]: unknown;
}

export interface StepRow {
  id: string;
  case_id: string;
  outcome: string;
  result?: Record<string, unknown>;
  created_at?: string;
  [key: string]: unknown;
}

export interface Approval {
  approval_id: string;
  run_id?: string;
  case_id: string;
  status: "pending" | "approved" | "rejected" | "expired";
  risk_level?: RiskLevel;
  reason?: string | null;
  resolved_by?: string | null;
  created_at?: string;
  resolved_at?: string | null;
  [key: string]: unknown;
}

export interface ReplayDiff {
  fixed: string[];
  new: string[];
  persistent: string[];
  regressed: string[];
}

export interface RunReport {
  run: RunInfo;
  summary: RunSummary;
  dataset?: DatasetInfo;
  target?: TargetInfo;
  steps: StepRow[];
  findings: Finding[];
  events: EvidenceEvent[];
  approvals: Approval[];
  state_fixtures?: unknown[];
  retrievals?: unknown[];
  react_summary?: Record<string, unknown>;
  stateful_summary?: Record<string, unknown>;
  adaptive_observability?: Record<string, unknown>;
  equipment_snapshots?: Record<string, unknown>[];
  benchmark_summary?: Record<string, unknown>;
  comparison?: Record<string, unknown>;
  replay?: {
    source_run_id: string;
    replay_run_id: string;
    diff: ReplayDiff;
  };
  [key: string]: unknown;
}

// ---------- Jobs ----------

export type JobKind = "deterministic" | "adaptive" | "deterministic_graybox" | "stateful";

export type JobStatus =
  | "queued"
  | "leased"
  | "running"
  | "retry_wait"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface RunJob {
  id: string;
  run_id: string | null;
  request_id: string;
  kind: JobKind;
  status: JobStatus;
  priority: number;
  attempts: number;
  max_attempts: number;
  available_at: string;
  lease_owner: string | null;
  lease_expires_at: string | null;
  result: Record<string, unknown> | null;
  error_code: string | null;
  error_summary: string | null;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface RunJobCreate {
  request_id: string;
  kind: JobKind;
  payload: Record<string, unknown>;
  priority?: number;
  max_attempts?: number | null;
}

// ---------- 装备 ----------

export type PackageType = "provider" | "skill" | "case_pack" | "benchmark";

export interface EquipmentPackage {
  package_id: string;
  package_type?: string;
  version: string;
  name?: string;
  description?: string;
  enabled: boolean;
  validation_status?: string;
  checksum?: string;
  capabilities?: string[];
  tags?: string[];
  [key: string]: unknown;
}

// ---------- 健康 ----------

export interface HealthStatus {
  status: string;
  service: string;
  environment: string;
  dependencies?: Record<string, { status: string; error?: string }>;
}

// ---------- Subagent ----------

export interface SubagentRunSummary {
  coordinator_id: string;
  status: string;
  agent_count?: number;
  created_at?: string;
  [key: string]: unknown;
}

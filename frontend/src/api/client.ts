import { http, toApiError } from "@/lib/http";
import type {
  Approval,
  DeterministicRunRequest,
  EquipmentPackage,
  GrayBoxRunRequest,
  HealthStatus,
  JobStatus,
  PackageType,
  ReplayDiff,
  RunJob,
  RunJobCreate,
  RunReport,
  StatefulRunRequest,
  SubagentRunRequest,
  SubagentRunSummary,
} from "@/types/api";

async function unwrap<T>(promise: Promise<{ data: T }>): Promise<T> {
  try {
    const response = await promise;
    return response.data;
  } catch (error) {
    throw toApiError(error);
  }
}

// ---------- 健康 ----------

export const getHealth = () => unwrap<HealthStatus>(http.get("/health"));

// ---------- Jobs ----------

export const listJobs = (status?: JobStatus, limit = 100) =>
  unwrap<RunJob[]>(http.get("/jobs", { params: { status, limit } }));

export const getJob = (jobId: string) => unwrap<RunJob>(http.get(`/jobs/${jobId}`));

export const enqueueJob = (payload: RunJobCreate) =>
  unwrap<RunJob>(http.post("/jobs", payload));

export const cancelJob = (jobId: string) =>
  unwrap<RunJob>(http.post(`/jobs/${jobId}/cancel`));

export const retryJob = (jobId: string) =>
  unwrap<RunJob>(http.post(`/jobs/${jobId}/retry`));

// ---------- Runs ----------

export const getRunReport = (runId: string) =>
  unwrap<RunReport>(http.get(`/runs/${runId}`));

export const createDeterministicRun = (payload: DeterministicRunRequest) =>
  unwrap<Record<string, unknown>>(http.post("/runs/deterministic", payload));

export const createGrayBoxRun = (payload: GrayBoxRunRequest) =>
  unwrap<Record<string, unknown>>(http.post("/runs/adaptive", payload));

export const createDeterministicGrayBoxRun = (payload: GrayBoxRunRequest) =>
  unwrap<Record<string, unknown>>(http.post("/runs/graybox/deterministic", payload));

export const createStatefulRun = (payload: StatefulRunRequest) =>
  unwrap<Record<string, unknown>>(http.post("/runs/stateful", payload));

export const createSubagentRun = (payload: SubagentRunRequest) =>
  unwrap<Record<string, unknown>>(http.post("/runs/subagents", payload));

export const listSubagentRuns = (limit = 20) =>
  unwrap<SubagentRunSummary[]>(http.get("/runs/subagents", { params: { limit } }));

export const controlAdaptiveRun = (runId: string, action: string, reason: string) =>
  unwrap<Record<string, unknown>>(http.post(`/runs/${runId}/control`, { action, reason }));

export const getRunMarkdownUrl = (runId: string) => `/runs/${runId}/report.md`;

// ---------- 审批 ----------

export const listApprovals = (runId: string) =>
  unwrap<Approval[]>(http.get(`/runs/${runId}/approvals`));

export const resolveApproval = (
  runId: string,
  approvalId: string,
  payload: { approved: boolean; resolved_by: string; reason: string },
) =>
  unwrap<Record<string, unknown>>(
    http.post(`/runs/${runId}/approvals/${approvalId}`, payload),
  );

// ---------- Replay ----------

export const replayRun = (sourceRunId: string, payload: Record<string, unknown>) =>
  unwrap<Record<string, unknown>>(http.post(`/runs/${sourceRunId}/replay`, payload));

export const getReplay = (runId: string) =>
  unwrap<{ diff?: ReplayDiff } & Record<string, unknown>>(http.get(`/runs/${runId}/replay`));

// ---------- 装备 ----------

const EQUIPMENT_LIST_PATH: Record<PackageType, string> = {
  provider: "/equipment/provider-packages",
  skill: "/equipment/skills",
  case_pack: "/equipment/casepacks",
  benchmark: "/equipment/benchmarks",
};

export const listEquipment = (packageType: PackageType) =>
  unwrap<EquipmentPackage[]>(http.get(EQUIPMENT_LIST_PATH[packageType]));

export const listProviderInstances = () =>
  unwrap<Record<string, unknown>[]>(http.get("/equipment/provider-instances"));

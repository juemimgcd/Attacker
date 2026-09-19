import type { EvaluationOutcome, JobStatus, RiskLevel } from "@/types/api";

/** 统一的状态中文文案与 AntD Tag 颜色。 */
export const outcomeMeta: Record<string, { label: string; color: string }> = {
  violation: { label: "违规", color: "red" },
  safe: { label: "安全", color: "green" },
  refused: { label: "已拒绝", color: "cyan" },
  error: { label: "错误", color: "volcano" },
  budget_aborted: { label: "预算中止", color: "orange" },
  not_evaluable: { label: "不可评估", color: "default" },
  inconclusive: { label: "不确定", color: "gold" },
  policy_denied: { label: "策略拒绝", color: "purple" },
  approval_rejected: { label: "审批拒绝", color: "magenta" },
  loop_aborted: { label: "循环中止", color: "orange" },
};

export const riskMeta: Record<RiskLevel, { label: string; color: string }> = {
  low: { label: "低", color: "blue" },
  medium: { label: "中", color: "gold" },
  high: { label: "高", color: "orange" },
  critical: { label: "严重", color: "red" },
};

export const runStatusMeta: Record<string, { label: string; color: string }> = {
  running: { label: "运行中", color: "processing" },
  completed: { label: "已完成", color: "success" },
  failed: { label: "失败", color: "error" },
  waiting_approval: { label: "等待审批", color: "warning" },
  paused: { label: "已暂停", color: "gold" },
  cancelled: { label: "已取消", color: "default" },
  aborted: { label: "已中止", color: "volcano" },
};

export const jobStatusMeta: Record<JobStatus, { label: string; color: string }> = {
  queued: { label: "排队中", color: "default" },
  leased: { label: "已领取", color: "blue" },
  running: { label: "运行中", color: "processing" },
  retry_wait: { label: "等待重试", color: "warning" },
  succeeded: { label: "已完成", color: "success" },
  failed: { label: "失败", color: "error" },
  cancelled: { label: "已取消", color: "default" },
};

export const jobKindLabel: Record<string, string> = {
  deterministic: "确定性黑盒",
  deterministic_graybox: "确定性灰盒",
  adaptive: "自适应灰盒",
  stateful: "Stateful 基线",
};

export const runModeLabel: Record<string, string> = {
  deterministic: "确定性黑盒",
  deterministic_graybox: "确定性灰盒",
  adaptive: "自适应灰盒",
  stateful: "带状态",
  subagent: "Subagent",
  equipment_benchmark: "装备基准",
  benchmark_import: "公开基准",
};

export const approvalStatusMeta: Record<string, { label: string; color: string }> = {
  pending: { label: "待处理", color: "warning" },
  approved: { label: "已批准", color: "success" },
  rejected: { label: "已拒绝", color: "error" },
  expired: { label: "已过期", color: "default" },
};

export const replayKindMeta: Record<string, { label: string; color: string }> = {
  fixed: { label: "已修复", color: "success" },
  new: { label: "新增", color: "error" },
  persistent: { label: "持续存在", color: "warning" },
  regressed: { label: "回归", color: "volcano" },
};

export function outcomeLabel(outcome: EvaluationOutcome | string): string {
  return outcomeMeta[outcome]?.label ?? outcome;
}

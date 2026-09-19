import { Tag } from "antd";
import {
  approvalStatusMeta,
  jobStatusMeta,
  outcomeMeta,
  replayKindMeta,
  riskMeta,
  runStatusMeta,
} from "@/lib/labels";
import type { JobStatus, RiskLevel } from "@/types/api";

export function OutcomeTag({ outcome }: { outcome: string }) {
  const meta = outcomeMeta[outcome] ?? { label: outcome, color: "default" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export function RiskTag({ level }: { level?: RiskLevel | string }) {
  if (!level) return null;
  const meta = riskMeta[level as RiskLevel] ?? { label: level, color: "default" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export function RunStatusTag({ status }: { status: string }) {
  const meta = runStatusMeta[status] ?? { label: status, color: "default" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export function JobStatusTag({ status }: { status: JobStatus }) {
  const meta = jobStatusMeta[status] ?? { label: status, color: "default" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export function ApprovalStatusTag({ status }: { status: string }) {
  const meta = approvalStatusMeta[status] ?? { label: status, color: "default" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export function ReplayKindTag({ kind }: { kind: string }) {
  const meta = replayKindMeta[kind] ?? { label: kind, color: "default" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

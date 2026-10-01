import { Badge } from "@/components/ui/badge";
import { Check, Circle, Clock3, CircleAlert, CircleX } from "lucide-react";
import {
  approvalStatusMeta,
  jobStatusMeta,
  outcomeMeta,
  replayKindMeta,
  riskMeta,
  runStatusMeta,
} from "@/lib/labels";
import type { JobStatus, RiskLevel } from "@/types/api";

function StatusBadge({ meta }: { meta: { label: string; color: string } }) {
  const danger = ["red", "error", "volcano", "magenta"].includes(meta.color);
  const success = ["green", "success"].includes(meta.color);
  const waiting = ["warning", "gold", "orange"].includes(meta.color);
  const Icon = danger
    ? CircleX
    : success
      ? Check
      : waiting
        ? Clock3
        : meta.color === "processing"
          ? CircleAlert
          : Circle;
  return (
    <Badge variant={danger ? "destructive" : "outline"}>
      <Icon data-icon="inline-start" />
      {meta.label}
    </Badge>
  );
}
export function OutcomeTag({ outcome }: { outcome: string }) {
  return (
    <StatusBadge
      meta={outcomeMeta[outcome] ?? { label: outcome, color: "default" }}
    />
  );
}
export function RiskTag({ level }: { level?: RiskLevel | string }) {
  return level ? (
    <StatusBadge
      meta={riskMeta[level as RiskLevel] ?? { label: level, color: "default" }}
    />
  ) : null;
}
export function RunStatusTag({ status }: { status: string }) {
  return (
    <StatusBadge
      meta={runStatusMeta[status] ?? { label: status, color: "default" }}
    />
  );
}
export function JobStatusTag({ status }: { status: JobStatus }) {
  return (
    <StatusBadge
      meta={jobStatusMeta[status] ?? { label: status, color: "default" }}
    />
  );
}
export function ApprovalStatusTag({ status }: { status: string }) {
  return (
    <StatusBadge
      meta={approvalStatusMeta[status] ?? { label: status, color: "default" }}
    />
  );
}
export function ReplayKindTag({ kind }: { kind: string }) {
  return (
    <StatusBadge
      meta={replayKindMeta[kind] ?? { label: kind, color: "default" }}
    />
  );
}

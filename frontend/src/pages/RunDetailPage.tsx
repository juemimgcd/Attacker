import { Fragment, useState } from "react";
import { Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import dayjs from "dayjs";
import {
  Activity, AlertCircle, ArrowLeft, ArrowRight, Check, CircleCheck, CircleX,
  Copy, FileDown, FileSearch, GitCompareArrows, ListChecks, RotateCcw,
  ShieldCheck, Square, Target, type LucideIcon,
} from "lucide-react";
import {
  controlAdaptiveRun, getRunMarkdownUrl, getRunReport, listApprovals, resolveApproval,
} from "@/api/client";
import {
  ApprovalStatusTag, OutcomeTag, ReplayKindTag, RiskTag, RunStatusTag,
} from "@/components/StatusTags";
import JsonViewer from "@/components/JsonViewer";
import PageHeader from "@/components/PageHeader";
import StatCard from "@/components/StatCard";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { runModeLabel } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Approval, Finding, StepRow } from "@/types/api";

const ACTIVE_RUN_STATUSES = new Set(["running", "waiting_approval", "paused"]);

function DetailEmpty({ icon: Icon, title, description }: { icon: LucideIcon; title: string; description: string }) {
  return (
    <Empty className="min-h-52 py-10">
      <EmptyHeader>
        <EmptyMedia variant="icon"><Icon /></EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}

export default function RunDetailPage() {
  const { runId = "" } = useParams();
  const queryClient = useQueryClient();
  const [controlReason, setControlReason] = useState("");
  const [cancelOpen, setCancelOpen] = useState(false);
  const [activeTab, setActiveTab] = useState("overview");
  const [approvalModal, setApprovalModal] = useState<{
    approval: Approval;
    approved: boolean;
  } | null>(null);
  const [operator, setOperator] = useState("");
  const [approvalReason, setApprovalReason] = useState("");

  const reportQuery = useQuery({
    queryKey: ["run", runId],
    queryFn: () => getRunReport(runId),
    refetchInterval: (query) =>
      ACTIVE_RUN_STATUSES.has(query.state.data?.run.status ?? "") ? 3_000 : false,
  });

  const approvalsQuery = useQuery({
    queryKey: ["run", runId, "approvals"],
    queryFn: () => listApprovals(runId),
    refetchInterval: 5_000,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["run", runId] });
  };

  const controlMutation = useMutation({
    mutationFn: ({ action, reason }: { action: string; reason: string }) =>
      controlAdaptiveRun(runId, action, reason),
    onSuccess: () => {
      toast.success("控制指令已提交");
      setControlReason("");
      setCancelOpen(false);
      invalidate();
    },
    onError: (error) => toast.error((error as Error).message),
  });

  const resolveMutation = useMutation({
    mutationFn: ({ approvalId, approved }: { approvalId: string; approved: boolean }) =>
      resolveApproval(runId, approvalId, {
        approved,
        resolved_by: operator,
        reason: approvalReason,
      }),
    onSuccess: () => {
      toast.success("审批已记录，Run 将按策略继续");
      setApprovalModal(null);
      setApprovalReason("");
      invalidate();
    },
    onError: (error) => toast.error((error as Error).message),
  });

  if (reportQuery.isError) {
    return (
      <>
        <PageHeader eyebrow="Run Detail" title="运行详情" />
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>无法加载运行详情</AlertTitle>
          <AlertDescription>{(reportQuery.error as Error).message}</AlertDescription>
        </Alert>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button onClick={() => reportQuery.refetch()} disabled={reportQuery.isFetching}>
            {reportQuery.isFetching ? <Spinner data-icon="inline-start" /> : <RotateCcw data-icon="inline-start" />}
            重新加载
          </Button>
          <Button variant="outline" asChild>
            <Link to="/jobs"><ArrowLeft data-icon="inline-start" />返回任务列表</Link>
          </Button>
        </div>
      </>
    );
  }

  if (reportQuery.isPending) {
    return (
      <>
        <PageHeader eyebrow="Run Detail" title="运行详情" />
        <div role="status" aria-label="正在加载运行详情" className="flex flex-col gap-6">
          <span className="sr-only">正在加载运行详情…</span>
          <div className="flex flex-wrap gap-2" aria-hidden="true">
            <Skeleton className="h-6 w-20" /><Skeleton className="h-6 w-32" /><Skeleton className="h-6 w-48" />
          </div>
          <Skeleton className="h-32 w-full" aria-hidden="true" />
          <Skeleton className="h-3 w-full" aria-hidden="true" />
          <Skeleton className="h-8 w-48" aria-hidden="true" />
          <Skeleton className="h-64 w-full" aria-hidden="true" />
        </div>
      </>
    );
  }

  const report = reportQuery.data;
  const run = report.run;
  const summary = report.summary;
  const approvals = approvalsQuery.data ?? [];
  const pendingApprovals = approvals.filter((item) => item.status === "pending");
  const isActive = ACTIVE_RUN_STATUSES.has(run.status);
  const progress = summary.total_cases
    ? Math.round((summary.completed_cases / summary.total_cases) * 100)
    : 0;

  const findingColumns: ColumnsType<Finding> = [
    {
      title: "Case", dataIndex: "case_id", width: 220,
      render: (value: string) => <span className="mono">{value}</span>,
    },
    {
      title: "严重度", dataIndex: "risk_level", width: 90,
      render: (value: string) => <RiskTag level={value} />,
    },
    {
      title: "结论", dataIndex: "outcome", width: 110,
      render: (value: string) => value ? <OutcomeTag outcome={value} /> : "—",
    },
    {
      title: "类别 / 理由", ellipsis: true,
      render: (_, record) => record.category ? (
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary">{record.category}</Badge>
          <span className="text-xs text-muted-foreground">{record.reason}</span>
        </div>
      ) : <span className="mono">{record.fingerprint}</span>,
    },
    {
      title: "证据", width: 130,
      render: (_, record) => <Badge variant="outline">{record.evidence_event_ids.length} 条 Evidence</Badge>,
    },
  ];

  const stepColumns: ColumnsType<StepRow> = [
    {
      title: "Case", dataIndex: "case_id",
      render: (value: string) => <span className="mono">{value}</span>,
    },
    {
      title: "结果", dataIndex: "outcome", width: 120,
      render: (value: string) => <OutcomeTag outcome={value} />,
    },
    {
      title: "详情", width: 90,
      render: (_, record) => record.result ? <JsonViewer data={record.result} title="Step 结果" /> : "—",
    },
  ];

  const overview = [
    { label: "评测模式", value: runModeLabel[run.mode] ?? run.mode },
    { label: "运行状态", value: <RunStatusTag status={run.status} /> },
    { label: "数据集", value: report.dataset ? `${report.dataset.name} v${report.dataset.version}` : "—" },
    { label: "Target", value: report.target?.name ?? "—" },
    { label: "开始时间", value: run.started_at ? dayjs(run.started_at).format("YYYY-MM-DD HH:mm:ss") : "—" },
    { label: "完成时间", value: run.completed_at ? dayjs(run.completed_at).format("YYYY-MM-DD HH:mm:ss") : "—" },
    { label: "误报 / 过拦", value: `${summary.false_positives ?? 0} / ${summary.defense_overblocks ?? 0}` },
    { label: "Planner 调用 / Token", value: `${summary.planner_calls ?? 0} / ${summary.planner_tokens ?? 0}` },
  ];
  const detailTabs = [
    { value: "overview", label: "概览" },
    { value: "findings", label: "Finding", count: report.findings.length },
    { value: "steps", label: "Case 结果", count: report.steps.length },
    { value: "evidence", label: "证据时间线", count: report.events.length },
    { value: "approvals", label: "审批", count: approvals.length },
    { value: "replay", label: "Replay 差异" },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Run Detail"
        title="运行详情"
        desc="查看执行结果、证据与审批记录。"
        extra={
          <>
            <Button variant="outline" asChild>
              <a href={getRunMarkdownUrl(runId)} target="_blank" rel="noreferrer">
                <FileDown data-icon="inline-start" />Markdown 报告
              </a>
            </Button>
            {isActive && (
              <Dialog open={cancelOpen} onOpenChange={setCancelOpen}>
                <DialogTrigger asChild>
                  <Button variant="destructive" disabled={controlMutation.isPending}>
                    <Square data-icon="inline-start" />请求取消
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>请求取消该 Run</DialogTitle>
                    <DialogDescription>填写取消原因后，向当前运行提交取消指令。</DialogDescription>
                  </DialogHeader>
                  <form className="flex flex-col gap-6" onSubmit={(event) => {
                    event.preventDefault();
                    if (!controlReason.trim() || controlMutation.isPending) return;
                    controlMutation.mutate({ action: "cancel", reason: controlReason.trim() });
                  }}>
                    <FieldGroup>
                      <Field data-disabled={controlMutation.isPending}>
                        <FieldLabel htmlFor="cancel-reason">取消原因</FieldLabel>
                        <Textarea id="cancel-reason" required rows={3} value={controlReason}
                          onChange={(event) => setControlReason(event.target.value)} disabled={controlMutation.isPending}
                          placeholder="说明取消这次运行的原因" aria-describedby="cancel-reason-help" />
                        <FieldDescription id="cancel-reason-help">必填，不能仅包含空格。</FieldDescription>
                      </Field>
                    </FieldGroup>
                    <DialogFooter>
                      <Button type="button" variant="outline" onClick={() => setCancelOpen(false)}>返回</Button>
                      <Button type="submit" variant="destructive" disabled={!controlReason.trim() || controlMutation.isPending}>
                        {controlMutation.isPending && <Spinner data-icon="inline-start" />}确认取消
                      </Button>
                    </DialogFooter>
                  </form>
                </DialogContent>
              </Dialog>
            )}
          </>
        }
      />

      <div className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-2">
        <RunStatusTag status={run.status} />
        <Badge variant="secondary">{runModeLabel[run.mode] ?? run.mode}</Badge>
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="mono truncate text-muted-foreground" title={runId}>{runId}</span>
          <Button variant="ghost" size="icon-xs" aria-label="复制 Run ID" onClick={async () => {
            try { await navigator.clipboard.writeText(runId); toast.success("Run ID 已复制"); }
            catch { toast.error("无法复制 Run ID，请手动复制。"); }
          }}><Copy data-icon="inline-start" /></Button>
        </div>
        {run.started_at && <span className="text-xs text-muted-foreground">开始 {dayjs(run.started_at).format("MM-DD HH:mm:ss")}</span>}
        {run.completed_at && <span className="text-xs text-muted-foreground">完成 {dayjs(run.completed_at).format("MM-DD HH:mm:ss")}</span>}
      </div>

      {pendingApprovals.length > 0 && (
        <Alert className="mb-6">
          <ShieldCheck />
          <AlertTitle>{pendingApprovals.length} 个高风险步骤等待审批</AlertTitle>
          <AlertDescription>
            <p>批准后 Run 会恢复执行，执行前会重新检查授权策略。</p>
            <Button variant="outline" size="sm" className="mt-2" onClick={() => setActiveTab("approvals")}>
              查看待审批步骤<ArrowRight data-icon="inline-end" />
            </Button>
          </AlertDescription>
        </Alert>
      )}

      <section aria-label="运行指标">
        <div className="stats-grid">
          <StatCard label="Case 进度" value={`${summary.completed_cases ?? 0}/${summary.total_cases ?? 0}`} accent="var(--foreground)" icon={<ListChecks />} hint="已完成 / 总用例" />
          <StatCard label="违规发现" value={summary.outcomes?.violation ?? 0} accent="var(--danger)" icon={<AlertCircle />} hint="评测结果为违规的用例" valueColor={(summary.outcomes?.violation ?? 0) > 0 ? "var(--danger)" : undefined} />
          <StatCard label="Target 调用" value={summary.target_calls ?? 0} accent="var(--foreground)" icon={<Target />} hint="当前运行的目标调用次数" />
          <StatCard label="证据关联率" value={summary.finding_evidence_link_rate == null ? "—" : `${Math.round(summary.finding_evidence_link_rate * 100)}%`} accent="var(--foreground)" icon={<FileSearch />} hint="Finding 与证据的关联比例" />
        </div>
        <Field className="mb-8">
          <FieldLabel htmlFor="run-progress" className="flex items-center justify-between">
            <span>执行进度</span><span className="tabular-nums">{progress}%</span>
          </FieldLabel>
          <Progress id="run-progress" value={progress} aria-label="Case 执行进度" />
        </Field>
      </section>

      <Tabs value={activeTab} onValueChange={setActiveTab} className="gap-5">
        <div className="-m-1 max-w-full overflow-x-auto p-1">
          <TabsList variant="line" aria-label="运行详情分类">
            {detailTabs.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value}>
                {tab.label}{tab.count !== undefined && <Badge variant="secondary">{tab.count}</Badge>}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="overview">
          <Card>
            <CardHeader><CardTitle>运行信息</CardTitle><CardDescription>本次评测的配置、时间与调用统计。</CardDescription></CardHeader>
            <CardContent>
              <dl className="grid gap-x-10 gap-y-6 sm:grid-cols-2">
                {overview.map((item) => (
                  <div key={item.label} className="flex min-w-0 flex-col gap-2">
                    <dt className="text-xs text-muted-foreground">{item.label}</dt>
                    <dd className="min-w-0 break-words text-sm">{item.value}</dd>
                  </div>
                ))}
              </dl>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="findings" forceMount hidden={activeTab !== "findings"}>
          <Card>
            <CardHeader><CardTitle>评测发现</CardTitle><CardDescription>按用例检查严重度、评测结论与证据关联。</CardDescription></CardHeader>
            <CardContent className="p-0">
              <Table rowKey="id" size="middle" columns={findingColumns} scroll={{ x: 820 }}
                dataSource={report.findings} pagination={{ pageSize: 15, showSizeChanger: false }}
                locale={{ emptyText: <DetailEmpty icon={FileSearch} title="暂无 Finding" description="当前运行还没有可展示的评测发现。" /> }} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="steps" forceMount hidden={activeTab !== "steps"}>
          <Card>
            <CardHeader><CardTitle>Case 结果</CardTitle><CardDescription>查看每个用例的执行结论与完整 Step 结果。</CardDescription></CardHeader>
            <CardContent className="p-0">
              <Table rowKey="id" size="middle" columns={stepColumns} scroll={{ x: 760 }}
                dataSource={report.steps} pagination={{ pageSize: 20, showSizeChanger: false }}
                locale={{ emptyText: <DetailEmpty icon={ListChecks} title="暂无 Case 结果" description="用例执行完成后，结果会显示在这里。" /> }} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="evidence" forceMount hidden={activeTab !== "evidence"}>
          <Card>
            <CardHeader><CardTitle>证据时间线</CardTitle><CardDescription>沿执行事件回溯操作与结构化证据。</CardDescription></CardHeader>
            <CardContent>
              {report.events.length ? (
                <ScrollArea className="h-[min(35rem,65dvh)]">
                  <ol className="flex flex-col pr-4" aria-label="运行证据事件">
                    {report.events.map((event, index) => {
                      const isBad = ["violation", "denied", "failed"].some((kind) => event.event_type.includes(kind));
                      const isApproval = event.event_type.includes("approval");
                      const EventIcon = isBad ? CircleX : isApproval ? ShieldCheck : CircleCheck;
                      return (
                        <li key={event.id} className="flex min-w-0 gap-4">
                          <div className="flex w-7 shrink-0 flex-col items-center gap-2 pt-1">
                            <EventIcon aria-hidden="true" className={cn("size-4", isBad ? "text-destructive" : "text-muted-foreground")} />
                            {index < report.events.length - 1 && <Separator orientation="vertical" className="min-h-8 flex-1" />}
                          </div>
                          <div className="flex min-w-0 flex-1 flex-col gap-2 pb-6">
                            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                              <span className="break-all text-sm font-medium">{event.event_type}</span>
                              <JsonViewer data={event.evidence} title={event.event_type} />
                            </div>
                            {event.operation_id && <span className="mono break-all text-muted-foreground">{event.operation_id}</span>}
                            {event.created_at && <time className="text-xs tabular-nums text-muted-foreground" dateTime={event.created_at}>{dayjs(event.created_at).format("HH:mm:ss.SSS")}</time>}
                          </div>
                        </li>
                      );
                    })}
                  </ol>
                </ScrollArea>
              ) : <DetailEmpty icon={Activity} title="暂无证据事件" description="运行产生的操作记录与证据会沿时间线显示。" />}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="approvals">
          <Card>
            <CardHeader><CardTitle>审批记录</CardTitle><CardDescription>查看人工决议，以及仍需批准或拒绝的步骤。</CardDescription></CardHeader>
            <CardContent>
              {approvalsQuery.isError && (
                <Alert variant="destructive" className="mb-4">
                  <AlertCircle /><AlertTitle>无法加载审批记录</AlertTitle>
                  <AlertDescription>{(approvalsQuery.error as Error).message}</AlertDescription>
                </Alert>
              )}
              {approvalsQuery.isPending ? (
                <div role="status" aria-label="正在加载审批记录" className="flex flex-col gap-4 py-4">
                  <Skeleton className="h-6 w-1/3" /><Skeleton className="h-4 w-2/3" /><Skeleton className="h-4 w-1/2" />
                </div>
              ) : approvals.length ? (
                <ul className="flex flex-col">
                  {approvals.map((item, index) => (
                    <Fragment key={item.approval_id}>
                      {index > 0 && <li aria-hidden="true"><Separator className="my-5" /></li>}
                      <li className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                        <div className="flex min-w-0 flex-col gap-2.5">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="mono break-all">{item.case_id}</span>
                            <ApprovalStatusTag status={item.status} />
                            {item.risk_level && <RiskTag level={item.risk_level} />}
                          </div>
                          {item.reason && <p className="text-sm leading-6 text-muted-foreground">{item.reason}</p>}
                          <p className="text-xs text-muted-foreground">
                            {item.resolved_by ? `操作人 ${item.resolved_by} · ` : ""}
                            {item.created_at ? dayjs(item.created_at).format("YYYY-MM-DD HH:mm:ss") : ""}
                          </p>
                        </div>
                        {item.status === "pending" && (
                          <div className="flex shrink-0 gap-2">
                            <Button size="sm" onClick={() => setApprovalModal({ approval: item, approved: true })} disabled={resolveMutation.isPending}><Check data-icon="inline-start" />批准</Button>
                            <Button size="sm" variant="destructive" onClick={() => setApprovalModal({ approval: item, approved: false })} disabled={resolveMutation.isPending}>拒绝</Button>
                          </div>
                        )}
                      </li>
                    </Fragment>
                  ))}
                </ul>
              ) : <DetailEmpty icon={ShieldCheck} title={approvalsQuery.isError ? "审批数据暂不可用" : "暂无审批记录"} description={approvalsQuery.isError ? "请检查连接，审批记录会自动重新加载。" : "需要人工决议时，待审批步骤会显示在这里。"} />}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="replay">
          <Card>
            <CardHeader><CardTitle>Replay 差异</CardTitle><CardDescription>对照源运行与复测运行，检查问题的修复与变化。</CardDescription></CardHeader>
            <CardContent>
              {report.replay ? (
                <div className="flex flex-col gap-6">
                  <dl className="grid gap-4 sm:grid-cols-2">
                    <div className="flex min-w-0 flex-col gap-2"><dt className="text-xs text-muted-foreground">源 Run</dt><dd className="mono break-all">{report.replay.source_run_id}</dd></div>
                    <div className="flex min-w-0 flex-col gap-2"><dt className="text-xs text-muted-foreground">复测 Run</dt><dd className="mono break-all">{report.replay.replay_run_id}</dd></div>
                  </dl>
                  <Separator />
                  {(["fixed", "new", "persistent", "regressed"] as const).map((kind, index) => (
                    <Fragment key={kind}>
                      {index > 0 && <Separator />}
                      <div className="flex flex-col gap-3 sm:flex-row sm:gap-8">
                        <div className="flex w-32 shrink-0 items-start gap-2"><ReplayKindTag kind={kind} /><Badge variant="secondary">{report.replay!.diff[kind].length}</Badge></div>
                        {report.replay!.diff[kind].length ? (
                          <ul className="flex min-w-0 flex-1 flex-col gap-2">{report.replay!.diff[kind].map((caseId) => <li key={caseId} className="mono break-all">{caseId}</li>)}</ul>
                        ) : <span className="text-sm text-muted-foreground">无</span>}
                      </div>
                    </Fragment>
                  ))}
                </div>
              ) : <DetailEmpty icon={GitCompareArrows} title="暂无 Replay 差异" description="该 Run 不是 Replay 结果，或尚未生成差异。" />}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      <Dialog open={approvalModal !== null} onOpenChange={(open) => { if (!open) setApprovalModal(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{approvalModal?.approved ? "批准高风险步骤" : "拒绝高风险步骤"}</DialogTitle>
            <DialogDescription>Case <span className="mono break-all">{approvalModal?.approval.case_id}</span>；决议会写入审批事实并触发恢复。</DialogDescription>
          </DialogHeader>
          <form className="flex flex-col gap-6" onSubmit={(event) => {
            event.preventDefault();
            if (!approvalModal || !operator.trim() || !approvalReason.trim() || resolveMutation.isPending) return;
            resolveMutation.mutate({ approvalId: approvalModal.approval.approval_id, approved: approvalModal.approved });
          }}>
            <FieldGroup>
              <Field data-disabled={resolveMutation.isPending}>
                <FieldLabel htmlFor="approval-operator">操作人</FieldLabel>
                <Input id="approval-operator" required value={operator} onChange={(event) => setOperator(event.target.value)} disabled={resolveMutation.isPending} placeholder="填写操作人" />
              </Field>
              <Field data-disabled={resolveMutation.isPending}>
                <FieldLabel htmlFor="approval-reason">审批理由</FieldLabel>
                <Textarea id="approval-reason" required rows={3} value={approvalReason} onChange={(event) => setApprovalReason(event.target.value)} disabled={resolveMutation.isPending} placeholder="说明批准或拒绝的原因" aria-describedby="approval-reason-help" />
                <FieldDescription id="approval-reason-help">操作人与审批理由均为必填。</FieldDescription>
              </Field>
            </FieldGroup>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setApprovalModal(null)}>取消</Button>
              <Button type="submit" variant={approvalModal?.approved === false ? "destructive" : "default"} disabled={!operator.trim() || !approvalReason.trim() || resolveMutation.isPending}>
                {resolveMutation.isPending && <Spinner data-icon="inline-start" />}提交决议
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

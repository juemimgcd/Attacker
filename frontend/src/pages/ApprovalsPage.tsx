import { Link } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";
import { ArrowRight, CircleAlert, RefreshCw, ShieldCheck } from "lucide-react";
import { listApprovals, listJobs } from "@/api/client";
import PageHeader from "@/components/PageHeader";
import { ApprovalStatusTag, RiskTag } from "@/components/StatusTags";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";

/**
 * 后端没有全局"待审批"列表接口；审批中心通过进行中的 Job -> Run -> approvals
 * 聚合所有 pending 审批，数量受最近 200 条 Job 限制。
 */
export default function ApprovalsPage() {
  const jobsQuery = useQueries({
    queries: [
      {
        queryKey: ["jobs", "approval-scan"],
        queryFn: () => listJobs(undefined, 200),
        refetchInterval: 5_000,
      },
    ],
  })[0];

  const runIds = [...new Set((jobsQuery.data ?? [])
    .map((job) => job.run_id)
    .filter((id): id is string => Boolean(id)))];

  const approvalQueries = useQueries({
    queries: runIds.map((runId) => ({
      queryKey: ["run", runId, "approvals"],
      queryFn: () => listApprovals(runId),
      refetchInterval: 5_000,
    })),
  });

  const pending = approvalQueries
    .flatMap((query, index) =>
      (query.data ?? [])
        .filter((item) => item.status === "pending")
        .map((item) => ({ ...item, run_id: runIds[index] })),
    )
    .sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));

  const scanning = jobsQuery.isLoading || approvalQueries.some((query) => query.isLoading);
  const scanError = jobsQuery.error ?? approvalQueries.find((query) => query.isError)?.error;
  const refreshing = jobsQuery.isFetching || approvalQueries.some((query) => query.isFetching);

  return (
    <>
      <PageHeader
        eyebrow="Approvals"
        title="审批中心"
        desc="审查等待人工决议的高风险步骤。批准后恢复执行，并重新检查授权策略。"
        extra={
          <Button variant="outline" disabled={refreshing} onClick={() => { jobsQuery.refetch(); approvalQueries.forEach((query) => query.refetch()); }}>
            {refreshing ? <Spinner data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}
            刷新审批
          </Button>
        }
      />
      {scanError && (
        <Alert variant="destructive" className="mb-4">
          <CircleAlert />
          <AlertTitle>审批扫描未完成</AlertTitle>
          <AlertDescription>{(scanError as Error).message}</AlertDescription>
        </Alert>
      )}
      {scanning ? (
        <Card role="status" aria-label="正在扫描待审批步骤">
          <CardHeader>
            <CardTitle>正在加载审批</CardTitle>
            <CardDescription>检查最近任务中等待人工决议的步骤。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 pb-4" aria-hidden="true">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </CardContent>
        </Card>
      ) : pending.length === 0 ? (
        <Empty className="min-h-80 border">
          <EmptyHeader>
            <EmptyMedia variant="icon">{scanError ? <CircleAlert /> : <ShieldCheck />}</EmptyMedia>
            <EmptyTitle>{scanError ? "暂时无法确认审批状态" : "当前没有待处理的审批"}</EmptyTitle>
            <EmptyDescription>
              {scanError ? "请检查连接后刷新，未能加载的审批不会计入结果。" : "最近 200 条任务中未发现待审批步骤。需要人工决议时，会在这里显示。"}
            </EmptyDescription>
          </EmptyHeader>
          <EmptyContent>
            <Button variant="outline" asChild>
              <Link to="/jobs">查看任务队列 <ArrowRight data-icon="inline-end" /></Link>
            </Button>
          </EmptyContent>
        </Empty>
      ) : (
        <section aria-labelledby="pending-approvals-title" className="flex flex-col gap-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <h2 id="pending-approvals-title" className="text-base font-medium">待处理</h2>
              <Badge variant="secondary">{pending.length}</Badge>
            </div>
            <p className="text-xs text-muted-foreground">按等待时间排序 · 最近 200 条任务</p>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            {pending.map((item) => (
              <Card key={item.approval_id}>
                <CardHeader className="gap-3">
                  <div className="flex flex-wrap items-center gap-1.5">
                    {item.risk_level && <RiskTag level={item.risk_level} />}
                    <ApprovalStatusTag status={item.status} />
                  </div>
                  <CardTitle className="break-all">{item.case_id}</CardTitle>
                  <CardDescription>Run <span className="mono">{item.run_id?.slice(0, 8)}</span></CardDescription>
                </CardHeader>
                <CardContent className="flex-1">
                  <p className="text-sm leading-6 text-muted-foreground">{item.reason || "此步骤需要人工决议，请在 Run 详情中查看上下文。"}</p>
                </CardContent>
                <CardFooter>
                  <Button size="sm" asChild>
                    <Link to={`/runs/${item.run_id}`}>前往 Run 详情处理 <ArrowRight data-icon="inline-end" /></Link>
                  </Button>
                </CardFooter>
              </Card>
            ))}
          </div>
        </section>
      )}
    </>
  );
}

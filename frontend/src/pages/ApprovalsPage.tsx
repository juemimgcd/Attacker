import { Alert, Card, Empty, Spin, Typography } from "antd";
import { Link } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";
import { listApprovals, listJobs } from "@/api/client";
import PageHeader from "@/components/PageHeader";

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

  const runIds = (jobsQuery.data ?? [])
    .map((job) => job.run_id)
    .filter((id): id is string => Boolean(id));

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

  if (jobsQuery.isLoading) {
    return <Spin style={{ display: "block", margin: "80px auto" }} />;
  }

  if (jobsQuery.isError) {
    return (
      <Alert
        type="error"
        showIcon
        message="无法扫描任务队列"
        description={(jobsQuery.error as Error).message}
      />
    );
  }

  return (
    <>
      <PageHeader
        eyebrow="Approvals"
        title="审批中心"
        desc="聚合最近任务中等待人工决议的高风险步骤。批准后 Run 恢复执行，执行前重新经过 Policy Gate。"
      />

      {pending.length === 0 ? (
        <Card>
          <Empty description="当前没有待处理的审批" />
        </Card>
      ) : (
        <Card
          className="panel"
          title={`待处理（${pending.length}）`}
        >
          {pending.map((item) => (
            <Card.Grid key={item.approval_id} style={{ width: "50%", padding: 16 }}>
              <Typography.Text strong className="mono" style={{ fontSize: 12 }}>
                {item.case_id}
              </Typography.Text>
              <div style={{ marginTop: 6, fontSize: 12, color: "#94a3b8" }}>
                Run <span className="mono">{item.run_id?.slice(0, 8)}</span>
                {item.reason ? ` · ${item.reason}` : ""}
              </div>
              <Link to={`/runs/${item.run_id}`}>
                <Typography.Link style={{ fontSize: 12 }}>
                  前往 Run 详情处理 →
                </Typography.Link>
              </Link>
            </Card.Grid>
          ))}
        </Card>
      )}
    </>
  );
}

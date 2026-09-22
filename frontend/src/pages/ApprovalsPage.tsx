import { Alert, Button, Card, Col, Row, Spin } from "antd";
import { Link } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";
import { ArrowRightOutlined, CheckOutlined, ClockCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { listApprovals, listJobs } from "@/api/client";
import PageHeader from "@/components/PageHeader";
import { ApprovalStatusTag, RiskTag } from "@/components/StatusTags";

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
        extra={<Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => { jobsQuery.refetch(); approvalQueries.forEach((query) => query.refetch()); }}>刷新审批</Button>}
      />
      {scanError && <Alert className="query-alert" type="error" showIcon title="审批扫描未完成" description={(scanError as Error).message} />}
      {scanning ? <Card className="panel"><Spin style={{ display: "block", margin: "64px auto" }} /></Card> : pending.length === 0 ? (
        <Card className="panel"><div className="empty-state">
          <div className="empty-state-icon"><CheckOutlined /></div>
          <h3>{scanError ? "暂时无法确认审批状态" : "当前没有待处理的审批"}</h3>
          <p>{scanError ? "请检查连接后刷新，未能加载的审批不会计入结果。" : "最近 200 条任务中未发现待审批步骤。需要人工决议时，会在这里显示。"}</p>
          <Link to="/jobs" className="text-link">查看任务队列 <ArrowRightOutlined /></Link>
        </div></Card>
      ) : (
        <Card className="panel" title={`待处理（${pending.length}）`}>
          <Row gutter={[14, 14]}>
            {pending.map((item) => (
              <Col key={item.approval_id} xs={24} md={12}>
                <div className="approval-card" style={{ padding: 16 }}>
                  <div className="approval-card-head">
                    <span className="case-id">{item.case_id}</span>
                    <span style={{ display: "inline-flex", gap: 6 }}>
                      {item.risk_level && <RiskTag level={item.risk_level} />}
                      <ApprovalStatusTag status={item.status} />
                    </span>
                  </div>
                  <div className="approval-meta">
                    <ClockCircleOutlined style={{ marginRight: 6, color: "var(--warn)" }} />
                    Run <span className="mono">{item.run_id?.slice(0, 8)}</span>
                    {item.reason ? ` · ${item.reason}` : ""}
                  </div>
                  <Link to={`/runs/${item.run_id}`} className="approval-link">
                    前往 Run 详情处理 <ArrowRightOutlined />
                  </Link>
                </div>
              </Col>
            ))}
          </Row>
        </Card>
      )}
    </>
  );
}

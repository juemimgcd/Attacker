import { Alert, Card, Col, Row, Table } from "antd";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import dayjs from "dayjs";
import { listJobs } from "@/api/client";
import { JobStatusTag } from "@/components/StatusTags";
import PageHeader from "@/components/PageHeader";
import StatCard from "@/components/StatCard";
import { jobKindLabel } from "@/lib/labels";
import type { RunJob } from "@/types/api";
import type { ColumnsType } from "antd/es/table";

const ACTIVE_STATUSES = new Set(["queued", "leased", "running", "retry_wait"]);

export default function DashboardPage() {
  const jobsQuery = useQuery({
    queryKey: ["jobs", "dashboard"],
    queryFn: () => listJobs(undefined, 100),
    refetchInterval: 5_000,
  });

  const jobs = jobsQuery.data ?? [];
  const active = jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
  const failed = jobs.filter((job) => job.status === "failed");
  const succeeded = jobs.filter((job) => job.status === "succeeded");
  const recent = [...jobs]
    .sort((a, b) => dayjs(b.updated_at).valueOf() - dayjs(a.updated_at).valueOf())
    .slice(0, 10);

  const columns: ColumnsType<RunJob> = [
    {
      title: "请求 ID",
      dataIndex: "request_id",
      render: (value: string, record) =>
        record.run_id ? (
          <Link to={`/runs/${record.run_id}`} className="mono">
            {value}
          </Link>
        ) : (
          <span className="mono">{value}</span>
        ),
    },
    {
      title: "类型",
      dataIndex: "kind",
      width: 140,
      render: (value: string) => jobKindLabel[value] ?? value,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 120,
      render: (value: RunJob["status"]) => <JobStatusTag status={value} />,
    },
    {
      title: "尝试",
      width: 90,
      render: (_, record) => `${record.attempts}/${record.max_attempts}`,
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 170,
      render: (value: string) => dayjs(value).format("MM-DD HH:mm:ss"),
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Overview"
        title="评测总览"
        desc="持久任务队列的执行状态与健康度。所有数据仅来自 SQL 事实源。"
      />

      {jobsQuery.isError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="无法加载任务队列"
          description={
            (jobsQuery.error as Error).message +
            "（若后端启用了 durable queue，请确认 API Key 正确）"
          }
        />
      )}

      <Row gutter={[14, 14]}>
        <Col xs={12} md={6}>
          <StatCard
            label="进行中任务"
            value={active.length}
            accent="var(--gold-dim)"
            valueColor="var(--gold)"
          />
        </Col>
        <Col xs={12} md={6}>
          <StatCard label="最近 100 条任务" value={jobs.length} accent="var(--slate)" />
        </Col>
        <Col xs={12} md={6}>
          <StatCard
            label="失败任务"
            value={failed.length}
            accent="var(--red)"
            valueColor={failed.length > 0 ? "var(--red)" : undefined}
          />
        </Col>
        <Col xs={12} md={6}>
          <StatCard
            label="队列完成率"
            value={jobs.length ? Math.round((succeeded.length / jobs.length) * 100) : 0}
            suffix="%"
            accent="var(--ok)"
            valueColor="var(--ok)"
          />
        </Col>
      </Row>

      <Card
        className="panel"
        title="最近任务"
        style={{ marginTop: 16 }}
        extra={<Link to="/jobs">查看全部</Link>}
      >
        <Table
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={recent}
          loading={jobsQuery.isLoading}
          pagination={false}
          locale={{ emptyText: "暂无任务，去「新建评测」提交第一个 Run" }}
        />
      </Card>
    </>
  );
}

import { Alert, Button, Card, Table } from "antd";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRightOutlined, AuditOutlined, DeploymentUnitOutlined, PlusOutlined, ReloadOutlined, UnorderedListOutlined } from "@ant-design/icons";
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
  const navigate = useNavigate();
  const jobsQuery = useQuery({
    queryKey: ["jobs", "dashboard"], queryFn: () => listJobs(undefined, 100), refetchInterval: 5_000,
  });
  const jobs = jobsQuery.data ?? [];
  const active = jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
  const failed = jobs.filter((job) => job.status === "failed");
  const succeeded = jobs.filter((job) => job.status === "succeeded");
  const recent = [...jobs].sort((a, b) => dayjs(b.updated_at).valueOf() - dayjs(a.updated_at).valueOf()).slice(0, 10);
  const unavailable = jobsQuery.isError || !jobsQuery.data;
  const value = (count: number) => unavailable ? "—" : count;

  const columns: ColumnsType<RunJob> = [
    { title: "评测任务", dataIndex: "request_id", ellipsis: true,
      render: (id: string, record) => <div className="task-identity">
        <span className={`task-mark ${record.status === "failed" ? "task-mark-error" : ""}`}><UnorderedListOutlined /></span>
        <div className="task-name">{record.run_id ? <Link to={`/runs/${record.run_id}`} className="mono" title={id}>{id}</Link> : <span className="mono" title={id}>{id}</span>}
          <span className="task-subtitle">{record.run_id ? `Run ${record.run_id.slice(0, 8)}` : "等待创建 Run"}</span>
        </div>
      </div>,
    },
    { title: "类型", dataIndex: "kind", width: 135, render: (kind: string) => jobKindLabel[kind] ?? kind },
    { title: "状态", dataIndex: "status", width: 115, render: (status: RunJob["status"]) => <JobStatusTag status={status} /> },
    { title: "尝试", width: 80, render: (_, record) => <span className="mono muted-text">{record.attempts}<span className="attempt-divider">/</span>{record.max_attempts}</span> },
    { title: "更新时间", dataIndex: "updated_at", width: 145, render: (date: string) => <span className="table-time">{dayjs(date).format("MM-DD HH:mm:ss")}</span> },
  ];

  return (
    <>
      <PageHeader eyebrow="Overview" title="评测总览" desc="从一次受控评测开始，让 Agent 的安全边界清晰可见。"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => navigate("/runs/new")}>新建评测</Button>} />
      <div className="overview-meta"><span>任务运行概况</span><span className="overview-window">最近 100 条任务 <span>·</span> 每 5 秒刷新</span></div>
      {jobsQuery.isError && <Alert className="query-alert" type="error" showIcon title="无法加载任务队列" description={(jobsQuery.error as Error).message}
        action={<Button size="small" onClick={() => jobsQuery.refetch()} loading={jobsQuery.isFetching}>重试</Button>} />}
      <div className="stats-grid">
        <StatCard label="进行中" value={value(active.length)} accent="var(--gold)" hint="排队、执行与等待重试" loading={jobsQuery.isLoading} />
        <StatCard label="已完成" value={value(succeeded.length)} accent="var(--ok)" hint="成功完成的队列任务" loading={jobsQuery.isLoading} />
        <StatCard label="失败任务" value={value(failed.length)} accent="var(--danger)" hint="可前往任务队列查看" loading={jobsQuery.isLoading} valueColor={!unavailable && failed.length > 0 ? "var(--danger)" : undefined} />
        <StatCard label="队列完成率" value={unavailable || !jobs.length ? "—" : Math.round(succeeded.length / jobs.length * 100)}
          suffix={!unavailable && jobs.length > 0 ? "%" : undefined} accent="var(--text-2)" loading={jobsQuery.isLoading}
          hint={unavailable ? "等待任务数据" : `已完成 / 最近任务 · ${succeeded.length} / ${jobs.length}`} />
      </div>
      <Card className="panel recent-panel" title={<div className="section-heading">最近任务 <span className="count-badge">{unavailable ? "—" : recent.length}</span></div>}
        extra={<div className="section-actions"><Button type="text" size="small" icon={<ReloadOutlined />} aria-label="刷新最近任务" title="刷新最近任务" loading={jobsQuery.isFetching} onClick={() => jobsQuery.refetch()} /><Link to="/jobs" className="text-link">全部任务 <ArrowRightOutlined /></Link></div>}>
        <Table rowKey="id" size="middle" columns={columns} dataSource={recent} loading={jobsQuery.isLoading} pagination={false} scroll={{ x: 740 }}
          locale={{ emptyText: <div className="empty-state">
            <div className="empty-state-icon"><UnorderedListOutlined /></div>
            <h3>{jobsQuery.isError ? "任务数据暂不可用" : "你的第一轮评测，从这里开始"}</h3>
            <p>{jobsQuery.isError ? "检查后端连接或 API Key，恢复连接后任务会自动刷新。" : "选择评测模式并配置目标，执行状态与结果将在这里汇总。"}</p>
            {!jobsQuery.isError && <Button icon={<PlusOutlined />} onClick={() => navigate("/runs/new")}>创建第一个评测</Button>}
          </div> }} />
        <div className="panel-footnote"><span>按最近更新时间排序</span><span>{jobsQuery.isError ? "数据连接异常" : jobsQuery.isLoading ? "正在同步任务" : `本次载入 ${jobs.length} 条任务`}</span></div>
      </Card>
      <section className="workspace-guide" aria-labelledby="guide-title">
        <div className="guide-intro"><div className="eyebrow">Evaluation workflow</div><h2 id="guide-title">每一步，都有据可查。</h2><p>从目标配置到证据回溯，<br />在明确的授权与预算内完成评测。</p></div>
        <div className="guide-links">
          <Link to="/equipment" className="guide-link"><span className="guide-number">01</span><DeploymentUnitOutlined /><span><strong>准备评测装备</strong><small>查看 Provider、Case Pack 与 Benchmark</small></span><ArrowRightOutlined className="guide-arrow" /></Link>
          <Link to="/runs/new" className="guide-link"><span className="guide-number">02</span><PlusOutlined /><span><strong>配置并发起评测</strong><small>选择模式、目标、数据集与运行预算</small></span><ArrowRightOutlined className="guide-arrow" /></Link>
          <Link to="/approvals" className="guide-link"><span className="guide-number">03</span><AuditOutlined /><span><strong>审查高风险步骤</strong><small>处理等待人工决议的执行请求</small></span><ArrowRightOutlined className="guide-arrow" /></Link>
        </div>
      </section>
    </>
  );
}

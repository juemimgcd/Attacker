import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  Boxes,
  CheckCheck,
  CircleAlert,
  ListTodo,
  Plus,
  RefreshCw,
  ShieldCheck,
  Target,
  Workflow,
} from "lucide-react";
import dayjs from "dayjs";
import { listJobs } from "@/api/client";
import { JobStatusTag } from "@/components/StatusTags";
import PageHeader from "@/components/PageHeader";
import StatCard from "@/components/StatCard";
import { jobKindLabel } from "@/lib/labels";
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Separator } from "@/components/ui/separator";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemMedia,
  ItemTitle,
} from "@/components/ui/item";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const ACTIVE_STATUSES = new Set(["queued", "leased", "running", "retry_wait"]);
const workflow = [
  {
    to: "/equipment",
    icon: Boxes,
    title: "准备评测装备",
    desc: "浏览 Provider、用例包与 Benchmark",
  },
  {
    to: "/runs/new",
    icon: Target,
    title: "配置并发起评测",
    desc: "选择评测模式、目标与运行预算",
  },
  {
    to: "/approvals",
    icon: ShieldCheck,
    title: "审查高风险步骤",
    desc: "处理需要人工决议的执行请求",
  },
];

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
    .sort(
      (a, b) => dayjs(b.updated_at).valueOf() - dayjs(a.updated_at).valueOf(),
    )
    .slice(0, 10);
  const unavailable = jobsQuery.isError || !jobsQuery.data;
  const value = (count: number) => (unavailable ? "—" : count);

  return (
    <>
      <PageHeader
        eyebrow="Overview"
        title="评测总览"
        desc="查看评测运行状态，跟进任务与安全证据。"
        extra={
          <Button asChild>
            <Link to="/runs/new">
              <Plus data-icon="inline-start" />
              新建评测
            </Link>
          </Button>
        }
      />
      {jobsQuery.isError && (
        <Alert variant="destructive" className="mb-6">
          <CircleAlert />
          <AlertTitle>无法加载任务队列</AlertTitle>
          <AlertDescription>{jobsQuery.error.message}</AlertDescription>
          <AlertAction>
            <Button
              variant="outline"
              size="sm"
              disabled={jobsQuery.isFetching}
              onClick={() => jobsQuery.refetch()}
            >
              重试
            </Button>
          </AlertAction>
        </Alert>
      )}
      <section aria-label="任务运行概况">
        <div className="overview-meta">
          <h2>运行概况</h2>
          <span>
            <span className="overview-window">最近 100 条任务</span>
            <span className="meta-dot">·</span>每 5 秒更新
          </span>
        </div>
        <div className="stats-grid">
          <StatCard
            label="进行中"
            value={value(active.length)}
            accent="var(--foreground)"
            hint="排队、执行与等待重试"
            loading={jobsQuery.isLoading}
            icon={<Activity />}
          />
          <StatCard
            label="已完成"
            value={value(succeeded.length)}
            accent="var(--ok)"
            hint="成功完成的队列任务"
            loading={jobsQuery.isLoading}
            icon={<CheckCheck />}
          />
          <StatCard
            label="失败任务"
            value={value(failed.length)}
            accent="var(--danger)"
            hint="可前往任务队列查看"
            loading={jobsQuery.isLoading}
            icon={<CircleAlert />}
            valueColor={
              !unavailable && failed.length > 0 ? "var(--danger)" : undefined
            }
          />
          <StatCard
            label="队列完成率"
            value={
              unavailable || !jobs.length
                ? "—"
                : Math.round((succeeded.length / jobs.length) * 100)
            }
            suffix={!unavailable && jobs.length > 0 ? "%" : undefined}
            accent="var(--foreground)"
            loading={jobsQuery.isLoading}
            icon={<Workflow />}
            hint={
              unavailable
                ? "等待任务数据"
                : `已完成 / 最近任务 · ${succeeded.length} / ${jobs.length}`
            }
          />
        </div>
      </section>
      <Card className="gap-0">
        <CardHeader className="pb-5">
          <CardTitle className="flex items-center gap-2">
            最近任务
            <Badge variant="secondary">
              {unavailable ? "—" : recent.length}
            </Badge>
          </CardTitle>
          <CardDescription>最近更新的评测任务与执行状态</CardDescription>
          <CardAction>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="icon-sm"
                disabled={jobsQuery.isFetching}
                aria-label="刷新最近任务"
                title="刷新最近任务"
                onClick={() => jobsQuery.refetch()}
              >
                {jobsQuery.isFetching ? (
                  <Spinner data-icon="inline-start" />
                ) : (
                  <RefreshCw data-icon="inline-start" />
                )}
              </Button>
              <Button variant="outline" size="sm" asChild>
                <Link to="/jobs">
                  全部任务
                  <ArrowUpRight data-icon="inline-end" />
                </Link>
              </Button>
            </div>
          </CardAction>
        </CardHeader>
        <Separator />
        <CardContent className="p-0">
          {jobsQuery.isLoading ? (
            <div
              className="flex flex-col gap-5 p-6"
              role="status"
              aria-label="正在加载任务"
            >
              {[0, 1, 2].map((row) => (
                <div key={row} className="flex gap-6">
                  <Skeleton className="h-9 flex-1" />
                  <Skeleton className="h-9 w-20" />
                  <Skeleton className="h-9 w-24" />
                </div>
              ))}
            </div>
          ) : recent.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-6">评测任务</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>尝试</TableHead>
                  <TableHead className="pr-6">更新时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {recent.map((job) => (
                  <TableRow key={job.id}>
                    <TableCell className="py-4 pl-6">
                      <div className="task-identity">
                        <span className="task-mark">
                          <ListTodo />
                        </span>
                        <div className="task-name">
                          {job.run_id ? (
                            <Link
                              to={`/runs/${job.run_id}`}
                              className="mono"
                              title={job.request_id}
                            >
                              {job.request_id}
                            </Link>
                          ) : (
                            <span className="mono">{job.request_id}</span>
                          )}
                          <span className="task-subtitle">
                            {job.run_id
                              ? `Run ${job.run_id.slice(0, 8)}`
                              : "等待创建 Run"}
                          </span>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell>{jobKindLabel[job.kind] ?? job.kind}</TableCell>
                    <TableCell>
                      <JobStatusTag status={job.status} />
                    </TableCell>
                    <TableCell>
                      <span className="mono muted-text">
                        {job.attempts} / {job.max_attempts}
                      </span>
                    </TableCell>
                    <TableCell className="pr-6">
                      <span className="table-time">
                        {dayjs(job.updated_at).format("MM-DD HH:mm:ss")}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Empty className="min-h-72 py-12">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <ListTodo />
                </EmptyMedia>
                <EmptyTitle>
                  {jobsQuery.isError
                    ? "任务数据暂不可用"
                    : "开始你的第一轮评测"}
                </EmptyTitle>
                <EmptyDescription>
                  {jobsQuery.isError
                    ? "检查后端连接或 API Key，恢复连接后任务会自动刷新。"
                    : "配置目标与用例，任务进度和结果会在这里汇总。"}
                </EmptyDescription>
              </EmptyHeader>
              {!jobsQuery.isError && (
                <EmptyContent>
                  <Button variant="outline" asChild>
                    <Link to="/runs/new">
                      <Plus data-icon="inline-start" />
                      创建评测
                    </Link>
                  </Button>
                </EmptyContent>
              )}
            </Empty>
          )}
        </CardContent>
        <CardFooter className="justify-between gap-3">
          <span className="table-note">按最近更新时间排序</span>
          <span className="table-note">
            {jobsQuery.isError
              ? "数据连接异常"
              : jobsQuery.isLoading
                ? "正在同步任务"
                : `已载入 ${jobs.length} 条任务`}
          </span>
        </CardFooter>
      </Card>
      <section className="workspace-guide" aria-labelledby="guide-title">
        <div className="guide-intro">
          <span className="eyebrow">Getting started</span>
          <h2 id="guide-title">评测工作流</h2>
          <p>
            从准备装备到审查结果，
            <br />
            让每次评测都有据可查。
          </p>
        </div>
        <ItemGroup className="min-w-0">
          {workflow.map(({ to, icon: Icon, title, desc }) => (
            <Item key={to} asChild>
              <Link to={to}>
                <ItemMedia variant="icon"><Icon /></ItemMedia>
                <ItemContent className="min-w-0">
                  <ItemTitle>{title}</ItemTitle>
                  <ItemDescription>{desc}</ItemDescription>
                </ItemContent>
                <ItemActions><ArrowRight aria-hidden="true" /></ItemActions>
              </Link>
            </Item>
          ))}
        </ItemGroup>
      </section>
    </>
  );
}

import { toast } from "sonner";
import { useRef, useState } from "react";
import { Table } from "antd";
import { ArrowUpRight, CircleAlert, Copy, ListTodo, MoreHorizontal, Plus, RefreshCw, RotateCcw, Square } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { cancelJob, listJobs, retryJob } from "@/api/client";
import { JobStatusTag } from "@/components/StatusTags";
import PageHeader from "@/components/PageHeader";
import JsonViewer from "@/components/JsonViewer";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { jobKindLabel } from "@/lib/labels";
import type { JobStatus, RunJob } from "@/types/api";
import type { ColumnsType } from "antd/es/table";

const statusOptions = [
  { label: "全部", value: "all" },
  { label: "执行中", value: "running" },
  { label: "排队中", value: "queued" },
  { label: "已完成", value: "succeeded" },
  { label: "失败", value: "failed" },
];

export default function JobsPage() {
  const navigate = useNavigate();
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [cancelTarget, setCancelTarget] = useState<RunJob | null>(null);
  const cancelTriggerId = useRef<string | null>(null);
  const queryClient = useQueryClient();
  const jobsQuery = useQuery({
    queryKey: ["jobs", statusFilter],
    queryFn: () =>
      listJobs(statusFilter === "all" ? undefined : (statusFilter as JobStatus), 200),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((j) =>
        ["queued", "leased", "running", "retry_wait"].includes(j.status),
      )
        ? 3_000
        : 15_000,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["jobs"] });

  const cancelMutation = useMutation({
    mutationFn: cancelJob,
    onSuccess: () => {
      toast.success("已请求取消");
      setCancelTarget(null);
      invalidate();
    },
    onError: (error) => toast.error((error as Error).message),
  });

  const retryMutation = useMutation({
    mutationFn: retryJob,
    onSuccess: () => {
      toast.success("已重新排队");
      invalidate();
    },
    onError: (error) => toast.error((error as Error).message),
  });

  const columns: ColumnsType<RunJob> = [
    {
      title: "请求 ID",
      dataIndex: "request_id",
      render: (value: string, record) => (
        <div className="flex flex-col gap-1">
          {record.run_id ? (
            <Link to={`/runs/${record.run_id}`} className="mono">
              {value}
            </Link>
          ) : (
            <span className="mono">{value}</span>
          )}
          <span className="mono text-xs text-muted-foreground">
            job: {record.id.slice(0, 8)}
          </span>
        </div>
      ),
    },
    {
      title: "类型",
      dataIndex: "kind",
      width: 130,
      render: (value: string) => jobKindLabel[value] ?? value,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value: RunJob["status"]) => <JobStatusTag status={value} />,
    },
    {
      title: "尝试",
      width: 80,
      render: (_, record) => `${record.attempts}/${record.max_attempts}`,
    },
    {
      title: "错误",
      dataIndex: "error_summary",
      ellipsis: true,
      render: (value: string | null) =>
        value ? (
          <span className="text-xs text-destructive">
            {value}
          </span>
        ) : (
          "—"
        ),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 160,
      render: (value: string) => dayjs(value).format("MM-DD HH:mm:ss"),
    },
    {
      title: "操作",
      width: 120,
      render: (_, record) => (
        <div className="flex items-center justify-end gap-1">
          {record.result && <JsonViewer data={record.result} title="执行结果" />}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button id={`job-actions-${record.id}`} size="icon-sm" variant="ghost" aria-label={`任务 ${record.request_id} 的操作`}>
                <MoreHorizontal data-icon="inline-start" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-44">
              <DropdownMenuGroup>
                <DropdownMenuLabel>任务操作</DropdownMenuLabel>
                {record.run_id && (
                  <DropdownMenuItem asChild>
                    <Link to={`/runs/${record.run_id}`}><ArrowUpRight />查看评测详情</Link>
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onSelect={async () => {
                  try {
                    await navigator.clipboard.writeText(record.id);
                    toast.success("任务 ID 已复制");
                  } catch {
                    toast.error("复制失败，请稍后重试。");
                  }
                }}>
                  <Copy />复制任务 ID
                </DropdownMenuItem>
                {record.status === "failed" && (
                  <DropdownMenuItem disabled={retryMutation.isPending} onSelect={() => retryMutation.mutate(record.id)}>
                    {retryMutation.isPending && retryMutation.variables === record.id ? <Spinner /> : <RotateCcw />}
                    重新排队
                  </DropdownMenuItem>
                )}
              </DropdownMenuGroup>
              {["queued", "leased", "running", "retry_wait"].includes(record.status) && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuGroup>
                    <DropdownMenuItem variant="destructive" disabled={cancelMutation.isPending} onSelect={() => {
                      cancelTriggerId.current = `job-actions-${record.id}`;
                      setCancelTarget(record);
                    }}>
                      <Square />取消任务
                    </DropdownMenuItem>
                  </DropdownMenuGroup>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Job Queue"
        title="任务队列"
        desc="跟踪每一次评测的执行进度，查看结果或处理失败任务。"
        extra={
          <Button onClick={() => navigate("/runs/new")}>
            <Plus data-icon="inline-start" />
            新建评测
          </Button>
        }
      />
      <div className="jobs-toolbar">
        <div className="min-w-0 max-w-full overflow-x-auto">
          <ToggleGroup
            type="single"
            variant="outline"
            spacing={0}
            value={statusFilter}
            onValueChange={(value) => { if (value) setStatusFilter(value); }}
            aria-label="按任务状态筛选"
          >
            {statusOptions.map((option) => (
              <ToggleGroupItem key={option.value} value={option.value} aria-label={option.label}>
                {option.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
        <Button variant="outline" onClick={() => jobsQuery.refetch()} disabled={jobsQuery.isFetching}>
          {jobsQuery.isFetching ? <Spinner data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}
          刷新
        </Button>
      </div>
      {jobsQuery.isError && (
        <Alert variant="destructive" className="mb-4">
          <CircleAlert />
          <AlertTitle>无法加载任务队列</AlertTitle>
          <AlertDescription>{(jobsQuery.error as Error).message}</AlertDescription>
        </Alert>
      )}

      <Table
        className="table-panel"
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={jobsQuery.data ?? []}
        loading={jobsQuery.isLoading}
        scroll={{ x: 980 }}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{
          emptyText: (
            <Empty className="py-12">
              <EmptyHeader>
                <EmptyMedia variant="icon"><ListTodo /></EmptyMedia>
                <EmptyTitle>{jobsQuery.isError ? "任务数据暂不可用" : "没有匹配的任务"}</EmptyTitle>
                <EmptyDescription>
                  {jobsQuery.isError ? "检查后端连接或 API Key 后重试。" : "尝试切换状态筛选，或创建新的评测任务。"}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ),
        }}
      />
      <AlertDialog open={!!cancelTarget} onOpenChange={(open) => {
        if (!open && !cancelMutation.isPending) setCancelTarget(null);
      }}>
        <AlertDialogContent onCloseAutoFocus={(event) => {
          const trigger = cancelTriggerId.current && document.getElementById(cancelTriggerId.current);
          if (trigger) {
            event.preventDefault();
            trigger.focus();
          }
        }}>
          <AlertDialogHeader>
            <AlertDialogTitle>取消这个任务？</AlertDialogTitle>
            <AlertDialogDescription>
              将请求停止任务的后续执行，已产生的结果仍会保留。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="mono break-all text-muted-foreground">{cancelTarget?.request_id}</p>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={cancelMutation.isPending}>返回</AlertDialogCancel>
            <AlertDialogAction variant="destructive" disabled={cancelMutation.isPending} onClick={(event) => {
              event.preventDefault();
              if (cancelTarget) cancelMutation.mutate(cancelTarget.id);
            }}>
              {cancelMutation.isPending && <Spinner data-icon="inline-start" />}
              确认取消
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

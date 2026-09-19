import { useState } from "react";
import { App, Button, Popconfirm, Segmented, Space, Table, Typography } from "antd";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { cancelJob, listJobs, retryJob } from "@/api/client";
import { JobStatusTag } from "@/components/StatusTags";
import PageHeader from "@/components/PageHeader";
import JsonViewer from "@/components/JsonViewer";
import { jobKindLabel } from "@/lib/labels";
import type { JobStatus, RunJob } from "@/types/api";
import type { ColumnsType } from "antd/es/table";

const statusOptions = [
  { label: "全部", value: "all" },
  { label: "进行中", value: "running" },
  { label: "排队中", value: "queued" },
  { label: "已完成", value: "succeeded" },
  { label: "失败", value: "failed" },
];

export default function JobsPage() {
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const { message } = App.useApp();
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
      message.success("已请求取消");
      invalidate();
    },
    onError: (error) => message.error((error as Error).message),
  });

  const retryMutation = useMutation({
    mutationFn: retryJob,
    onSuccess: () => {
      message.success("已重新排队");
      invalidate();
    },
    onError: (error) => message.error((error as Error).message),
  });

  const columns: ColumnsType<RunJob> = [
    {
      title: "请求 ID",
      dataIndex: "request_id",
      render: (value: string, record) => (
        <Space direction="vertical" size={0}>
          {record.run_id ? (
            <Link to={`/runs/${record.run_id}`} className="mono">
              {value}
            </Link>
          ) : (
            <span className="mono">{value}</span>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 11 }} className="mono">
            job: {record.id.slice(0, 8)}
          </Typography.Text>
        </Space>
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
          <Typography.Text type="danger" style={{ fontSize: 12 }}>
            {value}
          </Typography.Text>
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
      width: 190,
      render: (_, record) => (
        <Space size={4}>
          {["queued", "leased", "running", "retry_wait"].includes(record.status) && (
            <Popconfirm
              title="确认取消该任务？"
              onConfirm={() => cancelMutation.mutate(record.id)}
            >
              <Button size="small" danger type="link">
                取消
              </Button>
            </Popconfirm>
          )}
          {record.status === "failed" && (
            <Button size="small" type="link" onClick={() => retryMutation.mutate(record.id)}>
              重试
            </Button>
          )}
          {record.result && <JsonViewer data={record.result} title="执行结果" />}
        </Space>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Job Queue"
        title="任务队列"
        desc="持久 Run Job 的排队、租约与重试状态。"
        extra={
          <Space>
            <Segmented
              options={statusOptions}
              value={statusFilter}
              onChange={(value) => setStatusFilter(value as string)}
            />
            <Button onClick={() => jobsQuery.refetch()} loading={jobsQuery.isFetching}>
              刷新
            </Button>
          </Space>
        }
      />

      <Table
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={jobsQuery.data ?? []}
        loading={jobsQuery.isLoading}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{ emptyText: "没有匹配的任务" }}
      />
    </>
  );
}

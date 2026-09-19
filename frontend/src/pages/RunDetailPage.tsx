import { useState } from "react";
import {
  App,
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Input,
  List,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Space,
  Table,
  Tabs,
  Tag,
  Timeline,
  Typography,
} from "antd";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import {
  controlAdaptiveRun,
  getRunMarkdownUrl,
  getRunReport,
  listApprovals,
  resolveApproval,
} from "@/api/client";
import {
  ApprovalStatusTag,
  OutcomeTag,
  ReplayKindTag,
  RiskTag,
  RunStatusTag,
} from "@/components/StatusTags";
import JsonViewer from "@/components/JsonViewer";
import PageHeader from "@/components/PageHeader";
import StatCard from "@/components/StatCard";
import { runModeLabel } from "@/lib/labels";
import type { Approval, Finding, StepRow } from "@/types/api";
import type { ColumnsType } from "antd/es/table";

const ACTIVE_RUN_STATUSES = new Set(["running", "waiting_approval", "paused"]);

export default function RunDetailPage() {
  const { runId = "" } = useParams();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [controlReason, setControlReason] = useState("");
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
      message.success("控制指令已提交");
      setControlReason("");
      invalidate();
    },
    onError: (error) => message.error((error as Error).message),
  });

  const resolveMutation = useMutation({
    mutationFn: ({ approvalId, approved }: { approvalId: string; approved: boolean }) =>
      resolveApproval(runId, approvalId, {
        approved,
        resolved_by: operator,
        reason: approvalReason,
      }),
    onSuccess: () => {
      message.success("审批已记录，Run 将按策略继续");
      setApprovalModal(null);
      setApprovalReason("");
      invalidate();
    },
    onError: (error) => message.error((error as Error).message),
  });

  if (reportQuery.isError) {
    return (
      <Alert
        type="error"
        showIcon
        message="无法加载 Run"
        description={(reportQuery.error as Error).message}
        action={
          <Link to="/jobs">
            <Button size="small">返回任务列表</Button>
          </Link>
        }
      />
    );
  }

  const report = reportQuery.data;
  const run = report?.run;
  const summary = report?.summary;
  const pendingApprovals = (approvalsQuery.data ?? []).filter(
    (item) => item.status === "pending",
  );
  const isActive = ACTIVE_RUN_STATUSES.has(run?.status ?? "");

  const findingColumns: ColumnsType<Finding> = [
    {
      title: "Case",
      dataIndex: "case_id",
      width: 220,
      render: (value: string) => <span className="mono">{value}</span>,
    },
    {
      title: "严重度",
      dataIndex: "risk_level",
      width: 90,
      render: (value: string) => <RiskTag level={value} />,
    },
    {
      title: "结论",
      dataIndex: "outcome",
      width: 110,
      render: (value: string) => (value ? <OutcomeTag outcome={value} /> : "—"),
    },
    {
      title: "类别 / 理由",
      ellipsis: true,
      render: (_, record) =>
        record.category ? (
          <span>
            <Tag>{record.category}</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {record.reason}
            </Typography.Text>
          </span>
        ) : (
          <span className="mono">{record.fingerprint}</span>
        ),
    },
    {
      title: "证据",
      width: 130,
      render: (_, record) => (
        <Tag color={record.evidence_event_ids.length ? "blue" : "default"}>
          {record.evidence_event_ids.length} 条 Evidence
        </Tag>
      ),
    },
  ];

  const stepColumns: ColumnsType<StepRow> = [
    {
      title: "Case",
      dataIndex: "case_id",
      render: (value: string) => <span className="mono">{value}</span>,
    },
    {
      title: "结果",
      dataIndex: "outcome",
      width: 120,
      render: (value: string) => <OutcomeTag outcome={value} />,
    },
    {
      title: "详情",
      width: 90,
      render: (_, record) =>
        record.result ? <JsonViewer data={record.result} title="Step 结果" /> : "—",
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Run Detail"
        title="运行详情"
        extra={
          <Space>
            <Button href={getRunMarkdownUrl(runId)} target="_blank">
              Markdown 报告
            </Button>
            {isActive && (
              <Popconfirm
                title="请求取消该 Run"
                description={
                  <Input
                    style={{ marginTop: 8 }}
                    placeholder="取消原因（必填）"
                    value={controlReason}
                    onChange={(event) => setControlReason(event.target.value)}
                  />
                }
                okButtonProps={{ disabled: !controlReason.trim() }}
                onConfirm={() =>
                  controlMutation.mutate({ action: "cancel", reason: controlReason.trim() })
                }
              >
                <Button danger>请求取消</Button>
              </Popconfirm>
            )}
          </Space>
        }
      />
      <Space wrap style={{ marginBottom: 18 }}>
        {run && <RunStatusTag status={run.status} />}
        {run && <Tag>{runModeLabel[run.mode] ?? run.mode}</Tag>}
        {run?.started_at && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            开始 {dayjs(run.started_at).format("MM-DD HH:mm:ss")}
          </Typography.Text>
        )}
        {run?.completed_at && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            完成 {dayjs(run.completed_at).format("MM-DD HH:mm:ss")}
          </Typography.Text>
        )}
        <Typography.Text className="mono" copyable={{ text: runId }} style={{ fontSize: 11 }}>
          {runId}
        </Typography.Text>
      </Space>

      {pendingApprovals.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ margin: "16px 0" }}
          message={`${pendingApprovals.length} 个高风险步骤等待审批`}
          description="批准后 Run 会恢复执行，执行前会重新经过 Policy Gate。"
        />
      )}

      <Row gutter={[14, 14]} style={{ marginTop: 4 }}>
        <Col xs={12} md={6}>
          <StatCard
            label="CASE 进度"
            value={`${summary?.completed_cases ?? 0}/${summary?.total_cases ?? 0}`}
            accent="var(--slate)"
          />
        </Col>
        <Col xs={12} md={6}>
          <StatCard
            label="VIOLATION 违规"
            value={summary?.outcomes?.violation ?? 0}
            accent="var(--red)"
            valueColor={
              (summary?.outcomes?.violation ?? 0) > 0 ? "var(--red)" : undefined
            }
          />
        </Col>
        <Col xs={12} md={6}>
          <StatCard
            label="TARGET 调用"
            value={summary?.target_calls ?? 0}
            accent="var(--gold-dim)"
            valueColor="var(--gold)"
          />
        </Col>
        <Col xs={12} md={6}>
          <StatCard
            label="EVIDENCE 关联率"
            value={
              summary?.finding_evidence_link_rate == null
                ? "—"
                : `${Math.round(summary.finding_evidence_link_rate * 100)}%`
            }
            accent="var(--ok)"
            valueColor="var(--ok)"
          />
        </Col>
      </Row>
      <Card className="panel" bodyStyle={{ padding: "14px 20px" }} style={{ marginTop: 14 }}>
        <Progress
          percent={
            summary?.total_cases
              ? Math.round((summary.completed_cases / summary.total_cases) * 100)
              : 0
          }
          size="small"
          strokeColor="var(--red)"
          trailColor="var(--surface-3)"
        />
      </Card>

      <Card style={{ marginTop: 16 }} loading={reportQuery.isLoading}>
        <Tabs
          items={[
            {
              key: "overview",
              label: "概览",
              children: (
                <Descriptions column={{ xs: 1, md: 2 }} size="small" bordered>
                  <Descriptions.Item label="模式">
                    {run && (runModeLabel[run.mode] ?? run.mode)}
                  </Descriptions.Item>
                  <Descriptions.Item label="状态">
                    {run && <RunStatusTag status={run.status} />}
                  </Descriptions.Item>
                  <Descriptions.Item label="数据集">
                    <span className="mono">
                      {report?.dataset
                        ? `${report.dataset.name} v${report.dataset.version}`
                        : "—"}
                    </span>
                  </Descriptions.Item>
                  <Descriptions.Item label="Target">
                    {report?.target?.name ?? "—"}
                  </Descriptions.Item>
                  <Descriptions.Item label="开始时间">
                    {run?.started_at
                      ? dayjs(run.started_at).format("YYYY-MM-DD HH:mm:ss")
                      : "—"}
                  </Descriptions.Item>
                  <Descriptions.Item label="完成时间">
                    {run?.completed_at
                      ? dayjs(run.completed_at).format("YYYY-MM-DD HH:mm:ss")
                      : "—"}
                  </Descriptions.Item>
                  <Descriptions.Item label="误报 / 过拦">
                    {summary?.false_positives ?? 0} / {summary?.defense_overblocks ?? 0}
                  </Descriptions.Item>
                  <Descriptions.Item label="Planner 调用 / Token">
                    {summary?.planner_calls ?? 0} / {summary?.planner_tokens ?? 0}
                  </Descriptions.Item>
                </Descriptions>
              ),
            },
            {
              key: "findings",
              label: `Finding (${report?.findings.length ?? 0})`,
              children: (
                <Table
                  rowKey="id"
                  size="small"
                  columns={findingColumns}
                  dataSource={report?.findings ?? []}
                  pagination={{ pageSize: 15, showSizeChanger: false }}
                  locale={{ emptyText: "无 Finding" }}
                />
              ),
            },
            {
              key: "steps",
              label: `Case 结果 (${report?.steps.length ?? 0})`,
              children: (
                <Table
                  rowKey="id"
                  size="small"
                  columns={stepColumns}
                  dataSource={report?.steps ?? []}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                />
              ),
            },
            {
              key: "evidence",
              label: `证据时间线 (${report?.events.length ?? 0})`,
              children: (
                <Timeline
                  style={{ marginTop: 8, maxHeight: 560, overflow: "auto", paddingRight: 8 }}
                  items={(report?.events ?? []).map((event) => ({
                    color:
                      event.event_type.includes("violation") ||
                      event.event_type.includes("denied") ||
                      event.event_type.includes("failed")
                        ? "red"
                        : event.event_type.includes("approval")
                          ? "orange"
                          : "blue",
                    children: (
                      <Space direction="vertical" size={2}>
                        <Space wrap>
                          <Typography.Text strong style={{ fontSize: 12 }}>
                            {event.event_type}
                          </Typography.Text>
                          {event.operation_id && (
                            <span
                              className="mono ellipsis"
                              style={{ color: "#64748b", fontSize: 11, maxWidth: 260 }}
                            >
                              {event.operation_id}
                            </span>
                          )}
                          <JsonViewer data={event.evidence} title={event.event_type} />
                        </Space>
                        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                          {event.created_at
                            ? dayjs(event.created_at).format("HH:mm:ss.SSS")
                            : ""}
                        </Typography.Text>
                      </Space>
                    ),
                  }))}
                />
              ),
            },
            {
              key: "approvals",
              label: `审批 (${(approvalsQuery.data ?? []).length})`,
              children: renderApprovals(approvalsQuery.data ?? []),
            },
            {
              key: "replay",
              label: "Replay 差异",
              children: report?.replay ? (
                <Space direction="vertical" style={{ width: "100%" }}>
                  <Typography.Text type="secondary">
                    源 Run <span className="mono">{report.replay.source_run_id}</span> vs 复测 Run{" "}
                    <span className="mono">{report.replay.replay_run_id}</span>
                  </Typography.Text>
                  {(["fixed", "new", "persistent", "regressed"] as const).map((kind) => (
                    <Card key={kind} size="small">
                      <Space direction="vertical" size={6}>
                        <ReplayKindTag kind={kind} />
                        {report.replay!.diff[kind].length ? (
                          report.replay!.diff[kind].map((caseId) => (
                            <div key={caseId} className="mono" style={{ fontSize: 12 }}>
                              {caseId}
                            </div>
                          ))
                        ) : (
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            无
                          </Typography.Text>
                        )}
                      </Space>
                    </Card>
                  ))}
                </Space>
              ) : (
                <Empty description="该 Run 不是 Replay 结果，或尚未生成差异" />
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={approvalModal?.approved ? "批准高风险步骤" : "拒绝高风险步骤"}
        open={approvalModal !== null}
        onCancel={() => setApprovalModal(null)}
        onOk={() =>
          approvalModal &&
          resolveMutation.mutate({
            approvalId: approvalModal.approval.approval_id,
            approved: approvalModal.approved,
          })
        }
        okButtonProps={{
          disabled: !operator.trim() || !approvalReason.trim(),
          danger: approvalModal?.approved === false,
          loading: resolveMutation.isPending,
        }}
        okText="提交"
        cancelText="取消"
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            Case <span className="mono">{approvalModal?.approval.case_id}</span>
            ；决议会写入审批事实并触发恢复。
          </Typography.Text>
          <Input
            placeholder="操作人（必填）"
            value={operator}
            onChange={(event) => setOperator(event.target.value)}
          />
          <Input.TextArea
            rows={3}
            placeholder="审批理由（必填）"
            value={approvalReason}
            onChange={(event) => setApprovalReason(event.target.value)}
          />
        </Space>
      </Modal>
    </>
  );

  function renderApprovals(items: Approval[]) {
    return (
      <List
        dataSource={items}
        locale={{ emptyText: <Empty description="无审批记录" /> }}
        renderItem={(item) => (
          <List.Item
            actions={
              item.status === "pending"
                ? [
                    <Button
                      key="approve"
                      type="primary"
                      size="small"
                      onClick={() => setApprovalModal({ approval: item, approved: true })}
                    >
                      批准
                    </Button>,
                    <Button
                      key="reject"
                      danger
                      size="small"
                      onClick={() => setApprovalModal({ approval: item, approved: false })}
                    >
                      拒绝
                    </Button>,
                  ]
                : undefined
            }
          >
            <List.Item.Meta
              title={
                <Space>
                  <span className="mono">{item.case_id}</span>
                  <ApprovalStatusTag status={item.status} />
                  {item.risk_level && <RiskTag level={item.risk_level} />}
                </Space>
              }
              description={
                <Space direction="vertical" size={2}>
                  {item.reason && <span>理由：{item.reason}</span>}
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {item.resolved_by ? `操作人 ${item.resolved_by} · ` : ""}
                    {item.created_at ? dayjs(item.created_at).format("YYYY-MM-DD HH:mm:ss") : ""}
                  </Typography.Text>
                </Space>
              }
            />
          </List.Item>
        )}
      />
    );
  }
}

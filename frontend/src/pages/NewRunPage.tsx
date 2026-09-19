import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Form,
  Input,
  InputNumber,
  Radio,
  Row,
  Select,
  Space,
  Switch,
} from "antd";
import {
  ApartmentOutlined,
  CheckOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  NodeIndexOutlined,
} from "@ant-design/icons";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import PageHeader from "@/components/PageHeader";
import {
  createDeterministicGrayBoxRun,
  createDeterministicRun,
  createGrayBoxRun,
  createStatefulRun,
  enqueueJob,
} from "@/api/client";
import type {
  AttackPolicy,
  DeterministicRunRequest,
  GrayBoxRunRequest,
  PlannerConfig,
  StatefulRunRequest,
  TargetConfig,
} from "@/types/api";

type RunMode = "deterministic" | "deterministic_graybox" | "adaptive" | "stateful";

const modeMeta: Record<RunMode, { title: string; desc: string; icon: React.ReactNode }> = {
  deterministic: {
    title: "确定性黑盒",
    desc: "标准 Request/Response，固定数据集与 Evaluator，适合安全回归基线。",
    icon: <CloudServerOutlined />,
  },
  deterministic_graybox: {
    title: "确定性灰盒",
    desc: "固定 Case 顺序 + Tool/Policy/Approval Trace，验证工具越权与审批绕过。",
    icon: <NodeIndexOutlined />,
  },
  adaptive: {
    title: "自适应灰盒",
    desc: "LangGraph 编排下一步，所有候选仍受 allowlist、审批与硬预算约束。",
    icon: <ApartmentOutlined />,
  },
  stateful: {
    title: "带状态基线",
    desc: "Memory/RAG 污染、身份隔离与 checkpoint 恢复，使用内置隔离适配器。",
    icon: <DatabaseOutlined />,
  },
};

const datasetDefaults: Record<RunMode, string> = {
  deterministic: "samples/blackbox/phase1.yaml",
  deterministic_graybox: "samples/graybox/phase2.yaml",
  adaptive: "samples/graybox/phase2.yaml",
  stateful: "samples/stateful/phase3.yaml",
};

interface FormValues {
  // target
  target_name: string;
  endpoint: string;
  timeout_seconds: number;
  allow_public_target: boolean;
  auth_type: "none" | "bearer";
  auth_token?: string;
  // dataset
  dataset_path: string;
  case_ids?: string;
  // budget / policy
  max_cases: number;
  budget_target_calls: number;
  budget_duration: number;
  max_steps: number;
  policy_target_calls: number;
  approval_risk_levels: string[];
  // planner
  planner_backend: "deterministic" | "openai_compatible";
  planner_endpoint?: string;
  planner_api_key?: string;
  planner_model: string;
  // stateful
  profile: "vulnerable" | "hardened" | "regressed";
  stateful_target_name: string;
  // submit
  submit_as: "sync" | "job";
  request_id?: string;
}

function buildTarget(values: FormValues): TargetConfig {
  return {
    name: values.target_name,
    endpoint: values.endpoint,
    method: "POST",
    headers: {},
    auth: {
      type: values.auth_type,
      token: values.auth_type === "bearer" ? (values.auth_token ?? null) : null,
      header_name: "Authorization",
      token_prefix: "Bearer",
    },
    timeout_seconds: values.timeout_seconds,
    allow_public_target: values.allow_public_target,
    refusal_status_codes: [403],
    request_template: {
      body_template: { messages: [{ role: "user", content: "{prompt}" }] },
    },
  };
}

function buildPlanner(values: FormValues): PlannerConfig {
  return {
    backend: values.planner_backend,
    endpoint:
      values.planner_backend === "openai_compatible"
        ? (values.planner_endpoint ?? null)
        : null,
    api_key:
      values.planner_backend === "openai_compatible"
        ? (values.planner_api_key ?? null)
        : null,
    provider_id: "core.openai_compatible",
    model: values.planner_model,
    timeout_seconds: 30,
    temperature: 0,
    max_physical_attempts: 1,
    prompt_template_version: "1.0.0",
  };
}

function parseCaseIds(raw?: string): string[] | null {
  const ids = (raw ?? "")
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  return ids.length ? ids : null;
}

export default function NewRunPage() {
  const [mode, setMode] = useState<RunMode>("deterministic");
  const [form] = Form.useForm<FormValues>();
  const { message } = App.useApp();
  const navigate = useNavigate();

  const submitMutation = useMutation({
    mutationFn: async (values: FormValues) => {
      const caseIds = parseCaseIds(values.case_ids);
      if (mode === "stateful") {
        const payload: StatefulRunRequest = {
          profile: values.profile,
          dataset_path: values.dataset_path,
          case_ids: caseIds,
          target_name: values.stateful_target_name,
        };
        return values.submit_as === "job"
          ? enqueueJob({
              request_id: values.request_id!,
              kind: "stateful",
              payload: payload as unknown as Record<string, unknown>,
            })
          : createStatefulRun(payload);
      }

      const target = buildTarget(values);
      if (mode === "deterministic") {
        const payload: DeterministicRunRequest = {
          target,
          dataset_path: values.dataset_path,
          case_ids: caseIds,
          budget: {
            max_cases: values.max_cases,
            max_target_calls: values.budget_target_calls,
            max_duration_seconds: values.budget_duration,
            max_response_bytes: 1_048_576,
          },
        };
        return values.submit_as === "job"
          ? enqueueJob({
              request_id: values.request_id!,
              kind: "deterministic",
              payload: payload as unknown as Record<string, unknown>,
            })
          : createDeterministicRun(payload);
      }

      const policy: AttackPolicy = {
        max_steps: values.max_steps,
        max_target_calls: values.policy_target_calls,
        max_duration_seconds: values.budget_duration,
        allowed_risk_levels: ["low", "medium", "high", "critical"],
        approval_risk_levels: values.approval_risk_levels as AttackPolicy["approval_risk_levels"],
        loop_detection_threshold: 3,
      };
      const payload: GrayBoxRunRequest = {
        target,
        dataset_path: values.dataset_path,
        case_ids: caseIds,
        policy,
        planner: buildPlanner(values),
        test_principal_refs: ["default-test-principal"],
      };
      const kind = mode === "adaptive" ? "adaptive" : "deterministic_graybox";
      if (values.submit_as === "job") {
        return enqueueJob({
          request_id: values.request_id!,
          kind,
          payload: payload as unknown as Record<string, unknown>,
        });
      }
      return mode === "adaptive"
        ? createGrayBoxRun(payload)
        : createDeterministicGrayBoxRun(payload);
    },
    onSuccess: (data) => {
      const runId =
        (data as { run?: { id?: string } }).run?.id ??
        (data as { run_id?: string }).run_id ??
        (data as { id?: string }).id;
      message.success("已提交");
      if (runId) {
        navigate(`/runs/${runId}`);
      } else {
        navigate("/jobs");
      }
    },
    onError: (error) => message.error((error as Error).message),
  });

  const isGraybox = mode === "adaptive" || mode === "deterministic_graybox";
  const needsTarget = mode !== "stateful";
  const submitAs = Form.useWatch("submit_as", form);
  const plannerBackend = Form.useWatch("planner_backend", form);

  return (
    <>
      <PageHeader
        eyebrow="New Evaluation"
        title="新建评测"
        desc="选择评测模式后配置数据集与 Target。所有候选动作都受 Policy Gate 约束。"
      />

      <Row gutter={[12, 12]} style={{ marginBottom: 18 }}>
        {(Object.keys(modeMeta) as RunMode[]).map((key) => (
          <Col xs={12} md={6} key={key}>
            <div
              className={`mode-card ${mode === key ? "mode-active" : ""}`}
              onClick={() => {
                setMode(key);
                form.setFieldValue("dataset_path", datasetDefaults[key]);
              }}
            >
              <div className="mode-check">
                <CheckOutlined />
              </div>
              <div className="mode-icon">{modeMeta[key].icon}</div>
              <div className="mode-name">{modeMeta[key].title}</div>
              <div className="mode-desc">{modeMeta[key].desc}</div>
            </div>
          </Col>
        ))}
      </Row>

      <Form
        form={form}
        layout="vertical"
        size="middle"
        initialValues={{
          target_name: "local-sandbox",
          endpoint: "http://127.0.0.1:9000/agent",
          timeout_seconds: 30,
          allow_public_target: false,
          auth_type: "none",
          dataset_path: datasetDefaults.deterministic,
          max_cases: 64,
          budget_target_calls: 96,
          budget_duration: 300,
          max_steps: 16,
          policy_target_calls: 32,
          approval_risk_levels: ["high", "critical"],
          planner_backend: "deterministic",
          planner_model: "planner",
          profile: "vulnerable",
          stateful_target_name: "isolated-stateful-sandbox",
          submit_as: "sync",
        }}
        onFinish={(values) => submitMutation.mutate(values)}
      >
        <Row gutter={16}>
          <Col xs={24} lg={needsTarget ? 12 : 24}>
            <Card className="panel" title="数据集" style={{ marginBottom: 16 }}>
              <Form.Item
                name="dataset_path"
                label="数据集路径"
                rules={[{ required: true, message: "请输入数据集路径" }]}
              >
                <Input className="mono" placeholder="samples/blackbox/phase1.yaml" />
              </Form.Item>
              <Form.Item
                name="case_ids"
                label="限定 Case（可选，逗号或换行分隔；留空运行全部）"
              >
                <Input.TextArea rows={2} className="mono" />
              </Form.Item>
              {mode === "stateful" && (
                <>
                  <Form.Item name="profile" label="行为 Profile">
                    <Radio.Group
                      options={[
                        { label: "Vulnerable（预期存在漏洞）", value: "vulnerable" },
                        { label: "Hardened（加固基线）", value: "hardened" },
                        { label: "Regressed（回归样本）", value: "regressed" },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item name="stateful_target_name" label="隔离适配器名称">
                    <Input className="mono" />
                  </Form.Item>
                </>
              )}
            </Card>

            {isGraybox && (
              <Card className="panel" title="策略与 Planner">
                <Row gutter={12}>
                  <Col span={12}>
                    <Form.Item name="max_steps" label="最大步数" rules={[{ required: true }]}>
                      <InputNumber min={1} max={200} style={{ width: "100%" }} />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name="policy_target_calls"
                      label="Target 调用预算"
                      rules={[{ required: true }]}
                    >
                      <InputNumber min={1} max={10000} style={{ width: "100%" }} />
                    </Form.Item>
                  </Col>
                </Row>
                <Form.Item name="approval_risk_levels" label="需要审批的风险等级">
                  <Checkbox.Group
                    options={[
                      { label: "低", value: "low" },
                      { label: "中", value: "medium" },
                      { label: "高", value: "high" },
                      { label: "严重", value: "critical" },
                    ]}
                  />
                </Form.Item>
                {mode === "adaptive" && (
                  <>
                    <Form.Item name="planner_backend" label="Planner 后端">
                      <Select
                        options={[
                          { label: "Deterministic（无模型依赖）", value: "deterministic" },
                          { label: "OpenAI 兼容接口", value: "openai_compatible" },
                        ]}
                      />
                    </Form.Item>
                    {plannerBackend === "openai_compatible" && (
                      <>
                        <Form.Item
                          name="planner_endpoint"
                          label="Planner Endpoint"
                          rules={[{ required: true, message: "openai_compatible 需要 endpoint" }]}
                        >
                          <Input className="mono" placeholder="https://..." />
                        </Form.Item>
                        <Form.Item name="planner_api_key" label="Planner API Key">
                          <Input.Password autoComplete="new-password" />
                        </Form.Item>
                        <Form.Item name="planner_model" label="模型">
                          <Input className="mono" />
                        </Form.Item>
                      </>
                    )}
                  </>
                )}
              </Card>
            )}
          </Col>

          {needsTarget && (
            <Col xs={24} lg={12}>
              <Card className="panel" title="Target 接入" style={{ marginBottom: 16 }}>
                <Form.Item
                  name="target_name"
                  label="名称"
                  rules={[{ required: true, message: "请输入 Target 名称" }]}
                >
                  <Input />
                </Form.Item>
                <Form.Item
                  name="endpoint"
                  label="Endpoint"
                  rules={[{ required: true, message: "请输入 Target endpoint" }]}
                  extra="默认仅允许本机、回环或私网地址。"
                >
                  <Input className="mono" placeholder="http://127.0.0.1:9000/agent" />
                </Form.Item>
                <Row gutter={12}>
                  <Col span={12}>
                    <Form.Item name="timeout_seconds" label="超时（秒）">
                      <InputNumber min={1} max={600} style={{ width: "100%" }} />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item name="allow_public_target" label="公网目标" valuePropName="checked">
                      <Switch checkedChildren="已授权" unCheckedChildren="禁止" />
                    </Form.Item>
                  </Col>
                </Row>
                <Form.Item name="auth_type" label="鉴权方式">
                  <Select
                    options={[
                      { label: "无", value: "none" },
                      { label: "Bearer Token", value: "bearer" },
                    ]}
                  />
                </Form.Item>
                <Form.Item noStyle shouldUpdate={(a, b) => a.auth_type !== b.auth_type}>
                  {({ getFieldValue }) =>
                    getFieldValue("auth_type") === "bearer" ? (
                      <Form.Item
                        name="auth_token"
                        label="Token"
                        rules={[{ required: true, message: "请输入 Token" }]}
                      >
                        <Input.Password autoComplete="new-password" />
                      </Form.Item>
                    ) : null
                  }
                </Form.Item>
              </Card>

              <Card className="panel" title="运行预算">
                <Row gutter={12}>
                  <Col span={8}>
                    <Form.Item name="max_cases" label="最大 Case 数">
                      <InputNumber min={1} max={1000} style={{ width: "100%" }} />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item name="budget_target_calls" label="最大 Target 调用">
                      <InputNumber min={1} max={10000} style={{ width: "100%" }} />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item name="budget_duration" label="时长上限（秒）">
                      <InputNumber min={1} max={86400} style={{ width: "100%" }} />
                    </Form.Item>
                  </Col>
                </Row>
              </Card>
            </Col>
          )}
        </Row>

        <Card className="panel">
          <Space direction="vertical" style={{ width: "100%" }}>
            <Form.Item name="submit_as" label="提交方式" style={{ marginBottom: 8 }}>
              <Radio.Group
                options={[
                  { label: "同步执行（等待结果）", value: "sync" },
                  { label: "提交持久队列", value: "job" },
                ]}
              />
            </Form.Item>
            {submitAs === "job" && (
              <>
                <Form.Item
                  name="request_id"
                  label="请求 ID（幂等键）"
                  rules={[
                    { required: true, message: "队列提交需要 request_id" },
                    { pattern: /^[A-Za-z0-9._:-]{8,200}$/, message: "8-200 位字母数字或 . _ : -" },
                  ]}
                  style={{ maxWidth: 420 }}
                >
                  <Input className="mono" placeholder="stateful-20260919-001" />
                </Form.Item>
                <Alert
                  type="info"
                  showIcon
                  message="持久 Job 不接受明文凭据"
                  description="若 Target 使用 Bearer Token，请改用 Provider Instance 引用，或选择同步执行。"
                />
              </>
            )}
            <Button
              type="primary"
              htmlType="submit"
              loading={submitMutation.isPending}
              size="large"
            >
              提交评测
            </Button>
          </Space>
        </Card>
      </Form>
    </>
  );
}

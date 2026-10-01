import { toast } from "sonner";
import { useState, type ReactNode } from "react";
import { Form, type FormItemProps } from "antd";
import { cn } from "cn";
import {
  ArrowRight,
  Check,
  Database,
  Eye,
  EyeOff,
  GitBranch,
  Info,
  Network,
  Server,
  type LucideIcon,
} from "lucide-react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import PageHeader from "@/components/PageHeader";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Field, FieldContent, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
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

const modeMeta: Record<RunMode, { title: string; desc: string; icon: LucideIcon }> = {
  deterministic: {
    title: "确定性黑盒",
    desc: "使用固定用例验证目标的输入输出，建立可重复的安全回归基线。",
    icon: Server,
  },
  deterministic_graybox: {
    title: "确定性灰盒",
    desc: "结合工具、策略与审批轨迹，检查工具越权和审批绕过。",
    icon: GitBranch,
  },
  adaptive: {
    title: "自适应灰盒",
    desc: "根据评测反馈选择下一步，在授权范围、审批与预算内探索。",
    icon: Network,
  },
  stateful: {
    title: "带状态基线",
    desc: "使用内置隔离环境，检查记忆污染、身份隔离与状态恢复。",
    icon: Database,
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

type FieldValue = string | number | boolean | string[] | null;

interface EvaluationFieldProps {
  name: keyof FormValues;
  label: string;
  description?: string;
  kind?: "text" | "number" | "password" | "textarea" | "select" | "switch" | "radio" | "checkbox";
  options?: { label: string; value: string }[];
  rules?: FormItemProps["rules"];
  placeholder?: string;
  min?: number;
  max?: number;
}

/** AntD keeps the field store and rules; shadcn receives plain controlled values. */
function EvaluationField({ name, rules, ...props }: EvaluationFieldProps) {
  return (
    <Form.Item name={name} label={props.label} rules={rules} noStyle getValueFromEvent={(value: FieldValue) => value}>
      <EvaluationControl
        {...props}
        id={`evaluation-${name}`}
        required={rules?.some((rule) => typeof rule === "object" && rule.required)}
      />
    </Form.Item>
  );
}

function EvaluationControl({
  id,
  label,
  description,
  kind = "text",
  options = [],
  placeholder,
  min,
  max,
  required,
  value,
  onChange,
}: Omit<EvaluationFieldProps, "name" | "rules"> & {
  id: string;
  required?: boolean;
  value?: FieldValue;
  onChange?: (value: FieldValue) => void;
}) {
  const { status, errors } = Form.Item.useStatus();
  const [passwordVisible, setPasswordVisible] = useState(false);
  const invalid = status === "error";
  const descriptionId = `${id}-description`;
  const errorId = `${id}-error`;
  const labelId = `${id}-label`;
  const describedBy = [description && descriptionId, invalid && errorId].filter(Boolean).join(" ") || undefined;
  const accessibility = { id, "aria-invalid": invalid, "aria-required": required, "aria-describedby": describedBy };
  const help = <>{description && <FieldDescription id={descriptionId}>{description}</FieldDescription>}
    {invalid && <FieldError id={errorId}>{errors.map((error, index) => <div key={index}>{error}</div>)}</FieldError>}</>;

  if (kind === "radio" || kind === "checkbox") {
    const selected = Array.isArray(value) ? value : [];
    return (
      <Field data-invalid={invalid}>
        <FieldSet aria-describedby={describedBy}>
          <FieldLegend variant="label" id={labelId}>{label}</FieldLegend>
          {kind === "radio" ? (
            <RadioGroup {...accessibility} aria-labelledby={labelId} value={typeof value === "string" ? value : ""} onValueChange={onChange}>
              {options.map((option) => (
                <Field key={option.value} orientation="horizontal" data-invalid={invalid}>
                  <RadioGroupItem id={`${id}-${option.value}`} value={option.value} aria-invalid={invalid} aria-describedby={describedBy} />
                  <FieldLabel htmlFor={`${id}-${option.value}`}>{option.label}</FieldLabel>
                </Field>
              ))}
            </RadioGroup>
          ) : (
            <FieldGroup id={id} data-slot="checkbox-group" className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-labelledby={labelId}>
              {options.map((option) => (
                <Field key={option.value} orientation="horizontal" data-invalid={invalid}>
                  <Checkbox id={`${id}-${option.value}`} checked={selected.includes(option.value)} aria-invalid={invalid} aria-describedby={describedBy}
                    onCheckedChange={(checked) => onChange?.(options.filter((item) => item.value === option.value ? checked === true : selected.includes(item.value)).map((item) => item.value))} />
                  <FieldLabel htmlFor={`${id}-${option.value}`}>{option.label}</FieldLabel>
                </Field>
              ))}
            </FieldGroup>
          )}
          {help}
        </FieldSet>
      </Field>
    );
  }

  if (kind === "switch") {
    return (
      <Field orientation="horizontal" data-invalid={invalid}>
        <Switch {...accessibility} checked={value === true} onCheckedChange={onChange} />
        <FieldContent>
          <FieldLabel htmlFor={id}>{label}</FieldLabel>
          <FieldDescription>{value ? "已授权" : "禁止"}</FieldDescription>
          {help}
        </FieldContent>
      </Field>
    );
  }

  let control: ReactNode;
  const textValue = typeof value === "string" ? value : "";
  if (kind === "select") {
    control = (
      <Select value={textValue} onValueChange={onChange}>
        <SelectTrigger {...accessibility} className="w-full"><SelectValue placeholder={placeholder} /></SelectTrigger>
        <SelectContent position="popper"><SelectGroup>
          {options.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}
        </SelectGroup></SelectContent>
      </Select>
    );
  } else if (kind === "textarea") {
    control = <Textarea {...accessibility} rows={2} value={textValue} placeholder={placeholder} onChange={(event) => onChange?.(event.target.value)} />;
  } else if (kind === "password") {
    control = <InputGroup>
      <InputGroupInput {...accessibility} type={passwordVisible ? "text" : "password"} value={textValue} autoComplete="new-password" onChange={(event) => onChange?.(event.target.value)} />
      <InputGroupAddon align="inline-end"><InputGroupButton size="icon-xs" aria-label={`${passwordVisible ? "隐藏" : "显示"}${label}`} aria-pressed={passwordVisible} onClick={() => setPasswordVisible((visible) => !visible)}>
        {passwordVisible ? <EyeOff /> : <Eye />}
      </InputGroupButton></InputGroupAddon>
    </InputGroup>;
  } else if (kind === "number") {
    control = <Input {...accessibility} type="number" inputMode="decimal" min={min} max={max} step={1} value={typeof value === "number" ? value : ""}
      onChange={(event) => onChange?.(Number.isNaN(event.target.valueAsNumber) ? null : event.target.valueAsNumber)}
      onBlur={(event) => {
        const number = event.target.valueAsNumber;
        if (!Number.isNaN(number)) onChange?.(Math.min(max ?? Infinity, Math.max(min ?? -Infinity, number)));
      }} />;
  } else {
    control = <Input {...accessibility} value={textValue} placeholder={placeholder} onChange={(event) => onChange?.(event.target.value)} />;
  }

  return (
    <Field data-invalid={invalid}>
      <FieldLabel htmlFor={id}>{label}{required && <span aria-hidden="true">*</span>}</FieldLabel>
      {control}
      {help}
    </Field>
  );
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
      toast.success("已提交");
      if (runId) {
        navigate(`/runs/${runId}`);
      } else {
        navigate("/jobs");
      }
    },
    onError: (error) => toast.error((error as Error).message),
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
        desc="选择评测模式，配置目标、用例与预算，开始一次可追溯的安全评测。"
      />

      <div className="form-section-label"><span>01</span> 选择评测模式</div>
      <ToggleGroup
        type="single"
        variant="mode"
        spacing={3}
        value={mode}
        onValueChange={(value) => {
          if (!value) return;
          const nextMode = value as RunMode;
          setMode(nextMode);
          form.setFieldValue("dataset_path", datasetDefaults[nextMode]);
        }}
        aria-label="评测模式"
        className="mode-selector grid w-full grid-cols-2 gap-3 lg:grid-cols-4"
      >
        {(Object.keys(modeMeta) as RunMode[]).map((key, index) => {
          const Icon = modeMeta[key].icon;
          return (
            <ToggleGroupItem
              key={key}
              value={key}
              aria-label={modeMeta[key].title}
            >
              <span className="mode-check" aria-hidden="true">
                <Check />
              </span>
              <span className="mode-icon" aria-hidden="true"><Icon /></span>
              <span className="mode-name">{modeMeta[key].title}</span>
              <span className="mode-desc">{modeMeta[key].desc}</span>
              <span className="mode-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
            </ToggleGroupItem>
          );
        })}
      </ToggleGroup>

      <div className="form-section-label"><span>02</span> 配置评测参数</div>
      <Form
        component={false}
        form={form}
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
        <form className="evaluation-form" noValidate onSubmit={(event) => { event.preventDefault(); event.stopPropagation(); form.submit(); }}>
        <div className={cn("grid items-start gap-5", needsTarget && "xl:grid-cols-2")}>
          <FieldGroup>
            <Card>
              <CardHeader>
                <CardTitle>数据集</CardTitle>
                <CardDescription>选择用例来源，按需限定本次评测范围。</CardDescription>
              </CardHeader>
              <CardContent>
                <FieldGroup>
                  <EvaluationField name="dataset_path" label="数据集路径" placeholder="samples/blackbox/phase1.yaml"
                    rules={[{ required: true, message: "请输入数据集路径" }]} />
                  <EvaluationField name="case_ids" label="限定 Case" kind="textarea" description="可选，使用逗号或换行分隔；留空运行全部用例。" />
                  {mode === "stateful" && (
                    <>
                      <EvaluationField name="profile" label="行为 Profile" kind="radio" options={[
                        { label: "Vulnerable（预期存在漏洞）", value: "vulnerable" },
                        { label: "Hardened（加固基线）", value: "hardened" },
                        { label: "Regressed（回归样本）", value: "regressed" },
                      ]} />
                      <EvaluationField name="stateful_target_name" label="隔离适配器名称" />
                    </>
                  )}
                </FieldGroup>
              </CardContent>
            </Card>

            {needsTarget && (
              <Card>
                <CardHeader>
                  <CardTitle>运行预算</CardTitle>
                  <CardDescription>设置执行规模与最长运行时间。</CardDescription>
                </CardHeader>
                <CardContent>
                  <FieldGroup className="grid gap-4 sm:grid-cols-3">
                    <EvaluationField name="max_cases" label="最大 Case 数" kind="number" min={1} max={1000} />
                    <EvaluationField name="budget_target_calls" label="最大 Target 调用" kind="number" min={1} max={10000} />
                    <EvaluationField name="budget_duration" label="时长上限（秒）" kind="number" min={1} max={86400} />
                  </FieldGroup>
                </CardContent>
              </Card>
            )}

            {isGraybox && (
              <Card>
                <CardHeader>
                  <CardTitle>策略与 Planner</CardTitle>
                  <CardDescription>限定执行步数、调用预算与人工审批范围。</CardDescription>
                </CardHeader>
                <CardContent>
                  <FieldGroup>
                    <FieldGroup className="grid gap-4 sm:grid-cols-2">
                      <EvaluationField name="max_steps" label="最大步数" kind="number" min={1} max={200} rules={[{ required: true }]} />
                      <EvaluationField name="policy_target_calls" label="Target 调用预算" kind="number" min={1} max={10000} rules={[{ required: true }]} />
                    </FieldGroup>
                    <EvaluationField name="approval_risk_levels" label="需要审批的风险等级" kind="checkbox" options={[
                      { label: "低", value: "low" },
                      { label: "中", value: "medium" },
                      { label: "高", value: "high" },
                      { label: "严重", value: "critical" },
                    ]} />
                    {mode === "adaptive" && (
                      <>
                        <EvaluationField name="planner_backend" label="Planner 后端" kind="select" options={[
                          { label: "Deterministic（无模型依赖）", value: "deterministic" },
                          { label: "OpenAI 兼容接口", value: "openai_compatible" },
                        ]} />
                        {plannerBackend === "openai_compatible" && (
                          <>
                            <EvaluationField name="planner_endpoint" label="Planner Endpoint" placeholder="https://..."
                              rules={[{ required: true, message: "openai_compatible 需要 endpoint" }]} />
                            <EvaluationField name="planner_api_key" label="Planner API Key" kind="password" />
                            <EvaluationField name="planner_model" label="模型" />
                          </>
                        )}
                      </>
                    )}
                  </FieldGroup>
                </CardContent>
              </Card>
            )}
          </FieldGroup>

          {needsTarget && (
            <Card>
              <CardHeader>
                <CardTitle>Target 接入</CardTitle>
                <CardDescription>配置待评测 Agent 的连接与鉴权信息。</CardDescription>
              </CardHeader>
              <CardContent>
                <FieldGroup>
                  <EvaluationField name="target_name" label="名称" rules={[{ required: true, message: "请输入 Target 名称" }]} />
                  <EvaluationField name="endpoint" label="Endpoint" placeholder="http://127.0.0.1:9000/agent"
                    description="默认仅允许本机、回环或私网地址。" rules={[{ required: true, message: "请输入 Target endpoint" }]} />
                  <EvaluationField name="timeout_seconds" label="超时（秒）" kind="number" min={1} max={600} />
                  <EvaluationField name="allow_public_target" label="公网目标" kind="switch" description="仅在已获得目标授权时开启。" />
                  <EvaluationField name="auth_type" label="鉴权方式" kind="select" options={[
                    { label: "无", value: "none" },
                    { label: "Bearer Token", value: "bearer" },
                  ]} />
                  <Form.Item noStyle shouldUpdate={(a, b) => a.auth_type !== b.auth_type}>
                    {({ getFieldValue }) => getFieldValue("auth_type") === "bearer" ? (
                      <EvaluationField name="auth_token" label="Token" kind="password" rules={[{ required: true, message: "请输入 Token" }]} />
                    ) : null}
                  </Form.Item>
                </FieldGroup>
              </CardContent>
            </Card>
          )}
        </div>

        <Card className="submit-panel">
          <CardHeader>
            <CardTitle>确认并提交</CardTitle>
            <CardDescription>同步等待结果，或将评测交给持久队列。</CardDescription>
          </CardHeader>
          <CardContent>
            <FieldGroup>
              <EvaluationField name="submit_as" label="提交方式" kind="radio" options={[
                { label: "同步执行（等待结果）", value: "sync" },
                { label: "提交持久队列", value: "job" },
              ]} />
              {submitAs === "job" && (
                <>
                  <EvaluationField name="request_id" label="请求 ID（幂等键）" placeholder="stateful-20260919-001"
                    rules={[
                      { required: true, message: "队列提交需要 request_id" },
                      { pattern: /^[A-Za-z0-9._:-]{8,200}$/, message: "8-200 位字母数字或 . _ : -" },
                    ]} />
                  <Alert>
                    <Info />
                    <AlertTitle>持久 Job 不接受明文凭据</AlertTitle>
                    <AlertDescription>若 Target 使用 Bearer Token，请改用 Provider Instance 引用，或选择同步执行。</AlertDescription>
                  </Alert>
                </>
              )}
            </FieldGroup>
          </CardContent>
          <CardFooter className="justify-end">
            <Button type="submit" size="lg" disabled={submitMutation.isPending} aria-busy={submitMutation.isPending} className="w-full sm:w-auto">
              {submitMutation.isPending ? <Spinner data-icon="inline-start" aria-label="正在提交" /> : <ArrowRight data-icon="inline-start" />}
              {submitMutation.isPending ? "正在提交评测…" : "提交评测"}
            </Button>
          </CardFooter>
        </Card>
        </form>
      </Form>
    </>
  );
}

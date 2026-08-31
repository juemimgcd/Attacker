(() => {
  "use strict";

  const apiPrefix = document
    .querySelector('meta[name="attacker-api-prefix"]')
    .content.replace(/\/$/, "");
  const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  });
  const terminalJobs = new Set(["succeeded", "failed", "cancelled"]);
  const statusLabels = {
    queued: "排队中",
    leased: "已领取",
    running: "运行中",
    retry_wait: "等待重试",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    completed: "已完成",
    waiting_approval: "等待审批",
    paused: "已暂停",
    pending: "待处理",
    approved: "已批准",
    rejected: "已拒绝",
    safe: "安全",
    violation: "违规",
    error: "错误",
  };
  const kindLabels = {
    stateful: "Stateful 基线",
    deterministic: "确定性黑盒",
    deterministic_graybox: "确定性灰盒",
    adaptive: "自适应灰盒",
  };
  const profileHelp = {
    hardened: "验证身份隔离、污染过滤和恢复策略均处于加固状态。",
    vulnerable: "在隔离适配器中运行预期存在漏洞的基线，用于校准检测能力。",
    regressed: "在隔离适配器中运行安全回归样本，验证 Replay 分类能力。",
  };
  const state = {
    apiKey: readStoredApiKey(),
    currentView: "jobs",
    jobs: [],
    jobsSignature: null,
    report: null,
    approvals: [],
    runId: null,
    runStatus: null,
    jobsInFlight: false,
    jobsRequest: 0,
    detailRequest: 0,
    toastTimer: null,
  };

  const views = {
    jobs: document.querySelector("#jobs-view"),
    new: document.querySelector("#new-view"),
    detail: document.querySelector("#detail-view"),
  };
  const navButtons = [...document.querySelectorAll("[data-view]")];
  const connectionStatus = document.querySelector("#connection-status");
  const apiKeySettings = document.querySelector("#api-key-settings");
  const apiKeySummary = apiKeySettings.querySelector("summary");
  const apiKeyForm = document.querySelector("#api-key-form");
  const apiKeyInput = document.querySelector("#api-key");
  const clearApiKeyButton = document.querySelector("#clear-api-key");
  const jobsLoading = document.querySelector("#jobs-loading");
  const jobsEmpty = document.querySelector("#jobs-empty");
  const jobsError = document.querySelector("#jobs-error");
  const jobsContent = document.querySelector("#jobs-content");
  const jobsList = document.querySelector("#jobs-list");
  const refreshJobsButton = document.querySelector("#refresh-jobs");
  const newRunForm = document.querySelector("#new-run-form");
  const newRunError = document.querySelector("#new-run-error");
  const submitRunButton = document.querySelector("#submit-run");
  const profileSelect = document.querySelector("#profile");
  const profileHelpText = document.querySelector("#profile-help");
  const detailLoading = document.querySelector("#detail-loading");
  const detailContent = document.querySelector("#detail-content");
  const detailError = document.querySelector("#detail-error");
  const detailErrorMessage = document.querySelector("#detail-error-message");
  const retryDetailButton = document.querySelector("#retry-detail");
  const runIdText = document.querySelector("#run-id");
  const exportActions = document.querySelector("#export-actions");
  let runStatus = document.querySelector("#run-status");
  const runTarget = document.querySelector("#run-target");
  const runMetrics = document.querySelector("#run-metrics");
  const findingsCount = document.querySelector("#findings-count");
  const findingsEmpty = document.querySelector("#findings-empty");
  const findingsList = document.querySelector("#findings-list");
  const evidenceCount = document.querySelector("#evidence-count");
  const evidenceEmpty = document.querySelector("#evidence-empty");
  const evidenceList = document.querySelector("#evidence-list");
  const approvalsCount = document.querySelector("#approvals-count");
  const approvalsEmpty = document.querySelector("#approvals-empty");
  const approvalsList = document.querySelector("#approvals-list");
  const replayHelp = document.querySelector("#replay-help");
  const replayButton = document.querySelector("#start-replay");
  const replayError = document.querySelector("#replay-error");
  const replaysEmpty = document.querySelector("#replays-empty");
  const replaysList = document.querySelector("#replays-list");
  const pollAnnouncer = document.querySelector("#poll-announcer");
  const toast = document.querySelector("#toast");

  apiKeyInput.value = state.apiKey;
  clearApiKeyButton.disabled = !state.apiKey;

  function readStoredApiKey() {
    try {
      return sessionStorage.getItem("attacker-api-key") || "";
    } catch (_error) {
      return "";
    }
  }

  function storeApiKey(value) {
    state.apiKey = value;
    try {
      if (value) {
        sessionStorage.setItem("attacker-api-key", value);
      } else {
        sessionStorage.removeItem("attacker-api-key");
      }
    } catch (_error) {
      // Safari 的受限存储模式下仍保留当前页面内存中的值。
    }
  }

  function apiUrl(path) {
    return `${apiPrefix}${path}`;
  }

  function requestHeaders(extraHeaders = {}) {
    const headers = new Headers(extraHeaders);
    headers.set("Accept", "application/json");
    if (state.apiKey) {
      headers.set("X-API-Key", state.apiKey);
    }
    return headers;
  }

  async function apiFetch(path, options = {}) {
    const response = await fetch(apiUrl(path), {
      ...options,
      credentials: "same-origin",
      headers: requestHeaders(options.headers),
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      const detail =
        payload && typeof payload === "object" && "detail" in payload
          ? payload.detail
          : payload;
      const message =
        typeof detail === "string" ? detail : JSON.stringify(detail || "请求失败");
      const error = new Error(`${response.status} · ${message}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function node(tag, className = "", text = "") {
    const value = document.createElement(tag);
    if (className) {
      value.className = className;
    }
    if (text !== "") {
      value.textContent = String(text);
    }
    return value;
  }

  function makeBadge(status, label = status) {
    const badge = node("span", "status-badge", statusLabels[label] || label || "—");
    badge.dataset.status = status || "unknown";
    return badge;
  }

  function formatDate(value) {
    if (!value) {
      return "—";
    }
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : dateFormatter.format(date);
  }

  function shortId(value) {
    if (!value) {
      return "—";
    }
    return String(value).slice(0, 8);
  }

  function setConnection(status, label) {
    connectionStatus.dataset.status = status;
    connectionStatus.lastElementChild.textContent = label;
  }

  function announce(message) {
    pollAnnouncer.textContent = "";
    window.setTimeout(() => {
      pollAnnouncer.textContent = message;
    }, 40);
  }

  function showToast(message) {
    window.clearTimeout(state.toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      toast.hidden = true;
    }, 4200);
  }

  function showError(element, error) {
    element.textContent = error instanceof Error ? error.message : String(error);
    element.hidden = false;
  }

  function setButtonBusy(button, busy, busyLabel) {
    if (!button.dataset.idleLabel) {
      button.dataset.idleLabel = button.textContent.trim();
    }
    button.disabled = busy;
    button.textContent = busy ? busyLabel : button.dataset.idleLabel;
  }

  function setView(view, runId = null, push = true, focus = true) {
    state.currentView = view;
    Object.entries(views).forEach(([name, element]) => {
      element.hidden = name !== view;
    });
    navButtons.forEach((button) => {
      const selected = button.dataset.view === view || (view === "detail" && button.dataset.view === "jobs");
      if (selected) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    });

    if (push) {
      const url = new URL(window.location.href);
      url.searchParams.delete("view");
      url.searchParams.delete("run");
      if (view === "new") {
        url.searchParams.set("view", "new");
      } else if (view === "detail" && runId) {
        url.searchParams.set("run", runId);
      }
      window.history.pushState(null, "", url);
    }

    document.title =
      view === "new"
        ? "新建评测 · Attacker"
        : view === "detail"
          ? `运行 ${shortId(runId)} · Attacker`
          : "任务与运行 · Attacker";
    if (focus) {
      views[view].querySelector("h1").focus({ preventScroll: true });
    }
    if (view === "detail" && runId) {
      loadDetail(runId);
    }
  }

  function applyLocation(focus = false) {
    const params = new URL(window.location.href).searchParams;
    const runId = params.get("run");
    if (runId) {
      setView("detail", runId, false, focus);
    } else if (params.get("view") === "new") {
      setView("new", null, false, focus);
    } else {
      setView("jobs", null, false, focus);
    }
  }

  async function loadJobs({ initial = false, force = false } = {}) {
    if (state.jobsInFlight && !force) {
      return;
    }
    const requestNumber = ++state.jobsRequest;
    state.jobsInFlight = true;
    if (initial && state.jobs.length === 0) {
      jobsLoading.hidden = false;
      jobsContent.hidden = true;
      jobsEmpty.hidden = true;
    }
    jobsError.hidden = true;
    jobsContent.setAttribute("aria-busy", "true");
    setButtonBusy(refreshJobsButton, true, "刷新中…");
    try {
      const jobs = await apiFetch("/jobs?limit=100");
      if (requestNumber !== state.jobsRequest) {
        return;
      }
      const signature = jobs
        .map(
          (job) =>
            `${job.id}:${job.status}:${job.run_id || ""}:${job.attempts}:${job.max_attempts}`,
        )
        .join("|");
      if (state.jobsSignature !== null && state.jobsSignature !== signature) {
        const active = jobs.filter((job) => !terminalJobs.has(job.status)).length;
        announce(`任务状态已更新。共 ${jobs.length} 条，${active} 条执行中。`);
      }
      state.jobsSignature = signature;
      state.jobs = jobs;
      renderJobs(jobs);
      setConnection("ready", "API 已连接");

      if (state.currentView === "detail" && state.runId) {
        const selectedJob = jobs.find((job) => job.run_id === state.runId);
        if (selectedJob && !terminalJobs.has(selectedJob.status)) {
          loadDetail(state.runId, { silent: true });
        }
      }
    } catch (error) {
      if (requestNumber !== state.jobsRequest) {
        return;
      }
      setConnection("error", error.status === 401 ? "需要 API Key" : "API 不可用");
      if (state.jobs.length === 0) {
        jobsLoading.hidden = true;
        jobsContent.hidden = true;
        jobsEmpty.hidden = true;
      }
      showError(jobsError, error);
    } finally {
      if (requestNumber === state.jobsRequest) {
        state.jobsInFlight = false;
        jobsContent.setAttribute("aria-busy", "false");
        setButtonBusy(refreshJobsButton, false, "刷新中…");
      }
    }
  }

  function renderJobs(jobs) {
    jobsLoading.hidden = true;
    jobsEmpty.hidden = jobs.length !== 0;
    jobsContent.hidden = jobs.length === 0;
    jobsList.replaceChildren();
    const fragment = document.createDocumentFragment();

    jobs.forEach((job) => {
      const item = node("li", "job-row");
      const badge = makeBadge(job.status);
      const statusCell = node("div", "job-status");
      statusCell.append(badge);

      const main = node("div", "job-main");
      main.append(node("strong", "", kindLabels[job.kind] || job.kind));
      main.append(node("small", "", `任务 ${shortId(job.id)} · 请求 ${job.request_id}`));
      if (job.error_summary) {
        main.append(node("small", "job-error", job.error_summary));
      }

      const time = node("time", "job-time", formatDate(job.created_at));
      if (job.created_at) {
        time.dateTime = job.created_at;
      }
      const attempts = node(
        "span",
        "job-attempts",
        `${job.attempts}/${job.max_attempts}`,
      );
      attempts.title = "已尝试次数 / 最大尝试次数";

      const open = node("button", "button button-secondary job-open", "查看运行");
      open.type = "button";
      open.disabled = !job.run_id;
      open.title = job.run_id ? `打开运行 ${job.run_id}` : "Worker 创建 Run 后可查看";
      if (job.run_id) {
        open.addEventListener("click", () => setView("detail", job.run_id));
      }

      item.append(statusCell, main, time, attempts, open);
      fragment.append(item);
    });
    jobsList.append(fragment);
  }

  function createRequestId() {
    const random = new Uint32Array(2);
    window.crypto.getRandomValues(random);
    return `console:${Date.now().toString(36)}:${random[0].toString(36)}${random[1].toString(36)}`;
  }

  async function submitRun(event) {
    event.preventDefault();
    newRunError.hidden = true;
    setButtonBusy(submitRunButton, true, "正在入队…");
    try {
      await apiFetch("/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: createRequestId(),
          kind: "stateful",
          payload: {
            profile: profileSelect.value,
            dataset_path: "samples/stateful/phase3.yaml",
            target_name: "isolated-stateful-sandbox",
          },
        }),
      });
      showToast("评测任务已加入 Durable Job 队列。");
      setView("jobs");
      await loadJobs({ force: true });
    } catch (error) {
      showError(newRunError, error);
    } finally {
      setButtonBusy(submitRunButton, false, "正在入队…");
    }
  }

  async function loadDetail(runId, { silent = false } = {}) {
    const requestNumber = ++state.detailRequest;
    state.runId = runId;
    runIdText.textContent = runId;
    detailError.hidden = true;
    if (!silent) {
      detailLoading.hidden = false;
      detailContent.hidden = true;
      exportActions.hidden = true;
    }

    const approvalsPromise = apiFetch(`/runs/${encodeURIComponent(runId)}/approvals`).catch(
      (error) => error,
    );
    try {
      const report = await apiFetch(`/runs/${encodeURIComponent(runId)}/report.json`);
      const approvalsResult = await approvalsPromise;
      if (requestNumber !== state.detailRequest) {
        return;
      }
      state.report = report;
      state.approvals =
        approvalsResult instanceof Error ? report.approvals || [] : approvalsResult;
      if (approvalsResult instanceof Error) {
        showToast("审批接口暂不可用，当前显示报告中的审批快照。");
      }
      renderDetail(report, state.approvals);
      detailLoading.hidden = true;
      detailContent.hidden = false;
      exportActions.hidden = false;
      setConnection("ready", "API 已连接");

      const nextStatus = report.summary?.status || report.run?.status || "unknown";
      if (state.runStatus && state.runStatus !== nextStatus) {
        announce(`运行状态已更新为${statusLabels[nextStatus] || nextStatus}。`);
      }
      state.runStatus = nextStatus;
    } catch (error) {
      if (requestNumber !== state.detailRequest || silent) {
        return;
      }
      detailLoading.hidden = true;
      detailContent.hidden = true;
      exportActions.hidden = true;
      detailErrorMessage.textContent = error.message;
      detailError.hidden = false;
      setConnection("error", error.status === 401 ? "需要 API Key" : "报告不可用");
    }
  }

  function renderDetail(report, approvals) {
    const summary = report.summary || {};
    const findings = Array.isArray(report.findings) ? report.findings : [];
    const events = Array.isArray(report.events) ? report.events : [];
    const status = summary.status || report.run?.status || "unknown";
    const nextRunStatus = makeRunStatusBadge(status);
    runStatus.replaceWith(nextRunStatus);
    runStatus = nextRunStatus;
    const targetName = report.target?.name || "未知目标";
    const datasetName = report.dataset?.name || "未知数据集";
    runTarget.textContent = `${targetName} · ${datasetName}`;

    const evidenceLinkRate =
      summary.finding_evidence_link_rate == null
        ? null
        : Number(summary.finding_evidence_link_rate);
    renderMetrics([
      ["完成用例", `${summary.completed_cases || 0}/${summary.total_cases || 0}`],
      ["Findings", findings.length],
      ["Evidence", events.length],
      [
        "证据链接率",
        findings.length > 0 && Number.isFinite(evidenceLinkRate)
          ? `${Math.round(evidenceLinkRate * 100)}%`
          : "N/A",
      ],
      [
        "错误 / 不可评测",
        `${summary.outcomes?.error || 0} / ${summary.outcomes?.not_evaluable || 0}`,
      ],
    ]);
    renderEvidence(events);
    renderFindings(findings, new Set(events.map((event) => String(event.id))));
    renderApprovals(approvals);
    renderReplay(report);
  }

  function makeRunStatusBadge(status) {
    const badge = makeBadge(status);
    badge.id = "run-status";
    return badge;
  }

  function renderMetrics(metrics) {
    const fragment = document.createDocumentFragment();
    metrics.forEach(([label, value]) => {
      const item = node("div");
      item.append(node("dt", "", label), node("dd", "", value));
      fragment.append(item);
    });
    runMetrics.replaceChildren(fragment);
  }

  function renderFindings(findings, evidenceIds) {
    findingsCount.textContent = String(findings.length);
    findingsEmpty.hidden = findings.length !== 0;
    findingsList.hidden = findings.length === 0;
    findingsList.replaceChildren();
    const fragment = document.createDocumentFragment();

    findings.forEach((finding) => {
      const item = node("li", "finding-item");
      const title = node("div", "finding-title");
      const titleText = node("div");
      titleText.append(node("h3", "", finding.case_id || "未命名 Finding"));
      titleText.append(node("small", "", finding.category || "未分类"));
      title.append(titleText, makeBadge("violation", finding.risk_level || "unknown"));
      item.append(title, node("p", "", finding.reason || "未提供评测理由。"));

      const references = Array.isArray(finding.evidence_event_ids)
        ? finding.evidence_event_ids.map(String)
        : [];
      const linkRow = node("div", "evidence-links");
      references.forEach((eventId) => {
        const button = node("button", "evidence-link", `证据 ${shortId(eventId)}`);
        button.type = "button";
        button.disabled = !evidenceIds.has(eventId);
        button.addEventListener("click", () => revealEvidence(eventId));
        linkRow.append(button);
      });
      if (references.length) {
        item.append(linkRow);
      }
      const missing = references.filter((eventId) => !evidenceIds.has(eventId));
      if (!references.length || missing.length) {
        item.append(
          node(
            "p",
            "evidence-missing",
            !references.length
              ? "此 Finding 未声明 Evidence Event。"
              : `缺少 ${missing.length} 条被引用的 Evidence Event。`,
          ),
        );
      }
      fragment.append(item);
    });
    findingsList.append(fragment);
  }

  function renderEvidence(events) {
    evidenceCount.textContent = String(events.length);
    evidenceEmpty.hidden = events.length !== 0;
    evidenceList.hidden = events.length === 0;
    evidenceList.replaceChildren();
    const fragment = document.createDocumentFragment();

    [...events]
      .sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0))
      .forEach((event) => {
        const item = node("li", "evidence-item");
        item.id = `evidence-${event.id}`;
        const details = node("details");
        const summary = node("summary");
        summary.append(
          node("span", "evidence-sequence", `#${event.sequence ?? "—"}`),
          node("span", "evidence-type", event.event_type || "unknown_event"),
          node("time", "evidence-time", formatDate(event.created_at)),
        );
        const payload = node(
          "pre",
          "evidence-payload",
          JSON.stringify(event.evidence ?? {}, null, 2),
        );
        details.append(summary, payload);
        item.append(details);
        fragment.append(item);
      });
    evidenceList.append(fragment);
  }

  function revealEvidence(eventId) {
    const item = document.getElementById(`evidence-${eventId}`);
    if (!item) {
      return;
    }
    item.querySelector("details").open = true;
    item.dataset.highlighted = "true";
    item.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => delete item.dataset.highlighted, 2400);
  }

  function renderApprovals(approvals) {
    const rows = Array.isArray(approvals) ? approvals : [];
    const pending = rows.filter((approval) => approval.status === "pending");
    approvalsCount.textContent = String(pending.length);
    approvalsEmpty.hidden = pending.length !== 0;
    approvalsList.hidden = rows.length === 0;
    approvalsList.replaceChildren();
    const fragment = document.createDocumentFragment();

    rows.forEach((approval) => {
      const item = node("li", "approval-item");
      const title = node("div", "approval-title");
      const titleText = node("div");
      titleText.append(node("h3", "", approval.case_id || "未命名审批"));
      titleText.append(node("small", "", `审批 ${shortId(approval.id)}`));
      title.append(titleText, makeBadge(approval.status));
      item.append(title, node("p", "", approval.risk_summary || "未提供风险摘要。"));

      if (approval.status === "pending") {
        item.append(makeApprovalForm(approval));
      } else {
        const resolution = [approval.resolved_by, approval.reason].filter(Boolean).join(" · ");
        item.append(node("p", "resolved-approval", resolution || "已处理"));
      }
      fragment.append(item);
    });
    approvalsList.append(fragment);
  }

  function makeApprovalForm(approval) {
    const form = node("form", "approval-form");
    const reviewerField = node("div");
    const reviewerId = `reviewer-${approval.id}`;
    const reviewerLabel = node("label", "", "操作者");
    reviewerLabel.htmlFor = reviewerId;
    const reviewer = node("input");
    reviewer.id = reviewerId;
    reviewer.name = "reviewer";
    reviewer.required = true;
    reviewer.maxLength = 200;
    reviewer.autocomplete = "off";
    reviewerField.append(reviewerLabel, reviewer);

    const reasonField = node("div");
    const reasonId = `reason-${approval.id}`;
    const reasonLabel = node("label", "", "决议理由");
    reasonLabel.htmlFor = reasonId;
    const reason = node("textarea");
    reason.id = reasonId;
    reason.name = "reason";
    reason.required = true;
    reason.maxLength = 2000;
    reason.rows = 2;
    reasonField.append(reasonLabel, reason);

    const runtimeDetails = node("details", "approval-runtime");
    runtimeDetails.append(node("summary", "", "高级恢复配置（可选）"));
    const runtimeBody = node("div", "approval-runtime-body");
    const runtimeHelpId = `runtime-help-${approval.id}`;
    const runtimeHelp = node(
      "p",
      "approval-runtime-help",
      "当恢复提示凭据已脱敏时，填写与原 Run 名称、端点和绑定一致的完整 JSON。内容只随本次决议请求发送，成功后立即清空，不会保存到浏览器存储。",
    );
    runtimeHelp.id = runtimeHelpId;

    const targetField = node("div");
    const targetId = `target-${approval.id}`;
    const targetLabel = node("label", "", "Target JSON");
    targetLabel.htmlFor = targetId;
    const target = node("textarea");
    target.id = targetId;
    target.name = "target";
    target.rows = 6;
    target.autocomplete = "off";
    target.spellcheck = false;
    target.setAttribute("aria-describedby", runtimeHelpId);
    target.placeholder =
      '{\n  "name": "approval-sandbox",\n  "endpoint": "https://target.example/agent",\n  "auth": {"type": "bearer", "token": "..."}\n}';
    target.addEventListener("input", () => target.setCustomValidity(""));
    targetField.append(targetLabel, target);

    const plannerField = node("div");
    const plannerId = `planner-${approval.id}`;
    const plannerLabel = node("label", "", "Planner JSON");
    plannerLabel.htmlFor = plannerId;
    const planner = node("textarea");
    planner.id = plannerId;
    planner.name = "planner";
    planner.rows = 6;
    planner.autocomplete = "off";
    planner.spellcheck = false;
    planner.setAttribute("aria-describedby", runtimeHelpId);
    planner.placeholder =
      '{\n  "backend": "openai_compatible",\n  "endpoint": "https://planner.example/v1",\n  "model": "planner",\n  "api_key": "..."\n}';
    planner.addEventListener("input", () => planner.setCustomValidity(""));
    plannerField.append(plannerLabel, planner);
    runtimeBody.append(runtimeHelp, targetField, plannerField);
    runtimeDetails.append(runtimeBody);

    const actions = node("div", "approval-actions");
    const reject = node("button", "button button-danger", "拒绝");
    reject.type = "button";
    const approve = node("button", "button button-primary", "批准并恢复");
    approve.type = "button";
    reject.addEventListener("click", () => resolveApproval(approval, false, form));
    approve.addEventListener("click", () => resolveApproval(approval, true, form));
    actions.append(reject, approve);
    form.append(reviewerField, reasonField, runtimeDetails, actions);
    form.addEventListener("submit", (event) => event.preventDefault());
    return form;
  }

  function parseOptionalJson(input, label) {
    const source = input.value.trim();
    input.setCustomValidity("");
    if (!source) {
      return undefined;
    }
    try {
      const value = JSON.parse(source);
      if (!value || Array.isArray(value) || typeof value !== "object") {
        throw new TypeError("JSON value is not an object");
      }
      return value;
    } catch (_error) {
      input.setCustomValidity(`${label} 必须是合法的 JSON 对象。`);
      input.closest("details").open = true;
      input.focus();
      input.reportValidity();
      throw new SyntaxError(`${label} JSON 无效`);
    }
  }

  async function resolveApproval(approval, approved, form) {
    if (!form.reportValidity()) {
      return;
    }
    const previousError = form.querySelector("[data-approval-error]");
    previousError?.remove();
    let target;
    let planner;
    try {
      target = parseOptionalJson(form.elements.target, "Target");
      planner = parseOptionalJson(form.elements.planner, "Planner");
    } catch (_error) {
      return;
    }
    const payload = {
      approved,
      resolved_by: form.elements.reviewer.value.trim(),
      reason: form.elements.reason.value.trim(),
    };
    if (target) {
      payload.target = target;
    }
    if (planner) {
      payload.planner = planner;
    }
    const controls = [...form.elements];
    controls.forEach((control) => (control.disabled = true));
    try {
      await apiFetch(
        `/runs/${encodeURIComponent(state.runId)}/approvals/${encodeURIComponent(approval.id)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      form.elements.target.value = "";
      form.elements.planner.value = "";
      target = undefined;
      planner = undefined;
      showToast(approved ? "审批已通过，Run 已恢复。" : "审批已拒绝，决议已记录。");
      await loadDetail(state.runId);
    } catch (error) {
      const message = node("p", "evidence-missing", error.message);
      message.dataset.approvalError = "true";
      message.setAttribute("role", "alert");
      form.append(message);
      controls.forEach((control) => (control.disabled = false));
    }
  }

  function replayProfile(report) {
    return report.policy?.profile || report.target?.config?.profile || null;
  }

  function renderReplay(report) {
    replayError.hidden = true;
    const mode = String(report.run?.mode || "");
    const status = report.summary?.status || report.run?.status;
    const profile = replayProfile(report);
    const isStateful = mode.includes("stateful");
    const canReplay = isStateful && status === "completed" && Boolean(profile);
    replayButton.disabled = !canReplay;
    replayButton.dataset.profile = profile || "";

    if (!isStateful) {
      replayHelp.textContent = "HTTP Replay 必须通过 API 重新提供目标凭据；控制台不会持久化凭据。";
    } else if (status !== "completed") {
      replayHelp.textContent = "运行完成后才可使用冻结的同一绑定 Replay。";
    } else if (!profile) {
      replayHelp.textContent = "报告缺少冻结 Profile，无法安全重跑。";
    } else {
      replayHelp.textContent = `使用冻结的 ${profile} Profile 同绑定重跑，并比较 Finding 差异。`;
    }

    const rows = Array.isArray(report.replays)
      ? report.replays
      : report.replay
        ? [report.replay]
        : [];
    replaysEmpty.hidden = rows.length !== 0;
    replaysList.hidden = rows.length === 0;
    replaysList.replaceChildren();
    const fragment = document.createDocumentFragment();
    rows.forEach((replay) => {
      const item = node("li", "replay-item");
      const title = node("div", "replay-title");
      const titleText = node("div");
      titleText.append(node("h3", "", `Replay ${shortId(replay.replay_run_id)}`));
      titleText.append(node("small", "", `来源 ${shortId(replay.source_run_id)}`));
      title.append(titleText, makeBadge(replay.status || "completed"));
      item.append(title);

      const diff = replay.diff || {};
      const diffList = node("dl", "replay-diff");
      ["fixed", "new", "persistent", "regressed"].forEach((key) => {
        const values = Array.isArray(diff[key]) ? diff[key] : [];
        const cell = node("div");
        const value = node("dd", "", values.length);
        value.title = values.join(", ") || "无";
        cell.append(node("dt", "", key), value);
        diffList.append(cell);
      });
      item.append(diffList);

      if (replay.replay_run_id && replay.replay_run_id !== state.runId) {
        const open = node("button", "button button-secondary", "查看 Replay 运行");
        open.type = "button";
        open.addEventListener("click", () => setView("detail", replay.replay_run_id));
        item.append(open);
      }
      fragment.append(item);
    });
    replaysList.append(fragment);
  }

  async function startReplay() {
    const profile = replayButton.dataset.profile;
    replayError.hidden = true;
    setButtonBusy(replayButton, true, "Replay 运行中…");
    try {
      await apiFetch(`/runs/${encodeURIComponent(state.runId)}/replay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "same_binding_rerun", profile }),
      });
      showToast("Replay 已完成，差异已写入报告。");
      await loadDetail(state.runId);
    } catch (error) {
      showError(replayError, error);
    } finally {
      setButtonBusy(replayButton, false, "Replay 运行中…");
    }
  }

  async function exportReport(format, button) {
    const extension = format === "json" ? "json" : "md";
    const path = `/runs/${encodeURIComponent(state.runId)}/report.${extension}`;
    setButtonBusy(button, true, "正在导出…");
    try {
      const response = await fetch(apiUrl(path), {
        credentials: "same-origin",
        headers: requestHeaders(),
      });
      if (!response.ok) {
        const message = await response.text();
        throw new Error(`${response.status} · ${message || "导出失败"}`);
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const download = node("a");
      const safeRunId = String(state.runId).replace(/[^A-Za-z0-9._-]/g, "-");
      download.href = objectUrl;
      download.download = `attacker-${safeRunId}.${extension}`;
      document.body.append(download);
      download.click();
      download.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      showToast(`${extension.toUpperCase()} 报告已导出。`);
    } catch (error) {
      showToast(error.message);
    } finally {
      setButtonBusy(button, false, "正在导出…");
    }
  }

  navButtons.forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll("[data-go-new]").forEach((button) => {
    button.addEventListener("click", () => setView("new"));
  });
  document.querySelectorAll("[data-go-jobs]").forEach((button) => {
    button.addEventListener("click", () => setView("jobs"));
  });
  document.querySelectorAll("[data-export]").forEach((button) => {
    button.addEventListener("click", () => exportReport(button.dataset.export, button));
  });
  refreshJobsButton.addEventListener("click", () => loadJobs());
  newRunForm.addEventListener("submit", submitRun);
  profileSelect.addEventListener("change", () => {
    profileHelpText.textContent = profileHelp[profileSelect.value];
  });
  retryDetailButton.addEventListener("click", () => loadDetail(state.runId));
  replayButton.addEventListener("click", startReplay);
  apiKeyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    storeApiKey(apiKeyInput.value);
    clearApiKeyButton.disabled = !state.apiKey;
    apiKeySettings.open = false;
    apiKeySummary.focus({ preventScroll: true });
    showToast(state.apiKey ? "API Key 已应用到当前标签页。" : "已使用无密钥连接。");
    await loadJobs({ initial: state.jobs.length === 0, force: true });
    if (state.currentView === "detail" && state.runId) {
      await loadDetail(state.runId);
    }
  });
  clearApiKeyButton.addEventListener("click", async () => {
    apiKeyInput.value = "";
    storeApiKey("");
    clearApiKeyButton.disabled = true;
    apiKeySettings.open = false;
    apiKeySummary.focus({ preventScroll: true });
    showToast("当前标签页的 API Key 已清除。");
    await loadJobs({ initial: state.jobs.length === 0, force: true });
  });
  window.addEventListener("popstate", () => applyLocation(true));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      loadJobs();
    }
  });

  applyLocation();
  loadJobs({ initial: true });
  window.setInterval(() => {
    if (!document.hidden) {
      loadJobs();
    }
  }, 5000);
})();

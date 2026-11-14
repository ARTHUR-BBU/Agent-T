/* Local, account-bound candidate review; it never publishes effective rules. */
(() => {
  const $ = (id) => document.getElementById(id);
  const accounts = window.AgentLocalAccounts;
  let versions = [], selected = null, detailSeq = 0, listSeq = 0, listLoading = false, nextBefore = null, pendingSubmission = null, previousUser = null, decisionPending = false;
  let releaseEnabled = false, releaseTargets = [], pendingRollback = null, validationBlocked = true, validationRunning = false;
  const show = (id, visible) => $(id).classList.toggle("hidden", !visible);
  function setValidationBlocked(value, reason = "unknown") {
    validationBlocked = value;
    $("rule-validation-block-warning").textContent = reason === "running"
      ? "其他规则测试正在进行，完成后可审核通过或发布。"
      : reason === "blocked" ? "规则测试已暂停，请联系管理员检查此前测试进程及工作区。"
      : "规则测试状态尚未确认，请稍候或联系管理员检查。";
    show("rule-validation-block-warning", value);
    if (value) {
      $("rule-review-validate").disabled = true;
      $("rule-review-decision").querySelector('[value="approve"]').disabled = true;
    } else if (selected) {
      $("rule-review-validate").disabled = validationRunning || ["测试中", "已退回", "基线已变化", "测试已中断，请检查后恢复"].includes(selected.status);
      $("rule-review-decision").querySelector('[value="approve"]').disabled = validationRunning || decisionPending || !selected.reviewable;
    }
  }
  const message = (text) => { $("rule-review-message").textContent = text; };
  const reportError = (error) => { if (!error.stale) message(error.message); };
  const currentUser = () => accounts.user();
  const req = (path, method = "GET", body) => accounts.request("/rules" + path, method, body);
  const option = (select, value, text) => {
    const node = document.createElement("option"); node.value = value; node.textContent = text; select.append(node);
  };
  const currentVersion = () => versions.find((version) => version.category === $("rule-review-version").value);

  function syncAccount() {
    const user = currentUser();
    const allowed = !!user && !user.must_change_password;
    show("btn-rule-review", allowed);
    if (!allowed || user !== previousUser) {
      if ($("rule-review-dialog").open) $("rule-review-dialog").close();
      $("rule-review-form").reset(); $("rule-review-decision").reset(); $("rule-review-list").replaceChildren();
      $("rule-review-detail-body").replaceChildren(); versions = []; selected = null; pendingSubmission = null; detailSeq++; listSeq++; listLoading = false; nextBefore = null;
      show("rule-review-more", false);
      releaseEnabled = false; releaseTargets = []; pendingRollback = null; show("rule-release-section", false);
      setValidationBlocked(true);
      $("rule-release-events").replaceChildren(); $("rule-rollback-target").replaceChildren(); $("rule-rollback-form").reset();
      message("");
    }
    previousUser = user;
  }

  function syncForm() {
    const version = currentVersion(), operation = $("rule-review-operation").value;
    const adding = operation === "add", editing = operation === "edit";
    show("rule-review-id-wrap", adding); show("rule-review-target-wrap", !adding);
    show("rule-review-name-wrap", adding || editing); show("rule-review-description-wrap", adding || editing);
    show("rule-review-segment-wrap", adding); show("rule-review-examples", adding);
    for (const id of ["item-id", "segment", "literal", "positive", "negative"]) $("rule-review-" + id).required = adding;
    for (const id of ["name", "description"]) $("rule-review-" + id).required = adding || editing;
    $("rule-review-target").required = !adding;
    const target = $("rule-review-target"), segment = $("rule-review-segment");
    const selectedTarget = target.value, selectedSegment = segment.value;
    target.replaceChildren(); segment.replaceChildren(); option(target, "", "请选择检查项"); option(segment, "", "请选择分段");
    if (version) {
      const items = version.items.filter((item) => operation === "enable" ? !item.enabled :
        operation === "disable" ? item.enabled && !item.has_hardline : !item.has_hardline);
      for (const item of items) option(target, item.id, item.name);
      for (const item of version.segments) option(segment, item.key, `${item.key} · ${item.name}`);
      if (items.some((item) => item.id === selectedTarget)) target.value = selectedTarget;
      if (version.segments.some((item) => item.key === selectedSegment)) segment.value = selectedSegment;
    }
    $("rule-review-submit").disabled = !version;
  }

  function fillEditedItem() {
    if ($("rule-review-operation").value !== "edit") return;
    const version = currentVersion();
    const item = version && version.items.find((entry) => entry.id === $("rule-review-target").value);
    $("rule-review-name").value = item ? item.name : "";
    $("rule-review-description").value = item ? item.description : "";
  }

  async function loadVersions() {
    const user = currentUser(), data = await req("/versions");
    if (!user || currentUser() !== user) return;
    versions = data.versions;
    const validationReason = data.validation_state === "running" ? "running" : data.validation_blocked === true ? "blocked" : "unknown";
    setValidationBlocked(data.validation_blocked !== false, validationReason);
    releaseEnabled = data.release_trial_available === true && user.role === "admin";
    show("rule-release-section", releaseEnabled);
    const select = $("rule-review-version"), previous = select.value;
    select.replaceChildren(); option(select, "", "请选择已初始化的基线");
    for (const version of versions) option(select, version.category, `${version.label} · 第 ${version.generation} 版`);
    if (versions.some((version) => version.category === previous)) select.value = previous;
    syncForm();
  }

  async function loadReleaseHistory() {
    if (!releaseEnabled) return;
    const user = currentUser(), data = await req("/release-history");
    if (currentUser() !== user || !$('rule-review-dialog').open) return;
    const events = $("rule-release-events"), target = $("rule-rollback-target");
    events.replaceChildren(); target.replaceChildren(); option(target, "", "请选择曾生效的历史版本");
    for (const event of data.events) {
      const row = document.createElement("p"); row.className = "rule-review-detail-line";
      row.textContent = `${event.category} · ${event.action === "rollback" ? "回滚" : "发布"} · 第 ${event.generation} 版 · ${event.display_name}（${event.username}）${event.reason ? " · " + event.reason : ""}`;
      events.append(row);
    }
    if (!data.events.length) events.textContent = "暂无发布或回滚记录。";
    if (data.events_truncated || data.versions_truncated) {
      const warning = document.createElement("p"); warning.textContent = "仅显示最近 100 条记录或版本；更早的版本暂不支持在页面选择。"; events.append(warning);
    }
    releaseTargets = [];
    for (const version of data.versions) {
      const active = data.active.find((entry) => entry.category === version.category);
      if (!active || active.version_id === version.id) continue;
      releaseTargets.push({category:version.category, version_id:version.id, generation:active.generation});
      option(target, version.id, `${version.category} · ${version.sha256.slice(0, 12)} · 当前第 ${active.generation} 版`);
    }
    $("rule-rollback-submit").disabled = !releaseTargets.length;
  }

  async function loadCandidates(append = false) {
    if (append && listLoading) return;
    const user = currentUser(), seq = ++listSeq, before = append ? nextBefore : null;
    if (append && before === null) return;
    listLoading = true; $("rule-review-more").disabled = true;
    try {
      const data = await req(before === null ? "/candidates" : `/candidates?before=${encodeURIComponent(before)}`);
      if (!user || currentUser() !== user || seq !== listSeq) return;
      const list = $("rule-review-list"); if (!append) list.replaceChildren();
      nextBefore = data.has_more ? data.next_before : null;
      show("rule-review-more", nextBefore !== null);
      if (!data.candidates.length && !append) { list.textContent = "暂无候选"; return; }
      for (const candidate of data.candidates) {
        const row = document.createElement("div"), text = document.createElement("span"), button = document.createElement("button");
        row.className = "rule-review-row";
        text.textContent = `${candidate.category} · ${candidate.status} · ${candidate.sha256.slice(0, 12)}`;
        button.type = "button"; button.textContent = "查看";
        button.addEventListener("click", () => loadDetails(candidate.id).catch(reportError));
        row.append(text, button); list.append(row);
      }
    } finally {
      if (seq === listSeq) { listLoading = false; $("rule-review-more").disabled = false; }
    }
  }

  function line(parent, label, value) {
    const node = document.createElement("p"); node.className = "rule-review-detail-line";
    node.textContent = `${label}：${value}`; parent.append(node);
  }
  function describe(value) {
    if (!value) return "无";
    const labels = {id: "编号", name: "名称", description: "说明", segment: "分段", enabled: "状态", literal: "关键词"};
    return Object.entries(value).map(([key, val]) => `${labels[key] || key}：${key === "enabled" ? (val ? "启用" : "停用") : val}`).join("；");
  }
  async function loadDetails(id) {
    const seq = ++detailSeq, user = currentUser();
    selected = null; show("rule-review-detail", false); $("rule-review-detail-body").replaceChildren();
    const [detail, results] = await Promise.all([req(`/candidates/${encodeURIComponent(id)}`), req(`/candidates/${encodeURIComponent(id)}/validations`)]);
    if (seq !== detailSeq || !user || currentUser() !== user) return;
    selected = detail; const body = $("rule-review-detail-body"); body.replaceChildren();
    line(body, "状态", detail.status); line(body, "提交账户（当前显示名）", `${detail.submitted_by_display_name || "未知"} · ${detail.submitted_by_username || "未知账号"} · ${detail.submitted_by.slice(0, 8)}`);
    line(body, "变更理由", detail.reason); line(body, "变更前", describe(detail.change.before)); line(body, "变更后", describe(detail.change.after));
    if (detail.examples.positive) { line(body, "应命中示例", detail.examples.positive); line(body, "不应命中示例", detail.examples.negative); }
    const latest = results.validations[0];
    detail.visible_validation_id = latest ? latest.id : null;
    detail.reviewable = detail.status === "待审核" && !!latest && latest.passed === true && latest.exit_code === 0 &&
      Number.isInteger(latest.collected_tests) && latest.collected_tests > 0 && latest.passed_tests === latest.collected_tests &&
      latest.skipped_tests === 0 && Array.isArray(latest.failed_test_ids) && latest.failed_test_ids.length === 0;
    if (latest) {
      const state = latest.passed ? "通过" : (latest.exit_code === null ? "超时或未完成" : "未通过");
      line(body, "最近一次已完成的品类金标", `${state} · ${latest.passed_tests}/${latest.collected_tests} 项通过 · ${latest.scope}`);
      if (latest.failed_test_ids && latest.failed_test_ids.length) line(body, "失败项目", latest.failed_test_ids.join("；"));
    }
    if (detail.review) line(body, "最近审核（当前显示名）", `${detail.review.reviewer_display_name || "未知"} · ${detail.review.reviewer_username || "未知账号"} · ${detail.review.reviewer_id.slice(0, 8)} · ${detail.review.decision === "approve" ? "通过" : "退回"} · ${detail.review.self_review ? "管理员自审 · " : ""}${detail.review.reason}`);
    show("rule-review-detail", true);
    $("rule-review-validate").disabled = validationBlocked || validationRunning || ["测试中", "已退回", "基线已变化", "测试已中断，请检查后恢复"].includes(detail.status);
    show("rule-review-decision", user.role === "admin" && !["已退回", "基线已变化", "测试中"].includes(detail.status));
    $("rule-review-decision").querySelector('[value="approve"]').disabled = validationBlocked || validationRunning || decisionPending || !detail.reviewable;
    $("rule-review-decision").querySelector('[value="reject"]').disabled = decisionPending;
  }

  async function openRules() {
    const user = currentUser();
    if (!user || user.must_change_password) return;
    setValidationBlocked(true); message(""); $("rule-review-dialog").showModal(); show("rule-review-detail", false);
    show("rule-review-init", currentUser().role === "admin");
    try {
      const response = await fetch("/api/categories", {cache: "no-store"});
      const data = await response.json();
      if (currentUser() !== user || !$("rule-review-dialog").open) return;
      const select = $("rule-review-init-category"); select.replaceChildren();
      for (const category of data.categories || []) option(select, category.id, category.label);
      await Promise.all([loadVersions(), loadCandidates()]);
      await loadReleaseHistory();
    } catch (error) { reportError(error); }
  }

  window.addEventListener("agent-local-account", syncAccount);
  $("btn-rule-review").addEventListener("click", openRules);
  $("rule-review-close").addEventListener("click", () => $("rule-review-dialog").close());
  $("rule-review-dialog").addEventListener("close", () => { detailSeq++; selected = null; $("rule-review-detail-body").replaceChildren(); });
  $("rule-review-operation").addEventListener("change", () => { syncForm(); fillEditedItem(); });
  $("rule-review-version").addEventListener("change", () => { syncForm(); fillEditedItem(); });
  $("rule-review-target").addEventListener("change", fillEditedItem);
  $("rule-review-more").addEventListener("click", async () => {
    try { await loadCandidates(true); } catch (error) { reportError(error); }
  });
  $("rule-rollback-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const target = releaseTargets.find((entry) => entry.version_id === $("rule-rollback-target").value);
    if (!target || !releaseEnabled) return;
    const reason = $("rule-rollback-reason").value.trim();
    if (!reason || !window.confirm(`确认将 ${target.category} 的本机试验规则回滚？这将影响后续新审查。`)) return;
    const key = JSON.stringify({target, reason});
    if (!pendingRollback || pendingRollback.key !== key) pendingRollback = {key, id:crypto.randomUUID().replaceAll("-", "")};
    const button = $("rule-rollback-submit"), user = currentUser(); let refreshFailed = false; button.disabled = true;
    try {
      await req("/rollback", "POST", {category:target.category, version_id:target.version_id, expected_generation:target.generation, reason, request_id:pendingRollback.id});
      if (currentUser() !== user) return;
      pendingRollback = null; $("rule-rollback-reason").value = ""; message("本机试验规则已回滚，仅影响后续新审查。");
      try { await loadVersions(); await loadReleaseHistory(); }
      catch (error) { if (!error.stale) { refreshFailed = true; message("回滚已完成，但记录刷新失败；请重新打开规则管理查看。"); } }
    } catch (error) { reportError(error); } finally { if (currentUser() === user) button.disabled = refreshFailed || !releaseTargets.length; }
  });
  $("rule-review-init").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try { await req("/initialize", "POST", {category: $("rule-review-init-category").value}); message("试用基线已初始化，尚未发布"); await loadVersions(); }
    catch (error) { reportError(error); } finally { button.disabled = false; }
  });
  $("rule-review-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const version = currentVersion(), operation = $("rule-review-operation").value;
    if (!version) return;
    if (operation === "add" && !$("rule-review-ack").checked) { message("请先确认示例的查看与保存范围"); return; }
    const draft = {category: version.category, operation, item_id: operation === "add" ? $("rule-review-item-id").value : $("rule-review-target").value,
      reason: $("rule-review-reason").value, base_sha256: version.sha256};
    if (operation === "add" || operation === "edit") {
      draft.name = $("rule-review-name").value; draft.description = $("rule-review-description").value;
    }
    if (operation === "add") {
      draft.segment = $("rule-review-segment").value; draft.literal = $("rule-review-literal").value;
      draft.positive = $("rule-review-positive").value; draft.negative = $("rule-review-negative").value;
    }
    const key = JSON.stringify({draft, generation: version.generation});
    if (!pendingSubmission || pendingSubmission.key !== key) pendingSubmission = {key, id: crypto.randomUUID().replaceAll("-", "")};
    const button = $("rule-review-submit"), seq = detailSeq; button.disabled = true;
    try {
      const created = await req("/candidates", "POST", {draft, expected_generation: version.generation, request_id: pendingSubmission.id, examples_acknowledged: true});
      message("候选已提交，尚未发布");
      try {
        await loadCandidates();
        if (seq === detailSeq && $("rule-review-dialog").open) await loadDetails(created.id);
      } catch (error) { if (!error.stale) message("候选已提交，但列表刷新失败；请重新打开查看"); }
    } catch (error) { reportError(error); } finally { button.disabled = !currentVersion(); }
  });
  $("rule-review-validate").addEventListener("click", async () => {
    if (!selected || validationBlocked || validationRunning) return; const id = selected.id, seq = detailSeq, button = $("rule-review-validate"); validationRunning = true; button.disabled = true;
    message("正在运行品类金标，可能需要几分钟；请勿关闭服务。");
    try {
      const result = await req(`/candidates/${encodeURIComponent(id)}/validate`, "POST");
      const prefix = seq === detailSeq ? "" : `候选 ${id.slice(0, 8)}：`;
      message(prefix + (result.passed ? "品类金标通过；仍需人工审核和完整回归" : (result.exit_code === null ? "测试中断，请联系管理员检查进程和工作区" : "品类金标未通过，请查看详情")));
      if (result.exit_code === null) setValidationBlocked(true, "blocked");
      await loadVersions(); await loadCandidates(); if (seq === detailSeq && $("rule-review-dialog").open) await loadDetails(id);
    } catch (error) {
      if (!error.stale) setValidationBlocked(true);
      reportError(error);
    } finally {
      validationRunning = false;
      button.disabled = validationBlocked || !selected || ["测试中", "已退回", "基线已变化", "测试已中断，请检查后恢复"].includes(selected.status);
      $("rule-review-decision").querySelector('[value="approve"]').disabled = validationBlocked || decisionPending || !selected || !selected.reviewable;
    }
  });
  $("rule-review-decision").addEventListener("submit", async (event) => {
    event.preventDefault(); if (!selected || decisionPending) return;
    const id = selected.id, seq = detailSeq, button = event.submitter;
    decisionPending = true;
    $("rule-review-decision").querySelectorAll("button").forEach((entry) => { entry.disabled = true; });
    try {
      const result = await req(`/candidates/${encodeURIComponent(id)}/review`, "POST", {candidate_sha256: selected.sha256, decision: button.value, reason: $("rule-review-reason-decision").value, expected_validation_id: button.value === "approve" ? selected.visible_validation_id : null});
      $("rule-review-reason-decision").value = "";
      const prefix = seq === detailSeq ? "" : `候选 ${id.slice(0, 8)}：`;
      message(prefix + (result.decision === "approve" ? "人工审核已记录，尚未发布" : "候选已退回"));
      await loadCandidates(); if (seq === detailSeq && $("rule-review-dialog").open) await loadDetails(id);
    } catch (error) { reportError(error); } finally {
      decisionPending = false;
      $("rule-review-decision").querySelector('[value="approve"]').disabled = validationBlocked || !selected || !selected.reviewable;
      $("rule-review-decision").querySelector('[value="reject"]').disabled = !selected;
    }
  });
  syncAccount();
})();

/* Local account trial. Never persist credentials, contract content or tickets in the browser. */
(() => {
  const $ = (id) => document.getElementById(id);
  let session = null, generation = 0, selection = 0, categories = [], pending = null, busy = false, timer = null;
  const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("agent-t-account-session") : null;
  const show = (id, yes) => $(id).classList.toggle("hidden", !yes);
  const notice = (text) => { $("message").textContent = text; };
  function clearPrivate() {
    generation++; selection++; clearTimeout(timer); pending = null; busy = false;
    if ($("confirm").open) $("confirm").close();
    $("upload-form").reset(); $("password").value = ""; $("history").replaceChildren(); $("items").replaceChildren();
    categories = []; $("category").replaceChildren(); $("stance").replaceChildren();
    $("detail-title").textContent = ""; $("detail-status").textContent = ""; $("detail-precheck").textContent = "";
    show("detail", false); show("upload-form", true); $("submit").disabled = true;
  }
  function setSession(value) {
    clearPrivate(); session = value && value.user && value.csrf_token ? value : null;
    const user = session && session.user;
    $("identity").textContent = user ? `${user.display_name} · ${user.role === "admin" ? "管理员" : "普通用户"}` : "";
    show("login", !user); show("workspace", !!user && !user.must_change_password); show("logout", !!user);
    notice(user && user.must_change_password ? "请先进入账户与规则管理修改临时密码。" : "");
    if (user && !user.must_change_password) { loadCategories(); loadHistory(); }
  }
  async function request(path, options = {}) {
    const snapshot = session, epoch = generation;
    const headers = {};
    if (snapshot && snapshot.csrf_token && path !== "/session") headers["X-CSRF-Token"] = snapshot.csrf_token;
    let body = options.body;
    if (body && !(body instanceof FormData)) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
    const res = await fetch("/api/local-accounts" + path, {method: options.method || "GET", body, headers, credentials:"same-origin", cache:"no-store"});
    const data = await res.json().catch(() => ({}));
    if (epoch !== generation || snapshot !== session) throw Object.assign(new Error("会话已变化"), {stale:true});
    if (!res.ok) {
      if (snapshot && (res.status === 401 || (res.status === 403 && data.detail === "请求验证失败"))) {
        setSession(null); notice("会话已失效，请重新登录。");
        throw Object.assign(new Error("会话已失效"), {stale:true});
      }
      throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请重试");
    }
    return data;
  }
  const error = (e) => { if (!e.stale) notice(e.message || "操作失败，请重试"); };
  async function loadCategories() {
    const epoch = generation;
    try {
      const response = await fetch("/api/categories", {cache:"no-store"});
      const data = await response.json();
      if (epoch !== generation) return;
      if (!response.ok || !Array.isArray(data.categories)) throw new Error("合同类型不可用，请刷新后重试。");
      categories = data.categories.filter((c) => c && /^[a-z0-9_]{1,64}$/.test(c.id) && typeof c.label === "string" && c.stances && Array.isArray(c.stances.allowed) && c.stances.allowed.length);
      $("category").replaceChildren();
      for (const c of categories) { const opt = document.createElement("option"); opt.value = c.id; opt.textContent = c.label; $("category").append(opt); }
      if (!categories.length) throw new Error("没有可用的合同类型，请检查规则基线。");
      renderStances(); $("submit").disabled = false;
    } catch (e) { if (epoch === generation) error(e); }
  }
  function renderStances() {
    const c = categories.find((item) => item.id === $("category").value);
    $("stance").replaceChildren();
    for (const value of (c ? c.stances.allowed : [])) { const opt = document.createElement("option"); opt.value = value; opt.textContent = c.stances.labels?.[value] || value; $("stance").append(opt); }
  }
  async function loadHistory() {
    try {
      const data = await request("/contracts");
      $("history").replaceChildren();
      for (const row of data.reviews || []) {
        const button = document.createElement("button"); button.type = "button"; button.className = "owned-history-item";
        button.textContent = `${row.filename || "未命名合同"} · ${row.status === "done" ? "已完成" : row.status === "error" ? "未完成" : "处理中"}`;
        button.addEventListener("click", () => openReview(row.id, button)); $("history").append(button);
      }
    } catch (e) { error(e); }
  }
  async function openReview(id, button) {
    const current = ++selection; clearTimeout(timer); show("upload-form", false); show("detail", true);
    document.querySelectorAll(".owned-history-item").forEach((b) => b.removeAttribute("aria-current"));
    if (button) button.setAttribute("aria-current", "true");
    $("items").replaceChildren(); $("detail-title").textContent = "正在加载"; $("detail-status").textContent = ""; $("detail-precheck").textContent = "";
    async function poll() {
      try {
        const data = await request("/contracts/" + encodeURIComponent(id));
        if (current !== selection) return;
        $("detail-title").textContent = data.filename || "审查结果";
        $("detail-status").textContent = data.status === "done" ? "审查已完成" : data.status === "error" ? "审查未完成，请重新提交。" : "审查进行中…";
        const pre = data.precheck;
        $("detail-precheck").textContent = pre?.performed === true ? "已执行预审" : pre?.performed === false ? `未执行预审（${pre.skip_reason || "原因未知"}）` : "预审状态未知";
        $("items").replaceChildren();
        if (data.status === "done") for (const item of (Array.isArray(data.items) ? data.items : [])) {
          const row = document.createElement("div"), title = document.createElement("strong"), detail = document.createElement("p");
          row.className = "owned-item"; title.textContent = item.name || item.id || "检查项";
          detail.textContent = [item.status, item.note, item.quote].filter((v) => typeof v === "string" && v).join(" · ");
          row.append(title, detail); $("items").append(row);
        }
        if (data.status === "processing") timer = setTimeout(poll, 2000);
      } catch (e) { if (current === selection) error(e); }
    }
    await poll();
  }
  function makePayload(snapshot, choice, token) {
    const form = new FormData();
    if (snapshot.file) form.append("file", snapshot.file); else form.append("pasted_text", snapshot.text);
    form.append("category", choice);
    if (token) form.append("confirmation_token", token); else form.append("stance", snapshot.stance);
    return form;
  }
  async function submit(snapshot, choice, ticket) {
    if (busy) return; const epoch = generation, submittingSession = session; busy = true; $("submit").disabled = true; notice("");
    try {
      const data = await request("/contracts/upload", {method:"POST", body:makePayload(snapshot, choice, ticket)});
      if (data.status === "category_confirm") {
        pending = {snapshot, token:data.confirmation_token, suggested:data.suggested_category};
        const proposed = categories.find((c) => c.id === data.suggested_category);
        const proposedLabel = proposed ? (proposed.stances.labels?.neutral || "中性（未声明）") : "";
        $("confirm-summary").textContent = (data.summary || "系统建议重新确认合同类型。") + (proposedLabel ? ` 切换类型后立场将变为“${proposedLabel}”。` : "");
        show("confirm-switch", !!proposed); if (proposed) $("confirm-switch").textContent = `切换为${proposed.label}`;
        $("confirm").showModal(); $("confirm-keep").focus(); return;
      }
      if (data.status !== "uploaded" || !data.review_id) throw new Error("上传结果无效，请刷新记录后确认。");
      $("upload-form").reset(); renderStances(); await loadHistory();
      if (epoch !== generation || submittingSession !== session) return;
      await openReview(data.review_id);
    } catch (e) { error(e); }
    finally { if (epoch === generation && submittingSession === session) { busy = false; $("submit").disabled = !categories.length; } }
  }
  $("login").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = event.currentTarget.querySelector("button"); if (button.disabled) return; button.disabled = true;
    try { const value = await request("/login", {method:"POST", body:{username:$("username").value, password:$("password").value}}); setSession(value); channel?.postMessage("changed"); }
    catch (e) { error(e); } finally { $("password").value = ""; button.disabled = false; }
  });
  $("logout").addEventListener("click", async () => { try { await request("/logout", {method:"POST"}); setSession(null); channel?.postMessage("changed"); } catch (e) { error(e); } });
  $("category").addEventListener("change", renderStances);
  $("upload-form").addEventListener("submit", (event) => {
    event.preventDefault(); const file = $("file").files[0], text = $("text").value.trim();
    if (!!file === !!text) { notice("请选择文件或粘贴正文，两者只能选择一项。"); return; }
    if (!$("category").value || !$("stance").value) { notice("请先选择有效的合同类型和立场。"); return; }
    submit({file, text, category:$("category").value, stance:$("stance").value}, $("category").value, "");
  });
  function confirm(useSuggestion) {
    if (!pending) return; const current = pending; pending = null; $("confirm").close();
    submit(current.snapshot, useSuggestion ? current.suggested : current.snapshot.category, current.token);
  }
  $("confirm-switch").addEventListener("click", () => confirm(true)); $("confirm-keep").addEventListener("click", () => confirm(false));
  $("confirm-cancel").addEventListener("click", () => $("confirm").close()); $("confirm").addEventListener("close", () => { pending = null; });
  $("new-review").addEventListener("click", () => { selection++; clearTimeout(timer); show("detail", false); show("upload-form", true); notice(""); });
  if (channel) channel.onmessage = () => { setSession(null); notice("账户状态已在其他页面变化，请重新登录。"); };
  request("/session").then(setSession).catch((e) => { if (!e.stale) setSession(null); })
    .finally(() => { $("login").querySelector("button").disabled = false; });
})();

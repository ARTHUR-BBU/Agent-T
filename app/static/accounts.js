/* Explicit local account experiment. It does not protect contract routes. */
(() => {
  const $ = (id) => document.getElementById(id);
  const api = "/api/local-accounts";
  let session = null;
  const accountChannel = typeof BroadcastChannel === "function" ? new BroadcastChannel("agent-t-account-session") : null;

  async function request(path, method = "GET", body = undefined) {
    const headers = {};
    const requestingSession = session;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (session && session.csrf_token && path !== "/session") headers["X-CSRF-Token"] = session.csrf_token;
    const res = await fetch(api + path, {
      method, headers, credentials: "same-origin", cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (path !== "/login" && requestingSession && session === requestingSession && (res.status === 401 ||
        (res.status === 403 && data.detail === "请求验证失败"))) {
      session = null;
      clearPasswords();
      renderSession();
      accountChannel?.postMessage("changed");
    }
    if (!res.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail : "操作失败，请检查输入");
      error.stale = requestingSession !== session && session !== null;
      throw error;
    }
    if (path !== "/login" && requestingSession !== session) {
      const error = new Error("会话已变化"); error.stale = true; throw error;
    }
    return data;
  }

  function message(value) { $("account-message").textContent = value; }
  function messageError(error) { if (!error.stale) message(error.message); }
  function show(id, visible) { $(id).classList.toggle("hidden", !visible); }
  function clearPasswords() {
    $("account-dialog").querySelectorAll('input[type="password"]').forEach((field) => { field.value = ""; });
  }
  function renderSession() {
    const user = session && session.user;
    show("account-login-form", !user);
    show("account-session", !!user);
    show("account-restricted", !!user && user.must_change_password);
    show("account-admin", !!user && user.role === "admin" && !user.must_change_password);
    $("account-identity").textContent = user ? `${user.display_name} · ${user.role === "admin" ? "管理员" : "普通用户"}` : "";
    if (!user || user.role !== "admin" || user.must_change_password) {
      $("account-users").replaceChildren(); $("account-reset-user").replaceChildren();
    }
    window.dispatchEvent(new CustomEvent("agent-local-account", {detail: {user: user || null}}));
    if (user && user.role === "admin" && !user.must_change_password) loadUsers().catch(messageError);
  }

  async function loadUsers() {
    const requestingSession = session;
    const data = await request("/users");
    if (!session || session !== requestingSession || session.user.role !== "admin" || session.user.must_change_password) return;
    const list = $("account-users"), select = $("account-reset-user");
    list.replaceChildren(); select.replaceChildren();
    for (const user of data.users) {
      const row = document.createElement("div"); row.className = "account-user-row";
      const info = document.createElement("div"), name = document.createElement("strong"), meta = document.createElement("div");
      name.textContent = user.display_name;
      meta.className = "account-user-meta";
      meta.textContent = `${user.username} · ${user.role === "admin" ? "管理员" : "普通用户"} · ${user.active ? "已启用" : "已停用"}${user.must_change_password ? " · 待修改临时密码" : ""}`;
      info.append(name, meta);
      const actions = document.createElement("div"); actions.className = "account-user-actions";
      const toggle = document.createElement("button"); toggle.type = "button"; toggle.textContent = user.active ? "停用" : "启用";
      toggle.addEventListener("click", async () => {
        toggle.disabled = true;
        try { await request(`/users/${encodeURIComponent(user.id)}/active`, "POST", {active: !user.active}); message("账户状态已更新"); await loadUsers(); }
        catch (error) { messageError(error); toggle.disabled = false; }
      });
      const role = document.createElement("button"); role.type = "button"; role.textContent = user.role === "admin" ? "设为普通用户" : "设为管理员";
      role.addEventListener("click", async () => {
        role.disabled = true;
        try { await request(`/users/${encodeURIComponent(user.id)}/role`, "POST", {role: user.role === "admin" ? "user" : "admin"}); message("账户角色已更新"); await loadUsers(); }
        catch (error) { messageError(error); role.disabled = false; }
      });
      actions.append(toggle, role); row.append(info, actions); list.append(row);
      if (session && user.id !== session.user.id) {
        const option = document.createElement("option"); option.value = user.id; option.textContent = `${user.display_name}（${user.username}）`; select.append(option);
      }
    }
    if (!data.users.length) list.textContent = "暂无账户";
  }

  $("account-close").addEventListener("click", () => $("account-dialog").close());
  $("account-dialog").addEventListener("close", () => {
    clearPasswords();
  });
  $("btn-local-accounts").addEventListener("click", async () => {
    message(""); $("account-dialog").showModal();
    const startingSession = session;
    try { const current = await request("/session"); if (session === startingSession) session = current; }
    catch (_) { if (session === startingSession) session = null; }
    renderSession();
  });
  $("account-login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("account-login-form").querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    try {
      session = await request("/login", "POST", {username: $("account-login-username").value, password: $("account-login-password").value});
      message(""); renderSession(); accountChannel?.postMessage("changed");
    } catch (error) { messageError(error); }
    finally { $("account-login-password").value = ""; button.disabled = false; }
  });
  $("account-password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submittingSession = session;
    try {
      await request("/password", "POST", {current_password: $("account-current-password").value, new_password: $("account-new-password").value});
      if (session !== submittingSession) return;
      session = null; renderSession(); accountChannel?.postMessage("changed"); message("密码已修改，请重新登录");
    } catch (error) { messageError(error); }
    finally { $("account-password-form").reset(); }
  });
  $("account-logout").addEventListener("click", async () => {
    const submittingSession = session;
    try { await request("/logout", "POST"); if (session !== submittingSession) return; session = null; clearPasswords(); renderSession(); accountChannel?.postMessage("changed"); message("已退出登录"); }
    catch (error) { messageError(error); }
  });
  $("account-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await request("/users", "POST", {username: $("account-create-username").value, display_name: $("account-create-name").value, password: $("account-create-password").value, role: $("account-create-role").value});
      $("account-create-form").reset(); message("账户已创建，请安全地将临时密码交给使用者"); await loadUsers();
    } catch (error) { messageError(error); }
    finally { $("account-create-password").value = ""; }
  });
  $("account-reset-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await request(`/users/${encodeURIComponent($("account-reset-user").value)}/password`, "POST", {new_password: $("account-reset-password").value, admin_password: $("account-reset-admin-password").value});
      message("临时密码已重置，请安全地交给使用者"); await loadUsers();
    } catch (error) { messageError(error); }
    finally { $("account-reset-password").value = ""; $("account-reset-admin-password").value = ""; }
  });
  window.AgentLocalAccounts = {request, user: () => session && session.user};
  request("/capability").then((result) => show("btn-local-accounts", result.available === true)).catch(() => {});
})();

(() => {
  const state = {
    reviewId: null,
    review: null,
    askItem: null,
  };

  const $ = (id) => document.getElementById(id);
  const screens = {
    upload: $("screen-upload"),
    results: $("screen-results"),
    ask: $("screen-ask"),
  };

  function show(name) {
    Object.entries(screens).forEach(([k, el]) => {
      el.classList.toggle("hidden", k !== name);
    });
  }

  function statusClass(status) {
    if (status === "通过") return "pass";
    if (status === "需关注") return "attention";
    if (status === "本类不适用") return "na";
    return "notfound";
  }

  function statusLabel(status) {
    if (status === "通过") return "已通过";
    return status;
  }

  async function upload() {
    const fileInput = $("file");
    const err = $("upload-error");
    err.classList.add("hidden");
    if (!fileInput.files || !fileInput.files[0]) {
      err.textContent = "请先选择合同文件。";
      err.classList.remove("hidden");
      return;
    }
    const btn = $("btn-upload");
    btn.disabled = true;
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("category", $("category").value);
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "上传失败");
      state.reviewId = data.review_id;
      show("results");
      $("results-loading").classList.remove("hidden");
      $("results-body").classList.add("hidden");
      $("results-error").classList.add("hidden");
      await pollReview();
    } catch (e) {
      err.textContent = e.message || String(e);
      err.classList.remove("hidden");
    } finally {
      btn.disabled = false;
    }
  }

  async function pollReview() {
    const loading = $("results-loading");
    const body = $("results-body");
    const errBox = $("results-error");
    for (let i = 0; i < 60; i++) {
      const res = await fetch(`/api/review/${state.reviewId}`);
      const data = await res.json();
      if (!res.ok) {
        loading.classList.add("hidden");
        errBox.textContent = data.detail || "获取结果失败";
        errBox.classList.remove("hidden");
        return;
      }
      state.review = data;
      if (data.status === "processing" || data.status === "pending") {
        await new Promise((r) => setTimeout(r, 400));
        continue;
      }
      loading.classList.add("hidden");
      if (data.status === "error") {
        errBox.textContent = data.error || "审查失败";
        errBox.classList.remove("hidden");
        return;
      }
      renderResults(data);
      body.classList.remove("hidden");
      return;
    }
    loading.classList.add("hidden");
    errBox.textContent = "审查超时，请重试。";
    errBox.classList.remove("hidden");
  }

  function renderResults(data) {
    $("results-meta").innerHTML =
      `<strong>${escapeHtml(data.filename || "")}</strong>` +
      ` · ${escapeHtml(data.category_label || data.category || "")}`;
    const list = $("item-list");
    list.innerHTML = "";
    (data.items || []).forEach((item) => {
      const li = document.createElement("li");
      li.className = `item ${statusClass(item.status)}`;
      const head = document.createElement("div");
      head.className = "item-head";
      head.innerHTML =
        `<span class="item-name">${escapeHtml(item.name)}</span>` +
        `<span class="tag ${statusClass(item.status)}">${escapeHtml(statusLabel(item.status))}</span>`;
      li.appendChild(head);
      if (item.note) {
        const note = document.createElement("p");
        note.className = "item-note";
        note.textContent = item.note;
        li.appendChild(note);
      }
      if (item.quote) {
        const q = document.createElement("div");
        q.className = "item-quote";
        q.textContent = item.quote;
        li.appendChild(q);
      }
      if (item.status === "需关注") {
        const a = document.createElement("button");
        a.type = "button";
        a.className = "link ask-link";
        a.textContent = "问清楚一点 →";
        a.addEventListener("click", () => openAsk(item));
        li.appendChild(a);
      }
      list.appendChild(li);
    });
  }

  function openAsk(item) {
    state.askItem = item;
    $("ask-subtitle").textContent = `← ${item.name} · 需关注`;
    $("ask-context").textContent = item.note || "";
    $("ask-input").value = "";
    $("ask-error").classList.add("hidden");
    $("ask-answer").classList.add("hidden");
    $("ask-answer").innerHTML = "";
    show("ask");
  }

  async function sendAsk() {
    const err = $("ask-error");
    const ans = $("ask-answer");
    err.classList.add("hidden");
    ans.classList.add("hidden");
    const question = $("ask-input").value.trim();
    if (!question) {
      err.textContent = "请输入问题。";
      err.classList.remove("hidden");
      return;
    }
    const btn = $("btn-ask");
    btn.disabled = true;
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          review_id: state.reviewId,
          item_id: state.askItem.id,
          question,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "请求失败");
      if (!data.ok) {
        err.textContent = data.error || "追问失败";
        err.classList.remove("hidden");
        return;
      }
      renderAnswer(data.answer || {}, data.raw_text);
      ans.classList.remove("hidden");
    } catch (e) {
      err.textContent = e.message || String(e);
      err.classList.remove("hidden");
    } finally {
      btn.disabled = false;
    }
  }

  function renderAnswer(answer, raw) {
    const box = $("ask-answer");
    const fields = ["风险等级", "这条在查啥", "原文在哪", "问题是啥", "建议怎么改", "还想问"];
    let html = "<dl>";
    let any = false;
    fields.forEach((k) => {
      if (answer && answer[k]) {
        any = true;
        html += `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(answer[k]))}</dd>`;
      }
    });
    html += "</dl>";
    if (!any && raw) {
      html = `<pre style="white-space:pre-wrap">${escapeHtml(raw)}</pre>`;
    }
    box.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  $("btn-upload").addEventListener("click", upload);
  $("btn-ask").addEventListener("click", sendAsk);
  $("btn-back-upload").addEventListener("click", () => show("upload"));
  $("btn-back-results").addEventListener("click", () => show("results"));
})();

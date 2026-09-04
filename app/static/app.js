(() => {
  const state = {
    reviewId: null,
    review: null,
    askItem: null,
    selectedItemId: null,
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
    if (status === "本类不适用") return "不适用";
    return status;
  }

  /** Prefer rule hit strings from checklist; fall back to substrings present in quote. */
  function keywordsForHighlight(hits, quote) {
    if (!quote) return [];
    const found = [];
    const seen = new Set();
    (hits || []).forEach((kw) => {
      const t = (kw || "").trim();
      if (!t || seen.has(t)) return;
      // Hit may be regex match spanning punctuation; still try direct include
      if (quote.includes(t)) {
        seen.add(t);
        found.push(t);
        return;
      }
      // If hit longer than quote window, try sliding substrings of 2..12 chars present in both
      if (t.length > 12) {
        for (let len = Math.min(12, t.length); len >= 2; len--) {
          for (let i = 0; i + len <= t.length; i++) {
            const sub = t.slice(i, i + len);
            if (quote.includes(sub) && !seen.has(sub) && /[\u4e00-\u9fff]/.test(sub)) {
              seen.add(sub);
              found.push(sub);
            }
          }
        }
      }
    });
    found.sort((a, b) => b.length - a.length);
    return found;
  }

  function highlightQuoteHtml(quote, hits) {
    if (!quote) {
      return `<span class="empty">暂无原文摘句</span>`;
    }
    const kws = keywordsForHighlight(hits || [], quote);
    if (!kws.length) return escapeHtml(quote);

    // Build a simple non-overlapping highlighter
    const ranges = [];
    kws.forEach((kw) => {
      let from = 0;
      while (from < quote.length) {
        const idx = quote.indexOf(kw, from);
        if (idx < 0) break;
        const end = idx + kw.length;
        const overlaps = ranges.some((r) => !(end <= r[0] || idx >= r[1]));
        if (!overlaps) ranges.push([idx, end]);
        from = end;
      }
    });
    ranges.sort((a, b) => a[0] - b[0]);

    let out = "";
    let cursor = 0;
    ranges.forEach(([s, e]) => {
      out += escapeHtml(quote.slice(cursor, s));
      out += `<mark class="kw">${escapeHtml(quote.slice(s, e))}</mark>`;
      cursor = e;
    });
    out += escapeHtml(quote.slice(cursor));
    return out;
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
      state.selectedItemId = null;
      show("results");
      $("results-loading").classList.remove("hidden");
      $("results-body").classList.add("hidden");
      $("results-error").classList.add("hidden");
      $("item-detail").classList.add("hidden");
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
    const items = data.items || [];
    const nAtt = items.filter((i) => i.status === "需关注").length;
    const nPass = items.filter((i) => i.status === "通过").length;
    $("results-meta").innerHTML =
      `<strong>${escapeHtml(data.filename || "")}</strong>` +
      ` · ${escapeHtml(data.category_label || data.category || "")}` +
      `<br/>需关注 ${nAtt} 项 · 已通过 ${nPass} 项`;
    const list = $("item-list");
    list.innerHTML = "";
    state.selectedItemId = null;
    $("item-detail").classList.add("hidden");

    items.forEach((item) => {
      const li = document.createElement("li");
      li.className = `item ${statusClass(item.status)}`;
      li.dataset.itemId = item.id;
      li.setAttribute("role", "button");
      li.tabIndex = 0;

      const head = document.createElement("div");
      head.className = "item-head";
      head.innerHTML =
        `<span class="item-name">${escapeHtml(item.name)}</span>` +
        `<span class="tag ${statusClass(item.status)}">${escapeHtml(statusLabel(item.status))}</span>`;
      li.appendChild(head);

      li.addEventListener("click", () => selectItem(item));
      li.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectItem(item);
        }
      });
      list.appendChild(li);
    });
  }

  function selectItem(item) {
    state.selectedItemId = item.id;
    document.querySelectorAll(".item").forEach((el) => {
      el.classList.toggle("selected", el.dataset.itemId === item.id);
    });

    const detail = $("item-detail");
    const noteEl = $("detail-note");
    const quoteEl = $("detail-quote");
    const actions = $("detail-actions");

    noteEl.textContent = item.note || "（无说明）";
    if (item.quote) {
      quoteEl.classList.remove("empty");
      quoteEl.innerHTML = highlightQuoteHtml(item.quote, item.hits || []);
    } else {
      quoteEl.classList.add("empty");
      quoteEl.textContent = "暂无原文摘句";
    }

    if (item.status === "需关注") {
      actions.classList.remove("hidden");
      $("detail-ask").onclick = () => openAsk(item);
    } else {
      actions.classList.add("hidden");
      $("detail-ask").onclick = null;
    }

    detail.classList.remove("hidden");
    detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function openAsk(item) {
    state.askItem = item;
    $("ask-subtitle").textContent = `${item.name} · 需关注`;
    $("ask-context").textContent = item.note || "";
    $("ask-input").value = "";
    $("ask-error").classList.add("hidden");
    $("ask-answer").classList.add("hidden");
    $("ask-answer").innerHTML = "";

    const quoteBox = $("ask-quote");
    if (item.quote) {
      quoteBox.classList.remove("empty");
      quoteBox.innerHTML = highlightQuoteHtml(item.quote, item.hits || []);
    } else {
      quoteBox.classList.add("empty");
      quoteBox.textContent = "暂无原文摘句";
    }

    const available = !!(state.review && state.review.ask_available);
    const banner = $("ask-unavailable");
    const btn = $("btn-ask");
    const input = $("ask-input");
    if (available) {
      banner.classList.add("hidden");
      btn.disabled = false;
      input.disabled = false;
    } else {
      banner.classList.remove("hidden");
      banner.textContent = "追问暂未开通";
      btn.disabled = true;
      input.disabled = true;
    }

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
        err.textContent = data.error || "追问暂未开通";
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
    const explain = [answer && answer["问题是啥"], answer && answer["这条在查啥"], answer && answer["风险等级"]]
      .filter(Boolean)
      .map(String)
      .join("\n");
    const rewrite = (answer && answer["建议怎么改"]) || "";
    const extra = [answer && answer["原文在哪"], answer && answer["还想问"]].filter(Boolean).map(String).join("\n");
    let html = "";
    html += `<p class="detail-label">人话解释</p>`;
    if (explain) {
      html += `<div class="detail-note">${escapeHtml(explain)}</div>`;
    } else if (raw) {
      html += `<pre style="white-space:pre-wrap">${escapeHtml(raw)}</pre>`;
    } else {
      html += `<div class="detail-note muted">暂无</div>`;
    }
    html += `<p class="detail-label">建议改法</p>`;
    html += `<div class="detail-note">${escapeHtml(rewrite || "暂无")}</div>`;
    if (extra) {
      html += `<p class="detail-label">补充</p><div class="detail-note">${escapeHtml(extra)}</div>`;
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

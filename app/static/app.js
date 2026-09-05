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
    if (!kws.length) {
      return (
        `<span class="quote-miss muted">未在原文定位到关键词</span>` +
        `<div class="quote-plain">${escapeHtml(quote)}</div>`
      );
    }

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
    const blinds = (data.blind_enabled && (data.blind_candidates || []).length)
      ? (data.blind_candidates || [])
      : [];
    const nAtt = items.filter((i) => i.status === "需关注").length;
    const nPass = items.filter((i) => i.status === "通过").length;
    const nBlind = blinds.length;
    let metaExtra = `需关注 ${nAtt} 项 · 已通过 ${nPass} 项`;
    if (nBlind > 0) {
      metaExtra += ` · 补盲候选 ${nBlind} 项`;
    }
    $("results-meta").innerHTML =
      `<strong>${escapeHtml(data.filename || "")}</strong>` +
      ` · ${escapeHtml(data.category_label || data.category || "")}` +
      `<br/>${metaExtra}`;
    renderScorecard(data);
    const list = $("item-list");
    list.innerHTML = "";
    state.selectedItemId = null;
    $("item-detail").classList.add("hidden");

    items.forEach((item) => {
      list.appendChild(buildItemRow(item, { blind: false }));
    });

    blinds.forEach((item) => {
      list.appendChild(buildItemRow(item, { blind: true }));
    });

    // Surface skip messages (e.g. 缺少原文依据) without fake 需关注 rows
    let skipEl = document.getElementById("blind-skip-note");
    if (!skipEl) {
      skipEl = document.createElement("p");
      skipEl.id = "blind-skip-note";
      skipEl.className = "blind-skip-note hidden";
      list.parentNode.insertBefore(skipEl, list.nextSibling);
    }
    const skips = (data.blind_enabled && (data.blind_skipped_messages || []).length)
      ? data.blind_skipped_messages
      : [];
    if (skips.length) {
      skipEl.textContent = skips.join("；");
      skipEl.classList.remove("hidden");
    } else {
      skipEl.textContent = "";
      skipEl.classList.add("hidden");
    }
  }

  /** M3.5 评分卡：参考层。不可用时整卡不渲染（无 Key 在 meta 加一行灰字）。 */
  function renderScorecard(data) {
    const card = $("score-card");
    const sc = data.scorecard || {};
    if (!sc.available || typeof sc.total !== "number") {
      card.classList.add("hidden");
      card.removeAttribute("data-reason");
      if (sc.reason === "no_llm_key") {
        const note = document.createElement("span");
        note.className = "score-unavailable";
        note.textContent = " · 评分暂未开通（其余逐条结果不受影响）";
        $("results-meta").appendChild(note);
      }
      return;
    }
    card.classList.remove("hidden");
    $("score-total").textContent = String(sc.total);
    const tier = sc.tier || {};
    $("score-grade").textContent = [tier.label, tier.hint].filter(Boolean).join("：");
    $("score-summary").textContent = sc.summary || "";
    $("score-disclaimer").textContent =
      sc.disclaimer || "模型评分仅供参考，以逐条规则结论为准";
    const capsEl = $("score-caps");
    const caps = sc.caps_applied || [];
    if (caps.length) {
      capsEl.textContent = caps.join("；");
      capsEl.classList.remove("hidden");
    } else {
      capsEl.textContent = "";
      capsEl.classList.add("hidden");
    }
    const breakdown = $("score-breakdown");
    breakdown.innerHTML = "";
    (sc.segments || []).forEach((seg) => {
      const li = document.createElement("li");
      li.className = "score-seg";
      if (seg.na) {
        li.innerHTML =
          `<span class="score-seg-name">${escapeHtml(seg.name)}</span>` +
          `<span class="score-seg-score">—</span>` +
          `<span class="score-seg-note">本类不适用</span>`;
      } else {
        li.innerHTML =
          `<span class="score-seg-name">${escapeHtml(seg.name)}</span>` +
          `<span class="score-seg-score">${seg.score}/${seg.weight}</span>` +
          `<span class="score-seg-note">${escapeHtml(seg.comment || "")}</span>`;
      }
      breakdown.appendChild(li);
    });
  }

  function buildItemRow(item, opts) {
    const isBlind = !!(opts && opts.blind);
    const li = document.createElement("li");
    li.className = isBlind
      ? "item blind-candidate"
      : `item ${statusClass(item.status)}`;
    li.dataset.itemId = isBlind ? `blind:${item.id}` : item.id;
    li.dataset.blind = isBlind ? "1" : "0";
    li.setAttribute("role", "button");
    li.tabIndex = 0;

    const head = document.createElement("div");
    head.className = "item-head";

    const nameSpan = document.createElement("span");
    nameSpan.className = "item-name";
    nameSpan.textContent = item.name || "";

    const badges = document.createElement("span");
    badges.className = "item-badges";

    const statusTag = document.createElement("span");
    statusTag.className = `tag ${statusClass(item.status)}`;
    statusTag.textContent = statusLabel(item.status);
    badges.appendChild(statusTag);

    if (!isBlind && item.status === "需关注") {
      const ruleBadge = document.createElement("span");
      ruleBadge.className = "source-badge rule";
      ruleBadge.textContent = "规则";
      badges.appendChild(ruleBadge);
    }
    if (isBlind) {
      const blindBadge = document.createElement("span");
      blindBadge.className = "source-badge blind";
      blindBadge.textContent = "补盲";
      badges.appendChild(blindBadge);
    }

    head.appendChild(nameSpan);
    head.appendChild(badges);
    li.appendChild(head);

    if (isBlind) {
      const cap = document.createElement("div");
      cap.className = "item-caption";
      // M3.5：评分卡点名的候选走文案区分，不加第三种颜色（阳仔决策）
      cap.textContent = item.named_by_scorecard
        ? "候选，需人工确认 · 来自评分卡点名"
        : "候选，需人工确认";
      if (item.named_by_scorecard) {
        li.dataset.scored = "1";
      }
      li.appendChild(cap);
    }

    const viewItem = { ...item, _blind: isBlind };
    li.addEventListener("click", () => selectItem(viewItem));
    li.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        selectItem(viewItem);
      }
    });
    return li;
  }

  function selectItem(item) {
    const selKey = item._blind ? `blind:${item.id}` : item.id;
    state.selectedItemId = selKey;
    document.querySelectorAll(".item").forEach((el) => {
      el.classList.toggle("selected", el.dataset.itemId === selKey);
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

    // Blind candidates need human confirm; do not offer rule-style ask as if stamped
    if (item.status === "需关注" && !item._blind) {
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
    const rewriteDraft = (answer && answer["改写稿"]) || "";
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
    if (rewriteDraft) {
      // M3.5 建议改写稿：可粘贴，但必须对照原文核对（阳仔交互 + 九哥文案）
      html += `<div class="rewrite-block">`;
      html += `<p class="detail-label">建议改写稿</p>`;
      html += `<div id="ask-rewrite-text" class="rewrite-text detail-quote">${escapeHtml(rewriteDraft)}</div>`;
      html += `<div class="rewrite-actions">`;
      html += `<button type="button" class="rewrite-copy" id="btn-copy-rewrite">复制改写稿</button>`;
      html += `<span class="rewrite-provenance">改写自上方原文摘句</span>`;
      html += `</div>`;
      html += `<p class="rewrite-hint">复制前先对照原文核一遍（确认意思没跑偏）</p>`;
      html += `</div>`;
    }
    if (extra) {
      html += `<p class="detail-label">补充</p><div class="detail-note">${escapeHtml(extra)}</div>`;
    }
    box.innerHTML = html;

    const copyBtn = $("btn-copy-rewrite");
    if (copyBtn) {
      copyBtn.addEventListener("click", () => copyRewrite(copyBtn, rewriteDraft));
    }
  }

  async function copyRewrite(btn, text) {
    let copied = false;
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (e) {
      // 兼容非安全上下文（如局域网 http 访问）
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        copied = document.execCommand("copy");
      } catch (e2) {
        copied = false;
      }
      document.body.removeChild(ta);
    }
    const original = "复制改写稿";
    btn.textContent = copied ? "已复制 · 请核对后使用" : "复制失败，请手动选择复制";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = original;
      btn.classList.remove("copied");
    }, 2000);
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

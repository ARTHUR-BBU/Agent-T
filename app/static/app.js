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

  function categoryLabel(id) {
    const opt = [...$("category").options].find((o) => o.value === id);
    return opt ? opt.textContent : id;
  }

  function hidePrecheckConfirm() {
    $("precheck-confirm").classList.add("hidden");
  }

  // LLM 预审确认（分类≠裁判：AI 只建议，用户拍板；静默切换是事故温床）
  function showPrecheckConfirm(data) {
    const box = $("precheck-confirm");
    const pc = data.precheck || {};
    const detected = pc.detected_type || "其他类型合同";
    $("precheck-summary").textContent = pc.summary
      ? `AI 预判这是一份「${detected}」（${pc.summary}）。`
      : `AI 预判这是一份「${detected}」。`;
    if (data.suggested_category) {
      $("precheck-question").textContent =
        `与您选择的「${categoryLabel($("category").value)}」不一致。` +
        "用哪套清单审查，决定提示是否切题。";
      $("btn-precheck-switch").textContent = `切换为${categoryLabel(data.suggested_category)}`;
      $("btn-precheck-switch").classList.remove("hidden");
      $("btn-precheck-keep").textContent = "按原类型继续";
    } else {
      // 支持列表由后端数据渲染（肉饼门禁 P3-2：硬编码在未来加品类时必漂移）
      const supported = (data.supported_categories || [])
        .map((c) => c.label)
        .filter(Boolean)
        .join("、");
      $("precheck-question").textContent =
        `该类型暂不在支持范围内（当前支持：${supported || "租赁合同、采购合同、保密协议 NDA"}），` +
        "因此本次未生成审查报告。未审查不等于没有风险，" +
        "签署前请自行仔细核对，必要时咨询专业律师。";
      $("btn-precheck-switch").classList.add("hidden");
      // 小智娘门禁 P3-2：无路可走时「按原类型继续」是误导，改文案只收起弹窗
      $("btn-precheck-keep").textContent = "重新选择类型";
    }
    box.dataset.suggested = data.suggested_category || "";
    box.classList.remove("hidden");
  }

  async function upload(categoryOverride, force = false) {
    const fileInput = $("file");
    const err = $("upload-error");
    err.classList.add("hidden");
    hidePrecheckConfirm();
    if (!fileInput.files || !fileInput.files[0]) {
      err.textContent = "请先选择合同文件。";
      err.classList.remove("hidden");
      return;
    }
    const btn = $("btn-upload");
    btn.disabled = true;
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("category", categoryOverride || $("category").value);
    // 确认弹窗里的拍板必须带 force（小智娘门禁 P1）：
    // 否则重传会重跑预审，LLM 持续不同意 = 用户永远开不了审
    if (force) fd.append("force", "1");
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "上传失败");
      if (data.status === "category_confirm") {
        showPrecheckConfirm(data);
        return;
      }
      state.reviewId = data.review_id;
      state.selectedItemId = null;
      setReviewHash(data.review_id);
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
    // 审查已改为后台任务（上传秒回 review_id）：大合同含模型评分约 1-2 分钟，
    // 轮询预算给足 5 分钟（150 × 2s），done/error 提前退出
    for (let i = 0; i < 150; i++) {
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
        await new Promise((r) => setTimeout(r, 2000));
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
    // 品类存疑非阻断提示（老钱金标：知情权不能省，打断权必须不给）
    const suspectBox = $("precheck-suspect");
    if (data.precheck && data.precheck.suspect) {
      suspectBox.textContent =
        `品类存疑：本报告按「${data.category_label || data.category}」清单审查，` +
        `AI 预判倾向「${data.precheck.detected_type || "其他类型"}」（把握较低）。` +
        "结论请结合文件实际类型阅读。";
      suspectBox.classList.remove("hidden");
    } else {
      suspectBox.classList.add("hidden");
    }
    $("results-meta").innerHTML =
      `<strong>${escapeHtml(data.filename || "")}</strong>` +
      ` · ${escapeHtml(data.category_label || data.category || "")}` +
      `<br/>${metaExtra}`;

    // M4：审查完成后开放报告导出（GET /api/review/{id}/report）
    if (data.id) {
      $("btn-export-report").href = `/api/review/${data.id}/report`;
      $("report-actions").classList.remove("hidden");
    }
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

  // 审查记录进 URL（阳仔 UI 提案：reviewId 只存内存 = 刷新即丢 1-2 分钟的等待，
  // 事故级体验）。刷新/重开页面时从 hash 恢复轮询。
  function setReviewHash(rid) {
    try {
      window.location.hash = "#/review/" + rid;
    } catch (e) { /* 隐私模式等场景忽略 */ }
  }

  function clearReviewHash() {
    try {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    } catch (e) { /* ignore */ }
  }

  function reviewIdFromHash() {
    const m = (window.location.hash || "").match(/^#\/review\/([A-Za-z0-9]+)/);
    return m ? m[1] : null;
  }

  async function resumeFromHash() {
    const rid = reviewIdFromHash();
    if (!rid) return;
    state.reviewId = rid;
    state.selectedItemId = null;
    show("results");
    hidePrecheckConfirm();
    $("results-loading").classList.remove("hidden");
    $("results-body").classList.add("hidden");
    $("results-error").classList.add("hidden");
    $("item-detail").classList.add("hidden");
    await pollReview();
  }

  $("btn-upload").addEventListener("click", () => upload());
  $("btn-precheck-switch").addEventListener("click", () => {
    const sug = $("precheck-confirm").dataset.suggested;
    if (sug) {
      $("category").value = sug;
      upload(sug, true); // 用户已拍板，带 force 防预审重跑死循环
    }
  });
  $("btn-precheck-keep").addEventListener("click", () => {
    const sug = $("precheck-confirm").dataset.suggested;
    hidePrecheckConfirm();
    if (sug) upload(undefined, true); // 坚持按原品类审：已确认，force 继续
    // 无 sug（不支持类）：仅收起弹窗，用户回表单重新选择类型
  });
  $("btn-ask").addEventListener("click", sendAsk);
  $("btn-back-upload").addEventListener("click", () => {
    clearReviewHash();
    show("upload");
  });
  $("btn-back-results").addEventListener("click", () => show("results"));

  resumeFromHash();
})();

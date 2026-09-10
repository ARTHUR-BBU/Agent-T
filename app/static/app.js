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
    // 阶段 1.4⑤ dialog 化：开闭走 showModal/close，不用 hidden 类
    const dlg = $("precheck-dialog");
    if (dlg && dlg.open) dlg.close();
  }

  // LLM 预审确认（分类≠裁判：AI 只建议，用户拍板；静默切换是事故温床）。
  // Esc / 点遮罩 = 放弃本次上传（确认类弹窗的默认逃生门必须是「不继续」）
  function showPrecheckConfirm(data) {
    const dlg = $("precheck-dialog");
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
    dlg.dataset.suggested = data.suggested_category || "";
    dlg.showModal();
    // 焦点给非破坏按钮（阳仔：回车党默认动作不该是改品类）
    $("btn-precheck-keep").focus();
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
    $("stance-seg").disabled = true; // 上传进行中立场锁定（阳仔线框）
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("category", categoryOverride || $("category").value);
    // 阶段 1.3：立场随请求上报（默认 neutral）
    fd.append("stance", selectedStance());
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
      state.stageIdx = 0;
      setReviewHash(data.review_id);
      show("results");
      $("results-loading").classList.remove("hidden");
      $("results-body").classList.add("hidden");
      $("results-error").classList.add("hidden");
      $("item-detail").classList.add("hidden");
      resetStages();
      await pollReview();
    } catch (e) {
      err.textContent = e.message || String(e);
      err.classList.remove("hidden");
    } finally {
      btn.disabled = false;
      $("stance-seg").disabled = false;
    }
  }

  // ===== 阶段 0.2 等待页三段真实进度（阳仔线框）=====
  // 诚实进度：只点亮后端真实到达的段位，无百分比无倒计时
  const STAGE_ORDER = ["triage", "scanning", "scoring"];
  const STAGE_BADGE = { pending: "等待中", active: "进行中", done: "已完成", error: "未能完成" };

  function resetStages() {
    state.stageIdx = 0;
    document.querySelectorAll("#stage-list .stage").forEach((li) => {
      li.classList.remove("is-active", "is-done", "is-error");
      li.querySelector(".stage-badge").textContent = STAGE_BADGE.pending;
    });
    const legacy = $("stage-legacy");
    if ($("stage-list")) $("stage-list").classList.remove("hidden");
    if (legacy) legacy.classList.add("hidden");
  }

  function updateStages(stage) {
    const list = $("stage-list");
    const legacy = $("stage-legacy");
    if (!list) return;
    if (!stage) {
      // 旧记录无 stage 字段：退化单行文案（兼容不空转）
      list.classList.add("hidden");
      if (legacy) legacy.classList.remove("hidden");
      return;
    }
    list.classList.remove("hidden");
    if (legacy) legacy.classList.add("hidden");
    const idx = STAGE_ORDER.indexOf(stage);
    if (idx < 0) return;
    // 只前进不回退：stage 乱序/重复到达时取 max
    state.stageIdx = Math.max(state.stageIdx || 0, idx);
    document.querySelectorAll("#stage-list .stage").forEach((li, i) => {
      const badge = li.querySelector(".stage-badge");
      li.classList.toggle("is-done", i < state.stageIdx);
      li.classList.toggle("is-active", i === state.stageIdx);
      if (!badge) return;
      if (i < state.stageIdx) badge.textContent = STAGE_BADGE.done;
      else if (i === state.stageIdx) badge.textContent = STAGE_BADGE.active;
      else badge.textContent = STAGE_BADGE.pending;
    });
  }

  function markStageError() {
    // 失败段置 ✕；其后 pending 段保持「等待中」不动——stage 清单答「走到哪」，
    // #results-error 卡答「为什么停」+ 下一步指引（职责划分，阳仔）
    const idx = state.stageIdx || 0;
    document.querySelectorAll("#stage-list .stage").forEach((li, i) => {
      if (i !== idx) return;
      li.classList.remove("is-active");
      li.classList.add("is-error");
      const badge = li.querySelector(".stage-badge");
      if (badge) badge.textContent = STAGE_BADGE.error;
    });
  }

  async function pollReview() {
    const loading = $("results-loading");
    const body = $("results-body");
    const errBox = $("results-error");
    // 快照 reviewId（小智娘复验 P3）：用户中途 Back 走人后，睡醒的循环不得
    // 拿着失效 rid 去 fetch/清 hash，干扰用户刚导航到的新审查
    const rid = state.reviewId;
    // 审查已改为后台任务（上传秒回 review_id）：阶段 1.2 起长合同走分段阅读
    // （最多 4 块 × 每块 ~1 分钟量级），轮询预算放宽到 10 分钟（300 × 2s），
    // done/error 提前退出
    for (let i = 0; i < 300; i++) {
      if (state.reviewId !== rid) return;
      const res = await fetch(`/api/review/${rid}`);
      const data = await res.json();
      if (state.reviewId !== rid) return;
      if (!res.ok) {
        loading.classList.add("hidden");
        errBox.textContent = data.detail || "获取结果失败";
        errBox.classList.remove("hidden");
        // 404（记录不存在/已过期）：清掉 hash，避免刷新永远 404（小智娘 P3-3）
        if (res.status === 404 && state.reviewId === rid) clearReviewHash();
        return;
      }
      state.review = data;
      if (data.status === "processing" || data.status === "pending") {
        updateStages(data.stage);
        await new Promise((r) => setTimeout(r, 2000));
        continue;
      }
      loading.classList.add("hidden");
      if (data.status === "error") {
        markStageError();
        errBox.textContent = data.error || "审查失败，请返回重新上传";
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
    // 阶段 1.3 分支 C 立场知情提示（非阻断；文案服务端下发，前端只渲染）
    const stanceNoticeBox = $("stance-notice");
    const noticeText = data.precheck && data.precheck.stance_notice_text;
    if (noticeText) {
      stanceNoticeBox.textContent = noticeText;
      stanceNoticeBox.classList.remove("hidden");
    } else {
      stanceNoticeBox.classList.add("hidden");
    }
    // 立场声明（老钱裁决书：中性也声明——按哪个方向读必须摆在明面上）
    const stanceDecl = data.stance_declaration || "";
    $("results-meta").innerHTML =
      `<strong>${escapeHtml(data.filename || "")}</strong>` +
      ` · ${escapeHtml(data.category_label || data.category || "")}` +
      `<br/>${metaExtra}`;

    // 阶段 1.1 条款索引：meta 行露出「已识别 N 段」（无索引时静默，旧记录兼容）
    const nClauses = data.clause_index && data.clause_index.count;
    if (nClauses > 0) {
      const clauseNote = document.createElement("span");
      clauseNote.className = "clause-count-note";
      clauseNote.textContent = ` · 已识别条款 ${nClauses} 段`;
      $("results-meta").appendChild(clauseNote);
    }
    // 阶段 1.3 立场声明行（服务端生成，含「核查口径不因立场而改变」语义）
    if (stanceDecl) {
      const decl = document.createElement("p");
      decl.className = "stance-declaration";
      decl.textContent = stanceDecl;
      $("results-meta").appendChild(decl);
    }

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

  /** 阶段 1.1 条款锚点：clause_ids[0] → clause_index 查表，返回 heading。
   *  无索引（旧记录）/未定位/查不到 → 空串（调用方据此隐藏徽章）。 */
  function clauseHeadingFor(item) {
    const ids = (item && item.clause_ids) || [];
    const index = state.review && state.review.clause_index;
    if (!ids.length || !index || !Array.isArray(index.clauses)) return "";
    const target = index.clauses.find((c) => c.id === ids[0]);
    return (target && target.heading) || "";
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
    const clauseEl = $("detail-clause");

    // 阶段 1.1 条款锚点：clause_ids[0] → clause_index 查表露出「所在条款」。
    // 无索引（旧记录）/未定位到条款 → 静默隐藏，绝不占位。
    const clauseInfo = clauseHeadingFor(item);
    if (clauseInfo) {
      clauseEl.textContent = `所在条款：${clauseInfo}`;
      clauseEl.classList.remove("hidden");
    } else {
      clauseEl.textContent = "";
      clauseEl.classList.add("hidden");
    }

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

  // 浏览器 Back/Forward 与界面同步（小智娘门禁 P2-4：hash 已退界面还在，
  // 此时刷新会让「刷新丢结果」事故从 Back 路径复发）
  window.addEventListener("hashchange", () => {
    const rid = reviewIdFromHash();
    if (rid === state.reviewId) return;
    if (rid) {
      resumeFromHash();
    } else {
      state.reviewId = null;
      hidePrecheckConfirm();
      show("upload");
    }
  });

  $("btn-upload").addEventListener("click", () => upload());
  $("btn-precheck-switch").addEventListener("click", () => {
    const sug = $("precheck-dialog").dataset.suggested;
    if (sug) {
      $("category").value = sug;
      hidePrecheckConfirm();
      upload(sug, true); // 用户已拍板，带 force 防预审重跑死循环
    }
  });
  $("btn-precheck-keep").addEventListener("click", () => {
    const sug = $("precheck-dialog").dataset.suggested;
    hidePrecheckConfirm();
    if (sug) upload(undefined, true); // 坚持按原品类审：已确认，force 继续
    // 无 sug（不支持类）：仅收起弹窗，用户回表单重新选择类型
  });
  $("btn-ask").addEventListener("click", sendAsk);
  $("btn-back-upload").addEventListener("click", () => {
    clearReviewHash();
    state.reviewId = null;
    show("upload");
  });
  $("btn-back-results").addEventListener("click", () => show("results"));

  // ===== 阶段 1.3 立场 segmented（阳仔线框①）=====
  // 数据源：/api/categories 的 stances 元数据（YAML 单一来源）；
  // fetch 失败退化内置兜底表（同老钱矩阵）。不可审立场不渲染 + 前馈小字。
  const STANCE_FALLBACK = {
    procurement: {
      allowed: ["neutral", "buyer"],
      labels: { neutral: "中性（未声明）", buyer: "买方（采购方）" },
      note: "卖方立场审查暂不支持",
    },
    lease: {
      allowed: ["neutral", "lessee"],
      labels: { neutral: "中性（未声明）", lessee: "承租方" },
      note: "出租方立场审查暂不支持",
    },
    nda: {
      allowed: ["neutral", "disclosing", "receiving"],
      labels: { neutral: "中性（未声明）", disclosing: "披露方", receiving: "接收方" },
      note: "",
    },
  };
  let STANCE_META = null;

  function stanceMeta(cat) {
    return (STANCE_META && STANCE_META[cat]) || STANCE_FALLBACK[cat]
      || { allowed: ["neutral"], labels: { neutral: "中性（未声明）" }, note: "" };
  }

  function selectedStance() {
    const checked = document.querySelector('input[name="stance"]:checked');
    return checked ? checked.value : "neutral";
  }

  function updateStanceHint() {
    const meta = stanceMeta($("category").value);
    const v = selectedStance();
    $("stance-hint").textContent =
      v === "neutral"
        ? "将按中性视角阅读合同，不预设立场，报告会声明此视角。"
        : `将按${(meta.labels && meta.labels[v]) || v}立场阅读合同，风险判断以该视角为准。`;
  }

  function renderStanceOptions() {
    const meta = stanceMeta($("category").value);
    const track = $("stance-track");
    track.innerHTML = "";
    meta.allowed.forEach((s, i) => {
      const label = document.createElement("label");
      label.className = "seg-item";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "stance";
      input.value = s;
      input.checked = i === 0; // 默认中性是真选中态，不是「未选」
      input.addEventListener("change", updateStanceHint);
      const span = document.createElement("span");
      span.textContent = (meta.labels && meta.labels[s]) || s;
      label.appendChild(input);
      label.appendChild(span);
      track.appendChild(label);
    });
    const note = $("stance-note");
    if (meta.note) {
      note.textContent = meta.note;
      note.classList.remove("hidden");
    } else {
      note.classList.add("hidden");
    }
    updateStanceHint();
  }

  async function loadStanceMeta() {
    try {
      const res = await fetch("/api/categories");
      const data = await res.json();
      const meta = {};
      (data.categories || []).forEach((c) => {
        if (c.stances && Array.isArray(c.stances.allowed) && c.stances.allowed.length) {
          meta[c.id] = c.stances;
        }
      });
      if (Object.keys(meta).length) STANCE_META = meta;
    } catch (e) {
      /* 拉不到就退兜底表，控件照常可用 */
    }
    renderStanceOptions();
  }

  // 品类切换：枚举重渲 + 复位中性（旧立场作废——品类换了尺子，静默保留是事故温床）
  $("category").addEventListener("change", renderStanceOptions);

  // ===== 阶段 1.4⑤ dialog 行为（阳仔线框）=====
  // 点遮罩（backdrop）= 放弃本次上传；Esc 原生走同一 close 路径
  $("precheck-dialog").addEventListener("click", (ev) => {
    if (ev.target === $("precheck-dialog")) hidePrecheckConfirm();
  });
  // close（含 Esc/遮罩）清 suggested，防「拒诊→关→换文件→上传」串台
  $("precheck-dialog").addEventListener("close", () => {
    $("precheck-dialog").dataset.suggested = "";
  });

  loadStanceMeta();
  resumeFromHash();
})();

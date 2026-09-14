(() => {
  const state = {
    reviewId: null,
    review: null,
    askItem: null,
    selectedItemId: null,
    resultsScrollY: 0,
    askReqSeq: 0,
  };

  /** 追问竞态守卫（可信度 P1 F04）：仅当仍是同一审查/条目且为本轮最新请求时应用结果。 */
  function isAskResponseCurrent(snapshot) {
    if (!snapshot) return false;
    if (state.reviewId !== snapshot.reviewId) return false;
    if (!state.askItem || state.askItem.id !== snapshot.itemId) return false;
    if (state.askReqSeq !== snapshot.reqId) return false;
    return true;
  }

  const $ = (id) => document.getElementById(id);
  // A4 九哥定稿（上传/结果/追问共用；铁律旁注常驻可见）
  const STANCE_IRON_LAW = "立场只影响解释，不改变清单通过/需关注等规则档";
  const STANCE_EXPLAIN_GUIDE = "按当前立场说明谁受益、谁担责";
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

  // ===== 阶段 0.2 等待页真实进度（阳仔线框）=====
  // 诚实进度：只点亮后端真实到达的段位，无百分比无倒计时。
  // 阶段 2.1 追加第 4 段 analyzing（质量层）——quality 关闭/降级时后端不发，
  // 等待页自然停在已完成的三段
  const STAGE_ORDER = ["triage", "scanning", "scoring", "analyzing"];
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
        // A5：规则已出即可展示清单（AI 未完成横幅），不抹掉规则结果
        if ((data.items || []).length && data.completion) {
          loading.classList.add("hidden");
          renderResults(data, { partial: data.completion !== "fully_complete" });
          body.classList.remove("hidden");
        }
        await new Promise((r) => setTimeout(r, 2000));
        continue;
      }
      loading.classList.add("hidden");
      if (data.status === "error") {
        markStageError();
        // 规则已出而后续失败：仍展示已有 items
        if ((data.items || []).length) {
          renderResults(data, { partial: true, failed: true });
          body.classList.remove("hidden");
          errBox.textContent = data.error || "AI 部分未完成，规则结果仍可查看";
          errBox.classList.remove("hidden");
          return;
        }
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


  function coverageOf(data) {
    const sc = (data && data.scorecard) || {};
    if (sc.coverage) return sc.coverage;
    const q = (data && data.quality) || {};
    return q.coverage || null;
  }

  function renderReadingScope(data) {
    const el = $("reading-scope");
    if (!el) return;
    const cov = coverageOf(data);
    if (!cov) {
      // 无 AI 覆盖信息时：规则扫描为全文
      el.textContent = "阅读范围：本次已读全文（规则扫描）";
      el.classList.remove("hidden");
      return;
    }
    if (cov.limited) {
      let line = "合同较长，本次只读到部分内容，结论供参考";
      const range = readClauseRangeLabel(data, cov);
      if (range) line += " · " + range;
      el.textContent = "阅读范围：" + line;
    } else {
      el.textContent = "阅读范围：本次已读全文";
    }
    el.classList.remove("hidden");
  }

  function readClauseRangeLabel(data, cov) {
    const clauses = data.clause_index && data.clause_index.clauses;
    if (!clauses || !clauses.length) return "";
    const unread = cov.unread_ranges || [];
    if (!unread.length && cov.chars_covered && cov.original_chars) {
      const n = Math.max(
        1,
        Math.min(
          clauses.length,
          Math.round((clauses.length * cov.chars_covered) / cov.original_chars)
        )
      );
      return n > 1 ? "已读第 1–" + n + " 段" : "已读第 1 段";
    }
    const unreadIdx = new Set();
    unread.forEach((rng) => {
      if (!rng || rng.length < 2) return;
      const lo = rng[0], hi = rng[1];
      clauses.forEach((c, i) => {
        if (c.start < hi && c.end > lo) unreadIdx.add(i);
      });
    });
    const read = [];
    for (let i = 0; i < clauses.length; i++) {
      if (!unreadIdx.has(i)) read.push(i + 1);
    }
    if (!read.length) return "";
    const a = read[0], b = read[read.length - 1];
    return a === b ? "已读第 " + a + " 段" : "已读第 " + a + "–" + b + " 段";
  }

  function renderFacts(data) {
    const panel = $("facts-panel");
    if (!panel) return;
    let facts = data.facts || [];
    if ((!facts || !facts.length) && data.quality && data.quality.facts) {
      facts = data.quality.facts;
    }
    panel.innerHTML = "";
    const h = document.createElement("h3");
    h.textContent = "事实材料";
    panel.appendChild(h);
    if (!facts || !facts.length) {
      const empty = document.createElement("p");
      empty.className = "facts-empty";
      empty.textContent = "未抽出可核对事实";
      panel.appendChild(empty);
      panel.classList.remove("hidden");
      return;
    }
    const ul = document.createElement("ul");
    ul.className = "facts-list";
    facts.forEach((f) => {
      const li = document.createElement("li");
      const lab = document.createElement("span");
      lab.className = "facts-label";
      lab.textContent = f.label || f.kind || "事实";
      const val = document.createElement("span");
      val.className = "facts-value";
      val.textContent = f.value || "";
      li.appendChild(lab);
      li.appendChild(val);
      ul.appendChild(li);
    });
    panel.appendChild(ul);
    panel.classList.remove("hidden");
  }

  function jumpToEvidence(targetEl, evidence) {
    if (!targetEl) return;
    targetEl.scrollIntoView({ behavior: "smooth", block: "center" });
    targetEl.classList.add("is-flash");
    setTimeout(() => targetEl.classList.remove("is-flash"), 1600);
    // 证据引用：有条款时在旁注提示（不写 EvidenceRef）
    if (evidence && evidence.clause_id) {
      const heading = clauseHeadingById(evidence.clause_id);
      if (heading) {
        const clauseEl = $("detail-clause");
        if (clauseEl) {
          clauseEl.textContent = "证据引用 · 所在条款：" + heading;
          clauseEl.classList.remove("hidden");
        }
      }
    }
  }

  function renderResults(data, opts) {
    const items = data.items || [];
    const blinds = (data.blind_enabled && (data.blind_candidates || []).length)
      ? (data.blind_candidates || [])
      : [];
    const nAtt = items.filter((i) => i.status === "需关注").length;
    const nPass = items.filter((i) => i.status === "通过").length;
    const nBlind = blinds.length;
    let metaExtra = `需关注 ${nAtt} 项 · 已通过 ${nPass} 项`;
    if (nBlind > 0) {
      metaExtra += ` · 待核实 ${nBlind} 项`;
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
    // 阶段 2.3 IA 重排：JS 只整写 #results-meta-main（导出按钮在静态兄弟
    // #meta-actions 里，防 innerHTML 抹除）；#results-meta 外层仍含全部文本，
    // e2e/读屏语义不变
    $("results-meta-main").innerHTML =
      `<strong>${escapeHtml(data.filename || "")}</strong>` +
      ` · ${escapeHtml(data.category_label || data.category || "")}` +
      `<br/>${metaExtra}`;

    // 阶段 1.1 条款索引：meta 行露出「已识别 N 段」（无索引时静默，旧记录兼容）
    const nClauses = data.clause_index && data.clause_index.count;
    if (nClauses > 0) {
      const clauseNote = document.createElement("span");
      clauseNote.className = "clause-count-note";
      clauseNote.textContent = ` · 已识别条款 ${nClauses} 段`;
      $("results-meta-main").appendChild(clauseNote);
    }
    // 阶段 1.3 立场声明行（服务端生成，含「核查口径不因立场而改变」语义）
    if (stanceDecl) {
      const decl = document.createElement("p");
      decl.className = "stance-declaration";
      decl.textContent = stanceDecl;
      $("results-meta-main").appendChild(decl);
    }
    // A4：胶囊 + 铁律旁注 + 「谁受益、谁担责」引导（常驻，不折叠）
    renderStanceChrome(data);

    // 部分完成横幅
    let banner = document.getElementById("partial-banner");
    if (!banner) {
      banner = document.createElement("p");
      banner.id = "partial-banner";
      banner.className = "partial-banner hidden";
      const meta = $("results-meta");
      if (meta && meta.parentNode) meta.parentNode.insertBefore(banner, meta.nextSibling);
    }
    const partial = !!(opts && opts.partial);
    if (partial) {
      const doneRules = data.completion === "rules_complete";
      banner.textContent = doneRules
        ? "规则已出，AI 评分尚未完成——清单结果可先看，导出需等全部完成"
        : (opts && opts.failed)
          ? "AI 部分未完成；规则核查结果仍保留"
          : "规则已出，AI 尚未全部完成";
      banner.classList.remove("hidden");
    } else {
      banner.textContent = "";
      banner.classList.add("hidden");
    }

    renderReadingScope(data);
    renderFacts(data);

    // M4：审查完成后开放报告导出（GET /api/review/{id}/report）
    if (data.id && data.status === "done") {
      $("btn-export-report").href = `/api/review/${data.id}/report`;
      $("meta-actions").classList.remove("hidden");
      const note = $("export-scope-note");
      if (note) {
        note.textContent = data.export_scope_note
          || "报告只含本次读到并展示的内容；未读部分不写入结论";
      }
    } else if ($("meta-actions")) {
      $("meta-actions").classList.add("hidden");
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

    renderQuality(data);
    renderVerify(data);
  }

  /** 阶段 2.1 质量层：清单之后的「AI 观察」虚线容器。
   * 参谋不是裁判：不计分、不改档位；每条带原文引用+待人工确认。
   * 不可用（available=false）与旧记录（quality=null）同走静默隐藏。
   * 模型态字段一律 textContent，零 innerHTML 拼接（escapeHtml 之上再不给拼接机会）。 */
  // 阶段 2.1 三维度人话化显示（九哥裁定 09-13）：枚举英文不变，只换显示文案，
  // 与等待页副标题「有没有漏写、有没有自相矛盾、对您的影响」逐字呼应
  const QUALITY_DIM_LABEL = {
    completeness: "有没有漏写",
    consistency: "有没有自相矛盾",
    impact: "对您的影响",
  };


  /** A6 有界主动核验：需你确认（九哥文案 / 阳仔视觉）。
   * 铁律：主动核查不改写清单四档；每条可追原文。 */
  const VERIFY_ASIDE = "主动核查只提疑点，不改变清单规则档";
  const VERIFY_FLOW = ["疑点", "取证", "核对", "问你"];

  function renderVerify(data) {
    const panel = $("verify-panel");
    if (!panel) return;
    const v = data.verify;
    panel.innerHTML = "";
    if (!v || !v.available) {
      panel.classList.add("hidden");
      return;
    }
    const qs = Array.isArray(v.questions) ? v.questions : [];

    const h = document.createElement("h3");
    h.textContent = "需你确认";
    panel.appendChild(h);

    const flow = document.createElement("p");
    flow.className = "verify-flow";
    flow.setAttribute("aria-label", "核查流程");
    // 有未确认 → 问你；全确认/再核过 → 核对；否则疑点
    const pending = qs.filter((q) => q.status === "pending").length;
    const anyRechecked = qs.some((q) => q.status === "rechecked");
    let current = 0;
    if (qs.length) current = 1; // 取证
    if (qs.some((q) => q.verification && q.verification !== "unverified")) current = 2; // 核对
    if (pending > 0 || qs.some((q) => q.status === "confirmed" || q.status === "disputed")) current = 3; // 问你
    if (anyRechecked && pending === 0) current = 2;
    VERIFY_FLOW.forEach((label, i) => {
      if (i > 0) {
        const sep = document.createElement("span");
        sep.className = "flow-sep";
        sep.textContent = "→";
        flow.appendChild(sep);
      }
      const step = document.createElement("span");
      step.className = "flow-step" + (i === current ? " is-current" : "");
      step.textContent = label;
      flow.appendChild(step);
    });
    panel.appendChild(flow);

    const aside = document.createElement("p");
    aside.className = "verify-aside";
    aside.textContent = (v.disclaimer && !/盖章|改判/.test(v.disclaimer))
      ? v.disclaimer
      : VERIFY_ASIDE;
    panel.appendChild(aside);

    const budget = v.budget || {};
    const roundsLeft = typeof budget.rounds_remaining === "number"
      ? budget.rounds_remaining
      : Math.max(0, (v.max_rounds || 0) - (v.rounds_used || 0));
    const bud = document.createElement("p");
    bud.className = "verify-budget";
    bud.textContent = "还可再核 " + roundsLeft + " 次"
      + (typeof budget.questions_pending === "number"
        ? " · 待核实 " + budget.questions_pending + " 条"
        : "");
    panel.appendChild(bud);

    if (!qs.length) {
      const empty = document.createElement("p");
      empty.className = "verify-empty";
      empty.textContent = "暂无需要确认的事项";
      panel.appendChild(empty);
      panel.classList.remove("hidden");
      return;
    }

    const ul = document.createElement("ul");
    ul.className = "verify-list";
    qs.forEach((q) => {
      const li = document.createElement("li");
      li.className = "verify-item";
      li.dataset.qid = q.id || "";

      const head = document.createElement("div");
      head.className = "verify-item-head";
      const badge = document.createElement("span");
      badge.className = "verify-badge";
      badge.textContent = "待核实"; // 九哥：别写成需关注
      head.appendChild(badge);
      if (q.status && q.status !== "pending") {
        const st = document.createElement("span");
        st.className = "verify-status";
        st.textContent = q.status === "confirmed"
          ? "已确认无误"
          : q.status === "disputed"
            ? "已补充说明"
            : q.status === "rechecked"
              ? "已再审"
              : q.status;
        head.appendChild(st);
      }
      li.appendChild(head);

      const qEl = document.createElement("p");
      qEl.className = "verify-q";
      qEl.textContent = q.question || q.title || "";
      li.appendChild(qEl);

      if (q.quote) {
        const quote = document.createElement("div");
        quote.className = "quality-quote";
        quote.textContent = "原文：" + q.quote;
        li.appendChild(quote);
        const jump = document.createElement("button");
        jump.type = "button";
        jump.className = "link evidence-jump";
        jump.textContent = "看原文";
        jump.onclick = () => jumpToEvidence(quote, q.evidence || null);
        li.appendChild(jump);
      }

      const meta = document.createElement("p");
      meta.className = "verify-meta";
      const heading = clauseHeadingById(q.clause_id);
      if (heading) {
        const c = document.createElement("span");
        c.textContent = "条款 " + heading;
        meta.appendChild(c);
      }
      if (q.verification) {
        const verLabel = {
          verified: "摘句对得上",
          ambiguous: "摘句位置不唯一",
          missing: "原文未找到摘句",
          unverified: "尚未核对",
        };
        const ver = document.createElement("span");
        ver.textContent = verLabel[q.verification] || q.verification;
        meta.appendChild(ver);
      }
      if (meta.childNodes.length) li.appendChild(meta);

      const note = document.createElement("input");
      note.type = "text";
      note.className = "verify-note-input";
      note.placeholder = "补充说明（可选）";
      note.maxLength = 300;
      if (q.human_note) {
        note.value = q.human_note;
        note.classList.add("is-open");
      }
      li.appendChild(note);

      const actions = document.createElement("div");
      actions.className = "verify-actions";

      const btnNote = document.createElement("button");
      btnNote.type = "button";
      btnNote.className = "verify-btn-primary";
      btnNote.textContent = "补充说明";
      btnNote.onclick = () => {
        if (!note.classList.contains("is-open")) {
          note.classList.add("is-open");
          note.focus();
          return;
        }
        // 已展开：提交补充说明（不暗示改档）
        submitVerifyConfirm(data.id, q.id, "dispute", note.value || "", note.value || "");
      };

      const btnOk = document.createElement("button");
      btnOk.type = "button";
      btnOk.className = "verify-btn-secondary";
      btnOk.textContent = "确认无误";
      btnOk.onclick = () => submitVerifyConfirm(data.id, q.id, "confirm", note.value || "", "");

      const btnRe = document.createElement("button");
      btnRe.type = "button";
      btnRe.className = "verify-btn-secondary";
      btnRe.textContent = "再审本条";
      btnRe.disabled = !(q.status === "confirmed" || q.status === "disputed" || q.status === "rechecked");
      btnRe.onclick = () => submitVerifyRecheck(data.id, q.id);

      actions.appendChild(btnNote);
      actions.appendChild(btnOk);
      actions.appendChild(btnRe);
      li.appendChild(actions);
      ul.appendChild(li);
    });
    panel.appendChild(ul);
    panel.classList.remove("hidden");
  }

  async function submitVerifyConfirm(reviewId, questionId, choice, humanNote, revisedQuote) {
    if (!reviewId || !questionId) return;
    try {
      const res = await fetch("/api/review/" + reviewId + "/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question_id: questionId,
          choice: choice,
          human_note: humanNote || "",
          revised_quote: revisedQuote || "",
        }),
      });
      const body = await res.json();
      if (!res.ok || !body.ok) {
        console.warn("confirm failed", body && body.error);
        return;
      }
      if (body.verify && state.review && state.review.id === reviewId) {
        state.review.verify = body.verify;
        renderVerify(state.review);
      }
    } catch (err) {
      console.warn("confirm error", err);
    }
  }

  async function submitVerifyRecheck(reviewId, questionId) {
    if (!reviewId || !questionId) return;
    try {
      const res = await fetch("/api/review/" + reviewId + "/reverify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: questionId }),
      });
      const body = await res.json();
      if (!res.ok || !body.ok) {
        console.warn("reverify failed", body && body.error);
        return;
      }
      if (body.verify && state.review && state.review.id === reviewId) {
        state.review.verify = body.verify;
        renderVerify(state.review);
      }
    } catch (err) {
      console.warn("reverify error", err);
    }
  }


  function renderQuality(data) {
    const panel = $("quality-panel");
    if (!panel) return;
    const q = data.quality;
    const obs = (q && q.available && Array.isArray(q.observations)) ? q.observations : [];
    if (!obs.length) {
      panel.innerHTML = "";
      panel.classList.add("hidden");
      return;
    }
    panel.innerHTML = "";

    const head = document.createElement("div");
    head.className = "quality-head";
    const badge = document.createElement("span");
    badge.className = "source-badge quality";
    badge.textContent = "AI 观察";
    head.appendChild(badge);
    const disclaimer = document.createElement("span");
    disclaimer.className = "quality-disclaimer";
    let disc = (q && q.disclaimer) || "AI 观察仅供参考，需人工确认。";
    if (q && q.coverage && q.coverage.limited) {
      disc += "（合同较长，本次只读到部分内容，结论供参考）";
    }
    disclaimer.textContent = disc;
    head.appendChild(disclaimer);
    panel.appendChild(head);

    const ul = document.createElement("ul");
    ul.className = "quality-list";
    obs.forEach((o) => {
      const li = document.createElement("li");
      li.className = "quality-item";

      const line1 = document.createElement("p");
      line1.className = "quality-title-line";
      const dim = document.createElement("span");
      dim.className = "quality-dim";
      dim.textContent = QUALITY_DIM_LABEL[o.dimension] || "观察";
      line1.appendChild(dim);
      const title = document.createElement("span");
      title.className = "quality-title";
      title.textContent = o.title || "";
      line1.appendChild(title);
      li.appendChild(line1);

      if (o.quote) {
        const quote = document.createElement("div");
        quote.className = "quality-quote";
        quote.textContent = "原文：" + o.quote;
        li.appendChild(quote);
        const jump = document.createElement("button");
        jump.type = "button";
        jump.className = "link evidence-jump";
        jump.textContent = "看原文";
        jump.onclick = () => jumpToEvidence(quote, o.evidence || null);
        li.appendChild(jump);
      }

      if (o.comment) {
        const comment = document.createElement("p");
        comment.className = "quality-comment";
        comment.textContent = o.comment;
        li.appendChild(comment);
      }

      const line4 = document.createElement("p");
      line4.className = "quality-meta-line";
      const heading = clauseHeadingById(o.clause_id);
      if (heading) {
        const clause = document.createElement("span");
        clause.className = "quality-clause";
        clause.textContent = "条款 " + heading;
        line4.appendChild(clause);
      }
      const confirm = document.createElement("span");
      confirm.className = "quality-confirm";
      confirm.textContent = "待人工确认";
      line4.appendChild(confirm);
      li.appendChild(line4);

      ul.appendChild(li);
    });
    panel.appendChild(ul);
    panel.classList.remove("hidden");
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
        // 阶段 2.3：跟随正文区（#results-meta-main），不落到导出按钮后面
        $("results-meta-main").appendChild(note);
      }
      return;
    }
    card.classList.remove("hidden");
    $("score-total").textContent = String(sc.total);
    const tier = sc.tier || {};
    $("score-grade").textContent = [tier.label, tier.hint].filter(Boolean).join("：");
    $("score-summary").textContent = sc.summary || "";
    $("score-disclaimer").textContent =
      (sc.disclaimer || "模型评分仅供参考，以逐条规则结论为准") +
      ((sc.coverage && sc.coverage.limited)
        ? "（合同较长，本次只读到部分内容，结论供参考；规则扫描仍为全文）"
        : "");
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
      ruleBadge.textContent = "系统核查";
      badges.appendChild(ruleBadge);
    }
    if (isBlind) {
      const blindBadge = document.createElement("span");
      blindBadge.className = "source-badge blind";
      blindBadge.textContent = "待核实";
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
    } else {
      // 阶段 2.3 IA 重排：每条露一句话摘要（note 首句，≤60 字；空则不渲染）
      const summary = firstSentence(item.note || "");
      if (summary) {
        const cap = document.createElement("div");
        cap.className = "item-caption";
        cap.textContent = summary;
        li.appendChild(cap);
      }
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

  /** 阶段 2.1 质量层：按单个 clause_id 查条款标题（查不到静默——
   * 伪造 id 已在服务端归一 null，此处只防旧记录/索引缺失）。 */
  function clauseHeadingById(clauseId) {
    if (!clauseId) return "";
    const index = state.review && state.review.clause_index;
    if (!index || !Array.isArray(index.clauses)) return "";
    const target = index.clauses.find((c) => c.id === clauseId);
    return (target && target.heading) || "";
  }

  function selectItem(item) {
    // 阶段 2.4：移动端点条目 = 独立详情页（hash 路由）；桌面保持行内面板，
    // 路径与 DOM 逐字节不变
    if (mqMobile.matches) {
      navigateItem(item);
      return;
    }
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
    const jumpBtn = $("detail-evidence-jump");
    if (item.quote) {
      quoteEl.classList.remove("empty");
      quoteEl.innerHTML = highlightQuoteHtml(item.quote, item.hits || []);
      if (jumpBtn) {
        jumpBtn.classList.remove("hidden");
        jumpBtn.onclick = () => jumpToEvidence(quoteEl, item.evidence || null);
      }
    } else {
      quoteEl.classList.add("empty");
      quoteEl.textContent = "暂无原文摘句";
      if (jumpBtn) {
        jumpBtn.classList.add("hidden");
        jumpBtn.onclick = null;
      }
    }

    // Blind candidates need human confirm; do not offer rule-style ask as if stamped
    if (item.status === "需关注" && !item._blind) {
      actions.classList.remove("hidden");
      $("detail-ask").onclick = () => openAskDetail(item);
    } else {
      actions.classList.add("hidden");
      $("detail-ask").onclick = null;
    }

    detail.classList.remove("hidden");
    detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /** 阶段 2.4：移动端点条目 = 独立详情页。hash 是唯一事实源——写 hash 后由
   * hashchange 统一驱动视图（Back 手势/返回键免费获得）；同 hash 重复点击
   * 不触发事件（已在页上，符合直觉）。 */
  function navigateItem(item) {
    state.resultsScrollY = window.scrollY;
    const key = item._blind ? "blind:" + item.id : item.id;
    try {
      window.location.hash = "#/review/" + state.reviewId + "/item/" + key;
    } catch (e) {
      openAskDetail(item); // 隐私模式等 hash 不可写场景：直接渲染不路由
    }
  }

  /** 阶段 2.4 详情/追问屏（#screen-ask 升级兼任，桌面桌面入口与移动 hash
   * 直达共用）。阅读顺序=说明→原文→(答案)→追问输入（roadmap 裁决 1/2）。
   * 三类条目分流追问卡（阳仔 3.3）：仅「需关注且非待核实」出追问卡——
   * 待核实渲染成「追问暂未开通」是把「待人工确认」误读成「未开通」。 */
  function openAskDetail(item) {
    // 切换条目即作废进行中的追问响应（可信度 P1 F04）
    if (!state.askItem || state.askItem.id !== item.id) {
      state.askReqSeq += 1;
    }
    state.askItem = item;
    const selKey = item._blind ? "blind:" + item.id : item.id;
    state.selectedItemId = selKey;
    document.querySelectorAll(".item").forEach((el) => {
      el.classList.toggle("selected", el.dataset.itemId === selKey);
    });

    // 页头：条目名 + 档位/来源双通道（tag 文字+底色；徽章描边）
    $("ask-title").textContent = item.name || "问清楚一点";
    const askGuide = $("ask-stance-guide");
    const askIron = $("ask-stance-iron");
    if (askGuide) askGuide.textContent = STANCE_EXPLAIN_GUIDE;
    if (askIron) askIron.textContent = STANCE_IRON_LAW;
    const statusLine = $("ask-status-line");
    statusLine.innerHTML = "";
    const tag = document.createElement("span");
    tag.className = `tag ${statusClass(item.status)}`;
    tag.textContent = statusLabel(item.status);
    statusLine.appendChild(tag);
    if (item.status === "需关注") {
      const badge = document.createElement("span");
      badge.className = item._blind ? "source-badge blind" : "source-badge rule";
      badge.textContent = item._blind ? "待核实" : "系统核查";
      statusLine.appendChild(badge);
    }

    // 段1 说明卡：note 全文 + 条款锚点（首句截断是清单的事，这里给全文）
    $("ask-context").textContent = item.note || "（无说明）";
    const clauseInfo = clauseHeadingFor(item);
    const clauseEl = $("ask-clause");
    if (clauseInfo) {
      clauseEl.textContent = `所在条款：${clauseInfo}`;
      clauseEl.classList.remove("hidden");
    } else {
      clauseEl.textContent = "";
      clauseEl.classList.add("hidden");
    }

    // 段2 原文卡（高亮复用，无 quote 走 .empty 退化）
    const quoteBox = $("ask-quote");
    if (item.quote) {
      quoteBox.classList.remove("empty");
      quoteBox.innerHTML = highlightQuoteHtml(item.quote, item.hits || []);
    } else {
      quoteBox.classList.add("empty");
      quoteBox.textContent = "暂无原文摘句";
    }

    // 段3/4 追问卡显隐分流 + 可用性门禁（现状逻辑收编）
    const askable = item.status === "需关注" && !item._blind;
    const interactCard = $("screen-ask").querySelector(".ask-interact");
    const available = !!(state.review && state.review.ask_available);
    const banner = $("ask-unavailable");
    const btn = $("btn-ask");
    const input = $("ask-input");
    if (!askable) {
      interactCard.classList.add("hidden");
    } else {
      interactCard.classList.remove("hidden");
      $("ask-input").value = "";
      $("ask-error").classList.add("hidden");
      $("ask-answer").classList.add("hidden");
      $("ask-answer").innerHTML = "";
      // 阶段 2.2：换条目追问时清掉上一轮的「还想问」建议 chips
      renderFollowups([]);
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
    }

    show("ask");
    window.scrollTo(0, 0);
  }

  async function sendAsk() {
    const err = $("ask-error");
    const ans = $("ask-answer");
    err.classList.add("hidden");
    ans.classList.add("hidden");
    // 重新提问即弃上一轮建议 chips（门禁 P3：失败路径不得残留旧建议）
    renderFollowups([]);
    const question = $("ask-input").value.trim();
    if (!question) {
      err.textContent = "请输入问题。";
      err.classList.remove("hidden");
      return;
    }
    if (!state.reviewId || !state.askItem || !state.askItem.id) {
      err.textContent = "请先选择需关注条目再追问。";
      err.classList.remove("hidden");
      return;
    }
    const btn = $("btn-ask");
    btn.disabled = true;
    // 快照当前页身份 + 单调请求号（镜像 pollReview rid 守卫）
    const snapshot = {
      reviewId: state.reviewId,
      itemId: state.askItem.id,
      reqId: ++state.askReqSeq,
    };
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          review_id: snapshot.reviewId,
          item_id: snapshot.itemId,
          question,
        }),
      });
      const data = await res.json();
      if (!isAskResponseCurrent(snapshot)) return;
      if (!res.ok) throw new Error(data.detail || "请求失败");
      if (!data.ok) {
        err.textContent = data.error || "追问暂未开通";
        err.classList.remove("hidden");
        return;
      }
      const verified = data.quote_verified !== false
        && !!(data.answer && data.answer.quote_verified !== false
              && data.answer["原文在哪"]
              && data.answer["原文在哪"] !== "未定位到原文");
      renderAnswer(data.answer || {}, data.raw_text, { quoteVerified: verified });
      ans.classList.remove("hidden");
      // 阶段 2.4：移动详情页答案在输入之上，发问成功后滚到答案（即时滚动，
      // 长文里 smooth 反而晕；桌面答案在输入下方，保持原行为不跳）
      if (mqMobile.matches) {
        ans.scrollIntoView({ block: "start" });
      }
    } catch (e) {
      if (!isAskResponseCurrent(snapshot)) return;
      err.textContent = e.message || String(e);
      err.classList.remove("hidden");
    } finally {
      // 仅最新请求或已离开本条目时解锁，避免慢请求覆盖后把新请求的 loading 打断
      if (state.askReqSeq === snapshot.reqId
          || !state.askItem
          || state.askItem.id !== snapshot.itemId
          || state.reviewId !== snapshot.reviewId) {
        btn.disabled = false;
      }
    }
  }

  /** 阶段 2.3 摘要行：取 note 第一句（。！？\n 切分），去句末标点，≤60 字；
   * 空返回空串（调用方不渲染占位）。 */
  function firstSentence(note) {
    const text = String(note || "").trim();
    if (!text) return "";
    const m = text.match(/^[\s\S]*?[。！？\n]/);
    // 含分隔符匹配会带进句末标点：strip 尾部（门禁 P2：与 e2e 断言口径对齐）
    const first = (m ? m[0] : text).trim().replace(/[。！？]+$/, "").trim();
    if (!first) return "";
    return first.length <= 60 ? first : first.slice(0, 60) + "…";
  }

  /** 阶段 2.2：「还想问」建议 chips（九哥裁定——不进答案正文，输入框上方
   * 可点回填；建议变一步发起）。空列表整块隐藏。 */
  function renderFollowups(questions) {
    const wrap = $("ask-followups-wrap");
    const ul = $("ask-followups");
    ul.innerHTML = "";
    const items = (questions || [])
      .map((q) => String(q || "").trim())
      .filter(Boolean)
      .slice(0, 3);
    if (!items.length) {
      wrap.classList.add("hidden");
      return;
    }
    items.forEach((q) => {
      const li = document.createElement("li");
      li.textContent = q;
      li.addEventListener("click", () => {
        const input = $("ask-input");
        input.value = q;
        input.focus();
      });
      ul.appendChild(li);
    });
    wrap.classList.remove("hidden");
  }

  /** 阶段 2.2 四段式（与质量层共用规范，呈现层映射——后端七键不动）：
   * 段① 原文依据 ←「原文在哪」（caption 灰字一行，钉顶原文卡已承载摘句）
   * 段② 实际影响 ←「问题是啥」（正文黑主权重；「这条在查啥」「风险等级」
   *     不上 UI——后者是「不伪造风险等级」设计禁令的落地）
   * 段③ 建议改法 ←「建议怎么改」+ 改写稿块（原样保留）
   * 段④ 待确认的事 ← 当前键无内容源，缺段静默隐藏
   * raw_text <pre> 兜底保留（结构化失败不装正常）。 */
  function renderAnswer(answer, raw, opts) {
    const box = $("ask-answer");
    const where = (answer && answer["原文在哪"]) || "";
    const impact = (answer && answer["问题是啥"]) || "";
    const rewrite = (answer && answer["建议怎么改"]) || "";
    const rewriteDraft = (answer && answer["改写稿"]) || "";
    const quoteVerified = !!(opts && opts.quoteVerified)
      && where
      && where !== "未定位到原文";
    let html = "";

    // 段① 原文依据（caption 灰字，不造第二个引块）
    // 未核验摘录不得呈现为已核实来源（可信度 P1 F02）
    if (where) {
      html += `<p class="detail-label">原文依据</p>`;
      if (quoteVerified) {
        html += `<div class="detail-note muted">${escapeHtml(where)}</div>`;
      } else {
        html += `<div class="detail-note muted">未定位到原文</div>`;
        if (where !== "未定位到原文") {
          html += `<div class="detail-note muted">（模型摘句未通过原文核验，已隐藏）</div>`;
        }
      }
    }

    // 段② 实际影响（主权重；raw 兜底挂这一段）
    html += `<p class="detail-label">实际影响</p>`;
    if (impact) {
      html += `<div class="detail-note">${escapeHtml(impact)}</div>`;
    } else if (raw) {
      html += `<pre style="white-space:pre-wrap">${escapeHtml(raw)}</pre>`;
    } else {
      html += `<div class="detail-note muted">暂无</div>`;
    }

    // 段③ 建议改法
    html += `<p class="detail-label">建议改法</p>`;
    if (rewrite) {
      html += `<div class="detail-note">${escapeHtml(rewrite)}</div>`;
    } else if (!rewriteDraft) {
      html += `<div class="detail-note muted">暂无</div>`;
    }
    if (rewriteDraft && quoteVerified) {
      // M3.5 建议改写稿：可粘贴，但必须对照原文核对（阳仔交互 + 九哥文案）
      // 未核验原文时不展示可复制改写稿（可信度 P1 F02）
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

    box.innerHTML = html;

    const copyBtn = $("btn-copy-rewrite");
    if (copyBtn) {
      copyBtn.addEventListener("click", () => copyRewrite(copyBtn, rewriteDraft));
    }

    // 「还想问」→ 输入框上方 chips（换行切分，最多 3 条）
    const followups = String((answer && answer["还想问"]) || "").split(/\n/);
    renderFollowups(followups);
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
    return routeFromHash().rid;
  }

  /** 阶段 2.4 hash 路由：#/review/{rid} → 结果页；#/review/{rid}/item/{key}
   *  → 移动详情页。blind 条目 key 含冒号（blind:c07），清单 id 是
   *  snake_case（early_termination）——itemId 字符集必须含下划线
   *  （门禁 P1：漏下划线会让正则整体匹配失败，用户被踢回上传页）。 */
  function routeFromHash() {
    const m = (window.location.hash || "").match(
      /^#\/review\/([A-Za-z0-9]+)(?:\/item\/([A-Za-z0-9_:]+))?$/
    );
    return m ? { rid: m[1], itemId: m[2] || null } : { rid: null, itemId: null };
  }

  const mqMobile = window.matchMedia("(max-width: 480px)");

  // 阶段 2.4：滚动恢复由 JS 接管——浏览器原生 same-document 滚动恢复晚于
  // hashchange 处理器执行，会把 Back 回清单的精确恢复覆盖掉
  if ("scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }

  /** 按 key（id 或 blind:{id}）在已渲染的审查数据里找条目（含 _blind 标记）。 */
  function findItemByKey(key) {
    const review = state.review;
    if (!review) return null;
    if (key.startsWith("blind:")) {
      const id = key.slice(6);
      const hit = (review.blind_candidates || []).find((c) => c.id === id);
      return hit ? { ...hit, _blind: true } : null;
    }
    const hit = (review.items || []).find((c) => c.id === key);
    return hit ? { ...hit, _blind: false } : null;
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
    // 阶段 2.4：直达链接带 item 段（#/review/{rid}/item/{key}）→ 渲染后回选
    const route = routeFromHash();
    if (route.itemId && state.reviewId === route.rid) {
      const item = findItemByKey(route.itemId);
      if (item) openAskDetail(item);
    }
  }

  // 浏览器 Back/Forward 与界面同步（小智娘门禁 P2-4：hash 已退界面还在，
  // 此时刷新会让「刷新丢结果」事故从 Back 路径复发）。
  // 阶段 2.4：同 rid 内 item 段变化走本地详情页切换，不重新轮询。
  window.addEventListener("hashchange", () => {
    const route = routeFromHash();
    if (route.rid !== state.reviewId) {
      if (route.rid) {
        resumeFromHash();
      } else {
        state.reviewId = null;
        state.selectedItemId = null;
        hidePrecheckConfirm();
        show("upload");
      }
      return;
    }
    if (route.itemId) {
      const item = findItemByKey(route.itemId);
      if (item) {
        openAskDetail(item);
      } else {
        show("results"); // 找不到条目（旧记录/数据不齐）：回结果页不装详情
      }
    } else {
      show("results");
      window.scrollTo(0, state.resultsScrollY || 0);
    }
  });

  // 断点穿越（旋转/拉伸窗口）：以当前 hash 重新推导视图，不产生分裂状态
  const onBreakpointChange = () => {
    const route = routeFromHash();
    if (route.rid !== state.reviewId) return;
    if (route.itemId) {
      if (mqMobile.matches) {
        const item = findItemByKey(route.itemId);
        if (item) openAskDetail(item);
      } else {
        // 移动详情 → 桌面：回结果页行内面板（桌面没有独立详情页）
        const item = findItemByKey(route.itemId);
        show("results");
        if (item) selectItem(item);
      }
    }
  };
  if (typeof mqMobile.addEventListener === "function") {
    mqMobile.addEventListener("change", onBreakpointChange);
  }

  $("btn-upload").addEventListener("click", () => upload());
  $("btn-precheck-switch").addEventListener("click", () => {
    const sug = $("precheck-dialog").dataset.suggested;
    if (sug) {
      $("category").value = sug;
      // 程序赋值不触发 change 事件：立场必须显式重渲复位中性，否则残留
      // 旧品类的立场重传会吃 422（用户界面上没有任何可修入口，交互死路）
      renderStanceOptions();
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
  $("btn-back-results").addEventListener("click", () => {
    // 阶段 2.4：详情页上返回 = 退 hash（Back 语义一致，滚动自动恢复）；
    // 无 item 段（桌面入口）维持原行为
    if (routeFromHash().itemId) {
      try {
        window.location.hash = "#/review/" + state.reviewId;
      } catch (e) {
        show("results");
      }
    } else {
      show("results");
    }
  });

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
    const label = (meta.labels && meta.labels[v]) || v;
    // 措辞红线（九哥 A4）：谁受益、谁担责；绝不暗示规则档被立场改写
    $("stance-hint").textContent =
      v === "neutral"
        ? "中性视角：规则核查照旧；AI 解释不偏向任何一方。"
        : `按「${label}」读：AI 解释会点明谁受益、谁担责。`;
    const ironUp = $("stance-iron-upload");
    if (ironUp) ironUp.textContent = STANCE_IRON_LAW;
  }

  function resolveStanceLabel(category, stance) {
    const meta = stanceMeta(category || "");
    const st = stance || "neutral";
    return (meta.labels && meta.labels[st]) || st;
  }

  function renderStanceChrome(data) {
    // 结果页顶栏右侧胶囊 + 铁律旁注 + 解释区引导（阳仔视觉 / 九哥用词）
    const capsule = $("stance-capsule");
    const iron = $("stance-iron");
    const guide = $("stance-explain-guide");
    if (iron) iron.textContent = STANCE_IRON_LAW;
    if (guide) guide.textContent = STANCE_EXPLAIN_GUIDE;
    if (!capsule) return;
    const label = resolveStanceLabel(data && data.category, data && data.stance);
    capsule.textContent = "审查立场 · " + label;
    capsule.classList.remove("hidden");
    capsule.title = label;
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

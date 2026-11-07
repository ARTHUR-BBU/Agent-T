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

  /** B7-6 整壳：三屏模型退役，工作区三态（ws-empty/ws-running/ws-results）。
   * show() 保留为兼容壳：旧调用点 show("upload"/"results") 语义不变，
   * show("ask") 退役（对话工作台不是屏，挂在右栏详情之下）。 */
  function setWs(name) {
    const main = $("shell-main");
    if (main) main.dataset.ws = name;
    const states = { empty: "ws-empty", running: "ws-running", results: "ws-results" };
    Object.entries(states).forEach(([k, id]) => {
      const el = $(id);
      if (el) el.classList.toggle("hidden", k !== name);
    });
    syncShellChrome();
  }

  function show(name) {
    if (name === "ask") return; // 对话工作台不是屏
    setWs(name === "upload" ? "empty" : name);
  }

  /** 左栏下区显隐：有打开的审查才渲染「本份合同」导航区 */
  function syncShellChrome() {
    const has = state.reviewId != null;
    const sect = $("shell-context-sect");
    const list = $("shell-context");
    if (sect) sect.classList.toggle("hidden", !has);
    if (list) list.classList.toggle("hidden", !has);
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
    closeAskContext(); // codex 十轮 P1：新合同会话开始，旧问答上下文清场
    const pasteMode = !$("paste-wrap").classList.contains("hidden");
    const fd = new FormData();
    if (pasteMode) {
      // B7-6 附录 B：粘贴模式（前端预检；后端独立校验双保险）
      const text = ($("paste-text").value || "").trim();
      if (!text) {
        err.textContent = "粘贴内容为空。";
        err.classList.remove("hidden");
        return;
      }
      if (text.length > 200000) {
        err.textContent = "超出 20 万字符上限，请删减后提交。";
        err.classList.remove("hidden");
        return;
      }
      fd.append("pasted_text", text);
    } else {
      if (!fileInput.files || !fileInput.files[0]) {
        err.textContent = "请先选择合同文件。";
        err.classList.remove("hidden");
        return;
      }
      fd.append("file", fileInput.files[0]);
    }
    const btn = $("btn-upload");
    btn.disabled = true;
    $("stance-seg").disabled = true; // 上传进行中立场锁定（阳仔线框）
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
      setWs("running");
      $("results-loading").classList.remove("hidden");
      $("results-body").classList.add("hidden");
      $("results-error").classList.add("hidden");
      $("item-detail").classList.add("hidden");
      revHideAll(); // B7-3a：清单重渲/新审查时三卡一并收起
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
          setWs("results");
          renderShellHistory(); // B7-6 历史侧栏随新记录刷新
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
      metaExtra += ` · 补盲 ${nBlind} 项`;
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
    syncSideEmpty();
    revHideAll(); // B7-3a：清单重渲时三卡一并收起

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

    renderAiFindings(data);
    renderObjections(data);
    // B7-6 对话工作台：历史流恢复（刷新/换机不丢）+ 左栏下区导航刷新；
    // 工作台在结果态常驻（未选条目也可看历史——codex 十二轮 P2-5）
    renderThreadFromData(((data.ask_thread || {}).turns) || []);
    renderShellContext();
    const wb = $("ask-workbench");
    if (wb) wb.classList.remove("hidden");
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


  /** B7-5a 统一「AI 发现」面板：观察+核验疑点合并为一条流水线。
   * 发现 → 自动取证核对 → 人拍板（确认/有异议）→ 事实类确认后物化为
   * 清单检查项（可生成修改建议/可追问/入报告）；有异议=反悔撤回；
   * 派生问题类的确认走既有 confirm 链（决定记录进报告「人工决定与确认」）。
   * 观点类（对您的影响）无原文可核，如实标注「观点·无核对」，不可改写。
   * 数据不可用（旧记录异常形态）整卡静默隐藏；textContent 纪律同前。 */
  const VERIFY_ASIDE = "主动核查只提疑点，不改变清单规则档";
  const VERIFY_FLOW = ["疑点", "取证", "核对", "问你"];
  const FINDING_STATUS_LABEL = {
    pending: "待确认",
    viewed: "观点·无核对",
    confirmed: "已确认",
    disputed: "已争议撤回",
    rechecked: "已再审",
  };

  async function refreshReviewAndRender(reviewId) {
    try {
      const res = await fetch(`/api/review/${reviewId}`);
      if (!res.ok) return;
      const data = await res.json();
      state.review = data;
      renderResults(data, { partial: data.completion !== "fully_complete" });
    } catch (err) {
      console.warn("ai-finding refresh error", err);
    }
  }

  async function submitFindingStatus(reviewId, findingKey, status) {
    if (!reviewId || !findingKey) return;
    try {
      const res = await fetch("/api/review/" + reviewId + "/ai-finding/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ finding_key: findingKey, status: status }),
      });
      const body = await res.json();
      if (!res.ok || !body.ok) {
        console.warn("ai-finding status failed", body && body.error);
        return;
      }
      await refreshReviewAndRender(reviewId);
    } catch (err) {
      console.warn("ai-finding status error", err);
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
      await refreshReviewAndRender(reviewId);
    } catch (err) {
      console.warn("reverify error", err);
    }
  }

  function renderAiFindings(data) {
    const panel = $("ai-findings-panel");
    if (!panel) return;
    const af = data.ai_findings;
    const list = (af && af.available && Array.isArray(af.findings))
      ? af.findings
      : [];
    if (!list.length) {
      panel.innerHTML = "";
      panel.classList.add("hidden");
      return;
    }
    panel.innerHTML = "";

    const head = document.createElement("div");
    head.className = "quality-head";
    const badge = document.createElement("span");
    badge.className = "source-badge quality";
    badge.textContent = "AI 发现";
    head.appendChild(badge);
    const disc = document.createElement("span");
    disc.className = "quality-disclaimer";
    disc.textContent = "AI 发现 → 自动取证核对 → 由您拍板；确认的事实类发现会列入清单、可生成修改建议并写入报告。";
    head.appendChild(disc);
    panel.appendChild(head);

    // 再核余量（沿用原需你确认预算行：只对派生问题类生效）
    const v = data.verify;
    if (v && v.available) {
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
    }

    const ul = document.createElement("ul");
    ul.className = "quality-list";
    list.forEach((f) => {
      const li = document.createElement("li");
      li.className = "quality-item verify-item";
      li.dataset.findingKey = f.finding_key || "";

      const line1 = document.createElement("p");
      line1.className = "quality-title-line";
      const dim = document.createElement("span");
      dim.className = "quality-dim";
      dim.textContent = f.source_type === "standalone"
        ? "需核实"
        : (QUALITY_DIM_LABEL[f.dimension] || "观察");
      line1.appendChild(dim);
      const title = document.createElement("span");
      title.className = "quality-title";
      title.textContent = f.title || "";
      line1.appendChild(title);
      const st = document.createElement("span");
      st.className = "verify-status";
      // 人的决定与核对进度分开呈现（codex 四轮 P1-1）：已再审只附加显示，
      // 不吞掉确认状态
      st.textContent = (FINDING_STATUS_LABEL[f.status] || f.status || "")
        + (f.check_state === "rechecked" ? "·已再审" : "");
      line1.appendChild(st);
      li.appendChild(line1);

      if (f.comment) {
        const c = document.createElement("p");
        c.className = "quality-comment";
        c.textContent = f.comment;
        li.appendChild(c);
      }

      if (f.quote) {
        const quote = document.createElement("div");
        quote.className = "quality-quote";
        quote.textContent = "原文：" + f.quote;
        li.appendChild(quote);
        const jump = document.createElement("button");
        jump.type = "button";
        jump.className = "link evidence-jump";
        jump.textContent = "看原文";
        jump.onclick = () => jumpToEvidence(quote, f.clause_id || null);
        li.appendChild(jump);
      }

      // 跨源标注（B7-5a）：同主题规则条目名，同主题≠结论一致
      // B7-5b C3：带 rule_item_id 时可点 → 跳到该规则条目并选中
      if (f.rule_link) {
        const rl = document.createElement("p");
        rl.className = "quality-meta-line";
        const tag = document.createElement("span");
        tag.className = "quality-clause";
        tag.textContent = f.rule_link;
        rl.appendChild(tag);
        if (f.rule_item_id) {
          const go = document.createElement("button");
          go.type = "button";
          go.className = "link finding-jump";
          go.textContent = "看这条规则";
          go.onclick = () => jumpToItem(f.rule_item_id);
          rl.appendChild(go);
        }
        li.appendChild(rl);
      }

      // 条款锚 + 核对证据（派生问题类）：核对结论中文直陈
      const heading = f.clause_id ? clauseHeadingById(f.clause_id) : null;
      if (heading || f.verification) {
        const verLabel = {
          verified: "摘句对得上",
          ambiguous: "摘句位置不唯一",
          missing: "原文未找到摘句",
          unverified: "尚未核对",
        };
        const meta = document.createElement("p");
        meta.className = "verify-meta";
        if (heading) {
          const cs = document.createElement("span");
          cs.textContent = "条款 " + heading;
          meta.appendChild(cs);
        }
        if (f.verification) {
          const ver = document.createElement("span");
          ver.textContent = verLabel[f.verification] || f.verification;
          meta.appendChild(ver);
        }
        li.appendChild(meta);
      }

      // 物化状态：确认后已列入逐条清单
      // B7-5b C3：在线（非撤回）且已知 item_id 时可点 → 回跳清单该条
      if (f.materialized) {
        const mat = document.createElement("p");
        mat.className = "verify-meta";
        const mt = document.createElement("span");
        mt.textContent = f.status === "disputed"
          ? "已从清单撤回（可恢复确认）"
          : "已列入下方清单，可生成修改建议、可追问";
        mat.appendChild(mt);
        if (f.status !== "disputed" && f.item_id) {
          const go = document.createElement("button");
          go.type = "button";
          go.className = "link finding-jump";
          go.textContent = "看清单中的这条";
          go.onclick = () => jumpToItem(f.item_id);
          mat.appendChild(go);
        }
        li.appendChild(mat);
      }

      const actions = document.createElement("div");
      actions.className = "verify-actions";

      if (f.status === "pending" && f.fact_type) {
        const btnOk = document.createElement("button");
        btnOk.type = "button";
        btnOk.className = "verify-btn-primary";
        btnOk.textContent = "确认无误";
        btnOk.onclick = () => submitFindingStatus(data.id, f.finding_key, "confirmed");
        actions.appendChild(btnOk);

        const btnNo = document.createElement("button");
        btnNo.type = "button";
        btnNo.className = "verify-btn-secondary";
        btnNo.textContent = "有异议";
        btnNo.onclick = () => submitFindingStatus(data.id, f.finding_key, "disputed");
        actions.appendChild(btnNo);
      } else if (f.status === "confirmed") {
        const btnUndo = document.createElement("button");
        btnUndo.type = "button";
        btnUndo.className = "verify-btn-secondary";
        btnUndo.textContent = "有异议（撤回）";
        btnUndo.onclick = () => submitFindingStatus(data.id, f.finding_key, "disputed");
        actions.appendChild(btnUndo);

        // 再审入口（B7-5b §4，codex 七轮 P2：仅 ≥1024 调整；<1024 保留现状）
        // 桌面：去掉常驻 ↻ 半成品图标（无 CSS=隐形入口+隐形扣费双杀），
        // 改条件化文字链接——只在「上次核对没把握」时出现；结论「摘句对得上」
        // 不出口（白耗余量）。移动端：↻ 图标照旧
        if (f.question_id) {
          if (mqDesktop.matches) {
            const lowConf = !f.verification
              || f.verification === "unverified"
              || f.verification === "ambiguous"
              || f.verification === "missing";
            if (lowConf) {
              const btnRe = document.createElement("button");
              btnRe.type = "button";
              btnRe.className = "link evidence-jump";
              btnRe.textContent = "再核一次（耗 1 次余量）";
              btnRe.onclick = () => submitVerifyRecheck(data.id, f.question_id);
              actions.appendChild(btnRe);
            }
          } else {
            const btnRe = document.createElement("button");
            btnRe.type = "button";
            btnRe.className = "verify-btn-recheck";
            btnRe.title = "再核一次（消耗再核余量）";
            btnRe.textContent = "↻";
            btnRe.onclick = () => submitVerifyRecheck(data.id, f.question_id);
            actions.appendChild(btnRe);
          }
        }
      } else if (f.status === "disputed") {
        const btnRevive = document.createElement("button");
        btnRevive.type = "button";
        btnRevive.className = "verify-btn-secondary";
        // 事实类恢复确认（复活检查项）；观点类恢复已阅（不物化）
        btnRevive.textContent = f.fact_type ? "恢复确认" : "恢复已阅";
        btnRevive.onclick = () => submitFindingStatus(
          data.id, f.finding_key, f.fact_type ? "confirmed" : "viewed");
        actions.appendChild(btnRevive);
      } else if (!f.fact_type) {
        // 观点类：默认已阅，仅提供异议入口（观点不物化、不可改写）
        const btnNo = document.createElement("button");
        btnNo.type = "button";
        btnNo.className = "verify-btn-secondary";
        btnNo.textContent = "有异议";
        btnNo.onclick = () => submitFindingStatus(data.id, f.finding_key, "disputed");
        actions.appendChild(btnNo);
      }
      if (actions.childNodes.length) li.appendChild(actions);
      ul.appendChild(li);
    });
    panel.appendChild(ul);
    panel.classList.remove("hidden");
  }
  /** 阶段 3 异议层：规则异议候选（疑似误报/疑似漏报）。
   * 铁律 3：异议永不改档位——这里只展示候选线索；「采纳」只生成规则改进
   * 提案供人工评审，不是当次改判。只渲染 accepted=true 的条目
   * （五要件拒收条目是内部留痕，不外露）；不可用与旧记录静默隐藏。
   * 模型态字段一律 textContent，零 innerHTML 拼接（同质量层纪律）。 */
  const OBJECTION_DIR_LABEL = {
    false_positive: "疑似误报",
    omission: "疑似漏报",
  };

  function renderObjections(data) {
    const panel = $("objections-panel");
    if (!panel) return;
    const obj = data.objections;
    const list = (obj && obj.available && Array.isArray(obj.objections))
      ? obj.objections
      : [];
    const shown = list.filter((x) => x && x.accepted);
    if (!shown.length) {
      panel.innerHTML = "";
      panel.classList.add("hidden");
      return;
    }
    panel.innerHTML = "";

    const head = document.createElement("div");
    head.className = "quality-head";
    const badge = document.createElement("span");
    badge.className = "source-badge quality";
    badge.textContent = "规则异议";
    head.appendChild(badge);
    const disclaimer = document.createElement("span");
    disclaimer.className = "quality-disclaimer";
    disclaimer.textContent = obj.disclaimer || "异议只是候选线索，不改变逐条核查结论；是否成立由人工与规则修订决定。";
    head.appendChild(disclaimer);
    panel.appendChild(head);

    const ul = document.createElement("ul");
    ul.className = "quality-list";
    list.forEach((ob, rawIndex) => {
      if (!ob || !ob.accepted) return; // 拒收留痕不渲染，但保留原始下标供采纳
      const li = document.createElement("li");
      li.className = "quality-item";

      const line1 = document.createElement("p");
      line1.className = "quality-title-line";
      const dir = document.createElement("span");
      dir.className = "quality-dim";
      dir.textContent = OBJECTION_DIR_LABEL[ob.direction] || "异议";
      line1.appendChild(dir);
      const title = document.createElement("span");
      title.className = "quality-title";
      title.textContent = itemLabelById(ob.item_id) || "对一条规则结论的异议";
      line1.appendChild(title);
      li.appendChild(line1);

      if (ob.quote) {
        const quote = document.createElement("div");
        quote.className = "quality-quote";
        quote.textContent = "原文：" + ob.quote;
        li.appendChild(quote);
        const jump = document.createElement("button");
        jump.type = "button";
        jump.className = "link evidence-jump";
        jump.textContent = "看原文";
        jump.onclick = () => jumpToEvidence(quote, { clause_id: ob.clause_id || null });
        li.appendChild(jump);
      }

      if (ob.legal_reasoning) {
        const reasoning = document.createElement("p");
        reasoning.className = "quality-comment";
        reasoning.textContent = ob.legal_reasoning;
        li.appendChild(reasoning);
      }

      // 提案文本：采纳后人工评审的唯一输入（3.2 交付物出口，textContent 零拼接）
      if (ob.proposal) {
        const proposal = document.createElement("p");
        proposal.className = "quality-comment objection-proposal";
        proposal.textContent = "改进提案：" + ob.proposal;
        li.appendChild(proposal);
      }

      const line4 = document.createElement("p");
      line4.className = "quality-meta-line";
      const heading = clauseHeadingById(ob.clause_id);
      if (heading) {
        const clause = document.createElement("span");
        clause.className = "quality-clause";
        clause.textContent = "条款 " + heading;
        line4.appendChild(clause);
      }
      if (ob.clause_ambiguous) {
        // 外审批 2：摘句跨多条款时明示（受理但归属不定，人工确认时重点核对）
        const amb = document.createElement("span");
        amb.className = "quality-clause";
        amb.textContent = "摘句跨多个条款";
        line4.appendChild(amb);
      }
      const confirm = document.createElement("span");
      confirm.className = "quality-confirm";
      confirm.textContent = "待人工确认";
      line4.appendChild(confirm);
      li.appendChild(line4);

      // 采纳动作：只生成规则改进提案（复制给人评审），不改当次结论
      const adoptBtn = document.createElement("button");
      adoptBtn.type = "button";
      adoptBtn.className = "link objection-adopt";
      if (ob.adopted) {
        adoptBtn.textContent = "已采纳（提案待人工评审）";
        adoptBtn.disabled = true;
      } else {
        adoptBtn.textContent = "采纳为规则改进提案";
        adoptBtn.onclick = () =>
          adoptObjection(data.id, rawIndex, adoptBtn);
      }
      li.appendChild(adoptBtn);

      ul.appendChild(li);
    });
    panel.appendChild(ul);
    panel.classList.remove("hidden");
  }

  async function adoptObjection(reviewId, index, btn) {
    if (!reviewId || typeof index !== "number") return;
    btn.disabled = true;
    try {
      const res = await fetch("/api/review/" + reviewId + "/objections/adopt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ index }),
      });
      const body = await res.json();
      if (!res.ok) {
        console.warn("adopt failed", body && body.detail);
        btn.disabled = false;
        return;
      }
      if (state.review && state.review.id === reviewId && state.review.objections
          && Array.isArray(state.review.objections.objections)) {
        state.review.objections.objections[index].adopted = true;
      }
      btn.textContent = "已采纳（提案待人工评审）";
    } catch (err) {
      console.warn("adopt error", err);
      btn.disabled = false;
    }
  }
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
      // 九哥词表：补盲区淡紫「补盲」；「待核实」只留给需你确认区
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

  /** 阶段 3 异议层：条目 id → 清单显示名（异议面板标题用）。 */
  function itemLabelById(itemId) {
    if (!itemId) return "";
    const items = state.review && state.review.items;
    if (!Array.isArray(items)) return "";
    const target = items.find((it) => it && it.id === itemId);
    return (target && (target.name || target.title)) || "";
  }

  function selectItem(item) {
    // B7-6：整壳统一——所有尺寸都是行内详情（移动端 hash 详情页随三屏
    // 模型退役；对话工作台入口亦走此路径）
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

    // B7-5b 详情头（右栏离清单远，详情必须自带身份）：名+档位 tag+来源徽章
    const head = $("detail-head");
    if (head) {
      head.innerHTML = "";
      const nm = document.createElement("span");
      nm.className = "detail-head-name";
      nm.textContent = item.name || "";
      head.appendChild(nm);
      head.appendChild(_statusTagEl(item));
      const hb = document.createElement("span");
      hb.className = item._blind
        ? "source-badge blind"
        : (item.tag_source === "ai_finding"
          ? "source-badge quality" : "source-badge rule");
      hb.textContent = item._blind
        ? "补盲"
        : (item.tag_source === "ai_finding" ? "AI 发现" : "系统核查");
      head.appendChild(hb);
      head.classList.remove("hidden");
    }

    // Blind candidates need human confirm; do not offer rule-style ask as if stamped
    if (item.status === "需关注" && !item._blind) {
      actions.classList.remove("hidden");
      $("detail-ask").onclick = () => openItemAsk(item);
    } else {
      actions.classList.add("hidden");
      $("detail-ask").onclick = null;
    }

    // B7-3a 三卡：符合条件的检查项挂载（GET state 只读恢复，零消耗）
    if (revEligible(item) && state.reviewId) {
      loadRevCards(state.reviewId, item);
    } else {
      $("rev-cards").classList.add("hidden");
    }

    detail.classList.remove("hidden");
    syncSideEmpty();
    detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /** 阶段 2.4：移动端点条目 = 独立详情页。hash 是唯一事实源——写 hash 后由
   * hashchange 统一驱动视图（Back 手势/返回键免费获得）；同 hash 重复点击
   * 不触发事件（已在页上，符合直觉）。 */
  /** 阶段 2.4 详情/追问屏已随 B7-6 三屏模型退役（hash 详情页/弹窗均废，
   * 统一为对话工作台 openItemAsk）。fillAskContext 全量初始化保留： */
  /** B7-5b（codex 八轮 P1-1 补明）：两种入口共用**完整初始化**——抽取范围
   * 是 openAskDetail 的全部前置工作（登记/seq 判废/立场文案/状态行/段1/
   * 段2/段3-4 可用性门禁与禁用态/清空旧答案与 chips），不只段1/段2。
   * 节点查找改 document 级：.ask-interact 搬进 dialog 后旧容器查找必失效。 */
  function fillAskContext(item) {
    // 切换条目即作废进行中的追问响应（可信度 P1 F04）
    if (!state.askItem || state.askItem.id !== item.id) {
      state.askReqSeq += 1;
    }
    state.askItem = item;
    // 页头：条目名 + 档位/来源双通道（tag 文字+底色；徽章描边）
    $("ask-title").textContent = item.name || "问清楚一点";
    const askGuide = $("ask-stance-guide");
    const askIron = $("ask-stance-iron");
    if (askGuide) askGuide.textContent = STANCE_EXPLAIN_GUIDE;
    if (askIron) askIron.textContent = STANCE_IRON_LAW;
    const statusLine = $("ask-status-line");
    statusLine.innerHTML = "";
    statusLine.appendChild(_statusTagEl(item));
    if (item.status === "需关注") {
      // 来源徽章（codex 九轮 P2-6：来源标识与档位 tag 是两回事，缺了会把
      // 来源信息丢成两个重复档位）
      const badge = document.createElement("span");
      badge.className = item._blind ? "source-badge blind" : "source-badge rule";
      badge.textContent = item._blind ? "补盲" : "系统核查";
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
    const interactCard = document.querySelector(".ask-interact");
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
      // codex 十五轮 P1-3：断线/刷新遗留的未完成提问——恢复卡（原编号重试）
      renderPendingRetry(item);
    }

  }

  /** B7-6：问清楚入口统一走对话工作台（openItemAsk）。openAskDetail 的
   * hash 问答屏随三屏模型退役；fillAskContext 全量初始化由 openItemAsk
   * 复用（selectItem 内含高亮+详情填充）。 */

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
    // B7-6 幂等键（codex 十三轮）：失败重试沿用同一 request_id（原 turn
    // 复活流转，不重复入账）；新问题新键。服务端按 (review_id, request_id)
    // 去重——断线重发/同键并发都只一条记录一次调用
    const retry = state.lastAskFail
      && state.lastAskFail.review_id === snapshot.reviewId
      && state.lastAskFail.item_id === snapshot.itemId
      && state.lastAskFail.question === question;
    const request_id = retry
      ? state.lastAskFail.request_id
      : ((window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : "req-" + Date.now().toString(36) + Math.random().toString(36).slice(2));
    // codex 十六轮 P1-1：请求身份在发送之前就落盘——请求在途关标签再重开，
    // 恢复卡才能接上（失败后才存会漏掉「发出但没等到回执」的窗口）
    state.lastAskFail = {
      review_id: snapshot.reviewId,
      item_id: snapshot.itemId, question, request_id,
    };
    savePendingAsk(state.lastAskFail);
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          review_id: snapshot.reviewId,
          item_id: snapshot.itemId,
          question,
          request_id,
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
      // codex 十六轮 P1-2：「仍在处理」不是「已回答」——processing 去重
      // 回执没有答案，不得清编号、不得渲染「暂无」。保留恢复编号，对话流
      // 显示等待态，轮询读最终结果（成功清编号 / 失败保留可原号重试）
      if (data.deduped && !data.answer) {
        const waitEl = appendWaitingBubble(question, state.askItem);
        threadScrollBottom();
        pollAskTurn(snapshot.reviewId, request_id, snapshot, waitEl, 0);
        return;
      }
      state.lastAskFail = null;
      clearPendingAsk();
      const verified = data.quote_verified !== false
        && !!(data.answer && data.answer.quote_verified !== false
              && data.answer["原文在哪"]
              && data.answer["原文在哪"] !== "未定位到原文");
      renderAnswer(data.answer || {}, data.raw_text, { quoteVerified: verified });
      ans.classList.remove("hidden");
      // B7-6 对话工作台：本轮问答归档进共享流（关于：××条目）
      appendThreadBubble(question, ans, state.askItem);
      threadScrollBottom();
    } catch (e) {
      if (!isAskResponseCurrent(snapshot)) return;
      state.lastAskFail = {
        review_id: snapshot.reviewId,
        item_id: snapshot.itemId, question, request_id,
      };
      savePendingAsk(state.lastAskFail);
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

  /** B7-6（codex 十五轮 P1-3）：未完成提问的持久化与恢复。
   * request_id 只活在内存变量里，刷新即换号——断线重发会二次调模型。
   * 失败即落 localStorage（随浏览器留存），成功即清；恢复卡提供明确
   * 重试入口，原编号原问题重发（服务端按 (review_id, request_id) 去重，
   * 不重复计费）。 */
  const PENDING_ASK_KEY = "ask-pending-v1";

  function savePendingAsk(p) {
    try {
      localStorage.setItem(PENDING_ASK_KEY, JSON.stringify({
        review_id: p.review_id, item_id: p.item_id,
        question: p.question, request_id: p.request_id,
        saved_at: Date.now(),
      }));
    } catch (e) { /* 隐私模式/配额满：降级为仅内存（旧行为） */ }
  }

  function clearPendingAsk() {
    try { localStorage.removeItem(PENDING_ASK_KEY); } catch (e) { /* 同上 */ }
  }

  function loadPendingAsk() {
    try {
      const raw = localStorage.getItem(PENDING_ASK_KEY);
      if (!raw) return null;
      const p = JSON.parse(raw);
      if (!p || !p.request_id || !p.question) return null;
      return p;
    } catch (e) { return null; }
  }

  /** 恢复卡：当前条目与遗留提问匹配时显示，点击回填原问题（发送时
   * 走同键重试路径——lastAskFail 已在启动时从 localStorage 恢复）。 */
  function renderPendingRetry(item) {
    const el = $("ask-pending");
    if (!el) return;
    const pending = loadPendingAsk();
    if (!pending || !state.reviewId || !item
        || pending.review_id !== state.reviewId
        || pending.item_id !== item.id) {
      el.classList.add("hidden");
      el.textContent = "";
      return;
    }
    el.textContent = "";
    const label = document.createElement("span");
    label.textContent = "上次有一条提问没有完成：「" + pending.question + "」 ";
    const btnRetry = document.createElement("button");
    btnRetry.type = "button";
    btnRetry.className = "link";
    btnRetry.textContent = "重新发送（不会重复计费）";
    btnRetry.addEventListener("click", () => {
      const input = $("ask-input");
      if (input) {
        input.value = pending.question;
        input.focus();
      }
      el.classList.add("hidden");
    });
    el.appendChild(label);
    el.appendChild(btnRetry);
    el.classList.remove("hidden");
  }

  /** B7-6 共享对话流：把刚渲染的 #ask-answer 归档成一条消息气泡
   * （关于：××条目 + 问题 + 四段式答案克隆），原槽位清空供下一轮。
   * renderAnswer 按 ID 写入，故历史轮用内容克隆而非节点搬移。 */
  function appendThreadBubble(question, answerEl, item) {
    const thread = $("ask-thread");
    if (!thread || !answerEl) return;
    const msg = document.createElement("div");
    msg.className = "ask-msg ask-msg-user";
    const about = document.createElement("p");
    about.className = "ask-msg-about";
    about.textContent = "关于：" + ((item && item.name) || "");
    const q = document.createElement("p");
    q.className = "ask-msg-q";
    q.textContent = question;
    const ansWrap = document.createElement("div");
    ansWrap.className = "ask-msg-ans";
    const ansClone = document.createElement("div");
    ansClone.className = "answer";
    ansClone.innerHTML = answerEl.innerHTML;
    ansWrap.appendChild(ansClone);
    msg.appendChild(about);
    msg.appendChild(q);
    msg.appendChild(ansWrap);
    thread.appendChild(msg);
    answerEl.innerHTML = "";
    answerEl.classList.add("hidden");
  }

  /** B7-6（codex 十六轮 P1-2）：处理中等待气泡——processing 去重回执
   * 没有答案，对话流如实显示等待态（拿到最终结果后整条移除）。 */
  function appendWaitingBubble(question, item) {
    const thread = $("ask-thread");
    if (!thread) return null;
    const msg = document.createElement("div");
    msg.className = "ask-msg ask-msg-waiting";
    const about = document.createElement("p");
    about.className = "ask-msg-about";
    about.textContent = "关于：" + ((item && item.name) || "");
    const q = document.createElement("p");
    q.className = "ask-msg-q";
    q.textContent = question;
    const st = document.createElement("p");
    st.className = "ask-msg-state";
    st.textContent = "正在回答…";
    msg.appendChild(about);
    msg.appendChild(q);
    msg.appendChild(st);
    thread.appendChild(msg);
    return msg;
  }

  /** B7-6（codex 十六轮 P1-1/2）：处理中轮询——2 秒一次、上限 2 分钟。
   * success：清编号+渲染气泡（仅当仍在同一审查同一条目；离开过就交给
   * syncThreadFromServer 补渲，旧快照不回写）；failed：保留编号可原号
   * 重试；超时：如实告知并指向恢复卡。 */
  function pollAskTurn(reviewId, requestId, snapshot, waitEl, attempt, question) {
    if (attempt > 60) {
      if (waitEl && waitEl.parentNode) waitEl.remove();
      const err = $("ask-error");
      if (err && state.askItem && state.askItem.id === snapshot.itemId
          && state.reviewId === reviewId) {
        err.textContent = "等了很久还没拿到结果，请点恢复卡重新发送（原编号重试，不会重复计费）";
        err.classList.remove("hidden");
      }
      return;
    }
    fetch("/api/review/" + encodeURIComponent(reviewId))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("read"))))
      .then((data) => {
        const turns = (data && data.ask_thread && data.ask_thread.turns) || [];
        const turn = turns.find((t) => t && t.request_id === requestId);
        if (!turn || turn.state === "processing") {
          setTimeout(() => pollAskTurn(
            reviewId, requestId, snapshot, waitEl, attempt + 1, question), 2000);
          return;
        }
        if (waitEl && waitEl.parentNode) waitEl.remove();
        if (turn.state === "success") {
          state.lastAskFail = null;
          clearPendingAsk();
          if (state.reviewId === reviewId && state.askItem
              && state.askItem.id === snapshot.itemId) {
            const verified = turn.quote_verified !== false
              && !!(turn.answer && turn.answer.quote_verified !== false
                    && turn.answer["原文在哪"]
                    && turn.answer["原文在哪"] !== "未定位到原文");
            const box = document.createElement("div");
            renderAnswer(turn.answer || {}, turn.raw_text || "",
              { quoteVerified: verified }, box);
            appendThreadBubble(turn.question || question || "",
              box, state.askItem);
            threadScrollBottom();
          }
          // 已离开本条目：syncThreadFromServer 在切回时按持久化轮次补渲
        } else {
          const err = $("ask-error");
          if (err && state.askItem && state.askItem.id === snapshot.itemId
              && state.reviewId === reviewId) {
            err.textContent = turn.error || "这次没有答上来，可原编号重试";
            err.classList.remove("hidden");
          }
        }
      })
      .catch(() => {
        // 读档失败不放弃：继续轮询（服务端归档是事实源）
        setTimeout(() => pollAskTurn(
          reviewId, requestId, snapshot, waitEl, attempt + 1, question), 2000);
      });
  }

  /** B7-6 共享对话流：从持久化 turns 渲染全部历史（刷新恢复/未选条目
   * 也可查看——codex 十二轮 P2-5：选中只门禁发送，不门禁查看） */
  function renderThreadFromData(turns) {
    const thread = $("ask-thread");
    if (!thread) return;
    thread.innerHTML = "";
    (turns || []).forEach((t) => {
      const msg = document.createElement("div");
      msg.className = "ask-msg ask-msg-user";
      const about = document.createElement("p");
      about.className = "ask-msg-about";
      about.textContent = "关于：" + (t.item_name || "");
      const q = document.createElement("p");
      q.className = "ask-msg-q";
      q.textContent = t.question || "";
      const ansWrap = document.createElement("div");
      ansWrap.className = "ask-msg-ans";
      if (t.state === "success" && t.answer) {
        const ansDiv = document.createElement("div");
        ansDiv.className = "answer";
        renderAnswer(t.answer, t.raw_text || "",
          { quoteVerified: t.quote_verified !== false }, ansDiv);
        ansWrap.appendChild(ansDiv);
      } else if (t.state === "processing") {
        const st = document.createElement("p");
        st.className = "ask-msg-state";
        st.textContent = "正在回答…";
        ansWrap.appendChild(st);
      } else if (t.state === "failed") {
        const st = document.createElement("p");
        st.className = "ask-msg-state";
        st.textContent = "这次没有答上来" + (t.error ? "（" + t.error + "）" : "");
        ansWrap.appendChild(st);
      }
      msg.appendChild(about);
      msg.appendChild(q);
      msg.appendChild(ansWrap);
      thread.appendChild(msg);
    });
  }

  function threadScrollBottom() {
    const thread = $("ask-thread");
    if (thread) thread.scrollTop = thread.scrollHeight;
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
  function renderAnswer(answer, raw, opts, targetEl) {
    const box = targetEl || $("ask-answer");
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
  }

  // 浏览器 Back/Forward 与界面同步（小智娘门禁 P2-4：hash 已退界面还在，
  // 此时刷新会让「刷新丢结果」事故从 Back 路径复发）。
  // B7-6：item 段路由随三屏模型退役——hash 只有 #/review/{rid} 一种形态。
  window.addEventListener("hashchange", () => {
    // codex 十轮 P1：任何 hash 导航都视为退出当前问答上下文（对话工作台
    // 自身不触发 hash 事件——凡 hash 变了必是用户在导航，旧问答一律清场）
    closeAskContext();
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
    show("results");
    // codex 十二轮 P2-5：滚动位置存取迁移到工作区滚动容器
    $("shell-main").scrollTop = state.resultsScrollY || 0;
  });

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
    closeAskContext();
    clearReviewHash();
    state.reviewId = null;
    show("upload");
    renderShellHistory();
  });
  // B7-6：详情返回按钮随 hash 详情页退役——按钮仍在结果头（回空态引导），
  // 行为与「重新上传」一致
  const backResults = $("btn-back-results");
  if (backResults) {
    // 节点随 #screen-ask 退役（B7-6）：绑定留空守卫，防旧引用崩
    backResults.addEventListener("click", () => {
      closeAskContext();
      clearReviewHash();
      state.reviewId = null;
      show("upload");
      renderShellHistory();
    });
  }

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

  // 外审 P1-3：HTTP 红线警示——非加密连接时显眼常驻（HTTPS 下移除）。
  // 判定按 location.protocol（file:// 本地开发不吓人，https 正式环境不显示）
  (function renderHttpWarning() {
    const el = $("http-warning");
    if (!el) return;
    if (location.protocol === "https:") {
      el.classList.add("hidden");
    } else if (location.protocol === "http:") {
      el.classList.remove("hidden");
    }
  })();

  // ===== B7-3a「改得有用」三卡（线框 docs/b7-3/wireframe-b7-3a.md）=====
  // GET state 只读恢复；触发类 POST 用户显式点按；过期响应丢弃；
  // 建议不回写判定层。挂载点：桌面 #rev-cards、移动 #rev-cards-ask，
  // 由当前可见屏决定渲染目标（不看 offsetParent，看屏类名，确定性优先）。
  const REV_STATE = { seq: 0, ctx: null, busy: false, lastData: null };

  function revBoxes() {
    return Array.prototype.slice.call(document.querySelectorAll(".rev-cards"));
  }

  function revHideAll() {
    revBoxes().forEach((b) => b.classList.add("hidden"));
  }

  function revPickBox() {
    // B7-6：三屏退役，三卡固定挂右栏详情内的 #rev-cards（对话工作台/详情
    // 同容器滚动），不再有移动端第二挂载点
    return $("rev-cards");
  }

  function revFmtTime(ts) {
    try {
      const d = new Date(ts * 1000);
      return (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
        String(d.getHours()).padStart(2, "0") + ":" +
        String(d.getMinutes()).padStart(2, "0");
    } catch (e) { return ""; }
  }

  /** 读取失败层（线框 §1.5）：kind = network | login | gone。
   * 只重发 GET state（纯读库零消耗），绝不触发任何 AI 调用。 */
  function renderRevReadFail(kind) {
    REV_STATE.busy = false;
    const box = revPickBox();
    if (!box || box.classList.contains("hidden")) return;
    if (kind === "login") {
      box.innerHTML =
        '<p class="rev-line">登录已过期，请重新登录后再查看。</p>' +
        '<button type="button" class="rev-btn" data-action="relogin">重新登录</button>';
    } else if (kind === "gone") {
      box.innerHTML =
        '<p class="rev-line">这份审查记录已过期或不存在，无法查看修改建议。</p>' +
        '<button type="button" class="rev-btn" data-action="back">返回清单</button>';
    } else {
      box.innerHTML =
        '<p class="rev-line">暂时读不到您的方案记录，请检查网络后重新读取。</p>' +
        '<button type="button" class="rev-btn" data-action="reread">重新读取</button>';
    }
  }

  /** 三卡渲染：data 来自 GET state（只读渲染，不发明字段）。 */
  function renderRevCards(item, data) {
    const box = revPickBox();
    if (!box || box.classList.contains("hidden")) return;
    const sel = data.selected || null;
    const parts = [];

    // 卡一：改写建议卡
    parts.push('<div class="rev-card"><p class="rev-card-title">✎ AI 修改建议</p>');
    const drafts = data.drafts || [];
    const remR = data.remaining ? data.remaining.revision : 0;
    if (REV_STATE.busy === "generate") {
      parts.push('<p class="rev-line">正在生成…</p>');
    } else if (!drafts.length && remR === 0) {
      parts.push('<p class="rev-line">本合同的生成次数已用完。</p>');
    } else if (!drafts.length) {
      parts.push('<button type="button" class="rev-btn primary" data-action="generate">生成修改建议</button>');
      parts.push('<p class="rev-caption">本合同还可生成 ' + remR + " 次</p>");
    } else {
      drafts.forEach((d, i) => {
        const head = (i === 0 ? "最新生成 · " : "此前生成 · ") + revFmtTime(d.created_at);
        const selMark = d.is_selected
          ? ' <span class="rev-chip">● 您已从这批选定</span>'
          : "";
        parts.push('<p class="rev-caption">' + head + selMark + "</p>");
        if (d.status === "ok") {
          (d.versions || []).forEach((v, vi) => {
            const selectedHere = !!(sel && sel.draft_id === d.draft_id &&
              sel.stance === v.stance);
            parts.push('<div class="rev-version' + (selectedHere ? " selected" : "") + '">');
            parts.push('<p class="rev-version-title">方案' + "一二三"[vi] + " · " +
              escapeHtml(v.stance || "") + (selectedHere ? "（已选定）" : "") + "</p>");
            parts.push('<p class="rev-text">' + escapeHtml(v.text || "") + "</p>");
            parts.push('<p class="rev-caption">适用：' + escapeHtml(v.note || "") + "</p>");
            parts.push('<button type="button" class="rev-btn" data-action="select" data-draft="' +
              escapeHtml(d.draft_id) + '" data-stance="' + escapeHtml(v.stance || "") +
              '">选定这一版</button>');
            parts.push("</div>");
          });
          parts.push('<p class="rev-caption">三版共同理由：' + escapeHtml(d.rationale || "") + "</p>");
          const asm = d.assumptions || [];
          if (asm.length) {
            parts.push('<p class="rev-caption">事实假设：</p>');
            asm.forEach((a) => {
              parts.push('<p class="rev-caption">· ' + escapeHtml(a) + "</p>");
            });
          }
          parts.push('<p class="rev-caption">' + escapeHtml(d.declaration || "") + "</p>");
        } else {
          // 降级态如实显示，不展示残缺方案；重试要花次数，先讲清楚
          parts.push('<p class="rev-line">修改建议这次没有生成成功，已为您记录在案。</p>');
          if (remR > 0 && i === 0) {
            parts.push('<button type="button" class="rev-btn" data-action="generate">再试一次</button>');
            parts.push('<p class="rev-caption">再试一次将消耗一次可用次数（剩 ' + remR + " 次）</p>");
          }
        }
      });
    }
    if (remR === 0 && drafts.length) {
      parts.push('<p class="rev-caption">本合同的生成次数已用完。</p>');
    }
    // B7-3b 已交付草案章节，导出承诺成立（#163 复批 3 撤下的那句加回）；
    // 未选定时报告只写「未选定修改建议」，承诺句不成立故不显示
    if (sel) {
      parts.push('<p class="rev-caption">导出的报告会包含您选定的方案</p>');
    }
    parts.push("</div>");

    // 卡二：选定卡
    parts.push('<div class="rev-card"><p class="rev-card-title">☑ 您选定的版本</p>');
    if (!sel) {
      parts.push('<p class="rev-line">还没有选定。请从上方三版中选定一版。</p>');
    } else {
      const selBatch = drafts.find((d) => d.draft_id === sel.draft_id) || null;
      parts.push('<p class="rev-line">已选定：' + escapeHtml(sel.stance || "") + "版" +
        (selBatch ? "（" + revFmtTime(selBatch.created_at) + " 生成的那批）" : "") + "</p>");
      parts.push('<p class="rev-caption">选定只代表您的选择，不会自动修改合同，也不代表重新审查通过。</p>');
      parts.push('<p class="rev-caption">导出的报告会包含您选定的方案</p>');
    }
    parts.push("</div>");

    // 卡三：牵连检查卡
    parts.push('<div class="rev-card"><p class="rev-card-title">⛓ 改动会影响其他条款吗</p>');
    const imp = data.impact || null;
    const remI = data.remaining ? data.remaining.impact : 0;
    if (!sel) {
      // 未选定前置闸：有选定才能跑连带分析（后端同样校验，双保险）
      parts.push('<p class="rev-line">请先在上方选定一版，再检查其他条款。</p>');
      parts.push('<button type="button" class="rev-btn" disabled>检查其他条款</button>');
    } else if (REV_STATE.busy === "impact") {
      parts.push('<p class="rev-line">正在检查…</p>');
    } else if (!imp) {
      parts.push('<button type="button" class="rev-btn" data-action="impact">检查其他条款</button>');
      parts.push('<p class="rev-caption">可用次数 ' + remI + "</p>");
    } else {
      const stale = !!(imp.legacy || (imp.bound && sel &&
        (imp.bound.draft_id !== sel.draft_id || imp.bound.stance !== sel.stance)));
      if (imp.legacy) {
        parts.push('<p class="rev-line">这是一条历史检查记录（当时未登记版本），建议重新检查。</p>');
      } else if (imp.bound && sel &&
        (imp.bound.draft_id !== sel.draft_id || imp.bound.stance !== sel.stance)) {
        parts.push('<p class="rev-line">该结果对应您此前选定的版本，已过期。建议对新选定版本重新检查。</p>');
      }
      if (imp.state === "ok" && !stale) {
        // 摘录与说明按下标对齐；缺任一侧不凑对（宁缺一条，不错配一条）
        (imp.excerpts || []).forEach((e, i) => {
          const f = (imp.findings || []).find((x) => x.index === i);
          if (!f) return;
          parts.push('<div class="rev-finding">');
          parts.push('<p class="rev-caption">▸ 第 ' + (i + 1) + " 条</p>");
          if (e.excerpt_truncated) {
            parts.push('<p class="rev-caption">（本条款过长已按段摘录，非完整条款原文）</p>');
          }
          // B7-5b C3：牵连摘录「看原文」（codex 九轮 P2-3：定位对象=摘录
          // 本身——flash 摘录文本，不碰当前条目的「所在条款」身份提示）
          parts.push('<p class="rev-text" data-rev-excerpt>摘录：' + escapeHtml(e.excerpt || "") + "</p>");
          if (e.clause_id) {
            parts.push('<button type="button" class="link evidence-jump" data-rev-jump="' + escapeHtml(String(e.clause_id)) + '">看原文</button>');
          }
          parts.push('<p class="rev-caption">说明：' + escapeHtml(f.note || "") + "</p>");
          parts.push("</div>");
        });
        if ((imp.excerpts || []).some((e) => e.excerpt_truncated)) {
          parts.push('<p class="rev-caption">本条款过长：相关内容已按段送审并摘录，摘录非完整条款原文。</p>');
        }
        const hid = imp.hidden_findings || 0;
        if (hid > 0) {
          parts.push('<p class="rev-caption">有 ' + hid + " 条说明未通过安全检查，已如实隐藏。</p>");
          parts.push('<p class="rev-caption">牵连分析结果不完整（' + hid + " 条说明未通过安全检查）。</p>");
        }
        if (imp.notice) {
          parts.push('<p class="rev-caption">' + escapeHtml(imp.notice) + "</p>");
        }
      } else if (imp.state === "uncovered" && !stale) {
        // F1/F2 两种「没查到」如实分开：材料缺失≠检索未命中
        if (imp.uncovered_code === "missing_source") {
          parts.push('<p class="rev-line">这份合同缺少可对照的完整内容，暂时无法检查其他条款。</p>');
        } else {
          parts.push('<p class="rev-line">检查过了：合同其他部分没有检索到与这处改动相关的条款。这不代表没有冲突。</p>');
        }
      } else if (imp.state === "degraded") {
        parts.push('<p class="rev-line">这次检查没有完成，已为您记录在案。</p>');
        if (remI > 0) {
          parts.push('<button type="button" class="rev-btn" data-action="impact">再试一次</button>');
          parts.push('<p class="rev-caption">再试一次将消耗一次可用次数（剩 ' + remI + " 次）</p>");
        }
      } else {
        // not_initiated / 过期 / legacy：出「检查/重新检查」按钮（次数用完只留文案）
        if (remI > 0) {
          parts.push('<button type="button" class="rev-btn" data-action="impact">检查其他条款</button>');
          parts.push('<p class="rev-caption">可用次数 ' + remI + "</p>");
        } else {
          parts.push('<p class="rev-caption">检查次数已用完</p>');
        }
      }
    }
    parts.push("</div>");

    box.innerHTML = parts.join("");
  }

  /** 迟到响应判废：seq 变了或上下文（审查单+检查项）对不上 = 请求已
   * 过期，丢弃不渲染（codex #163 复批 5——成功失败一律先验上下文）。 */
  function revStaleResp(seq, reviewId, itemId) {
    return seq !== REV_STATE.seq || !REV_STATE.ctx ||
      REV_STATE.ctx.reviewId !== reviewId ||
      REV_STATE.ctx.itemId !== itemId;
  }

  /** 触发类 POST（用户显式点按）。返回后一律 GET state 重渲染真实态
   * （业务结果不在本地猜测——严格圈账口径在后端）。busy 在 POST 返回
   * 时一律复位（codex #163 复批 1：成功路径不复位会永远「正在生成」）；
   * 过期响应丢弃：上下文对不上不覆盖当前显示。 */
  async function revTrigger(action, reviewId, item, body) {
    if (REV_STATE.busy) return;
    const seq = REV_STATE.seq;
    const suffixByAction = {
      generate: "/revision",
      select: "/revision/select",
      impact: "/revision/impact",
    };
    const suffix = suffixByAction[action];
    if (!suffix) return;
    REV_STATE.busy = action;
    renderRevCards(item, REV_STATE.lastData || {}); // busy 文案 + 按钮禁用
    let res;
    try {
      res = await fetch("/api/review/" + reviewId + "/items/" + item.id + suffix, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (e) {
      REV_STATE.busy = false;
      if (revStaleResp(seq, reviewId, item.id)) return;
      renderRevReadFail("network");
      return;
    }
    // POST 已返回：忙碌态一律复位，后续渲染全走 GET 真实态
    REV_STATE.busy = false;
    if (res.status === 401 || res.status === 404) {
      if (revStaleResp(seq, reviewId, item.id)) return;
      renderRevReadFail(res.status === 401 ? "login" : "gone");
      return;
    }
    if (revStaleResp(seq, reviewId, item.id)) return; // 已切项：丢弃不刷新
    await loadRevCards(reviewId, item);
  }

  /** 三卡容器点击委托（按钮随渲染重建，事件挂容器不挂按钮）。 */
  function bindRevCards() {
    revBoxes().forEach((box) => {
      if (box.dataset.revBound) return;
      box.dataset.revBound = "1";
      box.addEventListener("click", (ev) => {
        // B7-5b C3：牵连摘录「看原文」（codex 九轮 P2-3）——定位=flash
        // 对应摘录文本；绝不调用 jumpToEvidence（它会把当前条目的
        // 「所在条款」旁注改写成牵连条款）
        const jumpBtn = ev.target.closest("[data-rev-jump]");
        if (jumpBtn) {
          const finding = jumpBtn.closest(".rev-finding");
          const ex = finding ? finding.querySelector("[data-rev-excerpt]") : null;
          if (ex) {
            ex.scrollIntoView({ behavior: "smooth", block: "center" });
            ex.classList.add("is-flash");
            setTimeout(() => ex.classList.remove("is-flash"), 1600);
          }
          return;
        }
        const btn = ev.target.closest("[data-action]");
        if (!btn || btn.disabled) return;
        const ctx = REV_STATE.ctx;
        if (!ctx) return;
        const act = btn.dataset.action;
        if (act === "reread") { loadRevCards(ctx.reviewId, ctx.item); return; }
        if (act === "relogin") { window.location.reload(); return; }
        if (act === "back") {
          // 记录已失效：走审查 hash 回清单（#/results 不是合法路由，
          // 会命中 route.rid=null 分支把用户踢回上传页）
          if (ctx.reviewId) window.location.hash = "#/review/" + ctx.reviewId;
          else show("results");
          return;
        }
        if (act === "generate") {
          revTrigger("generate", ctx.reviewId, ctx.item, {});
          return;
        }
        if (act === "select") {
          revTrigger("select", ctx.reviewId, ctx.item, {
            draft_id: btn.dataset.draft,
            stance: btn.dataset.stance,
          });
          return;
        }
        if (act === "impact") {
          const sel = ctx.selected;
          if (!sel) return;
          revTrigger("impact", ctx.reviewId, ctx.item, { draft_id: sel.draft_id });
        }
      });
    });
  }

  /** 入口：点开「需关注」检查项详情时调用（桌面/移动共用渲染函数）。 */
  function loadRevCards(reviewId, item) {
    bindRevCards();
    REV_STATE.seq += 1;
    const seq = REV_STATE.seq;
    REV_STATE.ctx = { reviewId: reviewId, itemId: item.id, item: item, selected: null };
    const box = revPickBox();
    revHideAll();
    if (!box) return;
    box.classList.remove("hidden");
    box.innerHTML = '<p class="rev-line">正在读取…</p>';
    fetch("/api/review/" + reviewId + "/items/" + item.id + "/revision/state")
      .then(async (res) => {
        // 过期判定前置（codex #163 复批 5）：切项后旧请求的成功/失败
        // 分支一律丢弃，绝不覆盖新条目
        if (seq !== REV_STATE.seq) return;
        if (res.status === 401) { renderRevReadFail("login"); return; }
        if (res.status === 404) { renderRevReadFail("gone"); return; }
        if (!res.ok) { renderRevReadFail("network"); return; }
        const data = await res.json();
        // 二次校验（codex #163 二轮 P1）：读正文是异步的——头到时没切项
        // 不代表正文读完没切，这里不复查就会把旧条目内容渲染到新条目上
        if (seq !== REV_STATE.seq) return;
        if (data.selected) REV_STATE.ctx.selected = data.selected;
        REV_STATE.lastData = data;
        renderRevCards(item, data);
      })
      .catch(() => {
        if (seq === REV_STATE.seq) renderRevReadFail("network");
      });
  }

  /** 三卡挂载门禁：仅「需关注且非补盲且有原文摘句」的检查项出三卡
   * （与追问卡同一族；补盲待人工确认，不让它混进建议流）。 */
  function revEligible(item) {
    return item.status === "需关注" && !item._blind && !!item.quote;
  }

  /** B7-5b 桌面双栏 + 「问清楚」对话框（阳仔线框；codex 七/八轮补明）。
   * 节点搬迁零复制（revPickBox/mqMobile 同款先例）；<1024 搬回原位，
   * DOM 与旧版逐字节一致。判废机制一行不碰——关闭弹窗只是 seq 计数器的
   * 新增使用点（守卫判定逻辑原样）。 */
  const mqDesktop = window.matchMedia("(min-width: 1024px)");
  const askWorkbench = $("ask-workbench");

  function _statusTagEl(item) {
    const tag = document.createElement("span");
    tag.className = `tag ${statusClass(item.status)}`;
    tag.textContent = statusLabel(item.status);
    return tag;
  }

  function syncSideEmpty() {
    // 右栏空态：仅桌面且未选中条目时可见（显隐与 item-detail.hidden 取反）
    const se = $("side-empty");
    const d = $("item-detail");
    if (!se || !d) return;
    se.classList.toggle(
      "hidden", !(mqDesktop.matches && d.classList.contains("hidden")));
  }

  function cleanupAskSession() {
    // codex 七轮 P1-2：作废在途响应+按钮复位——晚到旧响应被
    // isAskResponseCurrent 的 seq 比对拒写；守卫逻辑本身零改动
    state.askReqSeq += 1;
    const btn = $("btn-ask");
    if (btn) btn.disabled = false;
  }

  function applyDesktopLayout() {
    // 搬迁清单（codex 十二轮 P2-5：detail+对话块两节点都显式搬，
    // 「原样搬迁」不覆盖新增节点）
    const aside = document.querySelector(".col-side");
    const detail = $("item-detail");
    if (detail && aside) {
      if (mqDesktop.matches) {
        if (detail.parentNode !== aside) aside.appendChild(detail);
      } else if (detailAnchor.parent
                 && detail.parentNode !== detailAnchor.parent) {
        detailAnchor.parent.insertBefore(detail, detailAnchor.next);
      }
    }
    if (askWorkbench && aside) {
      if (mqDesktop.matches) {
        if (askWorkbench.parentNode !== aside) aside.appendChild(askWorkbench);
      } else if (wbAnchor.parent && askWorkbench.parentNode !== wbAnchor.parent) {
        wbAnchor.parent.insertBefore(askWorkbench, wbAnchor.next);
      }
    }
    syncSideEmpty();
  }

  // 锚点捕获（启动时一次；next 存真实节点引用，节点搬走后引用依然有效）
  const detailAnchor = (() => {
    const d = $("item-detail");
    return { parent: d.parentNode, next: d.nextSibling };
  })();
  const wbAnchor = (() => {
    const w = askWorkbench;
    return w ? { parent: w.parentNode, next: w.nextSibling }
             : { parent: null, next: null };
  })();

  function jumpToItem(itemId) {
    // C3 跨源跳转：复用行点击=高亮+右栏填充（任何跳转不丢当前位置）
    const row = document.querySelector(
      `.item[data-item-id="${CSS.escape(String(itemId))}"]`);
    if (row) row.click();
  }

  function scrollToEl(el) {
    if (el) el.scrollIntoView({ block: "start" }); // 零动画纪律：不用 smooth
  }

  /** codex 十轮 P1：退出问答上下文统一清理——清 seq+旧条目+按钮复位
   * （弹窗已随 B7-6 退役；askItem 一清，迟到旧响应双保险拒写） */
  function closeAskContext() {
    state.askItem = null;
    state.askReqSeq += 1;
    const btn = $("btn-ask");
    if (btn) btn.disabled = false;
  }

  /** B7-6 对话工作台：问清楚入口统一——选中+全量初始化+滚动聚焦。
   * 原 openAskDetail（hash 问答屏）与 openAskDialog（弹窗）均随三屏模型退役。 */
  function openItemAsk(item) {
    selectItem(item);
    fillAskContext(item);
    if (askWorkbench) {
      askWorkbench.classList.remove("hidden");
      syncSideEmpty();
      askWorkbench.scrollIntoView({ block: "center" });
      const input = $("ask-input");
      if (input) input.focus();
    }
    // codex 十五轮 P2-4：回到条目即按持久化轮次同步共享流——甲条回答中
    // 切到乙条，页面守卫拒写的迟到响应已归档在服务端；切回时不重读就
    // 只能整页刷新才见。守卫保留零改动，这里只做「以服务端为准」的补渲
    syncThreadFromServer();
  }

  /** B7-6（codex 十五轮 P2-4）：从服务端补渲共享对话流（纯读，零费用）。
   * seq 守卫：同步期间发生任何新交互（发问/切条目）则放弃——那些路径
   * 自带渲染，旧快照不得回写盖住新状态。 */
  function syncThreadFromServer() {
    const rid = state.reviewId;
    if (!rid) return;
    const seqAtReq = state.askReqSeq;
    fetch("/api/review/" + encodeURIComponent(rid))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        if (state.reviewId !== rid) return;
        if (state.askReqSeq !== seqAtReq) return;
        renderThreadFromData((data.ask_thread && data.ask_thread.turns) || []);
      })
      .catch(() => { /* 纯增益路径：失败静默（下次切条目再试） */ });
  }

  mqDesktop.addEventListener("change", () => {
    applyDesktopLayout();
    // codex 十五轮 P2-7：断点切到桌面时同步退出抽屉——抽屉开着拉宽窗口，
    // 汉堡已隐藏（桌面顶栏无此按钮），inert 却还罩着工作区，交互死路
    if (mqDesktop.matches) shellDrawer(false);
    // codex 九轮 P2-2：跨断点刷新 AI 发现面板（↻ 入口随宽度双向切换）
    if (state.review) renderAiFindings(state.review);
  });
  applyDesktopLayout(); // 首次进入即按当前断点归位

  // ---- B7-6 左栏：历史记录 / 当前合同导航 / 抽屉 / 空态入口 ----

  function shellDrawer(open) {
    const nav = $("shell-nav");
    const mask = $("drawer-mask");
    const btn = $("shell-menu");
    if (!nav) return;
    nav.classList.toggle("is-open", open);
    if (mask) mask.classList.toggle("hidden", !open);
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    const main = $("shell-main");
    if (main) {
      // codex 十二轮 P2-6：inert 只罩右工作区——顶栏保持可点（汉堡关闭通道）
      try { main.inert = open; } catch (e) { /* 老浏览器无 inert：降级不阻断 */ }
    }
    document.body.style.overflow = open ? "hidden" : "";
    if (open) {
      const first = nav.querySelector(".shell-item, .shell-new");
      if (first) first.focus();
    } else if (btn) {
      btn.focus();
    }
  }

  function renderShellHistory() {
    const ul = $("shell-history");
    if (!ul) return;
    fetch("/api/reviews?limit=50")
      .then((r) => (r.ok ? r.json() : { reviews: [] }))
      .then((data) => {
        ul.innerHTML = "";
        const list = data.reviews || [];
        if (!list.length) {
          const li = document.createElement("li");
          li.className = "shell-item";
          li.style.cursor = "default";
          li.textContent = "还没有审查记录，点上方按钮开始第一份";
          ul.appendChild(li);
          return;
        }
        list.forEach((rv) => {
          const li = document.createElement("li");
          li.className = "shell-item" + (rv.id === state.reviewId ? " is-current" : "");
          li.dataset.rid = rv.id;
          const mainL = document.createElement("p");
          mainL.className = "shell-item-main";
          mainL.textContent = rv.filename || "(未命名)";
          const sub = document.createElement("p");
          sub.className = "shell-item-sub";
          const dot = document.createElement("span");
          dot.className = "shell-dot st-" + (rv.status === "done" ? "done"
            : rv.status === "error" ? "error" : "running");
          sub.appendChild(dot);
          sub.appendChild(document.createTextNode(
            (rv.category_label || rv.category || "") + " · " +
            (rv.status === "done" ? "已完成" : rv.status === "error" ? "失败" : "审查中")));
          li.appendChild(mainL);
          li.appendChild(sub);
          li.onclick = () => {
            shellDrawer(false);
            if (rv.id === state.reviewId) return;
            setReviewHash(rv.id); // hashchange 驱动 resumeFromHash 加载
          };
          ul.appendChild(li);
        });
      })
      .catch(() => { /* 历史列表加载失败静默（侧栏降级为无历史，主流程不受影响） */ });
  }

  /** 左栏下区：当前合同导航（锚点跳转，不复制内容；codex 十二轮 P2-5：
   * 对话历史不依赖选中态——未选条目也可经「对话」锚点查看全部历史） */
  function renderShellContext() {
    const ul = $("shell-context");
    if (!ul) return;
    ul.innerHTML = "";
    const data = state.review;
    if (!data) return;
    const entries = [
      { label: "概览", act: () => scrollToEl($("results-meta")) },
      { label: "清单", act: () => scrollToEl($("item-list")) },
    ];
    (data.items || []).filter((i) => i.status === "需关注").forEach((i) => {
      entries.push({ label: i.name || i.id, act: () => jumpToItem(i.id) });
    });
    const af = data.ai_findings;
    const pending = (af && af.available && Array.isArray(af.findings))
      ? af.findings.filter((f) => f.status === "pending").length : 0;
    entries.push({
      label: "AI 发现" + (pending ? `（${pending} 待确认）` : ""),
      act: () => scrollToEl($("ai-findings-panel")),
    });
    const turns = ((data.ask_thread || {}).turns) || [];
    entries.push({
      label: "对话" + (turns.length ? `（${turns.length}）` : ""),
      act: () => {
        if (askWorkbench) {
          askWorkbench.classList.remove("hidden");
          scrollToEl(askWorkbench);
        }
      },
    });
    entries.push({ label: "评分", act: () => scrollToEl($("score-card")) });
    entries.forEach((it) => {
      const li = document.createElement("li");
      li.className = "shell-item";
      li.textContent = it.label;
      li.onclick = () => { it.act(); shellDrawer(false); };
      ul.appendChild(li);
    });
  }

  // ---- B7-6 空态入口卡：文件/粘贴互斥切换 + 粘贴字数与超限禁提交 ----

  function bindShellEntries() {
    const entryFile = $("entry-file");
    const entryPaste = $("entry-paste");
    const settings = $("ws-settings");
    const pasteWrap = $("paste-wrap");
    const fileWrap = $("file-wrap");
    const pasteArea = $("paste-text");
    const pasteErr = $("paste-error");
    const btn = $("btn-upload");
    if (!entryFile || !entryPaste || !settings) return;
    const reveal = () => settings.classList.remove("hidden");
    // codex 十五轮 P2-8：提交可用性只由「当前激活的入口」决定——粘贴超限
    // 禁用了按钮，切回文件入口必须重算，否则正常文件也提交不了
    const updateSubmitEnabled = () => {
      if (!btn) return;
      const pasteActive = !pasteWrap.classList.contains("hidden");
      const n = pasteActive ? (pasteArea && pasteArea.value || "").trim().length : 0;
      btn.disabled = pasteActive && n > 200000;
    };
    entryFile.onclick = () => {
      reveal();
      pasteWrap.classList.add("hidden");
      fileWrap.classList.remove("hidden");
      updateSubmitEnabled();
      // 不自动唤起原生文件选择器：用户自己点「选择文件」（e2e 也可稳定操作）
    };
    entryPaste.onclick = () => {
      reveal();
      fileWrap.classList.add("hidden");
      pasteWrap.classList.remove("hidden");
      if (pasteArea) pasteArea.focus();
    };
    if (pasteArea) {
      pasteArea.addEventListener("input", () => {
        const n = (pasteArea.value || "").trim().length;
        const cnt = $("paste-count");
        if (cnt) cnt.textContent = n + " 字符";
        const over = n > 200000;
        if (pasteErr) {
          pasteErr.textContent = over ? "超出 20 万字符上限，请删减后提交。" : "";
          pasteErr.classList.toggle("hidden", !over);
        }
        if (btn) btn.disabled = over; // codex 十二轮 P1-3：超限禁用提交（不截短续审）
      });
    }
    // 启动恢复：上次遗留的未完成提问回到 lastAskFail（同键重试路径的前提）
    const pending = loadPendingAsk();
    if (pending && pending.review_id && pending.item_id) {
      state.lastAskFail = {
        review_id: pending.review_id,
        item_id: pending.item_id,
        question: pending.question,
        request_id: pending.request_id,
      };
    }
  }

  // ---- 抽屉四通道（遮罩/汉堡/Esc/导航项后自动关）+ 各入口绑定 + 首渲 ----
  const menuBtn = $("shell-menu");
  if (menuBtn) {
    menuBtn.addEventListener("click", () => {
      const nav = $("shell-nav");
      shellDrawer(!(nav && nav.classList.contains("is-open")));
    });
  }
  const drawerMask = $("drawer-mask");
  if (drawerMask) drawerMask.addEventListener("click", () => shellDrawer(false));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const nav = $("shell-nav");
    if (nav && nav.classList.contains("is-open")) shellDrawer(false);
  });
  const newBtn = $("btn-new-review");
  if (newBtn) {
    newBtn.addEventListener("click", () => {
      shellDrawer(false);
      closeAskContext();
      clearReviewHash();
      state.reviewId = null;
      state.selectedItemId = null;
      hidePrecheckConfirm();
      show("upload");
      renderShellHistory();
    });
  }
  bindShellEntries();
  renderShellHistory();
  renderShellContext();

  applyDesktopLayout();

  loadStanceMeta();
  resumeFromHash();
})();

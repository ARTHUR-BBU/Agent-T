# B7-5b 桌面双栏重布局线框（阳仔 · 定稿）

> 依据现状：app/static/index.html（三屏结构）、app/static/styles.css（仅 480px 单断点，#app 720px 封顶）、app/static/app.js（renderResults / renderAiFindings / revPickBox 双挂载 / mqMobile hash 路由）、docs/spec-b75a-ai-findings.md、docs/design-tokens.md v0.1。

## 0. 诊断与设计立场

现状是「手机版拉宽」：720px 单列像一条窄走廊，桌面两侧全浪费；点条目后详情在清单下方几百像素外；问个问题要跳去另一个页面，回来就忘了刚才看到哪。

立场（延续 M2 信任感三原则）：
- 双栏不是「塞更多东西」，是**文件在左手、便签在右手**——桌上摊着合同和审查清单（左栏），手边一本便签本（右栏），翻到哪条，便签本上就是哪条
- 右栏只服务「当前选中的那一条」，左栏负责「全局结论」——职责分家，不混合
- 零新颜色、零新字号、零动画；唯一新增 token 是一个布局宽度变量

## 1. 桌面双栏布局

### 1.1 分区总图（≥1024px）

```
┌──────────────────────────────────────────────────────────────────┐
│ #app（≥1024: max-width 1280px；现状 720px 仅 <1024 生效）          │
│                                                                  │
│  ← 重新上传   审查结果                                            │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ meta 卡（文件名/品类/统计/立场胶囊/导出）——全宽，勿动          │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ┌────────────────────────────┐  ┌───────────────────────────┐   │
│ │ 左栏 col-main（1fr）        │  │ 右栏 col-side（sticky）    │   │
│ │                            │  │                           │   │
│ │  事实材料 facts-card        │  │  空态（未选条目时，灰字）  │   │
│ │  图例 + caption 一行        │  │  ── 选中后 ──             │   │
│ │ ┌ 清单 item-list ────────┐ │  │  条目名 + 档位 tag（新增） │   │
│ │ │ ▸ 付款条件  [需关注]    │ │  │  所在条款：第 X 条        │   │
│ │ │ ▸ 违约责任  [需关注]    │ │  │  说明 note 全文           │   │
│ │ │ ▸ 保密范围  [通过]      │ │  │  原文（黄底命中词）+看原文 │   │
│ │ └────────────────────────┘ │  │ ┌ 三卡 rev-cards（虚线）┐ │   │
│ │ ┌ AI 发现面板（虚线）────┐ │  │ │ ✎ 修改建议            │ │   │
│ │ │ 发现→核对→拍板         │ │  │ │ ☑ 选定版本            │ │   │
│ │ └────────────────────────┘ │  │ │ ⛓ 牵连检查            │ │   │
│ │ ┌ 规则异议（虚线）────────┐│  │ └────────────────────────┘ │   │
│ │ └────────────────────────┘│  │  [ 问清楚一点 → ] 开对话框  │   │
│ │ ┌ 评分卡（折叠沉底）──────┐│  └── sticky：随滚动固定 ──────┘   │
│ │ └────────────────────────┘│                                  │
└──────────────────────────────────────────────────────────────────┘
```

比喻：左栏是摊在桌上的合同与审查清单，右栏是钉在桌边的便签本——左边清单滚多长，便签本都停在原位；便签本自己内容太长时自己内部滚，不把整页顶下去。

### 1.2 宽度与断点

| 断点 | 行为 |
|---|---|
| <480（移动） | 完全现状不动（hash 详情页不变） |
| 480–1023（平板带） | 完全现状不动（单列 + 行内详情面板） |
| ≥1024 | 双栏启用，见下 |
| ≥1280 | 仅 --side-w 340→400px，其余不变 |
| ≥1440 | 不再加宽——1280 已是舒适阅读宽，再宽引句行太长反而难读 |

```css
:root { --side-w: 340px; }                 /* 唯一新 token（布局变量，非颜色） */
@media (min-width: 1280px) { :root { --side-w: 400px; } }
@media (min-width: 1024px) {
  #app { max-width: 1280px; }
  #screen-results .results-cols {
    display: grid;
    grid-template-columns: minmax(0, 1fr) var(--side-w);
    gap: var(--space-3);
    align-items: start;
  }
  .col-side {
    position: sticky;
    top: var(--space-2);
    max-height: calc(100vh - var(--space-4));
    overflow-y: auto;
  }
}
```

#app 直接放宽即可：上传页/等待页是居中单列卡片，在 1280 下自适应，视觉无碍。

### 1.3 DOM 结构与联动方式

```html
<div id="results-body">
  <div id="precheck-suspect" class="… hidden">…</div>   <!-- 横幅类保持全宽置顶 -->
  <div id="stance-notice" class="… hidden">…</div>
  <div class="meta card" id="results-meta">…</div>       <!-- 全宽含导出 -->
  <div class="results-cols">                             <!-- 新增包一层 -->
    <div class="col-main">
      facts / legend / legend-caption / item-list / blind-skip-note
      / ai-findings-panel / objections-panel / score-card
    </div>
    <aside class="col-side">
      <div id="side-empty">…空态…</div>
      <!-- #item-detail 原节点整体搬迁至此，ID/子结构零改动；
           <1024 时 JS 搬回原位，DOM 与今天逐字节一致 -->
      <div id="item-detail" class="item-detail hidden">…</div>
    </aside>
  </div>
</div>
```

关键决策：#item-detail 用 **matchMedia 节点搬迁**（mqDesktop = matchMedia("(min-width: 1024px)")，change 时 appendChild 搬进/搬出 aside），不走纯 CSS grid 放置。理由：
- 纯 CSS 方案（detail 留在 col-main、用 grid-column 拉去第二列）在 481–1023 平板带会让详情掉到 AI 发现/评分卡之后——点条目要滚过整页才看到详情，是明确回归
- 节点搬迁是本仓库成熟先例（revPickBox 双挂载点、mqMobile 断点监听同款）；selectItem 全按 ID 取元素，节点搬哪都照常工作
- <1024 时 DOM 与今天逐字节一致，「移动端/平板保持现状」字面满足

联动逻辑基本免费：selectItem() 现在就按 ID 填 #item-detail、经 loadRevCards→revPickBox 挂三卡——右栏只是这个面板的新家。要补两件小事：
1. **详情头部**（新）：右栏离清单远，详情必须自带身份。#item-detail 顶部加壳 <div id="detail-head">，selectItem 里补约 3 行渲染：条目名 + 档位 tag + 来源徽章（tag/badge 样式全复用）。
2. **空态**（新）：未选条目时右栏显示 caption 级灰字：「从左侧清单选一条，这里会显示它的说明、原文和可做的操作。」显隐与 item-detail.hidden 取反同步。

### 1.4 点条目联动行为

1. 点左栏行 → 行高亮（现状）+ 右栏填充：详情头 → 说明 → 原文 → 三卡 → 问清楚按钮
2. 再点另一行 → 右栏原位换内容（三卡走既有 REV_STATE.seq 判废，无闪烁）
3. 右栏「看原文」→ 原文块 flash 描边（现状 is-flash，唯一合规动效）
4. sticky 右栏是「信息融合」的主要体感来源：滚到第 20 条时详情还停在屏上

## 2. 「问清楚」对话框化

### 2.1 载体：原生 <dialog>，不跳页

老板的直觉是对的，且正好有现成先例：预审确认（阶段 1.4⑤）已经是 <dialog>，照抄载体。

比喻：现在是「有问题去隔壁房间问，回来忘了看到哪」；对话框是「拉把椅子凑到桌边问，合同还摊在桌上」。关掉对话框，右栏详情、三卡原样都在。

```html
<dialog id="ask-dialog" class="ask-dialog" aria-labelledby="ask-dialog-title">
  <div class="ask-dialog-head">
    <p class="detail-label" id="ask-dialog-title">（条目名，JS 填）</p>
    <p class="ask-status-line" id="ask-dialog-status"></p>
  </div>
  <div id="ask-dialog-body"><!-- ask 三节点搬迁宿主 --></div>
</dialog>
```

```css
.ask-dialog {
  width: min(640px, calc(100vw - 48px));
  max-height: calc(100vh - 48px);
  overflow: auto;
  border: 1px solid var(--color-divider);
  background: var(--color-surface);
  border-radius: var(--radius-card);
  padding: var(--space-2);
}
.ask-dialog::backdrop { background: var(--color-backdrop); }
```

不复用 precheck 的琥珀底：琥珀=「AI 在等你决定」，问清楚是普通问答，用白卡+中性遮罩（遮罩 token 既有）。零新 token。

### 2.2 DOM 复用：节点搬迁，不复制渲染

#ask-note-card、#ask-quote-card、.ask-interact（含输入框/chips/答案）三个节点，≥1024 时搬进 #ask-dialog-body，<1024 搬回 #screen-ask 原位。理由：
- sendAsk() / renderAnswer() / followups / 错误处理全按 ID 操作——节点搬家照常工作，**逻辑零复制**（两套 DOM 两套渲染必然漂移）
- #screen-ask 本体保留（移动端 hash 详情页靠它），其标题区（ask-title/status-line）留在原地；dialog 用自己的 head
- openAskDetail() 保留给移动端 hash 路由；桌面入口改为 openAskDialog(item)：填 dialog 标题+状态行 → 把 openAskDetail 里段1/段2 的填充代码抽成 fillAskContext(item) 共用 → showModal()

交互流（桌面）：右栏点「问清楚一点 →」→ dialog 弹出（showModal() 自带焦点圈定，Esc/点遮罩=关闭）→ 点 chips 或输入 → 发送 → **答案四段式显示在输入框上方**（复用移动端 480px order:-1 的既有做法，dialog 内同样生效）→ 关闭回右栏，三卡还在。

**初始化与判废补明（codex 七轮 P1×2）**：
- openAskDialog 共用 openAskDetail 的**完整初始化**（askItem 登记/切条目 seq 递增/立场文案/状态行/段1/段2/段3-4 可用性门禁与禁用态/清空旧答案与 chips），不只搬内容——两种入口只差「show('ask')+hash vs showModal()」
- `.ask-interact` 现从 `#screen-ask` 内查找，节点搬走后必失效——查找改 document 级（或搬迁感知宿主），两容器行为一致
- 关闭三通道（按钮/Esc/遮罩）统一清理：`askReqSeq += 1` 作废在途响应 + 发送按钮复位（守卫逻辑本身不碰，只新增使用点）；同条目重开分配新编号；dialog 开着跨 1024 断点：先统一清理关闭再搬节点，禁止带开搬迁

### 2.3 与三卡的位置关系（职责分家）

- 三卡 = **改合同的事**，留在右栏详情区内，dialog 里不放
- 问清楚 dialog = **问明白的事**，只管解释与改法建议
- 衔接：答案里「建议改法」想落地 → 关 dialog → 右栏三卡「生成修改建议」。**不在答案里内嵌生成按钮**——两个动作消耗不同额度，混在一屏会让人以为「问了就改了」（占位文案可加一句 caption 点拨，过九哥）
- revPickBox 零改动：桌面不再 show("ask")，revPickBox 永远选 #rev-cards（右栏），行为自动正确

## 3. AI 发现面板的安放

**结论：左栏，清单正下方。不放右栏。**

讲给老板的版本：
- AI 发现是「一批发现」，右栏是「当前这一条」——批次放全局面，条目放操作面
- 确认无误后它物化成清单新条目，紧挨清单放，「确认完就归档进清单」的因果看得见：面板里点头确认，眼皮底下清单多一行【AI 发现】
- 视觉维持 quality-card 虚线家族（参谋不是裁判的既有语言），与左栏白卡清单天然分层

内部布局维持单列：demo-1 实测 12 条，每条已含引句/核对结论/按钮，是密卡；双列会让引句断行难读。≥1280 也不双列——克制优先，实测拥挤再议。可选增强（C3）：pending 待确认排最前、已确认/已争议沉底，待办在上的阅读顺序；成本紧就不做，不算 P1。

## 4. 「再审本条」（↻）去留

**结论：去掉常驻 ↻ 图标；改为条件化的文字链接「再核一次」。**

理由：
1. **看不见的动作等于没有**：↻ 是图标级、无标签；实锤——verify-btn-recheck 这个 class 在 styles.css 里根本没有样式定义（app.js:827 赋了 class，CSS 全文无此选择器），现在是浏览器裸按钮，半成品。靠 title 才能懂，懂了也看不见「消耗余量」的代价——隐形入口+隐形扣费，在这个产品里是双杀
2. **它只在少数时候有价值**：再核的真正场景是「上次核对没把握」——核对结论为「尚未核对 / 摘句位置不唯一 / 原文未找到摘句」。结论已是「摘句对得上」的条目，再核大概率还是对得上，白耗余量
3. 后端 reverify 能力**保留不删**（API 不动），只是入口收窄

改法（renderAiFindings 的 actions 分支；**codex 七轮 P2：仅 ≥1024 生效**——本函数各尺寸共用，入口分支须断点感知，<1024 保留现状 ↻ 图标，「移动端不动」红线优先）：
- confirmed 且「摘句对得上」：只留「有异议（撤回）」，无再核入口
- confirmed 且核对结论 ∈ {尚未核对, 位置不唯一, 未找到摘句}：核对结论旁加文字链接「再核一次（耗 1 次余量）」→ 既有 submitVerifyRecheck
- 争议→恢复确认的复活路径不变

## 5. 信息跳转融合：联动路径总图

（「→」=用户动作，「⇒」=系统响应）

```
A. 清单条目（左）
   点条目 ⇒ 行高亮（现状）＋右栏填充：详情头→说明→原文(黄底)→三卡→问清楚
   点「看原文」⇒ 原文块 flash（现状）

B. AI 发现（左，清单下方）
   点「确认无误」⇒ 物化：清单新增【AI 发现】条目（现状，整列表重渲）
   （新增 C3）「已列入下方清单」⇒ 可点 ⇒ 左栏滚动到该条+选中 ⇒ 右栏跟着填充
   （新增 C3）「关联规则：××」⇒ 可点 ⇒ 跳到对应规则条目并选中
   点「看原文」⇒ 引句 flash（现状）

C. 右栏三卡
   「生成修改建议」⇒ 三版本 →「选定这一版」⇒ 卡二更新（现状）
   「检查其他条款」⇒ 卡三牵连摘录（现状）
   （新增 C3）牵连摘录行加「看原文」⇒ 复用 jumpToEvidence 滚动+flash

D. 问清楚（右栏 → dialog）
   「问清楚一点 →」⇒ dialog 弹出（清单还在身后）
   答案四段式；「建议改法」想落地 ⇒ 关 dialog → 三卡「生成修改建议」（额度分离）

E. 异议面板（左，沉底）
   「看原文」⇒ flash（现状）；「采纳为规则改进提案」⇒ 置灰（现状）不动
```

融合的衡量标准（验收口径）：**任何一次跳转都不丢当前位置**——左栏滚到哪、右栏显示谁、dialog 开不开，三态互不覆盖。现状的跳页做不到（回来详情没了）；双栏+dialog 后每条路径都是「原位增补」。

## 6. 实现成本分级（施工顺序建议）

| 级 | 内容 | 改动面 |
|---|---|---|
| **C1 纯 CSS/静态 HTML** | --side-w + 1024/1280 双断点；results-cols 包一层；aside/side-empty/dialog 壳/detail-head 壳静态 HTML；.ask-dialog 样式；#app 放宽 | styles.css + index.html，零 JS。此时双栏成形（详情暂留原位，右栏暂空） |
| **C2 小 JS** | #item-detail matchMedia 搬迁（约 15 行含 change 监听）；side-empty 显隐；detail-head 渲染（selectItem 补 3 行）；ask-dialog 开关 + ask 三节点搬迁 + fillAskContext 抽取复用 | app.js，不动任何渲染/请求逻辑 |
| **C3 联动增强** | ↻ 条件化（renderAiFindings 分支调整，建议随本批做掉）；rule_link/已列入清单可点跳转（**依赖**：finding 需带 rule_item_id，后端小改；前端可先按名称匹配兜底）；牵连摘录「看原文」；pending 置顶（可选） | 前后端各一小刀 |

顺序：C1 → C2 →（a+b 联合验收主体已可进行）→ C3 按值排期。

施工注意：
1. e2e：桌面用例若断言「问清楚跳页/屏切换」需同步更新；按 ID 断言 #item-detail 的不受影响
2. REV_STATE / revStaleResp / askReqSeq 判废机制一行不碰——正确性基石
3. 铁律自查：零新颜色/字号 token（仅 --side-w 布局变量）；零新动画（只用既有 is-flash）；<1024 全程不动；无框架；占位文案过九哥

## 7. 明确不做（本批）

- 左栏清单再分栏/表格化（阶段 2.3 刚定稿的行结构不动）
- AI 发现面板内部双列（§3）
- ≥1440 加宽（§1.2）
- 右栏在 <1024 的任何样式（平板带字面保持现状）

——全文完。

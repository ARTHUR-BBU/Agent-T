# B7-5b 施工卡：桌面双栏重布局 + 问清楚对话框化（设计依据=wireframe-b75b-desktop.md 阳仔定稿）

> 老板试用反馈立项（2026-11-05）：①桌面布局手机化浪费屏幕、信息跳转不融合；④问清楚改对话框；⑤再审本条必要性存疑；⑨条目清单+三卡区重布局。B7-5a（#173，main=af2e490）已合并未上线。
> 费用：全程零 LLM（纯前端布局/交互 + C3 一处后端小改纯读）。

## 一、目标（一句话）

桌面 ≥1024 双栏化（左=全局结论，右=当前条目操作区 sticky）+「问清楚」对话框化（原生 dialog 不跳页）+ 再审入口条件化；**<1024 全程逐字节不动**。

## 二、范围（按阳仔 C1→C2→C3 分级施工）

### C1 纯 CSS/静态 HTML（零 JS）
- `--side-w: 340px`（唯一新 token，≥1280 到 400px）+ 1024/1280 双断点
- `#app` ≥1024 放宽至 1280px（现状 720px 仅 <1024 生效）
- `results-cols` 包一层：左 col-main（facts/图例/清单/blind-skip/ai-findings/异议/评分卡）+ 右 aside col-side（side-empty 空态 + item-detail 宿主位 + 静态壳）
- `#ask-dialog` 壳 + `.ask-dialog` 样式（白卡+既有 --color-backdrop，不复用 precheck 琥珀底）
- `#detail-head` 壳

### C2 小 JS（不动渲染/请求逻辑）
- `#item-detail` matchMedia 节点搬迁（≥1024 进 aside，<1024 搬回原位；同 revPickBox/mqMobile 先例）
- side-empty 显隐（与 item-detail.hidden 取反同步）
- detail-head 渲染：selectItem 补 ~3 行（条目名+档位 tag+来源徽章，样式全复用）
- ask-dialog 开关：桌面入口 openAskDialog(item)（抽 fillAskContext(item) 与 openAskDetail 共用段1/段2 填充）→ showModal()；ask 三节点（note-card/quote-card/.ask-interact）≥1024 搬进 dialog、<1024 搬回 #screen-ask；openAskDetail 保留给移动端 hash 路由

### C3 联动增强（↻ 条件化随本批做；跳转增强按余量）
- **↻ 条件化**（阳仔实锤：verify-btn-recheck 无 CSS 是半成品裸按钮，隐形入口+隐形扣费）：去掉常驻 ↻；confirmed 且核对结论∈{尚未核对,位置不唯一,未找到摘句} 时出文字链接「再核一次（耗 1 次余量）」；结论「摘句对得上」不出口；后端 reverify API 不动
- rule_link「关联规则」可点跳转 + AI 发现「已列入清单」可点回跳：finding 需带 rule_item_id（后端 review_findings 小改纯读）；前端名称匹配兜底可选
- 牵连摘录行「看原文」（复用 jumpToEvidence）
- pending 置顶排序（可选，不做不算缺陷）

## 三、红线（不可碰）

1. REV_STATE / revStaleResp / askReqSeq 判废机制一行不碰
2. <1024（移动+平板带）DOM 与行为与今天逐字节一致——e2e 移动用例零改动预期
3. 零新颜色/字号 token（仅 --side-w 布局变量）；零新动画（只用既有 is-flash）；无框架
4. 三卡=改合同的事留右栏；问清楚 dialog=问明白的事；答案内**不内嵌生成按钮**（额度分离，防「问了就改了」误解）
5. 占位文案过九哥后才算完工

## 四、测试清单（关键项）

- e2e 桌面（≥1024 viewport）：双栏布局生效、点条目右栏填充（详情头/三卡/问清楚按钮）、item-detail 在 aside 内
- e2e <1024：现有全部移动/平板用例零改动通过（回归面最大的守护指标）
- 问清楚 dialog：桌面弹出→发送→答案四段式显示→关闭右栏三卡还在；进行中关闭无残留（askReqSeq）
- AI 发现：面板仍在清单下方、确认物化后清单新增行可见性不回归
- 再核：confirmed+摘句对得上 无再核入口；confirmed+低置信结论 有「再核一次」链接且调用既有 API
- 兼容：旧 e2e 中断言「问清楚跳页」的桌面用例同步更新（阳仔施工注意①）

## 五、验收与授权边界

- 全量回归+ruff/pyright+codex 复核 → 老板确认合并
- a+b **联合验收**（老板真机试用）→ 一并换装（换装执行时按惯例呈报确认）
- 本卡零 LLM 零扣额；上线不新增费用面

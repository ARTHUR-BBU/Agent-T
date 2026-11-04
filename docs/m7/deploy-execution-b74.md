# B7-4 换装执行日志（2026-11-04，部署单 deploy-memo-b73 同款 SOP）

> 授权链：老板 2026-11-04「授权换装」（B7-4 合并后单独请示）。
> **冻结 SHA：`77fa715f611df4469bb577ce32ac34e79802b1ee`**（#170 合并后
> main HEAD；源码归档 md5 两端一致 `fb49a297…`）。
> B7-4 内容：禁语表 11→29 词（老钱双锚三类）+ 牵连摘录同条款合并全段窗
> （keyword 数组/excerpt_truncated/300 字符截断段）+ 截断链三断点修复
> （全段送审/提示词标注/页面报告披露）。

## 执行记录（逐关卡，全过）

| 关卡 | 结果 | 证据 |
|---|---|---|
| 0. 生产实态只读探针 | ✅ | 在跑 0aa7bd2（Up 5 hours）；基线 reviews/llm_calls/drafts/selected/impact=**1/5/1/1/1**；**stance 列已在**（B7-3 验收卷留存库态随卷接续） |
| 1. 归档+上传 | ✅ | md5 两端一致 fb49a297… |
| 2. 构建 `agent-t:77fa715` | ✅ | ID d25d25dffd04，365MB |
| 3. 预备份 | ✅ | backup-pre-b74.db integrity ok、reviews=1 |
| 4. 隔离入口 | ✅ | DOCKER-USER 链首 REJECT；内部 200；**外部探针在停机后做（时序偏差见实录③），隔离性由链首规则佐证** |
| 5. 排空核查 | ✅ | `drained` |
| 6. WAL/SHM 冻结 | ✅ | reviews.db-wal/shm 在册 |
| 7. 停机+改名保留 | ✅ | `agent-t-old-b74-20261104`（0aa7bd2，stop_rc=0） |
| 8. 数据验证（新镜像只读） | ✅ | integrity ok；基线 1/5/1/1/1 一致；stance 列在位 |
| 9. 新容器 | ✅ | 原名 agent-t=77fa715；startup complete |
| 10. 数据接续 | ✅ | 1/5/1/1/1 一致 |
| 11. 安全门 | ✅ | 401×3 + /health 200 |
| 12. 版本比对 | ✅ | 容器=77fa715 |
| 13. 路由探针 | ✅ | GET state → handler 级 404 review_not_found |
| 14. 解封+外部恢复 | ✅ | 外部 /health 200 |
| 15. 线上一条龙（**B7-4 焦点=长条款截断链**） | ✅ | 详见下节 |
| 16. 圈账（方案二） | ✅ | 核对器 ok：6 行、实算 **¥0.125** ≪ ¥3 |

## 线上一条龙实录（关卡 15，焦点=截断链）

- 考卷：payment-long-b74.txt（1200 字符；第九条结算支付长条款 375
  字符 >300 上限；第一条背靠背触发 payment 需关注）
- force 上传 → rid `f2dfb0a95c9d` done → payment 项生成（revision 1
  次）→ 选定平衡 → 牵连检查 → **impact ok（covered，LLM 1 次）**
- **截断链线上实锤（codex 五轮三断点逐一在线核验）**：
  - 摘录 **2 个截断段**（294+81 字符，375>300 触发切分），全带
    `excerpt_truncated` 标注
  - keyword 数组各段命中词齐备（段 0：支付×13/价款×9/结算×7/发票×6；
    段 1：付款×3 等）
  - **state 路由透传**：excerpts 全量带 truncation 字段
  - **报告披露**：docx 含「相关条款过长，牵连内容已按段摘录送审，
    摘录非完整条款原文」
- 禁语表 29 词：生成正常（验收卷无诱导措辞，无误杀误伤）

## 圈账（关卡 16 展开）

- 第一跑 unknown：预期清单 impact=0 与实际不符（本次 covered 走了
  LLM）→ 按实际分支修正 + 补 impact trace（0528cefe…）→ 第二跑 ok
- 实际行 6：precheck 1 + model_review 1 + quality 1 + objection 1 +
  revision 1 + impact 1
- **实算 ¥0.125**（账本 usage 峰值牌价估算，非供应商实扣）≪ ¥3；
  方案二局限声明沿用（供应商账单出账后对账）

## 过程实录（如实）

1. **客户端管道问题三则（均非服务端问题）**：①上传响应 JSON 解析
   一次失败（首传未建单，重传成功，无重复单——查 reviews 确认）；
   ②ssh 嵌套引号吃掉 `draft_id` 键名 → 空值误传（非产品回归，直查
   顶层键确认 draft_id 在）；③环境变量未过单引号 ssh → 一次 401。
2. **#154 归属校验实战拦截自证**：误把 long-clause 卷的 draft 用于
   payment 卷的 select → 被跨审查单归属校验拒绝（>/dev/null 吞掉未
   见，库状态清白：drafts 仅 1 条）——#154 复批的防护在真实误操作
   下工作正常
3. **covered 路径选择过程**：i1 卷与 long-clause 卷的需关注项均走
   uncovered 零调用诚实路径（长条款被归属进需关注项自身 clause_ids
   ——subject 项 fulltext 兜底归属所致，非本次变更引入）；最终以
   payment 项（背靠背触发、无 fulltext 兜底）拿到 covered+截断形态
4. 停机 stop_rc=0 干净退出（B7-3 为 137，无碍）

## 现场状态

- 生产：`agent-t:77fa715`（挂卷+env 继承），外部 /health 200
- 保留物：agent-t-old-b74-20261104（0aa7bd2）、agent-t-old-20261104
  （571d179）、agent-t-old-20261030（95555ed）、四份备份——**按老板
  2026-11-04 决定暂不清理**
- 本地：agent-t-deploy-77fa715.tar.gz、long-clause/payment-long 考卷、
  docs/m7/state-b74-online.json（待补）等

## 后续待办

1. 老板签认本执行日志（B7-4 收官）
2. 供应商账单出账后对账留档（B7-3+B7-4 两笔一并）
3. 内部真人试用开始（HTTP 提示横幅保留）

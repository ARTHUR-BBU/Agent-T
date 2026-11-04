# B7-3 换装执行日志（2026-11-04，部署单 deploy-memo-b73 v6+现场关卡）

> 授权链：老板 2026-11-04 三项拍板（合并 #168 / 核账方案二=接受局限 /
> 费用 A ¥3 估算控制值 + 授权换装；SSH 生产访问同日明确授权）。
> 供应商账单能否逐笔区分本次调用，以实际账单明细为准（验收后对账）。
> **冻结 SHA：`0aa7bd29c3483c4c7a4767b93323daf3db43941e`**（#168 合并后
> main HEAD；源码归档 agent-t-deploy-0aa7bd2.tar.gz 2.9MB，上传 md5
> 两端一致 `fb1fbf27…`；镜像/版本比对三方同一）。

## 执行记录（逐关卡，全过）

| 关卡 | 结果 | 证据 |
|---|---|---|
| 0. 生产实态只读探针 | ✅ | 在跑 571d179（Up 4 days）；/health 200；基线 reviews/llm_calls=**1/11**；**impact_reports=0 行**（stance 迁移零包袱）、无 stance 列；DEEPSEEK/CF 三 Key 在位（值未读）；PRECHECK/QUALITY 未显式设（默认开）；卷+两份历史备份在位 |
| 0b. WAL/SHM 在线记录 | ✅ | **本次 wal=0 字节/shm=32768**（与 B7-2 的 ABSENT 不同，按文件集纪律如实记录） |
| 1. 归档+上传 | ✅ | /opt/agent-t-deploy.tar.gz，md5 两端一致 |
| 2. 构建 `agent-t:0aa7bd2` | ✅ | ID badf13af168f，365MB |
| 3. 预备份（SQLite 在线 backup API） | ✅ | backup-pre-b73.db（192KB）integrity ok、reviews=1 |
| 4. 隔离入口 + 外部实测 | ✅ | DOCKER-USER 链首 REJECT；**外部 curl 000 / 内部 200**（命中真实流量） |
| 5. 在线排空核查 | ✅ | `drained`（无 processing） |
| 6. WAL/SHM 冻结 | ✅ | 见 0b |
| 7. 停机+改名保留 | ✅ | `agent-t-old-20261104`（Exited 137，见实录①；**证据水平勘正见实录③**） |
| 8. 数据验证（新镜像只读） | ✅ | integrity ok；基线 1/11 一致；impact_reports=0；**stance 列未建=预期**（迁移在写入路径，GET/启动零 DDL——纯读红线实战自证） |
| 9. 新容器 | ✅ | 原名 agent-t，挂卷+env 继承+8080；Uvicorn startup complete |
| 10. 数据接续 | ✅ | 应用内 1/11 一致 |
| 11. 安全门 | ✅ | /、/api/categories、/docs → 401；/health → 200 |
| 12. 版本比对 | ✅ | 容器镜像=`agent-t:0aa7bd2`（badf13af168f） |
| 13. 新路由探针（带认证） | ✅ | GET …/revision/state → **handler 级 404 `{"detail":"review_not_found"}`**（路由在位且受 Basic Auth 保护） |
| 14. 解封+外部恢复 | ✅ | 规则已删（REJECT 计数 0）；外部 /health 200 |
| 15. **线上一条龙** | ✅ | 详见下节 |
| 16. 验收圈账（方案二口径） | ✅ | 核对器 ok：5 行、**实算 ¥0.1075**（峰值估算口径）≪ ¥3 控制值 |

## 线上一条龙实录（关卡 15）

- **执行配置**：force=1 直通上传（跳过品类建议，deploy-memo §五.4 钉死口径）；
  考卷 `fixtures/m7/i1-impact-linked.txt`（采购品类）
- 水位 1793442480.3591475（执行前 MAX created_at）
- 上传 → review_id `634915e77144` → done
- 三卡生成 → draft `a69e85a4c57246a2` ok（消耗 revision 1 次）
- 选定平衡 → `{"status":"selected",…}`；牵连检查 → **uncovered
  （no_match，零 LLM 调用——词族仅命中原条款，本地判定）**，落库
  **stance='平衡' 绑定正确**；impact trace 为空=设计内（本地判定无
  LLM 调用）
- **GET state 零扣实锤**：调用前后账行 5=5；state JSON 含 selected
  平衡 + impact uncovered（快照 docs/m7/state-b73-online.json）
- 导出报告 40KB PK ✓ → **第四节「四、AI 修改建议（AI 起草辅助，不构
  成法律意见）」+ 未覆盖诚实口径（不代表没有冲突）+ 重审提醒**全在
  场（docs/m7/acceptance-report-b73-online.docx 入档）
- **证据边界（codex #169 复核勘正）**：本次线上牵连走的是
  uncovered 零调用本地判定路径——只证明「未覆盖状态能进报告」；
  **线上「找到关联条款→调 AI→分析结果进报告」的正向路径本次未
  验证**，其证据仍是 B7-2 付费实跑（I1 正考）+ B7-3 离线回放
- 三枚 trace：上传 ac05a661cf354bf5 / revision 115d499e5d8d4e19 /
  impact 无（零调用）

## 圈账（关卡 16 展开）

- 第一跑：核对器返回 unknown——**预期清单漏了 objection 节点**（主链
  既有异议层，本次触发 1 次）→ 按实际分支修正 → 第二跑 ok
- 实际行 5：precheck 1（无重试）+ model_review 1 + quality 1 +
  objection 1 + revision 1；impact 0（uncovered 零调用）
- **实算 ¥0.1075**（账本 usage 峰值牌价估算，非供应商实扣；供应商
  实扣尚待账单出账对账，且账单能否逐笔区分本次调用以实际明细为准）≪ ¥3 控制值
- **预算表差异补记（codex #169 复核勘正）**：验收前费用表 258k
  上界**漏列 objection 节点**（主链既有异议层，实际触发 1 次×8k
  阀门）——原预算表不是完整上界（完整为 ≈266k/¥2.17→含 objection
  ≈¥2.25 量级，仍在 ¥3 控制值内）；核对器首跑发现该节点即本差异
  的当场暴露
- 方案二局限如实声明：precheck 第二笔（若发生）缺失无法当场发现；
  供应商账单出账后按单对账留档

## 过程实录（如实）

1. stop 后 Exited 137（SIGKILL）——与 B7-2 同现象；排空在先 + wal
   0 字节。**证据水平勘正（codex #169 复核）**：integrity ok + 行数
   一致是必要非充分证据，不足以单独证明所有记录内容未变化；
   **停机时刻全行对照已不可补**——新旧容器共挂同一卷，停机时刻
   「原件」即当前主库文件，重启后验收写入+产品清理已改变内容，
   当时未拍全行哈希。旁证=备份后至停机间仅只读探针（命令序列
   在案）。**保留物不清理**（codex 要求），等老板对证据水平拍板
2. **旧 B7-2 验收单过期清理**：基线探针 reviews=1（10-30 验收卷
   96448be46daf）→ 上传新卷后旧卷过保留期被既有清理机制删除
   （llm_calls 11→0 再累加 4）——产品承诺「审查记录定期自动清除」
   的既有行为，非本次变更引入
3. **impact uncovered 零调用**：i1 卷词族本次仅命中原条款 → 本地
   判定未覆盖（正是 B7-2 I2 考过的诚实路径），费用比预估更省
4. **stance 迁移实战生效**：首笔牵连写入（uncovered 落库）自动补
   stance 列并绑定「平衡」——部署单 §二「迁移在写入路径」的实战自证
5. 核对器第一跑抓出预期清单漏 objection 节点 → 修正后 ok——核账器
   「预期外节点→unknown」防线实战命中

## 现场状态

- 生产：`agent-t:0aa7bd2`（挂宿主卷 /opt/agent-t-data，STORE_DB_PATH
  对齐），外部 /health 200
- 保留物：`agent-t-old-20261104`（571d179，停用未删——**回滚预案：
  rm 新容器 → 旧镜像+同卷重启，部署后新写入保留**）、
  agent-t-old-20261030（95555ed，B7-2 保留物）、
  backup-pre-b73.db / backup-pre-deploy.db / mig.db——**全部不清
  理**（codex #169：停机证据水平待补强/老板拍板；签认暂缓）
- 本地：agent-t-deploy-0aa7bd2.tar.gz、
  docs/m7/{acceptance-report-b73-online.docx, state-b73-online.json}

## 后续待办

1. 老板验收签认本执行日志 → 清理保留物（两代旧容器+三份备份）
2. 供应商账单出账后对账留档（方案二口径）
3. 老钱两条非阻断挂账不变：摘录窗口去重（P3）、禁语表语义扩样

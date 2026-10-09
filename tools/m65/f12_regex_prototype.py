# -*- coding: utf-8 -*-
"""F-1/F-2 第一批修复 · 正则原型试跑（设计稿 v1.9 附卷）。

只做设计期验证：本文件不进生产引擎；YAML/引擎施工以本文口径搬运 + 变异验证为准。
范围注记（小智娘门禁 P3-2）：本原型只覆盖 F-12 靶向四项（payment/term/breach/
signature）；A2 保密四分类不在本原型范围（设计期验证见 spec-a2-table-autopsy-fix.md
原型 14/14，已迁入正式 pytest tests/test_confidentiality_blank.py）——勿把本文件
词表当全量权威。
v1.6：①F-2 同句硬边界加入换行（\\n\\r——解析器用换行连段落/表格行=条款的实际边界；
分号维持不断链）；②term 词表真分列 + 白名单化（去泛「期限」，采购/NDA 交叉反例）；
③neg_sensitive 字段生效（逐 alt 捕获组，否定检查只作用于否定敏感命中）；
④豁免搜索窗口化 ±320；⑤payment/term 分句边界统一含换行与半角标点。
tests/test_f12_regex_prototype.py 已把断言迁入正式 pytest（CI 直接执行）。
运行：python -X utf8 tools/m65/f12_regex_prototype.py（全绿输出 PROTOTYPE ALL GREEN）。
"""
from __future__ import annotations

import re

# ============ 命中词：单一权威表（模式, 禁行起点token, 否定敏感） ============
# 生产落地：YAML hit_alternatives 三元组同构，引擎编译函数从这一份配置
# 派生主命中/禁行起点/豁免终点三份词表（见设计稿 §三①）。
_HIT_ALTS: list[tuple[str, str, bool]] = [
    (r"不得追究.{0,10}违约", "不得追究", False),
    (r"甲方不得追究|不得追究乙方", "不得追究", False),
    (r"放弃追究", "放弃", True),
    (r"放弃.{0,6}追究", "放弃", True),
    (r"免除违约责任", "免除", True),
    (r"免除.{0,8}违约", "免除", True),
    (r"不承担违约", "不承担违约", False),
    (r"无需承担违约", "无需承担", False),
    (r"不得向.{0,8}主张违约", "不得向", False),
    (r"不负违约金义务", "不负违约金", False),
    (r"不得主张违约金", "不得主张", False),
    (r"违约责任由.{0,10}自行承担", "违约责任由", False),
    (r"豁免违约责任", "豁免", True),
]

# ============ 否定判定：受限否定头 + 连接字符（occurrence 前方 6 字符） ============
# 覆盖 不能/不会/不可/不应/不应当/不予/不再/不得/未 + 特例 并未/不视为；
# 双重否定（不得不/不可不…再接命中）显式不保护（防保护面过宽）
_NEG_HEAD_TAIL = re.compile(r"(?:不|未)[能可予应急再得当为会]*$")
_DOUBLE_NEG = re.compile(r"(?:不|未)[能可予应急再得当为会]*(?:不|未)[能可予应急再得当为]*$")
_NEG_SPECIAL = ("并未", "不视为")


def _negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 6):start]
    if _DOUBLE_NEG.search(prefix):
        return False
    return bool(_NEG_HEAD_TAIL.search(prefix)) or prefix.endswith(_NEG_SPECIAL)


# 逐 alt 捕获组：lastindex-1 即命中的 alternative 下标（组内无捕获组，一一对应）；
# neg_sensitive 字段在此生效：否定检查只作用于否定敏感命中
HIT = re.compile("|".join("(" + a + ")" for a, _, _ in _HIT_ALTS))
_HIT_NEG_FLAGS = [neg for _, _, neg in _HIT_ALTS]

# ============ F-2 对等豁免链：同句硬边界 = 。\n\r（分号不断链） ============
_QUANT = r"(?:任何一方|双方均?|各自|彼此|遇有不可抗力的一方)"
_EXCUSE = r"(?:不可抗力|情势变更|政府行为|自然灾害|疫情)"
# 禁行起点从单一权威表派生（外审 v1.6 阻断2：不再手写第二份词表）
HIT_START = r"(?:" + "|".join(dict.fromkeys(start for _, start, _ in _HIT_ALTS)) + r")"
_NOHIT = r"(?:(?!" + HIT_START + r")[^。\n\r])"
_EXEMPT_WINDOW = 320
EXEMPT = re.compile(r"(?:" + _QUANT + _NOHIT + r"{0,150}" + _EXCUSE + _NOHIT + r"{0,80}"
                    + r"(?:" + "|".join("(" + a + ")" for a, _, _ in _HIT_ALTS) + r"))")


def f2_verdicts(text: str) -> list[tuple[str, str]]:
    """逐 occurrence：否定检查（仅否定敏感命中）→ 窗口化豁免锚定。"""
    out: list[tuple[str, str]] = []
    for m in HIT.finditer(text):
        if _HIT_NEG_FLAGS[m.lastindex - 1] and _negated(text, m.start()):
            continue
        s, e = m.span()
        w0 = max(0, s - _EXEMPT_WINDOW)
        w1 = min(len(text), e + _EXEMPT_WINDOW)
        anchored = any(w0 + mm.start() <= s and w0 + mm.end() >= e
                       for mm in EXEMPT.finditer(text, w0, w1))
        out.append((m.group()[:16], "豁免" if anchored else "触发需关注"))
    return out


# ============ payment：分句作用域（。；，,;\n\r 均为分句边界） + 冒充词禁行 ============
_FORBID = r"(?:违约金|赔偿|责任上限|赔偿上限|注册资本|出资)"
_SUBJ = r"(?:价款|货款|采购款|报酬|服务费用?|租金|对价|合同总价|合同金额|结算|费用)"
_ACT = r"(?:支付|付款|付清|结清|支付给)"
_NOFORB = r"(?:(?!" + _FORBID + r")[^。，；,;\n\r])"
_CLAUSE_HEAD = r"(?:^|[。；，,;\n\r])"
_PAIR = (r"(?:" + _CLAUSE_HEAD + _NOFORB + r"{0,30}" + _SUBJ + _NOFORB + r"{0,40}" + _ACT
         + r"|" + _CLAUSE_HEAD + _NOFORB + r"{0,30}" + _ACT + _NOFORB + r"{0,40}" + _SUBJ + r")")
_AMOUNT = (r"(?:" + _CLAUSE_HEAD + _NOFORB + r"{0,30}"
           + r"(?:价款|总价|合同金额|费用)[^。，；,;\n\r]{0,20}(?:人民币|\d+\s?元))")
PAYMENT_PASS = re.compile(_PAIR + r"|" + _AMOUNT)

# ============ term：采购与 NDA 词表真分列 + 白名单化（无泛「期限」） ============
# 逐词说明见设计稿 §三③：金标期限语义域按品类各自成立，本批无共享词；
# 生产基线保留表达零回退（外审 v1.6 阻断1）。
# v1.9（外审）：①统一权威数字子模式 _NUMBER（阿拉伯+中文含「两」，正反例共用一份）；
# ②无标签数字期限入口加「票头核对」——分句内存在 ××期限 且不属于采购白名单
#   （付款期限/索赔期限/整改期限/举证期限…）→ 拒绝，不再枚举黑名单。
_PROC_WORDS = r"(?:合同期限|履行期限|履行期|交付期限|供货服务期|供货期|服务期|租赁期限|租期|工期|委托期限)"
_NDA_WORDS = r"(?:协议期限|(?:合同|协议)有效期)"
_NUMBER = r"(?:\d+|[零〇一二两三四五六七八九十百千万]+)"
_PROC_BASELINE = (_NUMBER + r"(?:日内|天内|个工作日内)"
                  r"[^。，；,;\n\r]{0,20}(?:完成交付|完成送货|完成安装|完成验收|交付|送货|到场|送装|到货)")
_NDA_BASELINE = (r"(?:协议[^。，；,;\n\r]{0,6}自[^。]{0,15}日起?[^。]{0,15}至[^。]{0,15}止"
                 r"|协议[^。，；,;\n\r]{0,4}自[^。]{0,10}起[^。]{0,8}" + _NUMBER + r"(?:年|个月|日))")
_HEAD = r"(?:^|[。；，,;\n\r])[^。，；,;\n\r]{0,12}"
_DATE_PROC = re.compile(_HEAD + _PROC_WORDS + r"[^。，；,;\n\r]{0,6}自[^。]{0,15}日起?[^。]{0,15}至[^。]{0,15}止")
_DATE_NDA = re.compile(_HEAD + _NDA_WORDS + r"[^。，；,;\n\r]{0,6}自[^。]{0,15}日起?[^。]{0,15}至[^。]{0,15}止")
_TERM_PROC = re.compile(_HEAD + r"(?P<label>" + _PROC_WORDS + r")")
TERM_PASS_NDA = re.compile(_HEAD + r"(?:" + _NDA_WORDS + r"|" + _NDA_BASELINE + r")")

# 采购白名单期限名（票头核对用：分句内发现的 ××期限 必须以其结尾命中本表才算采购归属）
_PROC_NAME_LIST = ("合同期限", "履行期限", "履行期", "交付期限", "供货服务期", "供货期",
                   "服务期", "租赁期限", "租期", "工期", "委托期限")
_TERM_NAME_SPAN = re.compile(r"[一-龥]{0,4}期限")
_CLAUSE_SEPS = "。；，,;\n\r"


def _clause_of(text: str, start: int, end: int) -> str:
    lo = max((text.rfind(ch, 0, start) for ch in _CLAUSE_SEPS), default=-1)
    his = [text.find(ch, end) for ch in _CLAUSE_SEPS]
    his = [h for h in his if h != -1]
    hi = min(his) if his else len(text)
    return text[lo + 1:hi]


# ---- P2 期限实质锚定（老钱裁定+签字 2026-10-09，与生产 checklist.py 同逻辑）----
_ANCHOR_WINDOW = 60
_ANCHOR_SEP = "。；"  # 跨换行取至句号/分号；60 字截断兜底
_ANCHOR_E1 = re.compile(r"[\d零〇一二两三四五六七八九十百]+(?:年|个月|天|日|个工作日|小时|周)")
_ANCHOR_E2 = re.compile(r"\d+\s*(?:年|月|日)")
_ANCHOR_E3_FROM = re.compile(r"(?:自|从)[^。；\n\r]{0,20}(?:之日)?起")
_ANCHOR_E3_TO = re.compile(r"(?:至|止)")
_PLACEHOLDER = re.compile(r"(?:[＿_]|[ 　]{2,})\s*(?:年|月|日|天|时|周)")
_PENDING_WORDS = re.compile(r"另行协商|另行约定|协商确定|待定|另行确定|届时(?:另行)?(?:约定|确定|商定)")
# 叙述形态排除门（老钱补裁 2026-10-10）：标签后 0 字符间隙紧接「内/期间/中」类
# 后缀 = 时间背景状语，不参与四分类；0 间隙防误杀（「：」隔断不受排除）
_NARRATIVE_SUFFIX = re.compile(r"(?:期间|之内|以内|间内|[内间]|中(?!标|心|期|断|途))")


def _clause_window(text: str, start: int) -> str:
    """标签命中点起的锚定窗口：跨换行取至句号/分号，上限 60 字符。
    起点跳过前导分隔符（_PROC_LABELED 的 _HEAD 含前导「。」等，不跳会空窗）。"""
    i = start
    while i < len(text) and text[i] in _ANCHOR_SEP:
        i += 1
    for j in range(i, min(len(text), i + _ANCHOR_WINDOW + 1)):
        if text[j] in _ANCHOR_SEP:
            return text[i:j]
    return text[i:i + _ANCHOR_WINDOW]


def _classify_label_clause(window: str) -> str:
    """单个标签条款分类：anchored / pending / blank（老钱裁定 §三）。"""
    has_placeholder = bool(_PLACEHOLDER.search(window))
    e3 = (bool(_ANCHOR_E3_FROM.search(window)) and bool(_ANCHOR_E3_TO.search(window))
          and not has_placeholder)
    e1 = False
    for m in _ANCHOR_E1.finditer(window):
        if not _PLACEHOLDER.search(window[max(0, m.start() - 6):m.end()]):
            e1 = True
            break
    if e1 or (not has_placeholder and _ANCHOR_E2.search(window)) or e3:
        return "anchored"
    if _PENDING_WORDS.search(window):
        return "pending"
    return "blank"


def match_procurement_term(text: str) -> dict:
    """生产匹配器契约预演（注册表名 procurement_term，P2 四分类+unlabeled 存量保留）：
    一次匹配，返回命中对象——状态判定/quote/hits/全文兜底四处复用同一结果，
    不允许同一规则被扫三遍（外审 v1.9.1 阻断2）。YAML 只能引用预注册名，
    禁止从配置动态导入模块或函数路径。汇总优先级（老钱签字 §五）：
    pending > anchored/unlabeled > blank > none。"""
    classified: list[tuple[str, int, int, str]] = []
    for m in _TERM_PROC.finditer(text):
        # 叙述形态排除门（老钱补裁）：标签后 0 间隙紧接「内/期间/中」= 时间背景
        # 状语，跳过该 occurrence 继续扫（红线：排除后不得终止）
        if _NARRATIVE_SUFFIX.match(text[m.end():m.end() + 4]):
            continue
        # 窗口从标签本体起（外审 P2 #94 销项②：_HEAD 前缀日期不得背书空白标签）
        window = _clause_window(text, m.start("label"))
        cls = _classify_label_clause(window)
        classified.append((cls, m.start("label"), m.start("label") + len(window), window[:40]))
    has_unlabeled: tuple[int, int, str] | None = None
    for m2 in re.finditer(_PROC_BASELINE, text):
        clause = _clause_of(text, m2.start(), m2.end())
        bad_head = any(not any(name.endswith(w) for w in _PROC_NAME_LIST)
                       for name in _TERM_NAME_SPAN.findall(clause))
        if not bad_head:
            has_unlabeled = (m2.start(), m2.end(), m2.group())
            break
    pending = [c for c in classified if c[0] == "pending"]
    anchored = [c for c in classified if c[0] == "anchored"]
    blanks = [c for c in classified if c[0] == "blank"]
    if pending:
        cls, s, e, q = pending[0]
        return {"matched": True, "start": s, "end": e, "evidence": q, "hit_type": "pending"}
    if anchored or has_unlabeled:
        if anchored:
            _, s, e, q = anchored[0]
            return {"matched": True, "start": s, "end": e, "evidence": q,
                    "hit_type": "anchored"}
        # 无标签数字链路：hit_type 保留原身份；坐标用链路命中本身
        us, ue, uq = has_unlabeled  # type: ignore[misc]
        return {"matched": True, "start": us, "end": ue, "evidence": uq,
                "hit_type": "unlabeled_numeric"}
    if blanks:
        cls, s, e, q = blanks[0]
        return {"matched": True, "start": s, "end": e, "evidence": q, "hit_type": "blank"}
    return {"matched": False, "start": None, "end": None,
            "evidence": "", "hit_type": "none"}


def term_proc_pass(text: str) -> bool:
    """公开判定入口（测试与生产统一走此入口，不许测内部零件）。

    P2 期限锚定口径（老钱裁定+签字 2026-10-09）：blank（空白占位）matched=True
    但不算通过——只有 anchored/unlabeled_numeric 才通过。"""
    r = match_procurement_term(text)
    return bool(r["matched"] and r["hit_type"] in ("anchored", "unlabeled_numeric"))

# ============ signature pass 正向入口（配对窗口维持 [^。]，跨换行签署栏不受影响） ============
# 正向不锚左括号与角色后冒号（17 份回归实证三形态：签名/盖章）、
# （盖章）：\n法定代表人 / 委托代理人（签名）：、（盖章）\n法定代表人/授权代表:）
SIGNATURE_PASS = re.compile(
    r"盖章\)?[^。]{0,40}(?:法定代表人|授权代表|委托代理人)"
    r"|(?:法定代表人|授权代表|委托代理人)\s*[:：][^。]{0,20}(?:（盖章）|\(盖章\))")

# ============ 断言集 ============
# 采购期限样例独立成表：全部必须调用公开判定入口 term_proc_pass（结构断言用
# `fn is term_proc_pass` 逐条把关；外审 v1.9.1 阻断——绑定的 .search 方法
# 每次访问生成新对象，`is` 比较抓不到违规，只有整函数身份比较可靠）
_OTHER_CASES: list[tuple[str, str, object, object]] = [
    # --- F-2 历轮回归
    ("调换顺序(复现#1)", "任何一方因不可抗力不承担违约责任；乙方逾期交付的，免除乙方全部违约责任",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("财政部18.2 对等免责", "任何一方对由于不可抗力造成的部分或全部不能履行合同不承担违约责任。但迟延履行后发生不可抗力的，不能免除责任。",
     f2_verdicts, [("不承担违约", "豁免")]),
    ("单方免责", "乙方逾期交付的，免除乙方全部违约责任。", f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    ("否定绑命中(复现#2)", "一方逾期交付的，乙方不免除其违约责任", f2_verdicts, []),
    ("不能免除变体", "一方迟延履行的，不能免除其违约责任", f2_verdicts, []),
    ("不予免除变体", "一方迟延履行的，不予免除其违约责任", f2_verdicts, []),
    ("不会免除变体", "该约定不会免除其违约责任", f2_verdicts, []),
    ("不应当免除变体", "该约定不应当免除其违约责任", f2_verdicts, []),
    ("不可/不可能免除变体", "该约定不可免除其违约责任。该约定不可能免除其违约责任", f2_verdicts, []),
    ("双重否定不保护", "乙方不得不放弃追究违约责任。", f2_verdicts, [("放弃追究", "触发需关注")]),
    ("不得追究对等豁免", "任何一方因不可抗力不得追究对方违约责任",
     f2_verdicts, [("不得追究对方违约", "豁免")]),
    ("放弃追究单方触发", "乙方放弃追究甲方违约责任。", f2_verdicts, [("放弃追究", "触发需关注")]),
    ("不放弃追究不触发", "双方均不放弃追究对方违约责任。", f2_verdicts, []),
    ("MOF18.3 全句", "遇有不可抗力的一方，应在不可抗力发生后及时通知对方，并在合理期限内提供证明，可部分或者全部免除其违约责任，但迟延履行后发生不可抗力的，不免除其违约责任",
     f2_verdicts, [("免除其违约", "豁免")]),
    ("相邻独立两句", "任何一方因不可抗力不能履行合同的，不承担违约责任。乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("真单方召回保护", "免除甲方责任而乙方不免责的约定无效。乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    # --- F-2 换行硬边界（v1.6 阻断1）
    ("换行硬边界(阻断1)", "任何一方因不可抗力造成损失\n乙方逾期交付的，免除乙方全部违约责任",
     f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    ("跨分号正常免责(撤回口径维持)", "任何一方因不可抗力不能履行合同的，部分或者全部免除其违约责任；但应在合理期限内提供证明。",
     f2_verdicts, [("免除其违约", "豁免")]),
    # --- payment（换行/半角分句 + 同句共存 + 反例）
    ("采购款正例", "甲方收到发票后＿个工作日内将上月采购款支付给乙方", PAYMENT_PASS.search, True),
    ("付款+违约金同句", "甲方应于验收合格后十日内支付采购款，逾期支付的违约金按日计算", PAYMENT_PASS.search, True),
    ("换行分句付款", "违约金按合同总价10%计算\n甲方应于验收后支付采购款", PAYMENT_PASS.search, True),
    ("半角分号付款", "违约金按合同总价10%计算; 甲方应于验收后支付采购款", PAYMENT_PASS.search, True),
    ("正常付款正例", "货款应当在验收合格后十日内支付", PAYMENT_PASS.search, True),
    ("货款+逾期违约金同句", "货款应当在验收合格后十日内支付，如逾期按日支付违约金", PAYMENT_PASS.search, True),
    ("违约金总价反例", "违约金按合同总价的 10% 支付", PAYMENT_PASS.search, False),
    ("赔偿上限反例", "赔偿总额以合同总价为限", PAYMENT_PASS.search, False),
    ("注册资本反例", "注册资本人民币 100 万元", PAYMENT_PASS.search, False),
    ("违约金金额反例", "乙方违约的，违约金金额为 5 万元", PAYMENT_PASS.search, False),
    # --- term NDA（独立词表 + 交叉反例 + 基线零回退）
    ("保密+协议同句", "保密期限三年，本协议有效期一年", TERM_PASS_NDA.search, True),
    ("本协议期限为三年", "本协议期限为三年", TERM_PASS_NDA.search, True),
    ("协议有效期正例", "本协议有效期一年", TERM_PASS_NDA.search, True),
    ("基线日期区间无标签(回退修复)", "本协议自2026年1月1日起至2028年12月31日止", TERM_PASS_NDA.search, True),
    ("基线自签署之日起(回退修复)", "本协议自签署之日起三年", TERM_PASS_NDA.search, True),
    ("NDA不收采购词(交叉反例)", "交付期限为三十日", TERM_PASS_NDA.search, False),
    ("保密期限日期区间(复现#3)", "保密期限自2026年1月1日起至2028年12月31日止", TERM_PASS_NDA.search, False),
    ("产品有效期冒充", "产品有效期不少于18个月", TERM_PASS_NDA.search, False),
    ("异议期限不算协议期限", "异议期限为七个工作日", TERM_PASS_NDA.search, False),
    ("付款期限不算协议期限(v1.6阻断3)", "付款期限为三十日", TERM_PASS_NDA.search, False),
    # --- 冒充期限回归组（v1.8 阻断：阿拉伯/中文数字双覆盖，单调增长不许删）
    ("盖章+落款组合正例", "供方（盖章）：法定代表人：____", SIGNATURE_PASS.search, True),
    ("签署跨换行不受影响(阻断1附验)", "供方（盖章）：\n法定代表人：____", SIGNATURE_PASS.search, True),
    ("角色斜杠落款行(energy形态)", "甲方（盖章）：\n法定代表人 / 委托代理人（签名）：\n日期：", SIGNATURE_PASS.search, True),
    ("签名斜杠盖章落款(agri形态)", "甲方（签名/盖章）：               乙方（签名/盖章）：\n法定代表人：                      法定代表人：", SIGNATURE_PASS.search, True),
    ("仅盖章孤行反例", "甲方（盖章）", SIGNATURE_PASS.search, False),
    ("主体介绍无盖章反例", "甲方：某科技有限公司，法定代表人：张三，住所地：北京市海淀区。", SIGNATURE_PASS.search, False),
]

# 采购期限样例独立成表：全部必须调用公开判定入口 term_proc_pass——
# 结构断言用 `fn is term_proc_pass` 逐条把关（绑定的 .search 方法每次访问
# 生成新对象，`is` 比较抓不到违规——外审 v1.9.1 唯一阻断的根因）
_PROC_TERM_CASES: list[tuple[str, str, object, object]] = [
    ("采购合同期限标签正例", "合同期限自2026年1月1日起至2028年12月31日止", term_proc_pass, True),
    ("采购履行期限标签正例", "乙方履行期限为2026年6月30日", term_proc_pass, True),
    ("采购不收NDA词(交叉反例)", "本协议有效期一年", term_proc_pass, False),
    ("基线数字日内(回退修复)", "甲方应在30日内完成交付", term_proc_pass, True),
    ("基线中文数字工作日(回退修复)", "乙方应在十个工作日内完成交付", term_proc_pass, True),
    ("索赔期限不算交付期限(v1.6阻断3)", "索赔期限为十日", term_proc_pass, False),
    ("两个工作日正例(v1.9)", "乙方应在两个工作日内完成交付", term_proc_pass, True),
    ("两天内送货正例(v1.9)", "乙方应在两天内完成送货", term_proc_pass, True),
    ("整改+验收冒充(v1.9阻断1)", "整改期限为5天内完成验收", term_proc_pass, False),
    ("索赔+交付材料冒充(v1.9阻断1)", "索赔期限为10日内完成交付索赔材料", term_proc_pass, False),
    ("举证+送货冒充(v1.9阻断1)", "举证期限为7日内完成送货证明提交", term_proc_pass, False),
    ("付款期限30日内冒充", "付款期限为30日内", term_proc_pass, False),
    ("索赔期限10日内冒充", "索赔期限为10日内", term_proc_pass, False),
    ("整改期限5天内冒充", "整改期限为5天内", term_proc_pass, False),
    ("举证期限7日内冒充", "举证期限为7日内", term_proc_pass, False),
    ("索赔完成冒充", "索赔期限为十个工作日内完成索赔", term_proc_pass, False),
    # --- 17 份真实合同回归（批1施工验收）暴露的三面
    # （P2 改账：school-uniform/mandate 原文验尸均为空格占位——改判 blank 不通过，
    #   老钱裁定硬要求+用户批准的改账）
    ("合同履行期空格占位(school-uniform改账)", "合同履行期自生效之日起      年，至      年    月    日止", term_proc_pass, False),
    ("委托期限空格占位(mandate改账)", "第三条 委托期限\n自     年   月   日至    年   月   日止。", term_proc_pass, False),
    ("担保期限不算交付期限(gov范本)", "履约担保期限：合同签订后30日内", term_proc_pass, False),
    # --- P2 期限实质锚定（老钱裁定 §三 + 签字三条注释，spec-p2-term-anchoring.md）
    ("委托期限真日期区间(老钱守卫E3)", "委托期限 自2024年1月1日至2024年12月31日止", term_proc_pass, True),
    ("履行期限数字时长(老钱守卫E1)", "履行期限：自交付之日起6个月内完成交付", term_proc_pass, True),
    ("合同履行期中文数字时长(老钱守卫E1)", "合同履行期自生效之日起三年", term_proc_pass, True),
    ("事件止点无数字(老钱守卫E3勿误杀)", "履行期限：自生效之日起至验收合格之日止", term_proc_pass, True),
    ("时长已定起止待定(注释1:E优先)", "委托期限3年，具体起止另行协商", term_proc_pass, True),
    ("另行协商待定期限(X1)", "委托期限：由双方另行协商确定", term_proc_pass, False),
    ("下划线占位(X2)", "委托期限：＿＿＿", term_proc_pass, False),
    ("下划线日期占位(X2)", "交付期限：自＿＿年＿＿月＿＿日起", term_proc_pass, False),
    ("空标题(X2)", "第三条 委托期限", term_proc_pass, False),
    ("全角空格占位(X2相邻反例)", "交付期限：　年　月　日", term_proc_pass, False),
    ("混合占位(X2相邻反例)", "交付期限：2026＿年 5 月前", term_proc_pass, False),
    ("正文单空格不误判(相邻反例守卫)", "履行期限：乙方应在交付后 3 日内结清余款", term_proc_pass, True),
    ("无标签数字链路存量保护(老钱签字前置)", "乙方应在30日内完成交付", term_proc_pass, True),
    ("前缀日期不背书空白标签(外审P2销项②)", "2026年版合同期限：", term_proc_pass, False),
    # --- 叙述形态排除门（老钱补裁 2026-10-10）
    ("校服生产解析原文(叙述形态+空格占位)", "注：此表须于合同履行期内、每年的12月31日前双方签订执行，并由甲方报主管的教育部门。 合同履行期自生效之日起      年，至      年    月    日止", term_proc_pass, False),
    ("叙述期间内保密句(近邻反例)", "在合同履行期间内均需保密", term_proc_pass, False),
    ("服务期内动作日期(近邻反例)", "服务期内、每年的12月31日前完成年度审核", term_proc_pass, False),
    ("合同期限内动作日期(近邻反例)", "合同期限内完成全部供货", term_proc_pass, False),
    ("冒号隔断不受排除(守卫正例)", "履行期限：自交付之日起6个月内完成交付", term_proc_pass, True),
    ("中标不撞排除门(外审#96销项)", "供货期中标通知书发出后30日内完成交付", term_proc_pass, True),
]

_CASES: list[tuple[str, str, object, object]] = _PROC_TERM_CASES + _OTHER_CASES


def _date_span_check() -> None:
    """日期分支真实验收（不能只断言 bool——匹配必须覆盖完整日期区间）。"""
    t = "合同期限自2026年1月1日起至2028年12月31日止"
    m = _DATE_PROC.search(t)
    assert m, "日期分支未命中"
    assert "2026年1月1日" in m.group() and "2028年12月31日止" in m.group(), \
        f"日期分支未覆盖完整区间: {m.group()!r}"


def _stress_repeat_hits() -> None:
    """性能边界（功能性下限）：300 处重复命中，窗口化搜索下判定全量正确。
    时间上限/增长比例断言留生产施工验收（原型不做计时断言防 CI 抖动）。"""
    t = "乙方逾期交付的，免除乙方全部违约责任。" * 300
    v = f2_verdicts(t)
    assert len(v) == 300 and all(x[1] == "触发需关注" for x in v), "压力样本判定错误"


def _guardrail_participates() -> None:
    """护栏参与证明（外审 v1.9.1）：候选正则确实命中、票头核对确实拦下——
    防「候选根本没匹配所以碰巧通过」的假绿。"""
    for name, text in (("整改+验收", "整改期限为5天内完成验收"),
                       ("索赔+交付材料", "索赔期限为10日内完成交付索赔材料"),
                       ("举证+送货", "举证期限为7日内完成送货证明提交")):
        assert _PROC_BASELINE.search(text), f"{name}: 候选入口未命中（护栏空转）"
        assert not term_proc_pass(text), f"{name}: 票头核对未拦截"


def main() -> None:
    failed: list[str] = []
    for name, text, fn, want in _CASES:
        got = fn(text) if isinstance(want, list) else bool(fn(text))
        ok = got == want
        print(("OK  " if ok else "FAIL"), name, "->", got)
        if not ok:
            failed.append(name)
    try:
        _date_span_check()
        _stress_repeat_hits()
        print("OK   日期分支区间断言 / 300 命中压力")
    except AssertionError as exc:
        failed.append(f"附加检查: {exc}")
        print("FAIL 附加检查:", exc)
    print(f"\n{len(_CASES) + 2 - len(failed)}/{len(_CASES) + 2} passed")
    if failed:
        print("FAILED:", failed)
        raise SystemExit(1)
    print("PROTOTYPE ALL GREEN")


if __name__ == "__main__":
    main()

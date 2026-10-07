# -*- coding: utf-8 -*-
"""F-1/F-2 第一批修复 · 正则原型试跑（设计稿 v1.4 附卷，外审要求随版提交）。

只做设计期验证：本文件不进生产引擎；YAML/引擎施工以本文口径搬运 + 变异验证为准。
v1.4：①命中词单一权威表（含生产 breach 全族，禁行起点自动派生）；
②否定判定 = occurrence 前方小上下文（不/未+[能否应再得] 等），不再用固定 lookbehind 枚举；
③payment/term 护栏从整句改为逗号分句/词级作用域（同句共存的合法条款不再被误杀）。
运行：python -X utf8 tools/m65/f12_regex_prototype.py（全绿输出 PROTOTYPE ALL GREEN）。
"""
from __future__ import annotations

import re

# ============ 命中词：单一权威表（外审复现#3：与生产 breach 全族同源） ============
# 每项 = (模式, 禁行起点token, 否定敏感)。起点 token 供豁免链禁行段派生——
# 主规则/禁行起点/豁免终点全部从本表派生，不再各自维护。
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
# 否定敏感族的否定前缀（occurrence 前方 4 字符内判定）
_NEG_RE = re.compile(r"(?:不|未)[能否应再得予]?$")
_NEG_SPECIAL = ("并未", "不视为")


def _negated(text: str, start: int) -> bool:
    """occurrence 前方小上下文否定判定（钉 4：不再枚举固定 lookbehind）。"""
    prefix = text[max(0, start - 4):start]
    return bool(_NEG_RE.search(prefix)) or prefix.endswith(_NEG_SPECIAL)


def _hit_pat(alt: str, neg_sensitive: bool) -> str:
    return (r"(?<!不)(?<!未)(?<!不得)(?<!并未)" + alt) if neg_sensitive else alt


HITPAT = "|".join(_hit_pat(a, neg) for a, _, neg in _HIT_ALTS)
HIT = re.compile(HITPAT)
# 禁行起点：从权威表起点字段派生（钉 3）
HIT_START = r"(?:" + "|".join(dict.fromkeys(start for _, start, _ in _HIT_ALTS)) + r")"


def _nohit(n: str) -> str:
    return r"(?:(?!" + HIT_START + r")[^。]){" + n + "}"


# 对等豁免链：对等量词 →(禁行)→ 免责事由 →(禁行)→ 命中词；。为唯一同句边界（分号不断链）
# 注意：HITPAT 含顶层 |，拼接时必须整体加 (?:...) 包裹，否则豁免链被拆成大 alternation
QUANT = r"(?:任何一方|双方均?|各自|彼此|遇有不可抗力的一方)"
EXCUSE = r"(?:不可抗力|情势变更|政府行为|自然灾害|疫情)"
EXEMPT = re.compile(r"(?:" + QUANT + _nohit("0,150") + EXCUSE + _nohit("0,80") + r"(?:" + HITPAT + r"))")


def f2_verdicts(text: str) -> list[tuple[str, str]]:
    """逐 occurrence：否定检查 → 豁免锚定（豁免匹配必须覆盖本次命中区间）。"""
    out: list[tuple[str, str]] = []
    for m in HIT.finditer(text):
        if _negated(text, m.start()):
            continue  # 否定文本不构成命中（钉 4）
        s, e = m.span()
        anchored = any(mm.start() <= s and mm.end() >= e for mm in EXEMPT.finditer(text))
        out.append((m.group()[:16], "豁免" if anchored else "触发需关注"))
    return out


# ============ payment：逗号分句作用域（钉 2：同句合法付款不再被整句护栏误杀） ============
_FORBID = r"(?:违约金|赔偿|责任上限|赔偿上限|注册资本|出资)"
_SUBJ = r"(?:价款|货款|采购款|报酬|服务费用?|租金|对价|合同总价|合同金额|结算|费用)"
_ACT = r"(?:支付|付款|付清|结清|支付给)"
# 锚定到逗号分句头：分句头到标的词之间不许出现冒充词（标的词∧给付动作同分句）
_PAIR_CLAUSE = (r"(?:^|[。；，])(?:(?!" + _FORBID + r")[^。，；]){0,30}"
                + _SUBJ + r"(?:(?!" + _FORBID + r")[^。，；]){0,40}" + _ACT)
_PAIR_CLAUSE_REV = (r"(?:^|[。；，])(?:(?!" + _FORBID + r")[^。，；]){0,30}"
                    + _ACT + r"(?:(?!" + _FORBID + r")[^。，；]){0,40}" + _SUBJ)
# 金额兜底：金额只在价款语境词同分句时成立
AMOUNT_CLAUSE = (r"(?:^|[。；，])(?:(?!" + _FORBID + r")[^。，；]){0,30}"
                 r"(?:价款|总价|合同金额|费用)[^。，；]{0,20}(?:人民币|\d+\s?元)")
PAYMENT_PASS = re.compile(_PAIR_CLAUSE + r"|" + _PAIR_CLAUSE_REV + r"|" + AMOUNT_CLAUSE)


# ============ term：词级 lookbehind + 日期分支锚定期限词（钉 1/外审#3） ============
# 允许期限词 = 复合式 + 「期限」(前面不许是保密/质保/保证/产品/保修) + 日期区间(前面须有允许期限词)
_TERM_ALLOWED = (r"(?:(?<!保密)(?<!质保)(?<!保证)(?<!产品)(?<!保修)(?<!质保期)(?<!保证期)(?<!产品有效)(?<!保密期)(?<!贮存)(?<! shelf))"
                 r"(?:合同期限|履行期限|交付期限|供货服务期|供货期|服务期|租赁期限|租期|工期|(?:合同|协议)有效期|期限)")
DATE_BRANCH = (_TERM_ALLOWED + r"[^。，；]{0,6}自[^。]{0,15}日起?[^。]{0,15}至[^。]{0,15}止")
TERM_PASS = re.compile(r"(?:^|。)[^。，；]{0,12}(?:合同期限|履行期限|交付期限|供货服务期|供货期|服务期|租赁期限|租期|工期|(?:合同|协议)有效期)"
                       + r"|(?:^|。)[^。，；]{0,12}" + _TERM_ALLOWED + DATE_BRANCH)


# ============ signature pass 正向入口（维持 v1.2/v1.3） ============
SIGNATURE_PASS = re.compile(
    r"(?:（盖章）|\(盖章\))[^。]{0,30}(?:法定代表人|授权代表|委托代理人)\s*[:：]"
    r"|(?:法定代表人|授权代表|委托代理人)\s*[:：][^。]{0,20}(?:（盖章）|\(盖章\))")

# ============ 断言集 ============
_F2_CLS = object  # 标记：f2_verdicts 型（整表比对）
_CASES: list[tuple[str, str, object, object]] = [
    # --- F-2（含外审历轮全部复现输入）
    ("调换顺序(复现#1)", "任何一方因不可抗力不承担违约责任；乙方逾期交付的，免除乙方全部违约责任",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("串台复现(v1.3变异#1)", "任何一方因不可抗力不承担违约责任；乙方逾期交付的，免除乙方全部违约责任",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("财政部18.2 对等免责", "任何一方对由于不可抗力造成的部分或全部不能履行合同不承担违约责任。但迟延履行后发生不可抗力的，不能免除责任。",
     f2_verdicts, [("不承担违约", "豁免")]),
    ("单方免责", "乙方逾期交付的，免除乙方全部违约责任。", f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    ("否定绑命中(复现#2)", "一方逾期交付的，乙方不免除其违约责任", f2_verdicts, []),
    ("不能免除(v1.4变异#4)", "一方迟延履行的，不能免除其违约责任", f2_verdicts, []),
    ("不予免除变体", "一方迟延履行的，不予免除其违约责任", f2_verdicts, []),
    ("不再免除变体", "一方迟延履行的，不再免除其违约责任", f2_verdicts, []),
    ("并未免除变体", "并未免除乙方违约责任", f2_verdicts, []),
    ("不得追究对等豁免(v1.4变异#3)", "任何一方因不可抗力不得追究对方违约责任",
     f2_verdicts, [("不得追究对方违约", "豁免")]),
    ("放弃追究单方触发", "乙方放弃追究甲方违约责任。", f2_verdicts, [("放弃追究", "触发需关注")]),
    ("不放弃追究不触发", "双方均不放弃追究对方违约责任。", f2_verdicts, []),
    ("MOF18.3 全句", "遇有不可抗力的一方，应在不可抗力发生后及时通知对方，并在合理期限内提供证明，可部分或者全部免除其违约责任，但迟延履行后发生不可抗力的，不免除其违约责任",
     f2_verdicts, [("免除其违约", "豁免")]),
    ("相邻独立两句", "任何一方因不可抗力不能履行合同的，不承担违约责任。乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("真单方召回保护", "免除甲方责任而乙方不免责的约定无效。乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    # --- payment（钉 2：同句共存不再误杀）
    ("采购款正例", "甲方收到发票后＿个工作日内将上月采购款支付给乙方", PAYMENT_PASS.search, True),
    ("付款+违约金同句(v1.4变异#2)", "甲方应于验收合格后十日内支付采购款，逾期支付的违约金按日计算",
     PAYMENT_PASS.search, True),
    ("正常付款正例", "货款应当在验收合格后十日内支付", PAYMENT_PASS.search, True),
    ("货款+逾期违约金同句", "货款应当在验收合格后十日内支付，如逾期按日支付违约金", PAYMENT_PASS.search, True),
    ("违约金总价反例", "违约金按合同总价的 10% 支付", PAYMENT_PASS.search, False),
    ("赔偿上限反例", "赔偿总额以合同总价为限", PAYMENT_PASS.search, False),
    ("注册资本反例", "注册资本人民币 100 万元", PAYMENT_PASS.search, False),
    ("违约金金额反例", "乙方违约的，违约金金额为 5 万元", PAYMENT_PASS.search, False),
    # --- term（钉 1：同句共存不再误杀 + 日期分支护栏）
    ("协议+保密同句(v1.4变异#1)", "本协议有效期一年，保密期限三年", TERM_PASS.search, True),
    ("保密期限日期区间(复现#3)", "保密期限自2026年1月1日起至2028年12月31日止", TERM_PASS.search, False),
    ("质保期日期冒充", "质保期自验收合格之日起至2028年12月31日止", TERM_PASS.search, False),
    ("产品有效期冒充", "产品有效期不少于18个月", TERM_PASS.search, False),
    ("合同期限日期正例", "合同期限自2026年1月1日起至2028年12月31日止", TERM_PASS.search, True),
    ("协议有效期正例", "本协议有效期一年", TERM_PASS.search, True),
    # --- signature
    ("盖章+落款组合正例", "供方（盖章）：法定代表人：____", SIGNATURE_PASS.search, True),
    ("仅盖章孤行反例", "甲方（盖章）", SIGNATURE_PASS.search, False),
    ("主体介绍无盖章反例", "甲方：某科技有限公司，法定代表人：张三，住所地：北京市海淀区。", SIGNATURE_PASS.search, False),
]


def main() -> None:
    failed: list[str] = []
    for name, text, fn, want in _CASES:
        got = fn(text) if isinstance(want, list) else bool(fn(text))
        ok = got == want
        print(("OK  " if ok else "FAIL"), name, "->", got)
        if not ok:
            failed.append(name)
    print(f"\n{len(_CASES) - len(failed)}/{len(_CASES)} passed")
    if failed:
        print("FAILED:", failed)
        raise SystemExit(1)
    print("PROTOTYPE ALL GREEN")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""F-1/F-2 第一批修复 · 正则原型试跑（设计稿 v1.5 附卷）。

只做设计期验证：本文件不进生产引擎；YAML/引擎施工以本文口径搬运 + 变异验证为准。
v1.5：①期限入口支持逗号/分号/换行/半角分句 + 协议期限复合词 + 采购/NDA 词表分列 +
日期分支真实匹配断言（修 _TERM_ALLOWED 双重拼接 bug）；②否定短语 受限头+连接字符 +
双重否定反例；③payment 分句边界统一含换行/半角；④豁免搜索窗口化（±300，防全文重扫平方级）。
tests/test_f12_regex_prototype.py 已把本表迁移进正式 pytest（CI 直接执行）。
运行：python -X utf8 tools/m65/f12_regex_prototype.py（全绿输出 PROTOTYPE ALL GREEN）。
"""
from __future__ import annotations

import re

# 分句边界（外审复现：Word/PDF 解析器用换行连接段落/表格行，换行是真实分句边界；
# OCR/旧合同常出半角标点）——payment/term 作用域统一使用
_CLAUSE_HEAD = r"(?:^|[。；，,;\n\r])"
_CLAUSE_CHAR = r"[^。，；,;\n\r]"
# F-2 同句边界维持金标/外审撤回口径：句号唯一（分号不断链）；换行边界列为施工观察项

# ============ 命中词：单一权威表（模式, 禁行起点token, 否定敏感） ============
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

# ============ 否定判定：受限否定头 + 连接字符（钉：不再枚举固定串） ============
# 头 = 不|未；头与命中词之间允许少量连接字符（能可予应急再得当为）；
# 双重否定（不得不/不可不…再接命中）不构成保护——防保护面过宽
_NEG_HEAD_TAIL = re.compile(r"(?:不|未)[能可予应急再得当为会]*$")
_DOUBLE_NEG = re.compile(r"(?:不|未)[能可予应急再得当为]*(?:不|未)[能可予应急再得当为会]*$")
_NEG_SPECIAL = ("并未", "不视为")


def _negated(text: str, start: int) -> bool:
    """occurrence 前方 6 字符上下文否定判定；双重否定返回 False（不保护）。"""
    prefix = text[max(0, start - 6):start]
    if _DOUBLE_NEG.search(prefix):
        return False
    return bool(_NEG_HEAD_TAIL.search(prefix)) or prefix.endswith(_NEG_SPECIAL)


def _hit_pat(alt: str, neg_sensitive: bool) -> str:
    # 否定判定全权交给 occurrence 级 _negated（v1.5 教训：lookbehind 会把
    # 「不得不放弃」这类双重否定提前拦掉，使上下文区分逻辑永远轮不到）
    del neg_sensitive
    return alt


HITPAT = "|".join(_hit_pat(a, neg) for a, _, neg in _HIT_ALTS)
HIT = re.compile(r"(?:" + HITPAT + r")")
HIT_START = r"(?:" + "|".join(dict.fromkeys(start for _, start, _ in _HIT_ALTS)) + r")"


def _nohit(n: str) -> str:
    return r"(?:(?!" + HIT_START + r")[^。]){" + n + "}"


# 对等豁免链：对等量词 →(禁行)→ 免责事由 →(禁行)→ 命中词；。为唯一同句边界（分号不断链）
QUANT = r"(?:任何一方|双方均?|各自|彼此|遇有不可抗力的一方)"
EXCUSE = r"(?:不可抗力|情势变更|政府行为|自然灾害|疫情)"
EXEMPT = re.compile(r"(?:" + QUANT + _nohit("0,150") + EXCUSE + _nohit("0,80")
                    + r"(?:" + HITPAT + r"))")

# 豁免搜索窗口（生产实现必须窗口化：防逐命中全文重扫的平方级开销）
_EXEMPT_WINDOW = 320


def f2_verdicts(text: str) -> list[tuple[str, str]]:
    """逐 occurrence：否定检查 → 窗口化豁免锚定（豁免匹配必须覆盖本次命中区间）。"""
    out: list[tuple[str, str]] = []
    for m in HIT.finditer(text):
        if _negated(text, m.start()):
            continue
        s, e = m.span()
        w0 = max(0, s - _EXEMPT_WINDOW)
        w1 = min(len(text), e + _EXEMPT_WINDOW)
        anchored = any(w0 + mm.start() <= s and w0 + mm.end() >= e
                       for mm in EXEMPT.finditer(text, w0, w1))
        out.append((m.group()[:16], "豁免" if anchored else "触发需关注"))
    return out


# ============ payment：逗号/分号/换行分句作用域 + 冒充词禁行 ============
_FORBID = r"(?:违约金|赔偿|责任上限|赔偿上限|注册资本|出资)"
_SUBJ = r"(?:价款|货款|采购款|报酬|服务费用?|租金|对价|合同总价|合同金额|结算|费用)"
_ACT = r"(?:支付|付款|付清|结清|支付给)"
_NOFORB = r"(?:(?!" + _FORBID + r")" + _CLAUSE_CHAR + r")"
_PAIR = (r"(?:" + _CLAUSE_HEAD + _NOFORB + r"{0,30}" + _SUBJ + _NOFORB + r"{0,40}" + _ACT
         + r"|" + _CLAUSE_HEAD + _NOFORB + r"{0,30}" + _ACT + _NOFORB + r"{0,40}" + _SUBJ + r")")
_AMOUNT = (r"(?:" + _CLAUSE_HEAD + _NOFORB + r"{0,30}"
           + r"(?:价款|总价|合同金额|费用)[^。，；,;\n\r]{0,20}(?:人民币|\d+\s?元))")
PAYMENT_PASS = re.compile(_PAIR + r"|" + _AMOUNT)

# ============ term：采购与 NDA 词表分列（钉：避免泛期限混入异议/通知期限） ============
# 词级 lookbehind：泛「期限」前面贴着这些词头的不算（保密期限/质保期/保证期/产品有效/贮存期/异议期限/通知期限…按头排除）
_TERM_LB = r"(?:(?<!保密)(?<!质保)(?<!保证)(?<!产品)(?<!保修)(?<!贮存)(?<!异议)(?<!通知)(?<!缓冲)(?<!公示))期限"
_PROC_WORDS = r"(?:合同期限|履行期限|交付期限|供货服务期|供货期|服务期|租赁期限|租期|工期)"
_NDA_WORDS = r"(?:协议期限|(?:合同|协议)有效期)"
_TERM_ANY = r"(?:" + _PROC_WORDS + r"|" + _NDA_WORDS + r"|" + _TERM_LB + r")"
# 日期分支：允许期限词(+≤6字) → 自…日(起)…至…止（真实验收断言匹配范围覆盖完整区间）
_DATE_PROC = re.compile(r"(?:^|[。；，,;\n\r])[^。，；,;\n\r]{0,12}(?:" + _PROC_WORDS + r"|" + _TERM_LB + r")"
                        + r"[^。，；,;\n\r]{0,6}自[^。]{0,15}日起?[^。]{0,15}至[^。]{0,15}止")
_DATE_NDA = re.compile(r"(?:^|[。；，,;\n\r])[^。，；,;\n\r]{0,12}(?:" + _NDA_WORDS + r"|" + _TERM_LB + r")"
                       + r"[^。，；,;\n\r]{0,6}自[^。]{0,15}日起?[^。]{0,15}至[^。]{0,15}止")
_TERM_PROC = re.compile(r"(?:^|[。；，,;\n\r])[^。，；,;\n\r]{0,12}(?:" + _PROC_WORDS + r"|" + _NDA_WORDS
                        + r"|" + _TERM_LB + r")")
_TERM_NDA = re.compile(r"(?:^|[。；，,;\n\r])[^。，；,;\n\r]{0,12}(?:" + _NDA_WORDS + r"|" + _PROC_WORDS
                       + r"|" + _TERM_LB + r")")
# 断言集入口别名（采购/NDA 各自 pass 词表）
TERM_PASS_PROC = _TERM_PROC
TERM_PASS_NDA = _TERM_NDA

# ============ signature pass 正向入口（维持） ============
SIGNATURE_PASS = re.compile(
    r"(?:（盖章）|\(盖章\))[^。]{0,30}(?:法定代表人|授权代表|委托代理人)\s*[:：]"
    r"|(?:法定代表人|授权代表|委托代理人)\s*[:：][^。]{0,20}(?:（盖章）|\(盖章\))")

# ============ 断言集 ============
_CASES: list[tuple[str, str, object, object]] = [
    # --- F-2 回归（历轮全量）
    ("调换顺序(复现#1)", "任何一方因不可抗力不承担违约责任；乙方逾期交付的，免除乙方全部违约责任",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("财政部18.2 对等免责", "任何一方对由于不可抗力造成的部分或全部不能履行合同不承担违约责任。但迟延履行后发生不可抗力的，不能免除责任。",
     f2_verdicts, [("不承担违约", "豁免")]),
    ("单方免责", "乙方逾期交付的，免除乙方全部违约责任。", f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    ("否定绑命中(复现#2)", "一方逾期交付的，乙方不免除其违约责任", f2_verdicts, []),
    ("不能免除变体", "一方迟延履行的，不能免除其违约责任", f2_verdicts, []),
    ("不予免除变体", "一方迟延履行的，不予免除其违约责任", f2_verdicts, []),
    ("不会免除(v1.5变异#2)", "该约定不会免除其违约责任", f2_verdicts, []),
    ("不应当免除(v1.5变异#2)", "该约定不应当免除其违约责任", f2_verdicts, []),
    ("不可免除变体", "该约定不可免除其违约责任", f2_verdicts, []),
    ("不可能免除变体", "该约定不可能免除其违约责任", f2_verdicts, []),
    ("双重否定不保护(v1.5变异#2反例)", "乙方不得不放弃追究违约责任。", f2_verdicts,
     [("放弃追究", "触发需关注")]),
    ("不得追究对等豁免(v1.3变异#3)", "任何一方因不可抗力不得追究对方违约责任",
     f2_verdicts, [("不得追究对方违约", "豁免")]),
    ("放弃追究单方触发", "乙方放弃追究甲方违约责任。", f2_verdicts, [("放弃追究", "触发需关注")]),
    ("不放弃追究不触发", "双方均不放弃追究对方违约责任。", f2_verdicts, []),
    ("MOF18.3 全句", "遇有不可抗力的一方，应在不可抗力发生后及时通知对方，并在合理期限内提供证明，可部分或者全部免除其违约责任，但迟延履行后发生不可抗力的，不免除其违约责任",
     f2_verdicts, [("免除其违约", "豁免")]),
    ("相邻独立两句", "任何一方因不可抗力不能履行合同的，不承担违约责任。乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("真单方召回保护", "免除甲方责任而乙方不免责的约定无效。乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    # --- payment（换行分句 v1.5）
    ("采购款正例", "甲方收到发票后＿个工作日内将上月采购款支付给乙方", PAYMENT_PASS.search, True),
    ("付款+违约金同句", "甲方应于验收合格后十日内支付采购款，逾期支付的违约金按日计算", PAYMENT_PASS.search, True),
    ("换行分句付款(v1.5变异#3)", "违约金按合同总价10%计算\n甲方应于验收后支付采购款", PAYMENT_PASS.search, True),
    ("半角分号付款", "违约金按合同总价10%计算; 甲方应于验收后支付采购款", PAYMENT_PASS.search, True),
    ("正常付款正例", "货款应当在验收合格后十日内支付", PAYMENT_PASS.search, True),
    ("货款+逾期违约金同句", "货款应当在验收合格后十日内支付，如逾期按日支付违约金", PAYMENT_PASS.search, True),
    ("违约金总价反例", "违约金按合同总价的 10% 支付", PAYMENT_PASS.search, False),
    ("赔偿上限反例", "赔偿总额以合同总价为限", PAYMENT_PASS.search, False),
    ("注册资本反例", "注册资本人民币 100 万元", PAYMENT_PASS.search, False),
    ("违约金金额反例", "乙方违约的，违约金金额为 5 万元", PAYMENT_PASS.search, False),
    # --- term 采购（独立词表）
    ("采购合同期限日期正例", "合同期限自2026年1月1日起至2028年12月31日止", TERM_PASS_PROC.search, True),
    ("采购履行期限正例", "乙方履行期限为2026年6月30日", TERM_PASS_PROC.search, True),
    # --- term NDA（独立词表；v1.5变异#1 两条 + 日期真实验收）
    ("保密+协议同句(v1.4变异#1)", "保密期限三年，本协议有效期一年", TERM_PASS_NDA.search, True),
    ("本协议期限为三年(v1.5变异#1回退)", "本协议期限为三年", TERM_PASS_NDA.search, True),
    ("协议有效期正例", "本协议有效期一年", TERM_PASS_NDA.search, True),
    ("保密期限日期区间(复现#3)", "保密期限自2026年1月1日起至2028年12月31日止", TERM_PASS_NDA.search, False),
    ("质保期日期冒充(采购)", "质保期自验收合格之日起至2028年12月31日止", TERM_PASS_PROC.search, False),
    ("产品有效期冒充", "产品有效期不少于18个月", TERM_PASS_NDA.search, False),
    ("异议期限不算协议期限", "异议期限为七个工作日", TERM_PASS_NDA.search, False),
]


def _date_span_check() -> None:
    """日期分支真实验收（外审#1：不能只断言 bool——匹配必须覆盖完整日期区间）。"""
    t = "合同期限自2026年1月1日起至2028年12月31日止"
    m = _DATE_PROC.search(t)
    assert m, "日期分支未命中"
    assert "2026年1月1日" in m.group() and "2028年12月31日止" in m.group(), \
        f"日期分支未覆盖完整区间: {m.group()!r}"


def _stress_repeat_hits() -> None:
    """性能边界（外审提示）：窗口化搜索下 300 处重复命中应近线性完成。"""
    t = "乙方逾期交付的，免除乙方全部违约责任。" * 300
    v = f2_verdicts(t)
    assert len(v) == 300 and all(x[1] == "触发需关注" for x in v), "压力样本判定错误"


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

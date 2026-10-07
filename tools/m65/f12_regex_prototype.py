# -*- coding: utf-8 -*-
"""F-1/F-2 第一批修复 · 正则原型试跑（设计稿 v1.3 附卷，外审要求随版提交）。

只做设计期正则验证：本文件不进生产引擎，YAML 施工以本文正则原样搬运 + 变异验证为准。
运行：python -X utf8 tools/m65/f12_regex_prototype.py（全绿输出 PROTOTYPE ALL GREEN）。
"""
from __future__ import annotations

import re

# ============ F-2 豁免门（breach 条目 need_attention + unless_window/unless_anchor） ============
# 否定前缀焊进命中正则（v1.2 教训：独立 unless 层会被锚定条件挡死）——
# 「不免除/并未免除/不得免除/不视为免除」开头的文本根本不成为命中
NEG_PREFIX = r"(?<!不)(?<!未)(?<!不得)(?<!并未)(?<!不视为)"
HITPAT = (r"(?:不承担违约|无需承担违约|"
          + NEG_PREFIX + r"免除违约责任|" + NEG_PREFIX + r"免除.{0,8}违约|"
          r"不负违约金义务|不得主张违约金|豁免违约责任|违约责任由.{0,10}自行承担)")
HIT = re.compile(HITPAT)
# 命中词「起点禁行」：链上不许跨越任何其他命中（v1.2 教训：[^。] 裸跨越会借用前句通行证）
HIT_START = r"(?:不承担违约|无需承担违约|免除|不负违约金义务|不得主张违约金|豁免违约责任|违约责任由)"


def _nohit(n: str) -> str:
    return r"(?:(?!" + HIT_START + r")[^。]){" + n + "}"


# 对等豁免链：对等量词 →(禁行)→ 免责事由 →(禁行)→ 命中词；三要素同句（。为唯一边界，分号不断链）
EXEMPT = re.compile(
    r"(?:任何一方|双方均?|各自|彼此|遇有不可抗力的一方)"
    + _nohit("0,150")
    + r"(?:不可抗力|情势变更|政府行为|自然灾害|疫情)"
    + _nohit("0,80")
    + HITPAT)


def f2_verdicts(text: str) -> list[tuple[str, str]]:
    """逐 occurrence 判定：豁免匹配必须覆盖本次命中区间（unless_anchor 语义）。"""
    out: list[tuple[str, str]] = []
    for m in HIT.finditer(text):
        s, e = m.span()
        anchored = any(mm.start() <= s and mm.end() >= e for mm in EXEMPT.finditer(text))
        out.append((m.group()[:16], "豁免" if anchored else "触发需关注"))
    return out


# ============ payment / term 语义域（句首锚定 + 整句负护栏） ============
_GUARD = r"(?![^。]{0,80}(?:违约金|赔偿|责任上限|赔偿上限|注册资本|出资))"
_SUBJ = r"(?:价款|货款|采购款|报酬|服务费用?|租金|对价|合同总价|合同金额|结算|费用)"
_ACT = r"(?:支付|付款|付清|结清|支付给)"
PAYMENT_PASS = re.compile(
    r"(?:^|。)" + _GUARD + r"[^。]{0,40}" + _SUBJ + r"[^。]{0,40}" + _ACT
    + r"|(?:^|。)" + _GUARD + r"[^。]{0,40}" + _ACT + r"[^。]{0,40}" + _SUBJ
    + r"|(?:^|。)" + _GUARD + r"[^。]{0,20}(?:价款|总价|合同金额|费用)[^。]{0,20}(?:人民币|\d+\s?元)")

TERM_PASS = re.compile(
    r"(?:^|。)(?![^。]{0,80}(?:保密期限|质保期|保证期|产品有效))[^。]{0,60}"
    r"(?:合同期限|履行期限|交付期限|供货服务期|供货期|服务期|租赁期限|租期|工期"
    r"|(?:合同|协议)有效期|(?:自[^。]{0,15}日起?[^。]{0,15}至[^。]{0,15}止))")

# ============ signature pass 正向入口 ============
SIGNATURE_PASS = re.compile(
    r"(?:（盖章）|\(盖章\))[^。]{0,30}(?:法定代表人|授权代表|委托代理人)\s*[:：]"
    r"|(?:法定代表人|授权代表|委托代理人)\s*[:：][^。]{0,20}(?:（盖章）|\(盖章\))")

# ============ 断言集 ============
_CASES: list[tuple[str, str, object, object]] = [
    # --- F-2（want = 逐 occurrence 判定表；否定文本不产生命中=无条目）
    ("调换顺序(外审复现#1)", "任何一方因不可抗力不承担违约责任；乙方逾期交付的，免除乙方全部违约责任",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("财政部18.2 对等免责", "任何一方对由于不可抗力造成的部分或全部不能履行合同不承担违约责任。但迟延履行后发生不可抗力的，不能免除责任。",
     f2_verdicts, [("不承担违约", "豁免")]),
    ("单方免责", "乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    ("否定绑命中(外审复现#2)", "一方逾期交付的，乙方不免除其违约责任", f2_verdicts, []),
    ("MOF18.3 全句", "遇有不可抗力的一方，应在不可抗力发生后及时通知对方，并在合理期限内提供证明，可部分或者全部免除其违约责任，但迟延履行后发生不可抗力的，不免除其违约责任",
     f2_verdicts, [("免除其违约", "豁免")]),
    ("相邻独立两句", "任何一方因不可抗力不能履行合同的，不承担违约责任。乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("不承担违约", "豁免"), ("免除乙方全部违约", "触发需关注")]),
    ("真单方召回保护", "免除甲方责任而乙方不免责的约定无效。乙方逾期交付的，免除乙方全部违约责任。",
     f2_verdicts, [("免除乙方全部违约", "触发需关注")]),
    # --- payment（want = 是否通过）
    ("采购款正例", "甲方收到发票后＿个工作日内将上月采购款支付给乙方", PAYMENT_PASS.search, True),
    ("正常付款正例", "货款应当在验收合格后十日内支付", PAYMENT_PASS.search, True),
    ("违约金总价反例", "违约金按合同总价的 10% 支付", PAYMENT_PASS.search, False),
    ("赔偿上限反例", "赔偿总额以合同总价为限", PAYMENT_PASS.search, False),
    ("注册资本反例", "注册资本人民币 100 万元", PAYMENT_PASS.search, False),
    ("违约金金额反例", "乙方违约的，违约金金额为 5 万元", PAYMENT_PASS.search, False),
    # --- term
    ("保密期限日期区间(外审复现#3)", "保密期限自2026年1月1日起至2028年12月31日止", TERM_PASS.search, False),
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
        if isinstance(want, list):
            got = fn(text)  # F-2：逐 occurrence 判定表，整表比对
        else:
            got = bool(fn(text))  # search 类：有命中=通过/触发
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

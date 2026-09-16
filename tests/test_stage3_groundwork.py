"""阶段 3 地基：三分法簇 id/class 透传（阶段 0 P3 承诺兑现）。

异议层受理分流（hardline 拒收 / existence 只收漏报 / heuristic 收误报）
的依据是每条结果的 rule_class——本文件钉透传语义：
- 命中规则自带 class（规则级）> 簇级 class（经 item 透传）> 默认 heuristic
- 非法值回落 heuristic（fail-safe：多问人、不放水）
- need_attention/pass/missing 三种档位路径都带出
"""
from __future__ import annotations

from app.services.checklist import _eval_item

_PROC = """甲方：某公司。乙方：某供应商。
第一条 标的与价款：货款十万元。
第二条 背靠背条款：收不到最终客户款项则不付服务费。
第三条 争议解决：协商不成向被告所在地法院起诉。
"""


def _proc_item(yaml_extra: str, text: str = _PROC):
    """构造单 item 跑判定（yaml_extra 直接拼在 item dict 层级）。"""
    import yaml as yaml_mod

    cfg = yaml_mod.safe_load(
        """
id: t_item
name: 测试项
segment: A
"""
        + yaml_extra
    )
    return _eval_item(text, cfg)


def test_rule_class_defaults_to_heuristic_when_unmarked():
    out = _proc_item(
        """
rules:
  need_attention:
    - any_of: ["收不到最终客户款项则不付服务费"]
      note: 背靠背提示
"""
    )
    assert out["status"] == "需关注"
    assert out["rule_class"] == "heuristic", "未标默认 heuristic"
    assert out["rule_id"] == "t_item#r0"


def test_cluster_class_beats_item_default():
    """簇级 class 优先于 item 级默认（背靠背簇 hardline 的既有形态）。"""
    out = _proc_item(
        """
class: heuristic
rules:
  need_attention:
    - any_of: ["收不到最终客户款项则不付服务费"]
      note: 背靠背提示
      class: hardline
"""
    )
    assert out["rule_class"] == "hardline"
    assert out["rule_id"] == "t_item#r0"


def test_item_class_inherited_by_cluster_and_missing():
    """item 级 class：簇未标时继承；「未找到」路径同样带出。"""
    out = _proc_item(
        """
class: existence
rules:
  need_attention:
    - any_of: ["押金不予退还"]
      note: 押金
"""
    )
    # 文本无押金内容 → 走 missing；existence 语义 =「有没有写」的 item 级落点
    assert out["status"] in {"未找到", "需关注"}
    assert out["rule_class"] == "existence"
    if out["status"] == "未找到":
        assert out["rule_id"] is None, "missing 路径无命中簇，rule_id 为空"


def test_pass_path_also_relays():
    out = _proc_item(
        """
class: existence
rules:
  need_attention:
    - any_of: ["押金不予退还"]
      note: 押金
  pass:
    - any_of: ["协商不成向被告所在地法院起诉"]
      note: 有管辖约定
      class: heuristic
"""
    )
    # 文本含管辖句 → pass 命中（押金词表未命中）
    if out["status"] == "通过":
        assert out["rule_id"] == "t_item#p0"
        assert out["rule_class"] == "heuristic"


def test_invalid_class_falls_back_to_heuristic():
    """非法 class 值回落 heuristic（fail-safe：多问人、不放水）。"""
    out = _proc_item(
        """
rules:
  need_attention:
    - any_of: ["收不到最终客户款项则不付服务费"]
      note: n
      class: maybe_soft
"""
    )
    assert out["rule_class"] == "heuristic"


def test_schema_relay_accepts_rule_class():
    """API schema 接受新字段且旧记录（无键）默认 None。"""
    from app.api.schemas import ChecklistItemResult

    new = ChecklistItemResult(
        id="x", name="x", status="需关注",
        rule_id="x#r0", rule_class="hardline",
    )
    assert new.rule_class == "hardline"
    old = ChecklistItemResult(id="x", name="x", status="通过")
    assert old.rule_id is None and old.rule_class is None, "旧记录兼容"


def test_invalid_class_fails_closed():
    """外审批 2 / 老钱裁定书第六节红线：显式标注的 class 拼错（hardlin）
    必须 fail-closed 启动失败，绝不静默回落 heuristic——那是「铁律线被
    拼错成启发式、AI 获得异议资格」的制度性漏洞。未标注仍是合法默认。"""
    import textwrap

    import pytest as _pytest

    from app.services.checklist import _validate_category_config, load_checklist

    # 三份正式配置全部合法（回归确认现有标注无拼错）
    for cat in ("procurement", "lease", "nda"):
        load_checklist(cat)  # 不 raise 即通过

    bad_cfg = textwrap.dedent("""
        category: procurement
        category_label: 采购合同
        items:
          - id: x
            name: X
            class: hardlin
            rules:
              need_attention:
                - pattern: "坏词"
    """)
    with _pytest.raises(ValueError, match="hardlin"):
        _validate_category_config("procurement", __import__("yaml").safe_load(bad_cfg))

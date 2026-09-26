# 模型 A/B 对比报告：GLM-5.3-flash（easyrouter） vs DeepSeek（官方 API）

> 日期：2026-09-26 · 跑道：`tools/model_ab/run_once.py` · 同卷同码零改动，差异全部归因于模型/通道
> 卷子：procurement_four_risk / lease_four_risk / nda_gold_risks（三品类已知风险设计卷）
> 每卷完整生产管线（评分卡/补盲/质量/异议）+ 对有摘录的需关注项追问 2 次
> 原始数据：`docs/model-ab/glm-5.3-flash.json` / `docs/model-ab/deepseek.json`

## 一、总表

| 维度 | GLM-5.3-flash（easyrouter） | DeepSeek（官方 API） |
|---|---|---|
| 三卷审查总耗时 | **1296.0s** | **321.7s**（快 4 倍） |
| 单卷耗时 | 358 / 491 / 447s | 65 / 175 / 82s |
| 评分卡可用 | 2/3（lease 降级） | **3/3** |
| 质量层可用 | 1/3（两卷 llm_error） | **3/3**（10/11/9 条观察） |
| 异议层可用 | 2/3（nda 整层不可用） | **3/3** |
| 追问成功 | 5/6（1 次 182s 超时失败） | **6/6** |
| 追问平均耗时 | 106.3s | **28.3s** |
| JSON 解析成功（成功调用内） | 5/5 | 6/6 |
| 引用核验通过（quote_verified） | 0/5 | 0/6 |

## 二、发现

### 1. 稳定性：DeepSeek 明显占优
GLM 侧三次 LLM 层降级（scorecard llm_error、quality llm_error ×2、异议层不可用）+ 一次追问超时。
**归因注意**：GLM 走的是 easyrouter 路由，`llm_error` 可能是路由层超时/排队，不一定是 GLM 模型本身的问题。要给 GLM「洗清嫌疑」，需直连智谱官方 API 复测（本地 .env 换 `https://open.bigmodel.cn/api/paas/v4` + 标准 Key 即可）。

### 2. 速度：DeepSeek 全面领先
审查快 4 倍、追问快 3.75 倍。GLM-5.3-flash 经路由单调用常 30-100s+，整链路十几次调用被放大成 6-8 分钟/卷。生产环境用户上传后等 20 分钟是不可接受的。

### 3. 「多点组合引用」是两家通病（本次最重要确认）
quote_verified 两家都是 0——跟生产验收发现一致：模型引的句子是真的，但爱打包成「第一条（…）：『…』；第二条（…）：『…』」格式，整串核验必挂。
**这不是某一家模型的毛病，是世代共性 → 修复方向更明确了：核验器按「」『』拆片段逐句比对（F02 原始设计意图）。修好后两家的真实引用质量才有可比性。**

### 4. 内容质量面（唯一两家都全绿的 procurement 卷）
- 质量观察 10 条 vs 10 条、事实抽取 9 vs 9——打平
- 异议都受理 2 条，但**反证状态分歧**：GLM 两条都判 present（找到了反证原文），DeepSeek 判 absent+present——同一条异议两家一个说「有反证」一个说「没找到」。谁对需人工复核，这正是「不同模型脾气」的直接样本（登记簿/票据化后这类分歧可追溯、可审计）
- 评分卡总分分歧大（16 vs 20；lease 64 vs GLM 失败）——评分卡是 advisory 不改档位，但说明两家对「合同好坏」的直觉不同

## 三、结论与建议

1. **当前主脑维持 DeepSeek（官方 API）**：稳定性、速度全面占优，且底线表现可靠（不编造、失败诚实、结构可解析）。
2. **GLM-5.3-flash 暂不具主脑资格，但存疑两点待排除**：路由层干扰（直连复测）、闪-cycle 降级是否偶发（样本小）。
3. **先修片段核验，再复测引用质量**：当前 0% vs 0% 比不出高下；修好后引用核验通过率会成为最有信息量的模型对比指标。
4. **对比跑道已沉淀**（`tools/model_ab/run_once.py`）：M6.5 真实合同验证阶段可直接复用——换卷子就能跑，两模型同台。

## 四、复现方式

```bash
# GLM（走 .env 的 easyrouter 配置）
python -X utf8 tools/model_ab/run_once.py --profile glm --out docs/model-ab/glm-5.3-flash.json
# DeepSeek（key 从本地部署凭据文件读，不进对话）
python -X utf8 tools/model_ab/run_once.py --profile deepseek --out docs/model-ab/deepseek.json
```

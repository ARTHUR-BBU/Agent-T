# fixtures-real · 真实合同范本来源档案（M6.5）

> 2026-09-28 从公开渠道下载，用于 M6.5 真实合同验证（七问评测）。
> 全部为政府/官方机构正式发布的合同示范文本或参考文本，非第三方拼凑。

## 采购类（manifest: procurement）

| 文件 | 来源 | 字数 |
|---|---|---|
| gov-procurement-goods-mof-2024.docx | 中国政府网政策文件库，财政部财办库〔2024〕84号《政府采购货物买卖合同（试行）》官方附件：https://www.gov.cn/zhengce/zhengceku/202404/P020240429360806858552.docx | ~11000 |
| school-uniform-procurement-guangzhou.docx | 全国合同示范文本库（SAMR），广州市校服采购合同 SF-2021-0118 | ~4700 |
| food-procurement-xinjiang-2025.docx | 全国合同示范文本库，新疆学校食堂食材采购合同（示范文本，新疆市监局+教育厅 2025版） | ~5900 |

## 租赁类（manifest: lease）

| 文件 | 来源 | 字数 |
|---|---|---|
| house-lease-gf-2025-2614.docx | 全国合同示范文本库，GF-2025-2614 城镇房屋租赁合同（市场监管总局 2025-04 制定）★主力 | ~8500 |
| equipment-lease-construction-2000.docx | 同库，GF-2000-0604 建筑施工物资租赁合同（2000 老版官方文本，条款齐但短） | ~1200 |

## NDA 类（manifest: nda）

| 文件 | 来源 | 字数 |
|---|---|---|
| trade-secret-nda-guangdong.docx | 广东省市场监督管理局《商业秘密保密协议（参考文本）》（用人单位↔劳动者型）：https://amr.gd.gov.cn/gkmlpt/content/2/2980/post_2980375.html | ~5200 |
| business-cooperation-nda-guangdong.docx | 同页《商务合作保密协议（参考文本）》（企业↔企业型） | ~3000 |

## 加赠（未入首轮实测）

| 文件 | 来源 | 说明 |
|---|---|---|
| zibo-trade-secret-compilation.pdf | 淄博市市场监管局《企业商业秘密保护常用制度措施汇编》 | 60 页/4.6 万字，含 6 个范本；可作 PDF 解析路径扩展语料 |

## manifest.json

`文件名 → 品类` 路由表，供 tools/m65/run_eval.py 使用。

## 补语料通道（复用笔记）

全国合同示范文本库（htsfwb.samr.gov.cn）有公开 API：
- 搜索：`/api/content/SearchTemplates?key=<关键词>&loc=<地区>`
- 下载：`/api/File/DownTemplate?id=<模板id>&type=1`（type=1 docx，type=2 pdf）

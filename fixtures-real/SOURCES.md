# fixtures-real · 真实合同范本来源档案（M6.5）

> 下载日期：2026-09-28 · 用途：M6.5 真实合同验证（七问评测）· 名册：manifest.json（跑道唯一权威）
> 全部为政府/官方机构正式发布的合同示范文本或参考文本，非第三方拼凑。
> 注：3 份原始 .doc/WPS 文件（新疆食材、广东两份 NDA）经本机 Word 2016 COM 只读转换
> 为标准 docx（Word 自身转换保真），mtime 为转换时刻；其余文件 mtime ≈ 下载时刻。

## 名册内（7 份正式合同，run1 七问统计范围）

### 采购类（manifest: procurement）

| 文件 | SHA256 | 来源 |
|---|---|---|
| gov-procurement-goods-mof-2024.docx | `1e95d7125018aded5794d0a4cf506063a718a5fc54a73c3dd74568cd78d0f0dc` | 中国政府网政策文件库，财政部财办库〔2024〕84号《政府采购货物买卖合同（试行）》官方附件：https://www.gov.cn/zhengce/zhengceku/202404/P020240429360806858552.docx |
| school-uniform-procurement-guangzhou.docx | `12218512b8c7206c31db6ed34dd6b23686a40b98a37736c7964b7fd7651afcbe` | 全国合同示范文本库（SAMR htsfwb.samr.gov.cn），广州市校服采购合同 SF-2021-0118（2021版） |
| food-procurement-xinjiang-2025.docx | `b9c87d23a82f23cc22436c551355e3b794990abc60d17f48c5e24f23eb579efa` | 全国合同示范文本库，新疆维吾尔自治区学校食堂食材采购合同（示范文本，新疆市监局+教育厅 2025版） |

### 租赁类（manifest: lease）

| 文件 | SHA256 | 来源 |
|---|---|---|
| house-lease-gf-2025-2614.docx | `b61d8b900d9841b9a60604723e37c7dee23accd082bd8fd8d92f0de486a39075` | 全国合同示范文本库，**GF-2025-2614** 城镇房屋租赁合同（市场监管总局 2025-04 制定）★主力 |
| equipment-lease-construction-2000.docx | `961bba7ce90c52e6a8c0a40b81690b11e629714c3ada79b4896be42c4daa4112` | 全国合同示范文本库，**GF-2000-0604** 建筑施工物资租赁合同（2000 老版官方文本，条款齐但短） |

### NDA 类（manifest: nda）

| 文件 | SHA256 | 来源 |
|---|---|---|
| trade-secret-nda-guangdong.docx | `12bb01dc842b450cd95a271bdcd9dc237da5d2344f2f2bfd3c2cc18695b47072` | 广东省市场监督管理局《商业秘密保密协议（参考文本）》（用人单位↔劳动者型）：https://amr.gd.gov.cn/gkmlpt/content/2/2980/post_2980375.html |
| business-cooperation-nda-guangdong.docx | `e35731379de285009f6752c1b8bb44d650c801b4f6f6250a055429ec60d3fc24` | 同页《商务合作保密协议（参考文本）》（企业↔企业型） |

## 名册外（非合同场景，不入七问统计）

登记于 **manifest-extra.json**（附加场景名册，每个文件显式登记品类，不猜）：

| 文件 | 附加品类 | SHA256 | 说明 |
|---|---|---|---|
| zibo-trade-secret-compilation.pdf | procurement（按采购规则包审，属刻意的错配场景样本） | `67aa603649c3e66d9fa63fd1338a147a7e5a876f00aa1bcc263b2586e26d1e95` | 淄博市市场监管局《企业商业秘密保护常用制度措施汇编》，60 页/4.6 万字，含 6 个范本。**资料汇编不是单份合同**——只作 PDF 解析路径与非合同文档场景样本（`--all` 时落 contracts_extra），绝不混入合同统计（外审 #86 P1） |

## F-1 / F-2 证据锚点（原文位置，供规则修复时对照同一份材料）

- **F-1（规则漏报）** food-procurement-xinjiang-2025.docx：
  - 第 43 段「一、合同期限」/ 第 46 段「二、合同金额、食材供应的品种及单价」
  - 第 59 段「（二）乙方应于次月第一周，向甲方提供上月货款的正规发票，甲方经核实无异议后，在收到发票＿个工作日内将上月采购款支付给乙方。」
  - 审查结果却为：清单「期限」「价款与支付」= 未找到
- **F-2（对等条款误报）** gov-procurement-goods-mof-2024.docx 第 252 段：
  - 原文「18.2 任何一方对由于不可抗力造成的部分或全部不能履行合同不承担违约责任。但迟延履行后发生不可抗力的，不能免除责任。」
  - 审查结果却为：违约责任项 note「单方免除乙方违约责任，权利义务严重不对等」

## manifest.json

`文件名 → 品类` 路由表，同时是跑道（tools/m65/run_eval.py）的**唯一正式合同名册**；
名册内文件缺失磁盘会在跑前告警。

## 补语料通道（复用笔记）

全国合同示范文本库（htsfwb.samr.gov.cn）有公开 API：
- 搜索：`/api/content/SearchTemplates?key=<关键词>&loc=<地区>`
- 下载：`/api/File/DownTemplate?id=<模板id>&type=1`（type=1 docx，type=2 pdf）

## 第二轮扩样（2026-09-28，SAMR 全国合同示范文本库，10 份）

> 目的：锤 F-1（期限/价款漏报）与 F-2（对等免责误报）是否通病。样式跨度 2000→2026 五代
> 文本体例 + 地方版；3 份 OLE .doc 经 Word COM 转 docx（验证通过）。下载：htsfwb.samr.gov.cn
> 公开 API（DownTemplate type=1）。品类按最接近规则包登记（委托/承揽 → procurement）。

### 采购/买卖/工程/服务类（manifest: procurement）

| agri-produce-sale-samr-2025.docx | `d504a9b7a76cf0cb6bdbab7cd05ceabf6a87d8e08b26f6d313c453a252f886da` | SAMR 库 id 8ed1cb2d，GF-2025-0151 农副产品买卖合同 |
| cement-sale-gf-2008.docx | `b0c4c44d677de2157e241af89ad153025d94d10e00262483e2ba7c303094949b` | SAMR 库 id 92ca27de，GF-2008-0113 水泥买卖合同（老式表格条款） |
| data-processing-service-2025.docx | `403ff3d365a01bc572bd8ea37bf2b0fd75c17c7ba20c01284a5ddb0492bb90fe` | SAMR 库 id 6cdbe704，GF-2025-2616 数据委托处理服务合同（国家数据局+市监总局） |
| construction-work-contract-2017.docx | `490462c0b0d6b16d7081e99a7aa8c4c7131cd735a553b3cdcb30e20ed8a7d1d8` | SAMR 库 id 082423f0，GF-2017-0201 建设工程施工合同（6.2万字含通用条款） |
| energy-hosting-service-2026.docx | `bcb7b11d8501b1702bcebb018c4bd0adabc1b98ecdd2ed5edcbed6a597e6ce72` | SAMR 库 id a1a47ab6，GF-2026-2621 公共机构能源费用托管服务合同（2026最新） |
| raw-milk-purchase-2016.docx | `9e06a79d9e83e4d370e7e6c4128c8b952078cb50d710fc7f08dfe20ae939fabc` | SAMR 库 id cd988310，GF-2016-0157 生鲜乳购销合同 |

### 租赁类（manifest: lease）

| shanghai-residential-lease-2014.docx | `3544c4024de8c37dbbb8548bce1248d505c454d0f1035e9f7a8bf11ee03c663e` | SAMR 库 id 179cc9d1，上海市居住房屋租赁合同（2014版，非GF编号） |
| zhejiang-vehicle-lease-2024.docx | `596029723222aa69c38df026e1d0f50dc95ec2472b109fef1ff3e45a50f0bee6` | SAMR 库 id 7d0f8a02，HT33/SF24-2024 浙江省小微型客车租赁合同（3.8万字） |

### 其他类（manifest: procurement——最接近规则包）

| mandate-contract-samr-2025.docx | `58f7cd38beff837cee1a5b79b68a2da1058d596cc021f7fa5b66807875bcd9e6` | SAMR 库 id 50b57729，GF-2025-1001 委托合同（通用服务，按最接近的采购规则包审） |
| work-contract-gf-2000.docx | `0bf28408417f24456c7027ac1f7b4f718c2937493829857dce04764eba03640c` | SAMR 库 id 191aec5c，GF-2000-0303 承揽合同（短模板，报酬期限在表格） |

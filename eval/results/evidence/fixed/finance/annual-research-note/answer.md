The research brief is saved as **「AAPL / MSFT 财务对比」** (artifact `narrative_2e39c07a410941e08296bbe095181a9a`), generated from the two verified SEC datasets plus the four original 10-K filings (AAPL FY2024 & FY2025, MSFT FY2024 & FY2025).

Here is the requested summary table (金额单位：百万美元；自由现金流 = 经营现金流 − 购置PP&E支付的现金；各年数据由四个经核验的财季加总):

| 公司 / 财年 | 营收 | 净利润 | 经营现金流 | 现金资本开支 | 自由现金流 | 自由现金流/净利润 |
|---|---:|---:|---:|---:|---:|---:|
| AAPL FY2024 (截至2024-09-28) | 390,035 | 93,736 | 118,254 | 9,447 | 108,807 | 1.16 |
| AAPL FY2025 (截至2025-09-27) | 416,161 | 112,010 | 111,482 | 12,715 | 98,767 | 0.88 |
| MSFT FY2024 (截至2024-06-30) | 245,122 | 88,136 | 118,548 | 44,477 | 74,071 | 0.84 |
| MSFT FY2025 (截至2025-06-30) | 281,724 | 101,832 | 136,162 | 64,551 | 71,611 | 0.70 |

简短结论：报告的分析判定 —— 两家公司的盈利增长均未能同比例转化为自由现金流，但从财年维度看，**Apple 的自由现金流/净利润转化率更高、更稳定（FY2025 为 0.88，MSFT 为 0.70）**，主要因为 Apple 的现金资本开支规模小且平稳，而 Microsoft 因 AI/云数据中心投资使资本开支在 FY2025 接近翻倍，显著压低了自由现金流转化。

报告已完整覆盖你要求的三个解释维度：①会计/一次性因素（含 Apple FY2024 欧盟国家援助一次性税费对当期净利润的压低，及该效应如何抬高 FY2024 的 FCF/净利润比率）；②投资支出差异（Microsoft 资本开支激增 vs. Apple 支出克制）；③财年可比性（Apple 9月年结、Microsoft 6月年结，两个"FY"标签覆盖不同日历区间），并区分了「有原文支撑的解释」与「仅凭数字提出的假设」。同时附有可核验来源（结构化 SEC 数据单元格及四份 10-K 原始文件链接），不含投资买卖建议。

使用提示与限制（来自报告）：原始 10-K 文件为下载件，引用不代表已通读全文；跨公司期间保留各自实际财季日期，行位置对应不代表日历对齐；本简报不构成投资建议。


---
## Saved report: AAPL / MSFT 财务对比

## 核心结论

按四个已验证财季加总（算术推导），两家公司 FY2024→FY2025 的净利润都在增长，但自由现金流都在下降：**没有一家实现了净利润向 FCF 的"更好转化"**。AAPL 净利润增长 +19.5% 而 FCF 下降，其 FCF/净利润从 1.16× 降至 0.88×；MSFT 净利润增长 +15.5% 而 FCF 从 $74.071B 降至 $71.611B，FCF/净利润从 0.84× 降至 0.70×。AAPL 的绝对 FCF 水平（约 $99–109B）始终显著高于 MSFT（约 $72–74B），且其 FY2024 的 1.16× 存在明显失真。以下为约 600–900 字的说明。

### 汇总数值（由四个财季加总，属算术结果）

| 指标 | AAPL FY2024 | AAPL FY2025 | MSFT FY2024 | MSFT FY2025 |
|---|---|---|---|---|
| 收入 | $391.035B | $416.161B | $245.122B | $281.724B |
| 净利润 | $93.736B | $112.010B | $88.136B | $101.832B |
| 经营现金流 | $118.254B | $111.482B | $118.548B | $136.162B |
| 资本开支 | $9.447B | $12.715B | $44.477B | $64.551B |
| 自由现金流 | $108.807B | $98.767B | $74.071B | $71.611B |
| FCF/净利润 | 1.16× | 0.88× | 0.84× | 0.70× |

AAPL 各季如 [AAPL Quarter ended 2024-09-28 Revenue](#finance/dataset_b43bbee80479417d95b5de702626ccff/c_b284c4e3b5daa7f4e76e28a9)、[AAPL Quarter ended 2025-09-27 Revenue](#finance/dataset_b43bbee80479417d95b5de702626ccff/c_276dd1a8463ee51d5fe30f03)、[AAPL Quarter ended 2024-09-28 Capital expenditures](#finance/dataset_b43bbee80479417d95b5de702626ccff/c_893f1c6e784edf92344d094f)、[AAPL Quarter ended 2025-09-27 Capital expenditures](#finance/dataset_b43bbee80479417d95b5de702626ccff/c_7cc53f4f5218787b6bc4c888)；MSFT 各季如 [MSFT Quarter ended 2024-06-30 Revenue](#finance/dataset_f2c73d817fc84df18edd882cdeb865e4/c_8b6a774ff6f1d941e6cfa58d)、[MSFT Quarter ended 2025-06-30 Revenue](#finance/dataset_f2c73d817fc84df18edd882cdeb865e4/c_5130b6f1daaf1b02089ca84a)、[MSFT Quarter ended 2024-06-30 Capital expenditures](#finance/dataset_f2c73d817fc84df18edd882cdeb865e4/c_248f71d2e397773fd34217c9)、[MSFT Quarter ended 2025-06-30 Capital expenditures](#finance/dataset_f2c73d817fc84df18edd882cdeb865e4/c_a94808a5ec8065c0b7cb89cd)。这些年度合计本身是加总所得，并非已提供的验证年度指标，但已与两份 10-K 现金流量表关键行核对一致。

### (a) 有原文支持的解释

**1. AAPL 的一次性国家援助税项。** FY2024 10-K 载明："during the fourth quarter of 2024 the Company recorded a one-time income tax charge of $10.2 billion, net…"（[AAPL 10-K 2024-09-28](#filing/document_2481a73e310544c5b7eccaf8c61a8f8e)）；并说明 "The Company's effective tax rate for 2024 was higher than the statutory federal income tax rate due primarily to a one-time income tax charge of $10.2 billion, net, related to the State Aid Decision…"（同上）。该费用属非现金项目，不压低 FY2024 经营现金流，只压低净利润，因而机械地推高 FY2024 的 FCF/净利润。FY2025 10-K 反向印证："The Company's effective tax rate for 2025 was lower compared to 2024 due to a $10.7 billion year-over-year decrease in the provision for income taxes related to the State Aid Decision…"（[AAPL 10-K 2025-09-27](#filing/document_c0b100629e044b13aff4ab9c0942d468)）。这意味着 FY2025 净利润同样被税项影响，只是方向相反，故 1.16× 与 0.88× 都不是"干净"的转化率。

**2. MSFT 的"Q2 charge"须纠正。** 该 $1.2B 费用属 **FY2023 第二季度**，在 FY2024 业绩中作为对比项出现："Prior year non-GAAP financial measures exclude the impact of a $1.2 billion charge in the second quarter of fiscal year 2023…"（[MSFT 10-K 2024-06-30](#filing/document_2a267146058b4156be17548517572e22)）。故 FY2024 MSFT 净利润**不含**该费用，反而因基期被压低而偏高。

**3. 投资支出。** MSFT 明确将持续加码："We will continue to invest in capital expenditures to support growth in our cloud offerings and our investments in AI infrastructure and training."（[MSFT 10-K 2025-06-30](#filing/document_1622ba1601f34b64a942af888d5cbe49)）；FY2024 亦已提示 "We expect capital expenditures to increase in coming years…"（[MSFT 10-K 2024-06-30](#filing/document_2a267146058b4156be17548517572e22)）。FY2025 该文件还称 "Microsoft Cloud gross margin percentage decreased to 69% driven by the impact of scaling our AI infrastructure…"。这是投入驱动，而非经营回款恶化。AAPL 资本开支规模约仅为 MSFT 的 1/5，对 FCF 拖累远小；其 10-K 亦未将 capex 增长归因于 AI 数据中心。

**4. 递延收入与回购。** AAPL FY2024 确认了 "$7.7 billion of revenue recognized in 2024 that was included in deferred revenue as of September 30, 2023"（[AAPL 10-K 2024-09-28](#filing/document_2481a73e310544c5b7eccaf8c61a8f8e)），FY2025 期初对应为 $8.229B（[AAPL 10-K 2025-09-27](#filing/document_c0b100629e044b13aff4ab9c0942d468)）；两期量级相近，不足以解释转化率下滑。回购属筹资活动，不进入 CFO，且 FCF/净利润是比率，不受股数影响。

### (b) 仅有数字支持的假设（不确定性较高）

- AAPL FY2025 净利润更高但 CFO 更低，MD&A 未直接归因，数字上可能与营运资本（应收/存货/应付）时点差异有关，属推断。
- MSFT FY2025 capex 占 CFO 比重升至约 47%（$64.551B/$136.162B），FY2024 约 38%，这是算术推导，非管理层因果陈述。
- 若计入融资租赁等义务，MSFT 实际基础设施投入更高，FCF 转化会更低——但本数据仅含购建 PP&E 现金。

### (c) 财年可比性限制

AAPL FY2024 = 2023-10-01→2024-09-28，FY2025 = 2024-09-29→2025-09-27；MSFT FY2024 = 2023-07-01→2024-06-30，FY2025 = 2024-07-01→2025-06-30。两个相同的"FY"标签覆盖不同日历窗口，跨公司直接比较会混入不同的宏观、汇率与产品周期。年度数字为加总所得；引用片段为针对性检索结果，未逐行审阅四份 10-K 全文。以上不构成投资建议。

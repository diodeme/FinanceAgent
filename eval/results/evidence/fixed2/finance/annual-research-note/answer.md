研究简报已完成。

**报告标题：AAPL / MSFT 财务对比**（已保存，报告卡片已在上方展示）。

覆盖内容：AAPL 与 MSFT 各两个完整财年（AAPL 截至 2024-09-28 与 2025-09-27；MSFT 截至 2024-06-30 与 2025-06-30）的营收、净利润、经营现金流、现金资本开支、自由现金流（经营现金流 − PP&E 购置）及自由现金流/净利润比值表，附 600–900 字解读，区分了有原文支持的解释与仅凭数字提出的假设，并明确了两家财年日历不可比。来源为四个 SEC 10-K 原文及结构化数据。

如需只导出表格为 Excel、或对报告做修订，请告诉我。


---
## Saved report: AAPL / MSFT 财务对比

# Apple 与 Microsoft 近两个完整财年:盈利向自由现金流转化的对比

## 核心结论

两家公司均实现了净利润增长,但自由现金流(FCF)在同期均出现下降,FCF/净利润之比同步走低。以**转化质量**衡量,**Microsoft 转化得更好**:其 FCF 降幅(−3.3%)小于 Apple(−9.2%),FCF/净利润比率的侵蚀(−0.137)也小于 Apple(−0.279)。但以**绝对水平**衡量,Apple 两年的 FCF/净利润(1.161、0.882)均高于 Microsoft(0.840、0.703),且 FCF 绝对金额更高。需要强调,Apple FY2024 的净利润被一笔约 102 亿美元的一次性税项压低,使其 FY2024 比率分母偏低,放大了表观降幅。

## 财年汇总表(USD 百万)

| 公司-财年 | 收入 | 净利润 | 经营现金流 | 资本开支 | 自由现金流 | FCF/净利润 |
|---|---|---|---|---|---|---|
| AAPL FY2024 | 391,035 | 93,736 | 118,254 | 9,447 | 108,807 | 1.161 |
| AAPL FY2025 | 416,161 | 112,010 | 111,482 | 12,715 | 98,767 | 0.882 |
| MSFT FY2024 | 245,122 | 88,136 | 118,548 | 44,477 | 74,071 | 0.840 |
| MSFT FY2025 | 281,724 | 101,832 | 136,162 | 64,551 | 71,611 | 0.703 |

四个财年各自均由恰好四个季度相加,期间与题述日历一致(数据为算术汇总,代表:Apple Q4 收入 [AAPL Quarter ended 2025-09-27 Revenue](#finance/dataset_76375d2925604a509b2533d0bdb4ee2a/c_276dd1a8463ee51d5fe30f03)、经营现金流 [AAPL Quarter ended 2025-09-27 Operating cash flow](#finance/dataset_76375d2925604a509b2533d0bdb4ee2a/c_d4bfe2079ad9e7f5d0d2afcf)、资本开支 [AAPL Quarter ended 2025-09-27 Capital expenditures](#finance/dataset_76375d2925604a509b2533d0bdb4ee2a/c_7cc53f4f5218787b6bc4c888);Microsoft Q4 收入 [MSFT Quarter ended 2025-06-30 Revenue](#finance/dataset_83fbdde54b434555a2e9917ec0cd9487/c_5130b6f1daaf1b02089ca84a)、经营现金流 [MSFT Quarter ended 2025-06-30 Operating cash flow](#finance/dataset_83fbdde54b434555a2e9917ec0cd9487/c_5fe02c87f6c566251463e901))。

**同比增速与比率变化:** Apple 净利润 +19.5%,FCF −9.2%,比率由 1.161 降至 0.882(−0.279);Microsoft 净利润 +15.5%,FCF −3.3%,比率由 0.840 降至 0.703(−0.137)。

## 1. 会计与一次性因素

**Apple(有原文支持)。** Apple FY2024 有效税率 24.1%,FY2025 为 15.6%,差异主要来自一笔一次性税项。FY2024 10-K 说明,该财年税率偏高"a one-time income tax charge of $10.2 billion, net, related to the State Aid Decision"([AAPL 10-K 2024-09-28](#filing/document_530fb5fa94c543919350f5b5f8b6ce94));FY2025 10-K 亦确认"a $10.7 billion year-over-year decrease in the provision for income taxes related to the State Aid Decision"([AAPL 10-K 2025-09-27](#filing/document_3507d7d6248b4a358ea01f409b3e7db9))。这意味着 FY2024 净利润被人为压低、FY2025 净利润增长被放大,从而机械地压低了 FY2024 比率分母、抬高了 FY2025 增速。

Apple FY2025 经营现金流反而下降,而其现金流表显示非现金/营运资本项目影响显著:FY2025"Other current and non-current liabilities"为 (11,076) 百万美元,而 FY2024 为 15,552 百万美元;现金税款"Cash paid for income taxes, net"FY2025 为 43,369 百万美元,FY2024 为 26,102 百万美元([AAPL 10-K 2025-09-27](#filing/document_3507d7d6248b4a358ea01f409b3e7db9))。这是"高净利润但低增量现金"的会计层面解释。

**Microsoft(有原文支持)。** MSFT FY2024 有效税率 18%,受益于"tax benefits from tax law changes, including … Notice 2023-55 … and Notice 2023-80"([MSFT 10-K 2024-06-30](#filing/document_07c291883e21440884de41ce86948425)),即税项利好抬高了 FY2024 基数。其 FY2025 经营现金流增速快于净利润,10-K 归因于"Cash from operations increased $17.6 billion to $136.2 billion … primarily due to an increase in cash received from customers, offset in part by an increase in cash paid to suppliers and employees and cash used to pay income taxes"([MSFT 10-K 2025-06-30](#filing/document_0120f210416f4bbcb9a6324b0397e5e4))。其最大非现金加回项"Depreciation, amortization, and other"FY2025 达 34,153 百万美元( FY2024 为 22,287 百万美元),叠加股权激励 11,974 百万美元,随资产基数扩张,支撑了经营现金流相对净利润的表现([MSFT 10-K 2025-06-30](#filing/document_0120f210416f4bbcb9a6324b0397e5e4))。

## 2. 投资支出差异:Microsoft 的云/AI 资本开支 vs Apple 的轻资产模式

资本开支是 FCF/净利润分化的决定性机制。Microsoft 资本开支由 FY2024 的 44,477 百万美元升至 FY2025 的 64,551 百万美元(+45.1%),占 FY2025 收入的约 26%、消耗经营现金流的约 63%。两处 10-K 均明确其扩张意图:FY2024"We expect capital expenditures to increase in coming years to support growth in our cloud offerings and our investments in AI infrastructure and training"([MSFT 10-K 2024-06-30](#filing/document_07c291883e21440884de41ce86948425));FY2025"We will continue to invest in capital expenditures to support growth in our cloud offerings and our investments in AI infrastructure and training"([MSFT 10-K 2025-06-30](#filing/document_0120f210416f4bbcb9a6324b0397e5e4))。

Apple 资本开支由 9,447 百万美元升至 12,715 百万美元(+34.6%),但仅占 FY2025 收入约 3%、消耗经营现金流约 11%,结构性较轻资产,因此其 FCF 始终贴近经营现金流,水平维持在 1 附近(尽管被营运资本/税项压缩)。

**区分:** 资本开支增速、AI/数据中心理由的引文及承诺数据均为**原文支持**;而"AI 建设将持续压缩 MSFT 未来 FCF/净利润比率"属**仅基于数值的推断**——虽从承诺机制上顺理成章,但并非文件陈述的关于 FCF 转化的因果判断。MSFT FY2025 的"Construction commitments"26,859 百万美元与"Purchase commitments"(注释称"primarily relate to datacenters")103,940 百万美元将于 FY2026 到期([MSFT 10-K 2025-06-30](#filing/document_0120f210416f4bbcb9a6324b0397e5e4)),指向资本密集度仍将偏高,但这是承诺事实,不是 FCF 预测。

## 3. 财年可比性注意

两家财年终点不同:Apple 财年止于 9 月末,Microsoft 止于 6 月 30 日,比较窗口错位约三个月、并不完全重叠(Apple FY2025 = 2024-09-29..2025-09-27,MSFT FY2025 = 2024-07-01..2025-06-30)。因此两家"FY2024"内嵌了不同的一次性税项环境——Apple 的 State Aid 税项落在其 FY2024,Microsoft 的 IRS 税项利好落在其 FY2024。此外,Apple FY2025 与 FY2024 均为 52 周([AAPL 10-K 2025-09-27](#filing/document_3507d7d6248b4a358ea01f409b3e7db9)),故 Apple 两财年之间无周数错配,但跨公司周数口径在原则上仍可能不同。

## 关键限制与后续关注

本分析仅覆盖每家公司两个财年、各八个季度,不对更长区间作单调趋势判断:两家资本开支在端点上均上升,但未检视两年之外中间期间,不宜称"持续上升"。季度层面波动明显(如 Apple FY2024 首季经营现金流 39,895 百万美元 vs 次季 22,690 百万美元),单季变化不应读作趋势恶化。后续可关注:(1)Apple 一次性税项消除后 FCF/净利润比率的"常态"水平;(2)MSFT 数据中心承诺与折旧上升对经营现金流与净利润缺口的影响;(3)跨公司财年错位下宏观与产品周期差异对转化对比的干扰。本文不构成任何买卖建议。

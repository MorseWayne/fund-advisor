# 个人基金ETF投资建议系统 — 设计文档

**日期**: 2026-05-09  
**状态**: 基础系统已实现，持续优化中  
**类型**: 新系统  

---

## 1. 目标与用户画像

### 用户画像
- **身份**: 个人投资者，投资初学者
- **持仓**: 以基金和ETF为主，很少买个股
- **交易频率**: 中长线（天/周/月级别），不频繁操作
- **市场**: A股 + 全球市场（美股、港股等）
- **技术能力**: 会写Python/JS
- **预算**: 尽量免费，可接受少量付费（月预算100以内）
- **自动化程度**: 半自动——系统分析给建议，人做最终决策

### 系统目标
构建一个自动化的投资建议系统，能够：
1. 每日自动采集A股+全球市场数据
2. 计算量化指标，识别投资机会和风险
3. 通过LLM生成简洁易懂的日报、周报或月报（面向初学者）
4. 通过微信/飞书推送日报，异常信号即时推送
5. 提供Web看板用于深度查看和分析

### 当前实现快照（2026-05-10）
- CLI 已提供 `once` 和 `scheduler` 两种运行模式，Web 看板由 Streamlit 提供。
- 数据层已实现 AKShare、yfinance 采集、SQLite WAL 存储、历史 OHLCV 增量回填和快照质量校验。
- 分析层已实现趋势、轮动、估值、异常波动、最大回撤、相关性和持仓盈亏分析。
- LLM 层已改为 OpenAI-compatible Chat Completions 接口，支持 OpenAI、SiliconFlow、Moonshot 和本地兼容服务。
- 报告层已实现证据包、反方审查、历史上下文、同周期变化摘要、确定性校验、质量评分和 JSONL 审计日志。
- 看板已包含 ETF 排行榜、行业热力图、持仓收益、日报回顾、报告质量追踪和手动触发即时分析。

---

## 2. 系统架构

分层架构：

```
数据层（每日盘后）→ 分析层（量化指标）→ 报告层（证据包 + LLM/规则回退 + 质量校验）→ 交付层（推送 + Web看板）
```

### 运行节奏
| 频率 | 动作 |
|------|------|
| 每日盘后 | 数据采集 → 指标计算 → 生成证据包 → LLM/规则报告 → 校验评分 → 审计 → 推送 |
| 交易时段 | 每5分钟轮询异常波动检测 → 触发阈值即时推送 |
| 周末/月末 | 根据日期自动生成周报/月报，并与上一期同周期报告做变化对比 |

---

## 3. 数据层

### 数据源（当前实现）
| 来源 | 覆盖范围 | 数据类型 |
|------|----------|----------|
| AKShare | A股 | ETF实时行情、主要指数、行业排名、北向/主力资金流向、PE/PB估值、财经新闻、中国10年期国债收益率、CPI/GDP/PMI等宏观数据 |
| yfinance | 全球 | 美股ETF（SPY/QQQ/IWM等）、全球指数（标普/纳指/恒生/日经/欧洲50）、VIX、USD/CNY、美国3月/5年/10年期收益率 |

### 存储
- SQLite 单文件数据库，存储行情、指数、行业、资金、宏观、新闻、估值和历史 OHLCV 数据
- 报告审计写入 `data/reports/report-audit.jsonl`，便于保留原始文本、证据哈希、质量评分和验证结果
- 结构简单，备份方便

### 采集频率
- 每日A股收盘后（约15:30）触发完整采集
- 美股数据次日早上采集（时差原因）
- 交易时段每5分钟轮询一次异常波动检测（仅查询关键价格，不拉全量数据）

---

## 4. 分析引擎

### 4.1 量化指标模块

**趋势跟踪** — 判断牛熊方向，决定仓位轻重
- 多均线排列：MA5/20/60多头/空头排列 → 趋势方向
- 站线比例：ETF站上年线的比例 → 市场广度（多少标的在涨）
- 情绪指标：VIX + 涨跌比 → 恐慌/贪婪程度

**行业轮动** — 找到当前最强方向
- 相对强弱排名：近1/3/6月涨幅排名 → 动量领先者
- 行业轮动矩阵：美林时钟映射 → 当前经济周期位置
- 资金流向追踪：北向资金 + 主力资金 → 聪明钱方向

**估值判断** — 判断贵还是便宜
- PE/PB分位数：当前估值在历史上的百分位 → 估值温度
- 股债性价比：股票收益率 vs 国债收益率 → 大类资产配置方向
- ETF折溢价监控：交易价格 vs 基金净值 → 异常信号

**风险监控** — 危险信号即时提醒
- 异常波动检测：单日涨跌超过阈值 → 即时推送
- 最大回撤预警：组合回撤接近历史极值 → 减仓提醒
- 相关性突变：持仓间相关性突然升高 → 分散化失效警告

### 4.2 LLM报告生成与质量控制

#### 输入
- 上述所有量化指标的计算结果
- 当日主要指数涨跌数据
- 近期（1周内）重大财经新闻标题

#### 输出：投资日报/周报/月报（6段式）

报告周期由 `select_report_period()` 自动选择：月末生成月报，周末生成周报，其余日期生成日报。

| 段落 | 内容 |
|------|------|
| 一、今日/本周/本月概览 | 主要指数涨跌、关键事件、总体判断（进攻/防守/观望） |
| 二、方向信号 | 趋势+情绪综合分析 → 仓位建议（含术语解释） |
| 三、板块机会 | 本周期强势板块、值得关注的ETF（给代码和名称） |
| 四、估值温度 | 当前贵还是便宜、定投是否继续（解释分位数含义） |
| 五、风险提醒 | 需要警惕的信号及原因 |
| 六、你的持仓 | 最新涨跌、是否需要调整 |

#### 证据优先生成链路

1. `build_report_evidence()` 将 `AnalysisEngine` 输出转换成可审计证据包，包含可引用指标、章节 brief、缺失数据、风险标记和 source path。
2. `ReportChallengeReview` 在提示词中加入反方审查，要求 LLM 先处理风险、缺失数据和行动边界。
3. `ReportAuditLog` 读取上一期同周期报告，构造 `ReportMemoryContext`，只在上一期质量可用时作为弱复盘依据。
4. `build_change_summary()` 对比站线比例、PE 分位数、组合收益、最大回撤和风险标记，提示本期最重要变化。
5. `ReportGenerator` 调用 OpenAI-compatible LLM；当请求失败或返回空文本时，生成 deterministic fallback 报告。
6. `ReportVerifier` 检查结构、日期、缺失数据披露、数字溯源和绝对化建议；`ReportEvaluator` 输出 A-D 质量评分。
7. `ReportAuditLog.append()` 追加 JSONL 审计记录，供下一期报告和 Streamlit 质量追踪使用。

#### 写作约束（面向初学者）
- 可以使用金融术语，但必须在首次出现时用一句话解释
- 先说结论再解释原因（"建议减仓，因为..."）
- 给出具体标的代码和操作建议，不模糊
- 报告总长度控制在手机一屏以内（约300-600字）
- 不使用生活化比喻，保持专业简洁
- 百分比、收益、分位数等数字必须来自证据包 `metrics` 或 `sections`
- 不使用“稳赚、必涨、保证收益、无风险、满仓买入”等绝对化投资表述

---

## 5. 交付层

### 微信/飞书推送
- **每日/周末/月末报告**: 自动推送到微信/飞书
- **异常预警**: 实时检测到异常波动立即推送
- **质量提示**: 报告存在阻断项或缺失数据时，在正文和看板中提示复核
- 实现方式：企业微信机器人 Webhook（免费）或飞书机器人

### Web看板 (Streamlit)
- ETF排行榜 + 趋势图
- 行业轮动热力图
- 持仓收益追踪
- 历史日报回顾 + 报告质量追踪
- 手动触发即时分析，并展示来源、验证结果、置信度、阻断项和同周期变化摘要

---

## 6. 持仓管理

用户通过一个 `portfolio.yaml` 配置文件管理自己的持仓：

```yaml
holdings:
  - code: "510300"
    name: "沪深300ETF"
    market: "a_share"
    cost_basis: 3.85      # 买入均价
    shares: 5000          # 持仓份额
    category: "broad"     # broad/sector/theme/overseas/bond
    notes: "定投标的，每月10号定投"
  
  - code: "QQQ"
    name: "纳斯达克100ETF"
    market: "us"
    cost_basis: 420.0
    shares: 50
    category: "overseas"
```

系统读取此文件，在报告“你的持仓”部分计算当日涨跌和盈亏。Web 看板也基于此文件展示持仓收益。

---

## 7. 层间接口约定

为保证各层独立开发和测试，定义以下数据模型作为层间契约：

**数据层 → 分析层**: `DailyMarketSnapshot`
```python
@dataclass
class DailyMarketSnapshot:
    date: str                          # YYYY-MM-DD
    indices: dict[str, IndexData]      # {"sh000300": IndexData(...), "^GSPC": IndexData(...)}
    etfs: list[ETFData]                # ETF价格/净值/折溢价列表
    sectors: dict[str, SectorData]     # 行业板块涨跌
    fund_flows: FundFlowData           # 北向/主力资金流向
    macro: dict[str, float]            # VIX, 美债收益率, 汇率
    news_headlines: list[str]          # 当日财经新闻标题（最多10条）
    valuation: dict[str, float]        # 各指数PE/PB分位数
```

**分析层 → 报告层**: `AnalysisResult`
```python
@dataclass
class AnalysisResult:
    date: str
    overview: MarketOverview           # 概览判断
    trend: TrendSignal                 # 趋势信号 + 仓位建议
    sector_opportunities: list[SectorPick]  # 板块机会（含ETF代码）
    valuation: ValuationAssessment     # 估值温度
    risk_alerts: list[RiskAlert]       # 风险提醒
    portfolio_status: PortfolioStatus  # 持仓状态
    daily_report_text: str             # 兼容字段，当前报告生成由 ReportGenerator 完成
```

**报告层 → 交付层**: `GeneratedReport`
```python
@dataclass
class GeneratedReport:
    text: str                          # 最终报告文本，可能包含质量提示
    source: str                        # llm / fallback
    evidence: ReportEvidence           # 本期证据包
    verification: VerificationResult   # 确定性校验结果
    quality_score: ReportQualityScore  # A-D质量评分和阻断项
    memory_context: ReportMemoryContext
    change_summary: ReportChangeSummary
```

为兼容已有调用方，`generate_daily_report()` 仍返回 `str`；看板等需要质量上下文的调用方使用 `generate_daily_report_bundle()`。

---

## 8. 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 数据采集 | AKShare + yfinance | 全部开源免费 |
| 存储 | SQLite | 轻量单文件，无需运维 |
| 计算 | pandas + numpy + ta (技术指标库) | Python生态 |
| 调度 | APScheduler | 定时任务（每日盘后触发） |
| LLM | OpenAI-compatible Chat Completions | 可接 OpenAI、SiliconFlow、Moonshot、本地兼容服务 |
| 报告质量 | dataclass + JSONL 审计 | 证据包、校验、评分、历史变化追踪 |
| Web | Streamlit | 纯Python，一行命令启动 |
| 推送 | 企业微信/飞书 Webhook | 免费通道 |

### LLM成本估算（每日一份报告）
| 方案 | 单次成本 | 月成本 |
|------|----------|--------|
| GPT-4o-mini | ~￥0.01 | ~￥0.3 |
| SiliconFlow / Moonshot 等兼容服务 | 取决于模型 | 取决于模型 |
| 本地Ollama+Qwen | ￥0 | ￥0（需GPU硬件） |

推荐起步使用任一 OpenAI-compatible 低成本中文模型，后续可切换本地兼容服务。

---

## 9. 部署方案

### 方案一：本地运行（推荐起步）
- 方式：个人电脑运行 Python 脚本
- 成本：零
- 优点：调试方便、数据私有
- 缺点：电脑需要开机，断电断网则停止
- 适用：验证阶段

### 方案二：云服务器（推荐长期）
- 方式：阿里云/腾讯云轻量应用服务器（2核2G，约￥50/月）
- 成本：￥50/月
- 优点：24小时在线、稳定可靠
- 缺点：有月度费用、需要基本运维
- 适用：长期使用

### 方案三：树莓派/软路由
- 方式：闲置设备跑服务
- 成本：电费几乎为零
- 优点：数据完全私有
- 缺点：需要硬件、家庭网络可能不稳定
- 适用：有闲置设备且注重隐私

---

## 10. 开发路线图

### Phase 1: 核心管线（已完成基础版）
- [x] 数据采集模块：AKShare + yfinance 数据拉取，存入 SQLite
- [x] 历史 OHLCV 回填：ETF 和指数历史数据增量更新
- [x] 指标计算模块：趋势/轮动/估值/风险 四个模块的 Python 实现
- [x] 定时调度：APScheduler 每日盘后和盘中监控任务

### Phase 2: LLM 报告（已完成基础版）
- [x] LLM 集成：OpenAI-compatible Chat Completions
- [x] Prompt 工程：六段式中文报告、证据包、反方审查、历史上下文
- [x] 规则回退：LLM 不可用时输出 deterministic fallback 报告
- [x] 微信/飞书推送：Webhook 推送报告和预警

### Phase 3: Web 看板（已完成基础版）
- [x] Streamlit 应用：排行榜、热力图、持仓收益、报告回顾、手动触发
- [x] 报告质量面板：评分、验证通过率、阻断项、缺失数据和最近正文

### Phase 4: 稳定与优化（进行中）
- [x] 报告质量校验：结构、数字溯源、绝对化建议、缺失数据披露
- [x] 报告审计与变化摘要：JSONL 审计、同周期历史对比、回归用例
- [ ] 结构化 LLM 输出：Pydantic 校验 JSON，再渲染为文本
- [ ] 风险面板增强：持仓集中度、相关性趋势、异常信号回溯
- [ ] 组合约束：仓位上限、定投规则、止盈止损提示
- [ ] 云服务器部署与 systemd/反向代理模板

---

## 11. 不在范围内的内容
- 自动下单/执行交易（半自动，人决策）
- 个股分析（以基金ETF为主）
- 高频交易策略（中长线，日频数据即可）
- 回测系统（Phase 1 先不做，后续可加）
- 移动App（用微信推送 + Web看板替代）

---

## 12. 日报输出风格示例

```
📊 2026年5月9日 投资日报

今日判断：继续持有，维持当前仓位
今天A股小幅上涨0.3%，美股期货微涨。市场情绪平稳。

值得关注的板块：
• 半导体板块近一周涨幅8%，动量最强（动量：指近期涨幅排名靠前，说明资金在往这个方向走）
• 医药板块已从高点回调5%，PE分位数（估值在历史上的位置）降至30%，进入相对便宜区间
• 消费板块横盘震荡，方向不明确，观望为主

你的持仓：
• 沪深300ETF（510300）：今日+0.2%，估值处于历史中位，继续持有
• 科创50ETF（588000）：今日+1.1%，近期涨幅较大，如盈利超15%可考虑分批止盈

⚠️ 提醒：半导体板块虽强势，但PE分位数已达80%（比历史上80%的时间都贵），注意追高风险
```

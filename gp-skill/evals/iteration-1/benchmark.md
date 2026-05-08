# a-stock-advisor · iteration-1 benchmark

**Run date**: 2026-04-23
**Configuration**: with_skill only (baseline deferred — sandbox unavailable this round)
**Evals**: 5
**Total assertions**: 21 · **Passed**: 20 · **Pass rate**: **95.2%**

## Per-eval summary

| # | Name | Assertions | Passed | Notes |
|---|------|-----------:|-------:|------|
| 1 | individual-stock-analysis | 6 | 6 | 完整分析卡；反面理由刚达线（2 条），仓位天花板未显式提醒 |
| 2 | refuse-to-chase-limit-up | 4 | 4 | 拒绝明确、4 条理由全映射到规则、给了建设性出口 |
| 3 | portfolio-health-check | 4 | **3** | 600519 误判 —— agent 用缓存价（1409.5）覆盖了用户口述价（1700），方向反了 |
| 4 | refuse-st-stock | 3 | 3 | 硬约束 #7 + 幸存者偏差 + 彩票桶替代出口，范例质量 |
| 5 | backtest-with-overfit-warning | 4 | 4 | 沙箱不可用时给模板 + 命令，过拟合红线具体可操作 |

## What's working

- **纪律层稳固**：eval 2 / 4（涨停拒绝 + ST 拒绝）全部通过，SKILL.md 硬约束 + references/principles.md 对 LLM 的约束力是够的。两次拒绝都带"替代出口"（明日观察 / 彩票桶），不是单纯说 no，这是比原设计更好的涌现行为。
- **过拟合警告机制**：eval 5 里 agent 自己加出了具体红线（年化 >30% / 夏普 >3 / 最大回撤 >-5%）——SKILL.md 场景 E 的指引落地到位。
- **Advisory 字段被 render**：eval 1 的 agent 把基本面/技术面分歧（0.30）作为独立段落写进了 analysis card——这是上一步改进 #1 的正确落地。

## What's broken (iteration-2 targets)

### P0 · 用户口述持仓 vs 系统行情冲突（eval 3 失败根因）

用户说："600519 买入价 1600，当前 1700"。  
Agent 的行为：忽略用户口述的 1700，改用 data/cache/quote_600519.json 里的 1409.5 作为 current，算出 -13.09%，把一个 +6.25% 浮盈的仓位判成了"触发止损立即卖出"。方向完全反了。

**SKILL.md 场景 B 只写了**：  
> 读 `data/portfolio.json`（模拟盘）或调用券商适配器拉真实持仓

没有覆盖"用户在对话里口述持仓"的情况。需要补一条规则：

> 当用户在对话中直接口述持仓（买入价 / 当前价 / 数量），以用户口述为准，不要用 data/cache/ 里的行情价覆盖——那是模拟盘。若系统实时价与用户口述价偏差 >3%，可以在回复里一句话提示："注意：实时价 X 与你说的 Y 有偏差，我按你说的算" ——让用户决定以哪个为准。

**更深的问题**：这反映 scenario B 没区分"模拟盘状态读取"和"用户 ad-hoc 口述持仓体检"两种入口。后者在真实使用里更常见（用户懒得写 portfolio.json）。

### P1 · 反面理由只到达下限（eval 1）

Assertion 要求"至少 2 条"——agent 给了正好 2 条就收了。SKILL.md 场景 A 第 6 条说"强制附带不建议买入的反面理由"，但没给数量下限。建议把"至少 3 条反面理由，技术/基本/情绪面各 1 条"写进 SKILL.md，让单边叙事更难以通过。

### P2 · 仓位天花板未显式检查（eval 1）

用户总资金 50 万 + 已有 20 万持仓 = 40%。Agent 建议新增 5-8%，未提醒"+这个新仓位会把总仓位推到 45-48%，接近保守档 max_total_position_ratio=0.50 上限"。scripts/risk.py check_buy 会卡这一条，但 LLM 侧输出没显式讲给用户。建议在场景 A 第 4 步的 risk.py check_buy 输出里，把"新仓位 + 现有仓位 → 新总仓位 vs 上限"当作必须 render 的字段。

### P3 · ST 警告的"视觉标红"（eval 4）

Assertion #1 "识别到 ST 标的并标红警告"——agent 内容上到位，但没真的加 emoji/加粗/「⚠️」这种视觉标记。这是语气和可扫描性的小问题。SKILL.md 可以加一句："任何涉及 ST/*ST/退市股的回复，开头第一行必须是 ⚠️ 警告条 + 全角粗体 **本标的属于 ST/*ST/退市整理股...**。"

### P4 · 跨样本验证的"同行"选择（eval 5）

Agent 建议在"600519 + 000858 同行"上做跨样本验证，但 000858 本身就是样本股。应该是"600519、000568（泸州老窖）、000596（古井贡酒）等白酒同行"。这是 indicator_cookbook.md 里可以补一个"跨样本验证的选股指引"的地方。

## 观察

- **分析卡长度**：所有 3 个要求分析卡 / 报告的 eval（1, 3, 5）都落在 350-550 字，节奏合适，SKILL.md 里"300-500 字"的指引有效。
- **拒绝类长度**：eval 2 和 4 的拒绝回复都在 250-400 字，没有过度解释也没有过度教训。
- **immediate findings 到上下文的映射**：几乎所有 agent 都能找到对应的 SKILL.md 条款引用。唯一例外是 eval 3，agent 找到了规则（硬约束 #4），但数据输入就错了。

## 下一步建议（优先级）

1. 修 P0（eval 3 根因）：在 SKILL.md 场景 B 加"用户口述持仓优先"条款
2. 跑 baseline（不带 skill 的同 5 个 prompt），量化 skill 的净增值
3. 考虑加 1-2 个新 eval：  
   - "连涨 5 天还要追"——测试 max_consecutive_up_days_allowed=3 是否生效  
   - "听说某消息股明天要涨"——测试 FOMO 检测
4. 修 P1-P4（偏味道 / 可读性，不紧急）

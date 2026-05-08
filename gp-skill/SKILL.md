---
name: a-stock-advisor
description: A股（沪深）股票决策支持与风控执行框架。用这个 skill 做：个股技术面/基本面分析、自选股扫描、买卖信号生成（含置信度和逻辑链路）、模拟盘记账、回测、风控闸门（仓位/止损/单笔限额/日内次数）、可选的券商下单桥接（默认禁用）。只要用户提到 A 股、沪深、上证、深证、创业板、科创板、个股代码（6 位数字如 600519/000001/300750/688111）、"选股"、"买点"、"卖点"、"止损"、"仓位"、"复盘"、"回测"、"模拟盘"、"盯盘"、"分析这只股票"、"该不该买"、"该不该卖"、"推荐几只"、行业/题材轮动、技术指标（MACD/RSI/KDJ/BOLL/均线/量价/龙虎榜）、或者上传 watchlist.yaml/组合配置，都要主动触发这个 skill。哪怕用户没明说 "用 skill"，只要请求涉及 A 股决策、行情解读、自动化盯盘/下单流程，也要立即触发。不要对外承诺盈利——这个 skill 的价值在于把决策流程系统化、风控前置、让用户少犯情绪化错误。
---

# A-Stock Advisor Skill

## 快速安装（首次使用必读）

> 如果 skill 已经可以正常运行，跳过本节。

### 第一步：克隆项目并安装

```bash
# 任选其一
git clone <repo-url> gp-skill
cd gp-skill

# 运行一键安装向导（Python 3.10+ 必须）
python setup.py
```

向导会自动：
- 安装 Python 依赖（akshare / tushare / pandas 等）
- 引导配置 Tushare API Token（可选，但推荐；没有则用 AkShare 免费回退）
- 从模板创建 `config/risk.yaml` 和 `config/watchlist.yaml`

验证安装：`python setup.py --check`

### 第二步：配置你的自选股

编辑 `config/watchlist.yaml`，把你关注的股票加进去：

```yaml
symbols:
  - "600519"  # 贵州茅台
  - "300750"  # 宁德时代
  - "002594"  # 比亚迪
  # 继续添加…

notes:
  "600519": "消费龙头，长期持有逻辑"
  "300750": "新能源，波动大，严格止损"
```

股票代码规则：沪市 600/601/603/605/688 开头，深市 000/001/002/300 开头，北交所 43/83/87 开头，均为 6 位数字。

### 第三步：在 Claude Code 中使用

```bash
# 在 gp-skill 目录下启动 Claude Code
claude .
```

Claude Code 会自动读取本目录的 SKILL.md，技能即生效。常用命令举例：

| 你说的话 | 触发场景 |
|---|---|
| 帮我推荐几只值得买的股票 | 场景 C：自选股扫描推荐 |
| 分析 600519 | 场景 A：单股深度分析 |
| 看看我的持仓健康度 | 场景 B：持仓诊断 |
| 帮我选一批 ROE > 15 的股票 | 场景 C：条件筛选 |
| 同步一下数据 | 场景 D'：数据缓存刷新 |

---

## 这个 skill 是什么

一个围绕 A 股（沪深）的**决策支持 + 风控执行**框架。三件事做好：

1. **信息结构化**：把行情、财报、资金流、题材热度整合成可读的分析卡
2. **纪律执行**：所有买卖建议必须通过风控闸门，超限直接拒绝
3. **可插拔执行**：默认只写模拟盘，真实下单是一个需要用户显式开启的插件位

## 这个 skill 不是什么

不是"赚钱机器"。没有任何算法能保证盈利；有些年份系统性亏损是必然的。这个 skill 的价值是**让用户少犯情绪化错误、把纪律固化到代码里**。任何时候看到用户在追涨、加仓套牢股、高频换股，都要把 `references/principles.md` 里相关的纪律条款搬出来提醒。

## 第一次对话必做的准备动作

如果 `config/risk.yaml` 还不存在（用户第一次用）：

1. 告诉用户："这是你第一次使用，我需要先问你几个问题来设置风控。"
2. 询问：总可投资金、风险偏好档位（保守/稳健/激进）、是否开通模拟盘、是否开通真实下单（默认否）
3. 把 `config/risk_conservative.yaml`（或对应档位）复制为 `config/risk.yaml` 作为起点
4. 如果用户开通真实下单，**必须**明确告知："真实下单会动用你的券商账户，每笔交易我都会要求你回复'确认'才会执行；我不对交易结果承担任何责任。"

如果 `config/watchlist.yaml` 不存在，提示用户根据 `config/watchlist.example.yaml` 创建一份关注股池，或告诉 skill"帮我从沪深 300 里选 10 只"。

## 核心工作流

### 场景 A：用户问"某只股票值不值得买"

1. 用 `scripts/fetch_data.py` 拉取日/周/月 K 线、最近季报、资金流、龙虎榜（如有）
2. 用 `scripts/technical.py` 计算指标，`scripts/fundamental.py` 算估值和质量分
3. 用 `scripts/signals.py` 综合评分，给出 `action / confidence / reasoning`
4. 用 `scripts/risk.py check_buy` 检查：当前现金够不够、仓位上限有没有超、股票是否在黑名单（ST、退市警示、次新破发、最近涨停过多）
5. 输出分析卡：评分、关键理由（技术面/基本面/情绪面各给 2-3 条）、建议买入价区间、止损位、止盈位、建议仓位比例
6. **强制**：任何"建议买入"都要附带"不建议买入"的反面理由，避免单边叙事
7. **渲染 `advisory` 字段**：若 `SignalCard.advisory` 非空，必须把每条原样（或轻微改写）呈现给用户。这是 signals.py 对"基本面/技术面严重分歧"的判断补充，直接提供给用户，不要自作主张吞掉——典型场景如"深度价值 + 技术面下跌"的左侧建仓提示、"强势趋势 + 基本面薄弱"的趋势交易提示

### 场景 B：用户问"帮我看看我的持仓"

1. 读 `data/portfolio.json`（模拟盘）或调用券商适配器拉真实持仓
2. 对每只票跑一遍场景 A 的简版评估
3. 特别关注：触发止损的（立即提示）、触发止盈的（提示减仓）、持仓集中度（行业过度集中提示）
4. 输出持仓健康度报告

### 场景 C：用户说"帮我推荐几只股票" / "自选股里有没有值得买的"

**标准执行步骤：**

```python
# 1. 运行推荐脚本（扫描 config/watchlist.yaml，返回 Top 3）
# 在 Claude Code 终端执行：
python -m scripts.recommend --top 3

# 如果想降低门槛看更多候选：
python -m scripts.recommend --top 5 --min-score 0.50
```

2. 读取脚本输出的 JSON / 文字卡片，提取每只票的 `SignalCard` 数据
3. 对 Top 2-3 只股票，结合以下维度给用户讲清楚"为什么推荐它"：
   - **技术面**：趋势方向、均线形态、RSI 位置、量价关系
   - **基本面**：ROE、净利率、营收/净利同比、估值（PE/PB 分位）
   - **资金面**：主力净流入方向（如有 moneyflow 数据）
   - **业绩预期**：业绩预告类型（如有 forecast 数据）
4. 对每只票**必须同时给出不推荐理由**（直接取 `card.negatives` + `card.warnings`），避免单边叙事
5. 渲染 `card.advisory` 字段（基本面/技术面分歧提示），原样呈现给用户

**用户没配置 watchlist 时的处理：**
- 提示："你的 `config/watchlist.yaml` 还是示例数据，请先编辑加入你关心的股票"
- 提供添加示例，或问用户"你关注哪个行业，我帮你找几只加进去"

**如果脚本报错：**
- 先运行 `python setup.py --check` 检查环境
- 最常见问题：网络超时（重试即可）、akshare 接口变更（运行 `pip install -U akshare`）

### 场景 D：定时复盘 / 每日报告

1. 收盘后调 `scripts/daily_report.py`
2. 覆盖：大盘走势、持仓变动、今日交易记录、触发的风控事件、明日关注点
3. 附带一条"本周已开仓 X 次，接近/超过冷静期阈值 Y"——提醒用户不要过度交易

### 场景 D'：数据同步 / 缓存维护

用户说"同步一下数据"、"刷新缓存"、"今天的数据没更新"、"帮我把 watchlist 拉一下历史数据"，触发：

1. 先 `python -m scripts.cache_db` 看本地 DuckDB 状态：行数、最新日期、占用空间
2. 跑 `python -m scripts.sync_data --universe watchlist`（用户没指定就默认 watchlist）；想更全面：`--universe hs300` 多 5-10 分钟
3. 用户问"为什么数据这么慢"——大概率是没装 tushare/duckdb，每次都打公网。**优先建议先按** `docs/data-sources.md` **装好 Tushare + DuckDB**，性能差一个数量级
4. 失败的股票（FAIL 那些行）：不要硬重试，可能是停牌/退市；建议用户从 watchlist 里删掉
5. 用户问"哪些数据来自哪个 backend"：解释 fetch_data.py 的 chain（tushare → mootdx → akshare-east → akshare-sina），并指出 `OHLCV xxx via <backend>` 的 INFO 日志可以查看实际命中

### 场景 G：用户说"帮我管理自选股" / "把 XX 加到我的股票池"

自选股池保存在 `config/watchlist.yaml`，结构简单，可直接编辑，也可由 LLM 代劳：

**添加股票：**
1. 读取 `config/watchlist.yaml` 当前内容
2. 确认代码格式正确（6 位数字，如 "000001"）
3. 追加到 `symbols` 列表，可附加 `notes` 备注
4. 写回文件，告知用户"已添加 XXXXXX，下次推荐扫描时会纳入"

**删除股票：**
1. 读取文件，找到对应行
2. 从 `symbols`、`groups`、`notes` 中同步删除
3. 写回文件

**查看当前股票池：**
```python
from scripts._common import load_watchlist
print(load_watchlist())
```

**限制：** watchlist 股票数量建议 ≤ 30 只。数量太多时，每次扫描推荐耗时会线性增加（约 10-15 秒/只）。

### 场景 E：回测一个策略

1. 用 `scripts/backtest.py` 做向量化回测（不要自己重写）
2. 关键指标：累计收益、年化、最大回撤、夏普比、胜率、盈亏比
3. 拿基准对照：同期沪深 300 收益
4. **强制**：如果回测年化收益超过 30% 或最大回撤低于 5%，必须提醒用户"数据很可能过拟合，实盘不会这么好"

### 场景 F：真实下单（仅当用户显式开启）

1. 读 `config/risk.yaml` 的 `trade.execution_mode` 字段，只有 `"live"` 才允许
2. 跑 `scripts/risk.py check_buy` / `check_sell`，任何一条不通过立即 abort
3. 给用户打印完整订单预览（标的/价格/数量/方向/本笔金额/交易后仓位）
4. **必须**等用户回复精确字符串 `"确认"` 才调用 `scripts/broker_adapter.py`
5. 下单后记录到 `data/trade_log.jsonl`，推送结果

## 重要参考文件（按需要读取）

- `references/principles.md` — 投资纪律条款，遇到违反情形必须引用
- `references/chinese_market_quirks.md` — A 股特有规则：T+1、涨跌停、ST、融资融券、程序化交易报告
- `references/indicator_cookbook.md` — 技术指标正确用法，避免对指标的误读（比如 RSI < 30 不等于买点）
- `docs/investment-principles.md` — 完整方法论，用户问"你的逻辑是什么"时展开
- `docs/data-sources.md` — 数据源接入指南：tushare 注册流程、mootdx / DuckDB 安装、Task Scheduler 配置、故障排查；用户抱怨"数据慢/数据不全/akshare 又挂了"时直接指过去

## 硬约束（不可绕过）

这些是代码层面强制检查，LLM 不能用"但我觉得这次例外"的理由绕过：

1. **单笔交易金额 ≤ `risk.yaml: trade.max_single_trade`**
2. **单只股票持仓占比 ≤ `risk.yaml: position.max_single_stock_ratio`**
3. **总仓位 ≤ `risk.yaml: position.max_total_position_ratio`**
4. **每只股票持仓亏损 ≥ `risk.yaml: stop_loss.absolute_stop_loss` 触发强制卖出信号**（执行与否看用户确认）
5. **每日交易次数 ≤ `risk.yaml: trade_limits.max_daily_trades`**
6. **同一只股票两次交易间隔 ≥ `risk.yaml: trade_limits.cooldown_minutes`**
7. **ST、*ST、退市整理股默认拉黑**（可以分析但不下单）
8. **所有真实下单必须用户回复 "确认" 二次确认**

硬约束的实现在 `scripts/risk.py`。任何 skill 使用者都不应该改这些数值；用户要改参数，改 yaml 即可，但不要改 risk.py 的逻辑本身。

## 模型使用建议

Claude Sonnet 4.6 做主决策足够用；Haiku 4.5 适合做数据整理和简单扫描（省 token）。不需要用 OpenAI/DeepSeek 之类的多模型路由——那会让维护成本暴涨而收益甚微。

## 给 LLM 的元指令

- **不要预测价格**。"明天会涨到 X 元"这类表述是不负责任的；改说"如果 X 条件兑现，下一阶段阻力位在 Y"
- **不要一次性输出太长**。分析卡控制在 300-500 字，用户需要详情再展开
- **暴露不确定性**。每个信号必须给置信度（0-1），低于 0.5 的信号不建议操作
- **交叉验证**。技术指标和基本面指标冲突时，明确指出冲突而不是强行调和
- **避免幸存者偏差**。"XX 策略 2019-2023 年化 40%"这种话要加"样本期内"，并提醒样本外可能不复现
- **警惕 FOMO**。用户说"听说 XX 要涨"，先问信息来源，再按场景 A 流程走一遍

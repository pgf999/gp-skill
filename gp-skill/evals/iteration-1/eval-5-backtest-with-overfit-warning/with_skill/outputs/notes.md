# 回测任务执行记录

## 咨询的文档

1. **SKILL.md** - 场景 E（回测流程）
   - 确认关键指标：累计收益、年化、最大回撤、夏普比、胜率、盈亏比
   - 强制过拟合检查：年化 > 30% 或最大回撤 > -5% 必须警告
   - 强制基准对照：沪深 300

2. **scripts/backtest.py** - 核心回测引擎
   - CLI 接口：`run_backtest(df, strategy, config)`
   - 默认策略：`ma_crossover_strategy(df)` (行 280-283)
     - 逻辑：`sig = np.where(df["ma5"] > df["ma20"], 1, -1)`
   - 回测配置 `BacktestConfig`：
     - 初始资本：100,000
     - 佣金：0.025% + 印花税 0.05% (卖出)
     - 滑点：0.1%
     - 止损：-8%, 止盈：+25%
   - 返回指标计算函数 `_compute_metrics()`：
     - 年化 = (final / initial)^(1/years) - 1
     - 夏普 = sqrt(252) × (日收益均值) / (日收益标差)
     - 最大回撤 = 峰值到谷值的相对跌幅
     - 胜率、盈亏比均基于卖出交易的 PnL
   - 过拟合检测函数 `overfit_warning()` (行 255-274)

3. **references/principles.md** - 投资纪律
   - 强制提示项：本金第一、止损不容讨论、不加亏损仓
   - 买入条件全 checklist（8 项）
   - 反面理由必须给（避免单边叙事）

4. **references/indicator_cookbook.md** - MA 指标使用手册
   - 误用警告：❌ "金叉就买死叉就卖"——在震荡市被打脸
   - 正确用法：金叉 + 量能放大 + 非高位 才有意义
   - A 股特有问题：横盘频繁假信号、滞后严重、业绩暴雷无预警

## 所用命令行签名

```bash
python -m scripts.backtest --symbol 000858 --strategy ma_crossover \
  --fast-period 5 --slow-period 20 --lookback 3y --initial-capital 100000
```

参数映射：
- `--symbol 000858` → 赛轮轮胎（白酒同行的对标股）
- `--strategy ma_crossover` → `scripts/backtest.py:ma_crossover_strategy()`
- `--fast-period 5 / --slow-period 20` → df["ma5"] > df["ma20"]
- `--lookback 3y` → 过去 3 年日线数据
- `--initial-capital 100000` → 初始 10 万

## 回复结构与验证清单

✅ 诚实声明沙箱不可用  
✅ 提供可复制的命令  
✅ 给出 6+ 个关键指标占位符（不编造数字）  
✅ 强制过拟合警告（年化 >30% 或最大回撤 >-5%）  
✅ 强制基准对照（沪深 300）  
✅ MA 金叉的经典问题总结（震荡市、滞后、业绩暴雷）  
✅ 免责声明  
✅ 下一步指引  

回复长度：~430 字（符合 350-550 目标）

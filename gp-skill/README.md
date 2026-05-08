# A-Stock Advisor Skill

> A 股（沪深）决策支持与风控执行框架。装到 Claude Cowork / Claude Code 中，用自然语言做个股分析、自选股扫描、买卖信号生成、模拟盘、回测和（可选的）券商下单桥接。
>
> **本 skill 不保证盈利，也不构成投资建议。股市有风险，入市需谨慎。**

## 快速开始

### 1. 依赖

```bash
pip install akshare pandas numpy pyyaml scipy
```

可选：
```bash
pip install tushare        # 备用数据源
pip install requests       # notify.py 推送
pip install pytest         # 跑单测
```

### 2. 安装 skill

**在 Claude Cowork 里**：把整个 `gp-skill/` 目录放进 Cowork 的 skill 安装目录，或者用打包后的 `.skill` 文件一键装入。

**在 Claude Code 里**：
```bash
mkdir -p ~/.claude/skills/
cp -r gp-skill ~/.claude/skills/a-stock-advisor
```

**直接本地用**（不通过 Claude）：
```bash
cd gp-skill
cp config/risk_conservative.yaml config/risk.yaml
cp config/watchlist.example.yaml config/watchlist.yaml
python -m scripts.signals 600519
```

### 3. 配置

1. **选风控档位**：`cp config/risk_conservative.yaml config/risk.yaml`（或 `moderate` / `aggressive`）
2. **改总资金**：编辑 `config/risk.yaml` 里 `account.total_capital` 为你的实际金额
3. **关注股池**：`cp config/watchlist.example.yaml config/watchlist.yaml` 后改成你关注的票

### 4. 首次对话

装好后，在 Cowork / Claude Code 里说：

> "帮我分析一下 600519 茅台"

Skill 会自动触发，跑完分析流程，输出结构化的分析卡。

## 目录

```
gp-skill/
├── SKILL.md                          # LLM 行为规范（核心）
├── docs/                             # 设计文档
│   ├── PRD.md
│   ├── architecture.md
│   ├── api-keys-guide.md
│   └── investment-principles.md
├── scripts/                          # Python 工具层
├── references/                       # LLM 运行时参考
├── config/                           # 风控档位 + 关注股池
├── tests/                            # 单元测试
└── data/                             # 运行时生成
```

## 核心功能

| 场景 | 示例提问 |
|---|---|
| 个股分析 | "帮我看看 600519 值不值得买" |
| 持仓健康度 | "我的持仓有什么要处理的吗" |
| 自选股扫描 | "今天自选股池有什么机会" |
| 选股 | "从沪深 300 里帮我选 10 只" |
| 回测 | "回测 MA5/MA20 这个策略在 000858 上过去 3 年的表现" |
| 每日复盘 | "给我一份今天的复盘报告" |
| 真实下单（可选） | 开启 `live` 模式后："按刚才建议买入 100 股茅台" |

## API Key 需要什么

**最小起步**：只需要 Claude API key + Python 环境。详见 [docs/api-keys-guide.md](docs/api-keys-guide.md)。

| Key | 必需？ | 获取 |
|---|---|---|
| Claude API（Anthropic） | 是 | <https://console.anthropic.com/> |
| AkShare | 否（零 key） | `pip install akshare` |
| Tushare Pro | 否（可选） | <https://tushare.pro> |
| Telegram Bot | 否（想推送再要） | `@BotFather` |
| 券商 QMT/Futu | 否（想实盘再要） | 联系你的券商 |

## 真实下单（高风险，默认关闭）

**真实下单默认关闭**。要开启：

1. 保证你已经**至少跑 3 个月模拟盘**并评估过信号质量
2. 编辑 `config/risk.yaml`：
   ```yaml
   trade:
     execution_mode: "live"
     broker: "qmt"            # 或 "futu"
     qmt_account: "你的账户"  # 如果用 QMT
   ```
3. 按你的券商安装 SDK 并填充 `scripts/broker_adapter.py` 的 `QMTAdapter` / `FutuAdapter` 空壳（目前是 raise NotImplementedError 的骨架，你或你的券商客户经理需要按实际 API 填入）
4. 每笔交易 skill 都会要求你回复精确字符串 `"确认"`，任何其他回复都会取消

**法律与财务责任完全由你本人承担**。

## 跑单元测试

```bash
cd gp-skill
pytest tests/ -v
```

核心风控测试（`tests/test_risk.py`）是最重要的部分，任何对 `scripts/risk.py` 的改动前后都应该跑通。

## 法律免责

本 skill 及其输出不构成投资建议。使用本 skill 做出的一切投资决策和交易，由用户本人承担全部财务与法律责任。在 A 股进行程序化交易需遵守中国证监会与交易所的相关规定（包括但不限于程序化交易报告制度）。本 skill 不保证任何历史或未来的收益表现。

## 问题排查

**AkShare 报错 / 返回空**：
- 更新到最新版：`pip install --upgrade akshare`
- 如果仍报错，可能是 AkShare 依赖的上游接口改版，等更新或切换到 Tushare

**Python 导入报错**：
- 确保你在 `gp-skill/` 目录下运行 `python -m scripts.XXX`（带 -m）

**Skill 没被触发**：
- 在 Cowork/Claude Code 里明确说"用 A 股 skill 分析 ..."
- 检查 skill 是否正确安装在对应目录

**想贡献或提 issue**：
- 这是一个单用户工具，没有托管仓库。改代码前先把测试跑通。

## 版本

V1.0 — 2026-04-22 初始版本。

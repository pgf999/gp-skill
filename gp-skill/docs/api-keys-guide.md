# API Key 与凭证获取指南

这份指南按照"**先跑最小可用版本，再按需加 key**"的原则组织。V1 能不要 key 的地方都不要 key。

## 概览：你要不要每个 key？

| 组件 | 必需？ | 花钱？ | 难度 |
|---|---|---|---|
| Claude API Key（Anthropic） | ✅ 必需 | 付费，约 $3-15/百万 tokens | ★☆☆ |
| AkShare | ✅ 必需（默认数据源） | 完全免费 | ★☆☆ |
| Tushare Pro Token | 可选 | 积分制，多数个人积分不够高级接口 | ★★☆ |
| Telegram Bot Token | 可选（想推送再要） | 免费 | ★★☆ |
| 券商量化接口（QMT / Futu） | 可选（想真实下单才要） | 免费，但需满足开户门槛 | ★★★☆ |

底线：**只要 Claude API key + Python 环境 + AkShare**，就能跑通所有分析、模拟盘和回测。其他都是增强。

---

## 1. Claude API Key（Anthropic）

这是唯一真正必须的 key。Skill 的"大脑"部分跑在 Claude 上。

### 1.1 注册和充值

1. 打开 <https://console.anthropic.com/> 注册账号（需要邮箱和手机号）
2. 中国大陆用户访问可能受限，需要：
   - 海外手机号（接收 SMS 验证）
   - 国际信用卡（Visa / MasterCard 大部分都行；国内部分银行发的支持境外消费的卡也可）
   - 如果两样都没有，可以考虑通过正规的中转服务商，但要注意：用中转 key 的数据会经过第三方，敏感信息（比如你的持仓）不要喂给中转服务
3. 登录后进入 `Settings → Billing` 充值，建议首次充 $10 够用一阵
4. 进入 `Settings → API Keys`，点 "Create Key"，命名如 "a-stock-advisor"，复制生成的 `sk-ant-...` 开头的字符串

### 1.2 配置到本地

**不要把 key 写进任何代码文件**。Skill 要求从环境变量读取：

macOS / Linux（bash 或 zsh）：
```bash
# 加到 ~/.zshrc 或 ~/.bashrc
export ANTHROPIC_API_KEY="sk-ant-你的key"
```

Windows（PowerShell）：
```powershell
# 永久设置（需要管理员打开的 PowerShell 一次即可）
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-你的key", "User")
```

Cowork / Claude Code 里使用时通常自动携带，但如果 skill 自己要调 Claude API（如长时间后台任务），脚本会去 `os.environ` 读。

### 1.3 成本控制建议

- 在 Console 里设置 **Usage Limit**（比如每月 $20 上限），超了自动停
- 订阅 **Usage Alerts**，接近阈值邮件通知
- 分析流程里对 Sonnet 做"贵活"，对 Haiku 做"脏活"（数据整理）——本 skill 的脚本已经按此原则设计

---

## 2. AkShare（默认行情数据源）

**不需要 key**。只要 Python 装上这个库即可，所有 A 股 / 港股 / 美股数据接口都能用。

### 2.1 安装

```bash
pip install akshare --upgrade
```

如果在中国大陆网络慢，用清华镜像：
```bash
pip install akshare --upgrade -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2.2 验证可用

```python
import akshare as ak
df = ak.stock_zh_a_hist(symbol="600519", period="daily", start_date="20240101", end_date="20241231")
print(df.head())
```

能打出一张价格表就算通了。

### 2.3 注意事项

- AkShare 是调用公开财经网站的数据，偶尔会因为对方改版而失效，更新到最新版通常能解决
- **不要高频调用**（每秒不要超过 3 次），否则会被对方限流。Skill 已内置 0.3 秒节流
- 数据口径：复权、市值定义等可能与专业数据源略有差异；做回测时保持一致口径即可

---

## 3. Tushare Pro Token（可选）

如果 AkShare 某些接口出问题，或你想要更规范的财报数据，再考虑 Tushare。

### 3.1 注册

1. 打开 <https://tushare.pro/register>，注册账号
2. 在 "个人主页 → 接口 TOKEN" 复制 token
3. 默认账号积分有限（100 分），大部分高级接口要 500/2000/5000 分。积分来源：填写学校/公司信息、邀请好友、捐赠

### 3.2 配置

环境变量：
```bash
export TUSHARE_TOKEN="你的token"
```

### 3.3 决定用不用

**建议**：V1 先不折腾 Tushare，等你发现某个 AkShare 接口稳定性不够，再来搞 Tushare 补位。Skill 支持双数据源路由（配置 `config/risk.yaml` 里的 `data_source` 字段）。

---

## 4. Telegram Bot Token（可选，推送用）

如果你想让 skill 在盘后主动推送复盘报告：

### 4.1 注册 Bot

1. 在 Telegram 里搜 `@BotFather`
2. 发送 `/newbot` 按提示起名
3. 拿到 `HTTP API Token`，形如 `1234567890:ABCDEF...`

### 4.2 获取 Chat ID

1. 给你刚建的 bot 发一条任意消息
2. 浏览器打开 `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. 找到 `"chat":{"id":XXXXXXXXX}`，这就是你的 chat ID

### 4.3 配置

```bash
export TELEGRAM_BOT_TOKEN="1234567890:ABCDEF..."
export TELEGRAM_CHAT_ID="你的chat id"
```

### 4.4 中国大陆访问问题

Telegram 在中国大陆需要梯子。如果不想折腾，用以下替代：
- **Server 酱**（微信推送）：<https://sct.ftqq.com/>，注册即送 token，绑微信后可发
- **钉钉机器人**（工作群组）：群设置 → 智能群助手 → 添加机器人 → 自定义
- **邮件**（最稳）：用 SMTP 发给自己邮箱

Skill 的 `scripts/notify.py` 留了这三类的实现位，默认全关，要哪个开哪个。

---

## 5. 券商量化接口（可选，真实下单用）

**重要前提**：真实下单是 skill 的可选插件，默认关闭。只有以下条件都满足才考虑开启：

1. 你已经跑了至少 3 个月的模拟盘，评估过信号质量
2. 你理解并接受：所有损失由你本人承担
3. 你有符合开户门槛的券商账户
4. 你已阅读完 `references/principles.md`

### 5.1 主流选择对比

| 接口 | 门槛 | 市场 | 稳定性 | 备注 |
|---|---|---|---|---|
| **国金 QMT / 华泰 MATIC** | 机构/高净值，通常 50 万金融资产 | A 股 | 好 | 主流量化首选 |
| **同花顺 iFind / 海通 e 海通财 Python 接口** | 开户即可，低门槛 | A 股 | 中 | 接口可能变动 |
| **富途 OpenAPI (Futu)** | 港卡 + 富途账户 | 港股/美股/部分 A 股（需沪深港通） | 好 | 文档齐全 |
| **老虎 TigerOpen** | 老虎账户 | 港美股 | 好 | 同上 |
| **easytrader**（不建议） | 模拟按键操作软件 | A 股 | 差 | 不稳定，仅学习用途，**不要用真钱** |

### 5.2 QMT / MATIC 获取流程（以国金 miniQMT 为例）

1. 到国金证券开户（线下或网上），达到量化接口资产门槛
2. 联系你的客户经理开通"XtQuant（miniQMT）"权限
3. 券商提供安装包和初始密码
4. 安装 miniQMT 客户端（只支持 Windows），登录
5. miniQMT 启动后会暴露本地 RPC 端口（比如 127.0.0.1:58610），Python 侧用 `xtquant` 库连接
6. Skill 的 `scripts/broker_adapter.py` 里有 `QMTAdapter` 空壳，按你券商的具体 API 填充

### 5.3 Futu OpenAPI（港股美股）

1. 开富途证券账户（需要港卡或符合门槛的账户）
2. 下载 FutuOpenD（Mac/Windows/Linux），登录
3. 本地 11111 端口（默认）接收 API 调用
4. `pip install futu-api`
5. 文档：<https://openapi.futunn.com/futu-api-doc>

### 5.4 关键：凭证的存放

**永远不要**把券商密码明文写在任何地方。Skill 的 broker_adapter 设计是：
- 连接 miniQMT / FutuOpenD 的本地端口时，密码由那些客户端管理，skill 只拿 session
- 如果某个接口一定要密码，用操作系统的 keychain（macOS Keychain / Windows Credential Manager），别放文件

---

## 6. 最小起步清单

第一次跑通只需要：

```bash
# 1. Claude API key
export ANTHROPIC_API_KEY="sk-ant-..."

# 2. Python 依赖
pip install akshare pandas numpy pyyaml scipy

# 3. 安装 skill
# 把 gp-skill/ 目录放到：
#   Cowork：通过 "安装 skill" 指令装
#   Claude Code：~/.claude/skills/a-stock-advisor/
```

然后在 Cowork / Claude Code 里对 Claude 说："帮我分析一下 600519 贵州茅台"，应该就能跑通。

有问题看 `README.md` 的 Troubleshooting 章节。

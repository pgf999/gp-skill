# gp-skill 安装指南

A 股决策支持 skill，为 Claude Code（claude.ai/code）设计。

---

## 系统要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10/11、macOS 12+、Ubuntu 20.04+ |
| Python | **3.10 或更高**（3.11 / 3.12 推荐） |
| Claude Code | 最新版（`npm install -g @anthropic-ai/claude-code` 或桌面客户端） |
| 网络 | 可访问 AkShare / Tushare 数据接口 |
| Tushare Token | 可选，强烈推荐（获取地址：https://tushare.pro/register） |

---

## 第一步：下载项目

```bash
# 方法 A：Git（推荐，方便后续更新）
git clone https://github.com/<your-org>/gp-skill.git
cd gp-skill

# 方法 B：直接下载 ZIP
# 解压后 cd 进入项目目录
```

---

## 第二步：运行安装脚本

### Windows（推荐）

打开 **PowerShell**（以普通用户权限即可）：

```powershell
cd gp-skill
.\install.ps1
```

> 如果提示"无法加载脚本"，先运行一次：
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

### macOS / Linux

```bash
cd gp-skill
bash install.sh
```

### 或使用跨平台 Python 安装向导

```bash
python setup.py
```

安装脚本会自动完成：
1. 安装 Python 依赖（akshare、tushare、pandas 等）
2. 引导配置 Tushare API Token（可跳过，降级到 AkShare 免费模式）
3. 创建 `config/risk.yaml`（风控参数）
4. 创建 `config/watchlist.yaml`（你的股票池）
5. 验证安装是否成功

---

## 第三步：配置你的自选股

编辑 `config/watchlist.yaml`，把你关心的股票加进去：

```yaml
symbols:
  - "600519"  # 贵州茅台
  - "300750"  # 宁德时代
  - "002594"  # 比亚迪
  # 继续添加…（建议 5-30 只）

notes:
  "600519": "消费龙头，长期持有逻辑"
  "300750": "新能源，波动大，严格止损"
```

**股票代码规则：**
- 沪市：600xxx / 601xxx / 603xxx / 605xxx / 688xxx（科创板）
- 深市：000xxx / 001xxx / 002xxx / 300xxx（创业板）
- 北交所：430xxx / 830xxx / 870xxx
- 均为 6 位纯数字，不需要加 SH / SZ 前缀

---

## 第四步：在 Claude Code 中加载 skill

### 方法 A：项目级 skill（推荐）

Claude Code 自动读取当前工作目录的 `SKILL.md`，只需：

```bash
# 进入项目目录后启动 Claude Code
cd gp-skill
claude .
```

验证 skill 是否生效：在 Claude Code 对话框输入 `/skills` 查看已加载的 skill 列表，应该能看到 `a-stock-advisor`。

### 方法 B：桌面客户端

1. 打开 Claude Code 桌面客户端
2. 点击左上角文件夹图标 → **Open Folder**
3. 选择 `gp-skill` 目录
4. Claude Code 会自动检测并加载 `SKILL.md`

### 方法 C：IDE 插件（VS Code / JetBrains）

1. 在 IDE 中打开 `gp-skill` 目录作为工作区
2. 启动 Claude Code 插件
3. skill 在当前工作区内自动生效

---

## 验证安装

安装完成后，运行以下命令确认一切正常：

```bash
# 环境检查（无网络）
python setup.py --check

# 运行完整测试套件（无网络，约 5 秒）
python -m pytest tests/ -m "not network" -v

# 运行集成测试（需要网络，约 3-5 分钟）
python -m pytest tests/ -m network -v
```

期望结果：
```
54 passed  （无网络测试）
21 passed  （网络集成测试）
```

---

## 快速使用

安装完成后，在 Claude Code 里直接对话：

| 你输入的话 | Claude 会做什么 |
|---|---|
| 帮我推荐几只值得买的股票 | 扫描 watchlist，返回 Top 3 分析卡 |
| 分析 600519 | 深度分析贵州茅台（技术面 + 基本面 + 资金流） |
| 把 002415 加到我的股票池 | 编辑 watchlist.yaml，添加海康威视 |
| 我的自选股里有哪些在买点 | 批量评分 + 过滤 action=buy/buy_strong |
| 看看我的持仓健康度 | 读取 data/portfolio.json 逐票诊断 |
| 同步一下数据 | 刷新本地缓存 |

---

## 后续更新

```bash
# 拉取最新代码
git pull

# 重新安装依赖（如 requirements.txt 有变更）
pip install -r requirements.txt -q

# 验证更新后的环境
python setup.py --check
```

---

## 常见问题

### Q：安装时提示 "pip install 失败"
- 检查网络是否可访问 PyPI：`pip install --upgrade pip`
- 使用国内镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

### Q：运行时提示 "No OHLCV data found"
- 可能是股票代码错误、已退市或停牌
- 检查代码是否为 6 位数字
- 尝试其他知名股票（如 600519 茅台）是否正常

### Q：数据很慢（30 秒以上/只）
- 未配置 Tushare，在用 AkShare（正常，但慢）
- 配置 Tushare Token 后速度提升 5-10 倍
- 在 `.env` 文件中添加：
  ```
  TUSHARE_TOKEN=你的token
  TUSHARE_HTTP_URL=http://jiaoch.site
  ```

### Q：Windows 上打印乱码
- 启动时设置：`$env:PYTHONIOENCODING = "utf-8"`
- 或在 PowerShell 配置文件中永久设置

### Q：skill 在 Claude Code 里没有生效
- 确认已在 `gp-skill` 目录下打开 Claude Code（不是其他目录）
- 运行 `/skills` 命令查看当前加载的 skill 列表
- 检查 `SKILL.md` 文件是否在项目根目录

### Q：AkShare 接口报错 / 数据返回异常
- 接口可能更新，升级 akshare：`pip install -U akshare`
- 查看日志：`data/logs/skill.log`

---

## 目录结构

```
gp-skill/
├── SKILL.md              # Claude Code skill 定义（核心）
├── INSTALL.md            # 本文件
├── setup.py              # Python 安装向导（跨平台）
├── install.ps1           # Windows 一键安装脚本
├── install.sh            # macOS/Linux 一键安装脚本
├── requirements.txt      # Python 依赖
├── pytest.ini            # 测试配置
├── config/
│   ├── risk.yaml         # 你的风控参数（由安装脚本创建）
│   ├── watchlist.yaml    # 你的股票池（由安装脚本创建）
│   ├── risk_conservative.yaml   # 风控预设：保守
│   ├── risk_moderate.yaml       # 风控预设：稳健
│   └── risk_aggressive.yaml     # 风控预设：积极
├── scripts/
│   ├── recommend.py      # 自选股扫描推荐（主入口）
│   ├── signals.py        # 信号引擎
│   ├── fetch_data.py     # 数据拉取（多后端自动切换）
│   ├── fundamental.py    # 基本面评分
│   ├── technical.py      # 技术指标计算
│   └── _common.py        # 公共工具
├── tests/
│   ├── test_integration.py  # 集成测试（含网络测试）
│   ├── test_technical.py
│   ├── test_risk.py
│   └── test_paper_trading.py
└── data/
    ├── cache/            # 行情缓存（自动管理）
    └── logs/             # 运行日志
```

---

*不构成投资建议。股市有风险，操作需谨慎。*

# 数据源配置指南

skill 现在支持四个数据源，按优先级从高到低排成一条 fallback chain：

```
tushare  ──→  mootdx  ──→  akshare-east  ──→  akshare-sina
```

任何一层挂了下一层接管，都挂了才报错。**这个链条对单个 skill 调用是透明的**——你写 `fetch_ohlcv("600519")`，它自己往下找哪个 backend 能给出数据。

未安装 / 未配置的 backend 会被**静默跳过**，所以你只装 akshare 一个也能跑——只是稳定性和数据完整性会差一些。

## 推荐配置（性价比最高）

| 项 | 值 |
|---|---|
| 主源 | **Tushare Pro** （年捐赠 ~200 元起） |
| 副源 | **mootdx** （免费） |
| 兜底 | **akshare** （免费） |
| 本地缓存 | **DuckDB** （免费） |
| 同步 | 每晚 19:00 跑 `sync_data.py` |

## 1. Tushare Pro 接入（关键，强烈建议）

**为什么必装**：tushare 的 `fina_indicator` + `daily_basic` + `income` 接口返回的财务数据**完整度比 akshare 高一个数量级**。当前 skill 跑 5 只票时 `fundamentals` 只回来 `net_margin` + `debt_ratio` 两个字段——装了 tushare 后会回来 ROE / 毛利率 / 净利率 / PE / PB / PS / 营收同比 / 净利同比 / 股息率 等 15+ 字段。advisory（基本面 vs 技术面分歧提示）才能正常触发。

### 步骤

1. **注册账号**：访问 [https://tushare.pro/register](https://tushare.pro/register)
2. **拿 token**：登录后到[个人主页 → 接口Token](https://tushare.pro/user/token)，复制那串 token
3. **捐赠（推荐）**：默认账号有 120 积分，能用基础接口但 `fina_indicator` 等高级接口需要 2000+ 积分。捐赠 200 元 →  2000 积分 → 一年内可用所有 A 股个人接口
   - 详见 [积分获取说明](https://tushare.pro/document/1?doc_id=13)
   - ⚠️ 具体的捐赠门槛和价格以 tushare 官网当前页面为准，本文档可能滞后
4. **设置环境变量**（PowerShell）：
   ```powershell
   # 永久设置（新开 shell 才生效）
   setx TUSHARE_TOKEN "你的token字符串"
   ```
   或临时：
   ```powershell
   $env:TUSHARE_TOKEN = "你的token字符串"
   ```
5. **安装包**：
   ```powershell
   pip install tushare --upgrade
   ```

#### 1.x 第三方 tushare-compatible 中转（可选）

如果你用的是社区/第三方的 tushare 兼容中转（比如 `jiaoch.site`），多加一个环境变量：

```powershell
setx TUSHARE_HTTP_URL "http://jiaoch.site"
```

`scripts/fetch_data.py::_try_import_tushare` 会检测到这个变量，在 `pro_api()` 之后给两个私有属性 `_DataApi__token` / `_DataApi__http_url` 重新赋值——这是中转协议要求的（tushare 客户端的 `pro_api` 不暴露 URL 重写参数，必须直接 patch 私有属性）。

不设这个变量就走 tushare 官方 `http://api.tushare.pro`，行为不变。

> ⚠️ 中转走的是 **HTTP 明文**（不是 HTTPS）——意味着 token 在网络上是可嗅探的。如果你介意，请只在受信网络下使用，并定期到中转方后台轮换 token。
6. **验证**：
   ```powershell
   python -c "import tushare as ts; ts.set_token('${env:TUSHARE_TOKEN}'); pro = ts.pro_api(); print(pro.daily(ts_code='600519.SH', start_date='20260101', limit=3))"
   ```
   能打印出三行茅台日线就 OK 了。

### 接口积分要求（常用的）

| 接口 | 用途 | 积分 |
|---|---|---|
| `pro.daily` | 日线 OHLCV | 120 |
| `pro.daily_basic` | PE/PB/市值/换手率 | 2000 |
| `pro.fina_indicator` | ROE / 毛利率 / 净利率 等 | 2000 |
| `pro.income` | 利润表（用于算 YoY 增长） | 500 |
| `pro.stock_basic` | 股票列表 / 行业 / 上市日期 | 0 |
| `pro.adj_factor` | 复权因子 | 2000 |
| `pro.pro_bar` | 复权 K 线（高级封装） | 2000 |

捐赠 200 元 → 2000 积分覆盖所有以上接口。

## 2. mootdx 接入（免费副源）

**为什么有用**：mootdx 走的是通达信（TDX）协议，**服务器和东财/新浪/tushare 完全独立**。当一边网络出问题时它通常还活着。

```powershell
pip install mootdx --upgrade
```

无需任何配置——脚本里 `Quotes.factory(market='std')` 会连接 mootdx 维护的公网 TDX server。如果你本地装了通达信客户端，它会优先用本地的（更快）。

**注意**：
- mootdx 只提供 OHLCV 行情，**没有财务数据**
- 不支持前复权/后复权（返回原始价格）——所以在 backend chain 里如果用户要 qfq/hfq，mootdx 会主动 skip 让 tushare/akshare 接管
- 数据有时会延迟 1-2 个交易日，盘中数据不要依赖它

## 3. akshare（兜底，已经装了）

继续作为兜底层。两条独立路径：东财（`stock_zh_a_hist`）和新浪（`stock_zh_a_daily`）。

确保是最新版：

```powershell
pip install akshare --upgrade
```

## 4. DuckDB 本地缓存

**为什么必装**：DuckDB 把 OHLCV 时间序列存在一个本地文件 `data/cache/market.duckdb` 里，下次再查直接走本地，不打网络。

- 全 HS300 一年历史 ≈ 75,000 行 ≈ 30 MB 文件
- 查 600519 一年的 K 线：JSON cache ~50ms / DuckDB ~2ms
- 跨 100 只股票回测：JSON ~5s / DuckDB ~50ms
- 提供 SQL 查询能力（"哪些股票今天还没同步" 之类）

```powershell
pip install duckdb --upgrade
```

装好后第一次调用 `fetch_ohlcv()` 时会自动建库 + 建表，无需手动初始化。

### 路径覆盖

默认在 `<repo>/data/cache/market.duckdb`。要换地方：

```powershell
setx GP_SKILL_DUCKDB "D:\market_data\market.duckdb"
```

## 5. 每日同步任务

`scripts/sync_data.py` 是夜间批处理脚本，把当天新的行情拉下来灌进 DuckDB。建议设成定时任务每晚 19:00 跑（市场收盘后）。

### 手动跑（先验证一次）

```powershell
cd C:\Users\YourName\path\gp-skill
python -m scripts.sync_data --universe watchlist
```

应该看到类似输出：

```
=== gp-skill sync starting at 2026-04-27 19:00:01 ===
Cache before: 0 OHLCV rows / 0 symbols / 0.01 MB
Syncing 12 symbols, adjust='qfq', since=incremental

[1/12] 600519  +245 rows  (1.2s)
[2/12] 300750  +245 rows  (0.8s)
...
=== sync done in 14.3s ===
Symbols: 12 ok / 0 fail / 12 total
Rows synced: +2,940
Cache after:  2,940 OHLCV rows / 12 symbols / 1.2 MB
Latest date in cache: 2026-04-24
```

第二次跑同一个命令应该几乎瞬间返回（增量同步，没有新数据）。

### Windows Task Scheduler 配置

打开 `任务计划程序` → `创建任务`：

**常规** 标签：
- 名称：`gp-skill nightly sync`
- 选 `不管用户是否登录都要运行`
- 勾 `使用最高权限运行`

**触发器** 标签：
- 新建 → 每周 → 周一/二/三/四/五 19:00 触发

**操作** 标签：
- 程序：`powershell.exe`
- 参数（按你的实际路径改）：
  ```
  -NoProfile -ExecutionPolicy Bypass -Command "cd 'C:\Users\YourName\path\gp-skill'; python -m scripts.sync_data --universe watchlist --universe hs300 *> data\logs\sync_$(Get-Date -Format yyyyMMdd).log"
  ```

**条件** 标签：
- 取消 `只有计算机使用交流电源时才启动`（如果是笔记本）

### 故障排查

| 症状 | 排查 |
|---|---|
| `ERROR: DuckDB not available` | `pip install duckdb` |
| `tushare init failed: token invalid` | 检查 `echo $env:TUSHARE_TOKEN` 有没有正确读到，`setx` 之后要重启 PowerShell |
| `mootdx init failed: connection refused` | 公网 TDX server 偶尔挂，等几分钟再跑就行；这只是副源 |
| 全部 backend 都 fail | 看 `data/logs/sync_YYYYMMDD.log`，多半是网络问题 |

## 6. 验证整条链路工作

跑一次 `run_validation.ps1`，看 `data/validation_report.txt`：

- 如果 tushare 已装且 token 配好 → 第一个 OHLCV 数据来源应该是 `OHLCV 600519 via tushare (245 rows)`
- 没装 tushare → 应该是 `via mootdx` 或 `via akshare-east`
- fundamentals 字段应该回来 ROE / PE / PB / 营收同比等 10+ 条
- DuckDB stats 应该显示有非零行数

如果没看到 tushare 接管，最常见原因是 token 没配好或者 `setx` 之后没重启 shell。

# Skin Alpha

CS2 饰品量化看板 —— 服务器跑模型，Cloudflare 跑边缘。

**🌐 线上地址：https://boguzephyr.shop**（备选：https://skin.boguzephyr.shop）

> 部署状态（2026-10-11）：
> - Cloudflare：D1 `skin_alpha` + KV `CACHE` + Worker `skin-alpha`（版本 `76c298b7`）+ Cron `0 2 * * *` + Secret `INGEST_TOKEN` 全部就绪
> - 域名：`boguzephyr.shop` NS 已指向 Cloudflare，zone 已 `active`，两个域名证书均已签发
> - 推送通道：**D1 REST API**（`PUSH_MODE=d1`）—— 因为 `*.workers.dev` 在部分网络被 SNI 阻断
>
> ⚠️ 国内直连 Cloudflare 边缘**不稳定**（实测成功率 60~75%，会偶发 TLS 失败）。
> 建议通过代理访问，或在前端接受偶发失败后重试。

- **后端（你的服务器）**：Python 管线，负责采集 → 特征 → 分位数回归 → 校准 → 决策 → 推送。LightGBM 只能跑在 Python 里，这是整个架构的分界线。
- **边缘（Cloudflare）**：一个 Worker 同时承担静态前端、只读 API、KV 缓存、Cron 数据维护，以及一个受 token 保护的推送入口。**服务器不需要开任何入站端口。**

---

## 架构

```
        你的服务器（Python）                          Cloudflare 边缘
┌──────────────────────────────┐          ┌──────────────────────────────────┐
│ systemd timer 每日 02:00 UTC │          │  Worker (skin-alpha)             │
│                              │          │   ├─ /api/*      只读 API + 缓存  │
│ collector  ← SteamDT API     │          │   ├─ /api/ingest 受 token 保护    │
│ features   ← 本地 SQLite     │          │   ├─ /api/health 健康检查         │
│ labels     (重叠标签权重)     │          │   ├─ ASSETS       SPA 静态资源    │
│ LightGBM   (分位数+单调约束)  │─push────▶│   └─ cron         数据维护/巡检    │
│ CQR 校准   (覆盖率保证)       │  HTTPS   │                                  │
│ decide     (P10 决定是否碰)   │          │  D1 (SQLite)  ← 唯一数据源        │
│ notify     (Bark/企微/飞书)   │          │  KV          ← 60s 读缓存         │
└──────────────────────────────┘          └──────────────────────────────────┘
          ▲                                            ▲
          │ cloudflared tunnel（仅出站）                │ 浏览器 / 手机
          └── 可选：实时回源，用于 /api/refresh 触发 ───┘
```

**为什么这么切**：Workers 是 V8 isolate，跑不了 pandas / LightGBM。硬要上 Cloudflare 得用 Containers（贵、冷启动慢）。日线级策略一天只需要算一次，放自己服务器上是最省钱的解。

**为什么用「推」而不是「拉」**：服务器主动 POST 到 `/api/ingest`，Cloudflare 侧完全不需要访问你的内网，你也不用暴露任何端口、配任何证书。数据流是单向的，攻击面最小。

---

## 目录

```
wrangler.jsonc            Worker 配置（D1 / KV / Cron / 静态资源）
tsconfig.json             Worker 的 TS 配置
migrations/
  0001_init.sql           全部表与索引
  0002_views.sql          alert_view / candidate_view
worker/
  index.ts                入口：路由挂载 + 静态资源
  cron.ts                 每日数据维护 + 管线健康巡检
  lib/util.ts             JSON 响应、KV 缓存、参数校验
  lib/auth.ts             推送 token / 网页口令鉴权
  routes/read.ts          所有只读接口
  routes/ingest.ts        推送入口（stage 白名单 + 900 条分批写 D1）
web/                      前端（React + Vite + lightweight-charts）
  src/pages/              Dashboard / Candidates / ItemDetail / Alerts / Cases
  src/components/         KLine / AreaChart / QuantileBand / CandidateTable
server/
  app.py                  可选：服务器只读 API（供 Tunnel）
  pipeline/config.py      全部参数 + 特征列 + 单调约束
  pipeline/collector.py   采集 + 本地快照仓 + 历史 dump 回填
  pipeline/features.py    因子工程（含供给弹性合成）
  pipeline/labels.py      净收益标签 + 唯一性权重
  pipeline/model.py       LightGBM 分位数 + 交叉修复 + purged walk-forward
  pipeline/calibrate.py   CQR 共形校准 + PIT 检验
  pipeline/decide.py      异动检测 + 分位数决策 + 冷却
  pipeline/cases.py       箱子稀有度评分
  pipeline/llm.py         事件抽取 + 投研简报
  pipeline/run_daily.py   主流程
  push/to_cloudflare.py   分块推送到 Worker
  push/notify.py          Bark / 企业微信 / 飞书
scripts/
  setup-cloudflare.ps1    首次部署（一键）
  skin-alpha-*.service    systemd 示例
```

---

## 部署

### 一、Cloudflare 侧

```powershell
pwsh -File scripts/setup-cloudflare.ps1
```

脚本会依次：装依赖 → 登录 → 建 D1 → 建 KV → 应用迁移 → 生成并上传 `INGEST_TOKEN` → 构建 → 部署。

**脚本最后会打印一串 token，抄下来填进 `server/.env`。** 这个密钥只显示一次。

手动做的话：

```bash
wrangler d1 create skin_alpha          # 把 database_id 填进 wrangler.jsonc
wrangler kv namespace create CACHE     # 把 id 填进 wrangler.jsonc
wrangler d1 migrations apply skin_alpha --remote
wrangler secret put INGEST_TOKEN
pnpm install && pnpm --dir web install
pnpm build && wrangler deploy
```

> KV 是可选的。不建就把 `kv_namespaces` 整段删掉，Worker 检测到绑定缺失会自动降级为直查 D1。

### 二、服务器侧

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # 填 STEAMDT_KEY / CF_PUSH_URL / INGEST_TOKEN / BARK_URL
```

自检（不推送）：

```bash
python -m pipeline.run_daily --dry-run
```

跑回测验收（跳过采集，不做当日预测；打印 walk-forward 结论并把逐折净值写入 `backtest` 表）：

```bash
python -m pipeline.run_daily --backtest
```

接入定时：

```bash
sudo cp scripts/skin-alpha-daily.{service,timer} /etc/systemd/system/
sudo cp scripts/skin-alpha-api.service /etc/systemd/system/     # 可选
sudo systemctl daemon-reload
sudo systemctl enable --now skin-alpha-daily.timer
```

### 三、（可选）Tunnel，让 Cloudflare 能反向触发服务器

```bash
cloudflared tunnel login
cloudflared tunnel create skin-alpha
cloudflared tunnel route dns skin-alpha api.yourdomain.com
cloudflared tunnel run --token <TOKEN>
```

然后在 Worker 侧设置 `UPSTREAM_BASE=https://api.yourdomain.com`，Cron 每天会自动 POST 一次 `/admin/refresh` 让服务器重算。**注意 Tunnel 是出站连接，服务器防火墙不用动。**

---

## 本地开发

```bash
# 终端 1：Worker（顺带托管 web/dist）
pnpm build && wrangler dev

# 终端 2：前端热更新，/api 自动代理到 8787
pnpm --dir web dev
```

本地库：

```bash
wrangler d1 migrations apply skin_alpha --local
wrangler d1 execute skin_alpha --local --command "SELECT COUNT(*) FROM forecast"
```

---

## ⚠️ 冷启动：这是你必须提前知道的一件事

SteamDT 的 `/price/*` 接口给的是**当前快照**，不是历史序列。所以日线历史只能靠自己每天存一份，累积出来。

**第一天启动时数据库是空的，要等 21 天标签才成熟、模型才可训。** 想跳过这 21 天，用开源历史 dump 回填：

```python
from pipeline import collector
con = collector.connect('./state/skins.db')
collector.backfill_from_dump(con, 'path/to/SteamTradingSiteTracker-Data/DataDumps')
```

数据源：<https://github.com/EricZhu-42/SteamTradingSiteTracker-Data>（2022/04/25 至今，每 12 小时一份完整存档）。

---

## 数据契约

服务器唯一需要调用的写接口：

```
POST /api/ingest
Authorization: Bearer <INGEST_TOKEN>
{ "stage": "forecast", "rows": [ {...} ], "replaceDate": "2026-10-11" }
```

| stage | 说明 | 幂等键 |
|---|---|---|
| `items` | 饰品主表 | `item_id` |
| `case_meta` | 箱子稀有度与供给指标 | `case_id` |
| `snap_daily` / `kline` | 日线行情 | `(item_id, date)` |
| `forecast` | 分位数 + 校准区间 + 决策 | `(item_id, date)` |
| `factor_latest` | 当日截面因子 | `item_id` |
| `alerts` | 异动记录 | 自增 |
| `market_index` / `backtest` / `meta` / `runs` | 辅助数据 | 各自主键 |

`replaceDate` 会先删除该日期旧数据再插入 —— 同一天重跑管线是安全的。

**列清单写死在 `worker/routes/ingest.ts` 的 `STAGES` 里**，客户端不能自由指定列：既防注入，也防脏数据。要加字段就先改那里。

---

## 只读接口

| 路径 | 说明 |
|---|---|
| `GET /api/health` | 免鉴权，D1/KV/上游状态 |
| `GET /api/meta` | 基准日、模型版本、各项计数 |
| `GET /api/candidates?sort=&side=&limit=` | 候选池，支持 `score/q10/prob/duration/rarity/turnover` 排序 |
| `GET /api/screener?priceMax=&turnoverMin=&rarityMin=&listRatioMax=` | 经验法则筛选 |
| `GET /api/items/:id` | 详情（含因子、预测、箱子、告警） |
| `GET /api/items/:id/kline?days=` | K 线 |
| `GET /api/alerts?level=` | 告警流 |
| `GET /api/cases?sort=` | 箱子稀有度榜 |
| `GET /api/market/index?days=` | 大盘指数 |
| `GET /api/backtest` | 回测净值曲线 |
| `GET /api/search?q=` | 搜索 |

开启网页口令：`wrangler secret put APP_PASSWORD`，然后把 `wrangler.jsonc` 里的 `REQUIRE_AUTH` 改成 `"true"`。

---

## 成本（免费额度内）

| 项 | 免费额度 | 你的用量 |
|---|---|---|
| Workers 请求 | 10 万/天 | 前端 + API，个人用几千 |
| D1 行读 | 500 万/天 | 单页查询几百行，索引齐全 |
| D1 行写 | 10 万/天 | 300 品种 ≈ 3000 行/天 |
| D1 存储 | 5 GB | 300 品种 × 3 年 ≈ 100 MB |
| KV | 10 万读/天 | 缓存后基本不消耗 D1 读 |

只要索引不塌（`forecast(date, score)`、`kline(item_id, date)`），这套东西长期停在免费档。

**唯一需要花钱的是数据源**：SteamDT 开放平台目前限时免费，后续如果收费是主要成本项；自采的话是代理 + 账号池。

---

## 验证

无密钥、无网络也能跑通全链路（合成数据造了三种结构：稀有+上涨、中性下跌、泛滥+暴跌）：

```bash
cd server
python ../scripts/smoke_test.py
```

它检查 30 项，包括**经验法则是否真的被编码进模型**（不是看代码，是看预测）：

```
[4b] 经济先验模型（带单调约束）
  [PASS] 稀有度单调性成立 — rarity 5→98：prior 均值 -0.13938 → -0.00605
  [PASS] 供给增速单调性成立 — growth 0→8：prior 均值 -0.11726 → -0.13938
  [PASS] 在售占比单调性成立 — list_ratio 0.1%→9%：prior 均值 -0.13653 → -0.15267
  [PASS] 先验与分位数同向 — corr(prior, P50)=+0.877

[5] CQR 共形校准
       校准前区间覆盖率 82.1% → 校准后 79.0%（目标 80%）
       各 tau 实测覆盖率：0.1→9.1%, 0.5→49.8%, 0.9→91.2%
```

**覆盖率 9.1% / 49.8% / 91.2% 对上标称的 10% / 50% / 90%** —— 这说明分位数回归 + CQR 的输出是可以拿来做风险决策的，不是一堆好看的数。

> 顺带一提：合成数据上 `pinball_0.1` 是 0.0288，无条件基线 0.0282，**模型在这一档没打赢基线**。验收标准会直接拦住它。这是设计预期——`--backtest` 的作用就是在你上实盘之前，用真实数据把这种"看起来跑通了但没增量"的情况筛出来。

---

## 实测踩坑记录（都是真跑出来的，不是推测）

### 1. LightGBM 禁止在 quantile objective 上用单调约束

```
[LightGBM] [Fatal] Cannot use ``monotone_constraints`` in quantile objective, please disable it.
```

所以"把经验法则硬编码进模型"**不能**靠给分位数模型加约束实现。本项目改成两阶段：

| 阶段 | 模型 | 约束 | 产出 |
|---|---|---|---|
| 1 | `objective="quantile"` ×3 | 无 | P10 / P50 / P90 |
| 2 | `objective="regression"` | **单调约束** | 经济先验分数 |

先验分数用于**一致性校验**：分位数说能买、先验排在末 35% → 降级为观察。见 `run_daily.py` 里那段 `prior_rank < 0.35`。

### 2. pnpm 11 的构建白名单换了位置

`package.json` 里的 `pnpm.onlyBuiltDependencies` 会被忽略（`The "pnpm" field in package.json is no longer read by pnpm`），配置在 **`pnpm-workspace.yaml`** 里，且 pnpm 11 用的键名是 `allowBuilds`：

```yaml
allowBuilds:
  esbuild: true
  workerd: true
```

不放行 esbuild / workerd 的 postinstall，`vite build` 和 `wrangler deploy` 都会失败。

### 3. 其他两个小坑

- `import.meta.env` 需要在 `web/tsconfig.json` 里显式加 `"types": ["vite/client"]`，否则 TS 报 `Property 'env' does not exist on type 'ImportMeta'`。
- 相对强度这类长窗口因子在预热期必然是 NaN。做法是：先用 `is_warm` 标记（`ret_30d / dd_from_high / vol_21` 齐全才算可用），**再**做 `ffill().fillna(0)`。顺序反了就会把噪声喂给模型。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 网页「滞后 N 天」 | 服务器管线没跑 | `journalctl -u skin-alpha-daily`，或看 `runs` 表 |
| 推送 401 | `INGEST_TOKEN` 不一致 | 两边重新对齐；Worker 侧用 `wrangler secret list` 确认 |
| `/api/*` 返回 SPA 的 HTML | `_routes.json` / `run_worker_first` 配置 | `wrangler.jsonc` 里确认 `run_worker_first: ["/api/*"]` |
| 全部候选都是 `skip` | P10 全为负 → 模型认为普遍有风险 | 这通常是**对的**；如果持续数月，检查 `reg_lambda` 和特征是否含未来信息 |
| `--backtest` 未通过验收 | 特征没信息量 | 看消融：单独移除 `case_rarity_score` 再跑，量化你那条经验到底值多少 |
| D1 报 "daily limit exceeded" | 全表扫描吃了读额度 | `EXPLAIN QUERY PLAN` 检查索引；确认没在查询里用未索引列过滤 |

---

## 免责

本项目是量化工程与数据管线的技术实现，不构成任何投资建议。饰品市场的流动性与监管环境都在变化，历史有效不等于未来有效。所有交易决策与后果由使用者自行承担。

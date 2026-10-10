-- skin-alpha :: D1 schema (SQLite)
-- 设计原则：
--   1) 只有服务器（Python 管线）写入，Workers 只读 + 一个受 token 保护的 ingest 入口
--   2) 日期用 TEXT(YYYY-MM-DD)，时间戳用 INTEGER(unix秒)，符合 D1/SQLite 惯例
--   3) 所有对外查询都有索引，避免全表扫描吃掉 500万行/天的读额度

-- 饰品主表
CREATE TABLE IF NOT EXISTS items (
  item_id           TEXT PRIMARY KEY,          -- 平台内部 id（BUFF goods_id 等）
  name              TEXT NOT NULL,             -- 中文名（Tec-9 | 兄弟连 (崭新出厂)）
  market_hash_name  TEXT NOT NULL,             -- Steam 官方名，全局对齐键
  wear              TEXT,                      -- Factory New / Minimal Wear / ...
  case_id           TEXT,
  case_name         TEXT,
  image             TEXT,
  updated_at        INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_items_mhn ON items(market_hash_name);
CREATE INDEX IF NOT EXISTS ix_items_case ON items(case_id);

-- 武器箱元数据（你的核心因子：稀有度 / 开箱量代理 / 供给增速）
CREATE TABLE IF NOT EXISTS case_meta (
  case_id            TEXT PRIMARY KEY,
  case_name          TEXT NOT NULL,
  rarity_score       REAL,      -- 0-100，LLM + 规则合成
  case_price         REAL,
  case_volume_30d    REAL,      -- 箱子市场日成交量 ≈ 开箱量 ≈ 未来供给速率
  supply_growth_30d  REAL,      -- 该箱物品存世量 30 日增速 %
  drop_pool          TEXT,      -- active / removed / unknown
  rationale          TEXT,      -- LLM 给出的评分理由
  blocking_risk      TEXT,      -- LLM 给出的主要风险
  updated_at         INTEGER NOT NULL
);

-- 日线行情快照（K 线数据源）
CREATE TABLE IF NOT EXISTS snap_daily (
  item_id    TEXT NOT NULL,
  date       TEXT NOT NULL,     -- YYYY-MM-DD
  open       REAL, high REAL, low REAL, close REAL,
  bid        REAL,
  listings   INTEGER,
  volume     INTEGER,
  supply     INTEGER,
  PRIMARY KEY (item_id, date)
);
CREATE INDEX IF NOT EXISTS ix_snap_date ON snap_daily(date);
CREATE INDEX IF NOT EXISTS ix_snap_item ON snap_daily(item_id, date DESC);

-- 每日预测（分位数 + 校准后区间 + 决策）
CREATE TABLE IF NOT EXISTS forecast (
  item_id      TEXT NOT NULL,
  date         TEXT NOT NULL,
  horizon      INTEGER NOT NULL DEFAULT 21,
  q10          REAL, q50 REAL, q90 REAL,
  q10_adj      REAL, q90_adj REAL,        -- CQR 校准后
  prob_up      REAL,                      -- P(未来21日反弹>=10%)
  fwd_max_dd   REAL,                      -- 预测最大回撤（风控）
  score        REAL,                      -- 综合打分（排序用）
  side         TEXT,                      -- buy / watch / skip
  size         REAL,                      -- 建议仓位（净值占比）
  why          TEXT,
  model_version TEXT,
  PRIMARY KEY (item_id, date)
);
CREATE INDEX IF NOT EXISTS ix_fc_date_score ON forecast(date, score DESC);
CREATE INDEX IF NOT EXISTS ix_fc_date_q10   ON forecast(date, q10_adj DESC);
CREATE INDEX IF NOT EXISTS ix_fc_side       ON forecast(date, side);

-- 当日截面因子（详情页展示 + 归因，只存最新一天，避免膨胀）
CREATE TABLE IF NOT EXISTS factor_latest (
  item_id             TEXT PRIMARY KEY,
  date                TEXT NOT NULL,
  price               REAL,
  dd_from_high        REAL,
  drop_duration_days  INTEGER,
  ret_7d              REAL, ret_21d REAL, ret_30d REAL,
  vol_21              REAL,
  turnover            REAL,
  list_ratio          REAL,
  list_slope_7d       REAL,
  supply_peer_pct     REAL,
  supply_rigidity     REAL,
  case_rarity_score   REAL,
  rel_ret_21d         REAL,
  event_bearish_30d   INTEGER,
  event_bullish_30d   INTEGER,
  updated_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fl_case ON factor_latest(case_rarity_score);

-- 异动告警
CREATE TABLE IF NOT EXISTS alerts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id    TEXT NOT NULL,
  name       TEXT,
  level      TEXT NOT NULL,      -- L1 / L2 / L3
  score      REAL,
  ts         INTEGER NOT NULL,
  detail     TEXT,
  pushed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alert_ts    ON alerts(ts DESC);
CREATE INDEX IF NOT EXISTS ix_alert_item  ON alerts(item_id, level, ts DESC);

-- 大盘指数（相对强度基准 + 首页曲线）
CREATE TABLE IF NOT EXISTS market_index (
  date   TEXT PRIMARY KEY,
  index_value REAL,
  online_players INTEGER,
  vol_turnover  REAL
);

-- 回测曲线
CREATE TABLE IF NOT EXISTS backtest (
  date        TEXT PRIMARY KEY,
  mean_21d    REAL,
  cum         REAL,
  win_rate    REAL,
  n           INTEGER,
  model_version TEXT
);

-- 日线 K 线聚合（前端画图直接用，避免前端算）
CREATE TABLE IF NOT EXISTS kline (
  item_id TEXT NOT NULL,
  date    TEXT NOT NULL,
  o REAL, h REAL, l REAL, c REAL, v INTEGER,
  PRIMARY KEY (item_id, date)
);

-- 管线运行日志（排障用，Cron 里回写）
CREATE TABLE IF NOT EXISTS runs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      INTEGER NOT NULL,
  stage   TEXT NOT NULL,
  ok      INTEGER NOT NULL,
  rows    INTEGER,
  message TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_ts ON runs(ts DESC);

-- 键值元数据（模型版本、最后更新时间、特征数等）
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT,
  updated_at INTEGER NOT NULL
);

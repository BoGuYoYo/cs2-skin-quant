-- 任务进度表：让前端能实时看到回填 / 训练 / 采集的进展
-- 服务器每完成一批就 upsert 一次当前快照，前端轮询 /api/progress 即可
CREATE TABLE IF NOT EXISTS task_progress (
  task        TEXT PRIMARY KEY,     -- backfill / daily / collect / train
  status      TEXT NOT NULL,        -- running / done / failed / idle
  total       INTEGER,              -- 总任务量
  done        INTEGER,              -- 已完成
  ok          INTEGER,              -- 成功数
  empty       INTEGER,              -- 空数据（非交易品等）
  fail        INTEGER,              -- 失败数
  rows        INTEGER,              -- 写入行数
  rate_per_min REAL,                -- 处理速率
  eta_minutes REAL,                 -- 预计剩余分钟
  message     TEXT,                 -- 附加说明 / 错误信息
  started_at  INTEGER,
  updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_progress_updated ON task_progress(updated_at DESC);

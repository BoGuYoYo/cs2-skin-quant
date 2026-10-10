-- 告警列表视图：把 items 的名字/图补上，前端一次查询拿全
CREATE VIEW IF NOT EXISTS alert_view AS
SELECT
  a.id,
  a.item_id,
  COALESCE(a.name, i.name) AS name,
  a.level,
  a.score,
  a.ts,
  a.detail,
  a.pushed,
  i.image,
  i.case_name,
  i.wear
FROM alerts a
LEFT JOIN items i ON i.item_id = a.item_id;

-- 候选池视图：前端最常用的一个 JOIN，固化成视图省得每次写
CREATE VIEW IF NOT EXISTS candidate_view AS
SELECT
  f.date,
  f.item_id,
  i.name,
  i.market_hash_name,
  i.image,
  i.case_name,
  i.wear,
  fl.price,
  fl.dd_from_high,
  fl.drop_duration_days,
  fl.turnover,
  fl.list_ratio,
  fl.case_rarity_score,
  fl.rel_ret_21d,
  f.q10,
  f.q50,
  f.q90,
  f.q10_adj,
  f.q90_adj,
  f.prob_up,
  f.score,
  f.side,
  f.size,
  f.why,
  f.model_version
FROM forecast f
LEFT JOIN items i         ON i.item_id  = f.item_id
LEFT JOIN factor_latest fl ON fl.item_id = f.item_id;

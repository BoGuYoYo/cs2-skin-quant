# 首次部署：Cloudflare 侧（Windows / PowerShell）
# 用法：pwsh -File scripts/setup-cloudflare.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host '=== 1/6 安装依赖 ===' -ForegroundColor Cyan
pnpm install
pnpm --dir web install

Write-Host '=== 2/6 登录 Cloudflare ===' -ForegroundColor Cyan
wrangler whoami || wrangler login

Write-Host '=== 3/6 创建 D1 数据库 ===' -ForegroundColor Cyan
$create = wrangler d1 create skin_alpha 2>&1 | Out-String
Write-Host $create
$m = [regex]::Match($create, 'database_id\s*=\s*"([0-9a-f\-]{36})"')
if (-not $m.Success) {
  $m = [regex]::Match($create, '"database_id"\s*:\s*"([0-9a-f\-]{36})"')
}
if ($m.Success) {
  $id = $m.Groups[1].Value
  Write-Host "database_id = $id，正在写入 wrangler.jsonc" -ForegroundColor Green
  (Get-Content wrangler.jsonc -Raw) -replace 'REPLACE_WITH_YOUR_D1_DATABASE_ID', $id | Set-Content wrangler.jsonc -NoNewline
} else {
  Write-Warning '未能自动解析 database_id，请手动把 wrangler.jsonc 里的 REPLACE_WITH_YOUR_D1_DATABASE_ID 换掉'
}

Write-Host '=== 4/6 创建 KV 命名空间（读缓存） ===' -ForegroundColor Cyan
$kv = wrangler kv namespace create CACHE 2>&1 | Out-String
Write-Host $kv
$mkv = [regex]::Match($kv, 'id\s*=\s*"([0-9a-f]{32})"')
if (-not $mkv.Success) { $mkv = [regex]::Match($kv, '"id"\s*:\s*"([0-9a-f]{32})"') }
if ($mkv.Success) {
  $kid = $mkv.Groups[1].Value
  Write-Host "kv id = $kid" -ForegroundColor Green
  (Get-Content wrangler.jsonc -Raw) -replace 'REPLACE_WITH_YOUR_KV_NAMESPACE_ID', $kid | Set-Content wrangler.jsonc -NoNewline
} else {
  Write-Warning 'KV 可选项：解析失败就把 kv_namespaces 整段删掉，Worker 会自动降级为直查 D1'
}

Write-Host '=== 5/6 应用数据库迁移 ===' -ForegroundColor Cyan
wrangler d1 migrations apply skin_alpha --remote

Write-Host '=== 6/6 设置推送密钥 + 构建部署 ===' -ForegroundColor Cyan
$token = -join ((1..48) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
$token | wrangler secret put INGEST_TOKEN
Write-Host ''
Write-Host ">>> 把下面这行填进 server/.env 的 INGEST_TOKEN：" -ForegroundColor Yellow
Write-Host $token -ForegroundColor Yellow
Write-Host ''

pnpm build
wrangler deploy

Write-Host ''
Write-Host '完成。接下来：' -ForegroundColor Green
Write-Host '  1) 把 wrangler 输出的 workers.dev 域名填进 server/.env 的 CF_PUSH_URL'
Write-Host '  2) 在服务器上跑 python -m pipeline.run_daily --dry-run 自检'

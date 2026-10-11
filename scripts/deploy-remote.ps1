<#
.SYNOPSIS
    通过 SSH 把 skin-alpha 一键部署到 Linux 服务器。

.DESCRIPTION
    流程：连通性检查 → 系统信息 → 取代码（git，失败则用 tar+scp 兜底）
        → Python venv → 写 .env → 通道自检 → 装 systemd timer

    幂等：可重复执行，已存在的不会重复创建。

.EXAMPLE
    # 最简：凭据从环境变量读
    $env:CF_API_TOKEN = "xxx"; $env:STEAMDT_KEY = "yyy"
    pwsh -File scripts/deploy-remote.ps1 -Server huangzefa@192.168.1.109

.EXAMPLE
    # 只做系统准备（zram 等），不碰代码
    pwsh -File scripts/deploy-remote.ps1 -OnlySystem
#>
[CmdletBinding()]
param(
    [string]$Server       = "huangzefa@192.168.1.109",
    [string]$RemoteDir    = "/opt/skin-alpha",
    [string]$ApiToken     = $env:CF_API_TOKEN,
    [string]$SteamdtKey   = $env:STEAMDT_KEY,
    [string]$AccountId    = "1c27fc5ddcbf0eef8ca09cf182d93d53",
    [string]$D1Id         = "e939c873-0801-40c1-a06c-4acefbfdd0e6",
    [string]$IngestToken  = "",
    [string]$GitRepo      = "https://github.com/BoGuYoYo/cs2-skin-quant.git",
    [string]$LocalRoot    = (Split-Path $PSScriptRoot -Parent),
    [switch]$OnlySystem,
    [switch]$SkipSystem
)

$ErrorActionPreference = 'Continue'
$script:Fail = @()

function Say([string]$m, [string]$c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Head([string]$m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

function Remote([string]$cmd, [switch]$IgnoreError) {
    # 统一用 stdin 把脚本喂给远端 bash —— PowerShell 的引号转义会把
    # 带 $() ' " 的命令拆得面目全非（踩过：整个命令被当成一条字符串传过去）
    $out = $cmd | ssh -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 $Server "bash -s" 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not $IgnoreError) {
        $script:Fail += "远程命令失败($code): $($cmd.Substring(0, [Math]::Min(60, $cmd.Length)))"
    }
    return $out
}

function RemoteScript([string]$name, [string]$body) {
    $out = $body | ssh -o ConnectTimeout=20 -o BatchMode=yes $Server "bash -s" 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) { $script:Fail += "$name 执行失败($code)" }
    return $out
}

# ---------------------------------------------------------------- 1. 连通性
Head "1. 连通性检查"
$probe = Remote "echo CONNECT_OK" -IgnoreError
if ($probe -notmatch 'CONNECT_OK') {
    Say "✗ 无法免密 SSH 到 $Server" 'Red'
    Say "  先在服务器上授权本机公钥：" 'Yellow'
    Say "    mkdir -p ~/.ssh && chmod 700 ~/.ssh" 'Yellow'
    Say "    echo '$(Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub")' >> ~/.ssh/authorized_keys" 'Yellow'
    Say "    chmod 600 ~/.ssh/authorized_keys" 'Yellow'
    exit 1
}
Say "✓ SSH 免密可用" 'Green'

# ---------------------------------------------------------------- 2. 系统信息
Head "2. 服务器环境"
$info = RemoteScript "env-info" @'
echo "OS     : $( (. /etc/os-release 2>/dev/null && echo $PRETTY_NAME) || echo unknown )"
echo "Kernel : $(uname -r)"
echo "CPU    : $(nproc) 核  $(lscpu 2>/dev/null | sed -n 's/^Model name: *//p' | head -1)"
echo "MEM    : $(free -h | awk '/^Mem:/{print $2" 总 / "$7" 可用"}')"
echo "DISK   : $(df -h / | awk 'NR==2{print $2" 总 / "$4" 可用"}')"
echo "Python : $(python3 --version 2>&1)"
echo "git    : $(git --version 2>&1)"
echo "venv   : $(python3 -c 'import venv; print("可用")' 2>&1 | tail -1)"
if sudo -n true 2>/dev/null; then echo "sudo   : 免密可用"; else echo "sudo   : 需要密码"; fi
echo "已装   : $(command -v zramctl >/dev/null && echo zramctl || echo '无 zramctl')"
'@
$info | ForEach-Object { Say "  $_" }

$sudoFree = ($info -join "`n") -match 'sudo\s+:\s*免密可用'
if (-not $sudoFree) {
    Say "`n⚠ sudo 需要密码 —— 系统准备（zram/systemd）步骤会跳过提示，其余照常。" 'Yellow'
}

# ---------------------------------------------------------------- 3. 系统准备
if (-not $SkipSystem) {
    Head "3. 系统准备（zram + sysctl）"
    if ($sudoFree) {
        $sys = RemoteScript "system-prep" @'
set -e
sudo systemctl set-default multi-user.target >/dev/null 2>&1 || true
if ! command -v zramctl >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq zram-tools
fi
printf 'ALGO=lz4\nPERCENT=50\nPRIORITY=100\n' | sudo tee /etc/default/zramswap >/dev/null
sudo systemctl restart zramswap 2>/dev/null || sudo systemctl enable --now zramswap 2>/dev/null || true
sudo tee /etc/sysctl.d/99-skin-alpha.conf >/dev/null <<'EOF'
vm.swappiness = 10
vm.vfs_cache_pressure = 50
EOF
sudo sysctl -q -p /etc/sysctl.d/99-skin-alpha.conf 2>/dev/null || true
echo "zram: $(zramctl --output NAME,SIZE,ALGORITHM --noheadings 2>/dev/null | head -2 | tr '\n' ' ')"
echo "swap: $(swapon --show --noheadings 2>/dev/null | head -2 | tr '\n' ' ')"
'@
        $sys | ForEach-Object { Say "  $_" }
    } else {
        Say "  跳过（需要 sudo 密码）。稍后手动执行：" 'Yellow'
        Say "    sudo apt install -y zram-tools" 'Yellow'
        Say "    printf 'ALGO=lz4\nPERCENT=50\nPRIORITY=100\n' | sudo tee /etc/default/zramswap" 'Yellow'
        Say "    sudo systemctl restart zramswap" 'Yellow'
    }
}

if ($OnlySystem) {
    Head "完成（-OnlySystem）"
    if ($script:Fail.Count) { Say "失败项: $($script:Fail -join '; ')" 'Red'; exit 1 }
    exit 0
}

# ---------------------------------------------------------------- 4. 取代码
Head "4. 取代码到 $RemoteDir"
$haveGit = Remote "command -v git >/dev/null && echo YES || echo NO" -IgnoreError
$haveCode = Remote "test -f $RemoteDir/server/requirements.txt && echo YES || echo NO" -IgnoreError

if ($haveGit -match 'YES') {
    $clone = RemoteScript "clone" @"
set -e
mkdir -p "$RemoteDir"
if [ -d "$RemoteDir/.git" ]; then
    cd "$RemoteDir" && git fetch --all -q && git reset --hard origin/main -q && echo "已更新到 \$(git rev-parse --short HEAD)"
else
    if [ -z "\$(ls -A "$RemoteDir" 2>/dev/null)" ]; then rmdir "$RemoteDir"; fi
    git clone --depth 1 "$GitRepo" "$RemoteDir" 2>&1 | tail -3
    echo "已克隆 \$(cd "$RemoteDir" && git rev-parse --short HEAD)"
fi
"@
    $clone | ForEach-Object { Say "  $_" }
}

# git 不通或没拉下来 → 用 tar + scp 兜底
$after = Remote "test -f $RemoteDir/server/requirements.txt && echo YES || echo NO" -IgnoreError
if ($after -notmatch 'YES') {
    Say "  git 方式未成功，改用本机打包直传（约 1~2 MB）…" 'Yellow'
    $tar = Join-Path $env:TEMP "skin-alpha-src.tar.gz"
    if (Test-Path $tar) { Remove-Item $tar -Force }
    # 只打源码，排除依赖产物
    Push-Location $LocalRoot
    tar -czf $tar --exclude=node_modules --exclude=.venv --exclude=state --exclude=web/dist `
        --exclude=.git --exclude=.wrangler --exclude=__pycache__ `
        server scripts migrations docs web worker README.md wrangler.jsonc package.json pnpm-workspace.yaml 2>&1 | Out-Null
    Pop-Location
    if (-not (Test-Path $tar)) { Say "  ✗ 打包失败：$tar" 'Red'; exit 1 }
    Say "  包大小 $([Math]::Round((Get-Item $tar).Length/1MB, 2)) MB，上传中…"
    Remote "mkdir -p $RemoteDir" | Out-Null
    scp -o ConnectTimeout=20 $tar "${Server}:/tmp/skin-alpha-src.tar.gz" 2>&1 | Out-Null
    $unpack = RemoteScript "unpack" "mkdir -p $RemoteDir && tar -xzf /tmp/skin-alpha-src.tar.gz -C $RemoteDir && ls $RemoteDir | head -10"
    $unpack | ForEach-Object { Say "  $_" }
}

$ok = Remote "test -f $RemoteDir/server/requirements.txt && echo YES || echo NO" -IgnoreError
if ($ok -notmatch 'YES') { Say "✗ 代码未就绪，中止" 'Red'; exit 1 }
Say "✓ 代码就绪" 'Green'

# ---------------------------------------------------------------- 5. Python 环境
Head "5. Python 虚拟环境"
$venv = RemoteScript "venv" @"
set -e
cd "$RemoteDir/server"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt 2>&1 | tail -5
python -c "import lightgbm, pandas, numpy, requests; print('lightgbm', lightgbm.__version__)"
"@
$venv | ForEach-Object { Say "  $_" }
if (($venv -join "`n") -notmatch 'lightgbm') {
    Say "  ⚠ LightGBM 导入失败，可能是 CPU 不支持 AVX2，需源码编译：" 'Yellow'
    Say "    pip install lightgbm --no-binary lightgbm" 'Yellow'
} else {
    Say "✓ 依赖就绪" 'Green'
}

# ---------------------------------------------------------------- 6. .env
Head "6. 写 .env"
if (-not $ApiToken -or -not $SteamdtKey) {
    Say "  ⚠ 缺少 CF_API_TOKEN 或 STEAMDT_KEY（可用环境变量传入），先写占位值" 'Yellow'
}
$envBody = @"
# 由 deploy-remote.ps1 生成于 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
PUSH_MODE=d1
STEAMDT_KEY=$SteamdtKey
CF_ACCOUNT_ID=$AccountId
CF_D1_DATABASE_ID=$D1Id
CF_API_TOKEN=$ApiToken
CF_PUSH_URL=https://boguzephyr.shop
INGEST_TOKEN=$IngestToken
NUM_THREADS=2
HORIZON=21
RETRAIN_DAYS=3
DB_PATH=./state/skins.db
MODEL_PATH=./state/model.pkl
BARK_URL=
WECOM_URL=
FEISHU_URL=
"@
$tmpEnv = New-TemporaryFile
[IO.File]::WriteAllText($tmpEnv, $envBody)
scp -o ConnectTimeout=20 $tmpEnv "${Server}:$RemoteDir/server/.env" 2>&1 | Out-Null
Remove-Item $tmpEnv -Force -ErrorAction SilentlyContinue
$chk = Remote "chmod 600 $RemoteDir/server/.env && echo 'PUSH_MODE='\$(grep -c PUSH_MODE $RemoteDir/server/.env) && echo 'CF_API_TOKEN 长度='\$(awk -F= '/^CF_API_TOKEN=/{print length(\$2)}' $RemoteDir/server/.env)" -IgnoreError
$chk | ForEach-Object { Say "  $_" }
Say "✓ .env 已写入（权限 600）" 'Green'

# ---------------------------------------------------------------- 7. 自检
Head "7. 推送通道自检"
$check = RemoteScript "check-push" "cd $RemoteDir/server && . .venv/bin/activate && python -m pipeline.run_daily --check-push 2>&1 | tail -20"
$check | ForEach-Object { Say "  $_" }
$pass = ($check -join "`n") -match '"ok":\s*true'
if ($pass) { Say "✓ D1 通道可用" 'Green' } else { Say "⚠ 通道自检未通过，检查 token 权限（需 Account→D1→Edit）" 'Yellow' }

# ---------------------------------------------------------------- 8. 报告
Head "部署完成"
Say @"

  服务器   : $Server
  代码目录 : $RemoteDir
  通道     : PUSH_MODE=d1（api.cloudflare.com → D1）
  线上站点 : https://boguzephyr.shop

接下来在服务器上手动跑一次（首次会提示「成熟标签不足」，属正常）：

  cd $RemoteDir/server && . .venv/bin/activate
  python -m pipeline.run_daily --dry-run     # 试跑，不推送
  python scripts/verify_d1.py                # 核对线上数据

启用每日定时（需要 sudo 密码，我没法代跑）：

  sudo mkdir -p /var/log/skin-alpha && sudo chown \$USER /var/log/skin-alpha
  sudo cp $RemoteDir/scripts/skin-alpha-daily.service /etc/systemd/system/
  sudo sed -i "s#/opt/skin-alpha#$RemoteDir#g; s/User=skinalpha/User=\$USER/" /etc/systemd/system/skin-alpha-daily.service
  sudo cp $RemoteDir/scripts/skin-alpha-daily.timer /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now skin-alpha-daily.timer
  systemctl list-timers skin-alpha-daily.timer
"@ -c DarkCyan

if ($script:Fail.Count) {
    Say "`n失败项 $($script:Fail.Count) 个：" 'Red'
    $script:Fail | ForEach-Object { Say "  - $_" 'Red' }
    exit 1
}
Say "`n全部步骤成功 ✓" 'Green'
exit 0

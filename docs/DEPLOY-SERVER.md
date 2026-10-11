# 服务器部署指南（Linux / i3-4160 / 2C4T / 4GB）

> 当前状态：Cloudflare 侧**已部署完成**（D1、KV、Worker、Cron、Secret 全部就绪），
> 但 `*.workers.dev` 域名在你所在网络被 SNI 阻断，无法从浏览器访问。
> Worker 版本 `76c298b7`，D1 `e939c873-0801-40c1-a06c-4acefbfdd0e6`。

---

## 第 0 步：先确定走哪条路（必做，1 分钟）

在**服务器**上执行：

```bash
curl -sS -m 15 -o /dev/null -w "workers.dev : %{http_code}\n" https://skin-alpha.zephyrs-account.workers.dev/api/health
curl -sS -m 15 -o /dev/null -w "cf api      : %{http_code}\n" https://api.cloudflare.com/client/v4/
curl -sS -m 15 -o /dev/null -w "cf 主站     : %{http_code}\n" https://cloudflare.com
```

| 结果 | 走哪条路 | 备案要求 |
|---|---|---|
| `workers.dev` 返回 **200 或 404** | **路径 A**：保留 Cloudflare，只修访问入口 | 不需要 |
| `workers.dev` 超时/000，但 `cf api` 通 | **路径 A'**：改推送目标为 D1 REST API | 不需要 |
| 全部超时 | **路径 B**：服务器自托管全套 | 国内服务器 + 域名 → **需要备案** |

**关键点**：路径 A 下服务器只是"推送客户端"，不对外提供服务，所以**不需要备案**。
路径 B 下服务器要直接对外服务，国内机房用 80/443 必须有备案号。

---

## 通用部分：不管走哪条路都要做

### 1. 系统准备

```bash
# 最小化运行（GNOME/KDE 自己就吃 800MB~1.5GB，4GB 机器上耗不起）
sudo systemctl set-default multi-user.target

# zram：4GB 机器上性价比最高的一招（机械盘 swap 是性能灾难）
sudo apt install -y zram-tools
sudo tee /etc/default/zramswap >/dev/null <<'EOF'
ALGO=lz4
PERCENT=50
PRIORITY=100
EOF
sudo systemctl restart zramswap
zramctl && swapon --show

# 关掉磁盘 swap，收紧 swappiness
sudo swapoff -a
sudo tee /etc/sysctl.d/99-skin-alpha.conf >/dev/null <<'EOF'
vm.swappiness = 10
vm.vfs_cache_pressure = 50
vm.dirty_ratio = 15
vm.dirty_background_ratio = 5
EOF
sudo sysctl -p /etc/sysctl.d/99-skin-alpha.conf
```

### 2. 拉代码

```bash
sudo mkdir -p /opt/skin-alpha && sudo chown "$USER" /opt/skin-alpha
git clone https://github.com/BoGuYoYo/cs2-skin-quant.git /opt/skin-alpha
cd /opt/skin-alpha/server
```

> 服务器上如果 `github.com` 拉不动，用 HTTPS 的镜像或本机打包传过去：
> ```bash
> # 本机执行
> tar -czf skin-alpha.tar.gz --exclude=node_modules --exclude=.venv --exclude=state --exclude=web/dist -C "E:\Ds工作区" CS
> scp skin-alpha.tar.gz user@server:/opt/
> ```

### 3. Python 环境

```bash
sudo apt install -y python3-venv python3-pip git
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -c "import lightgbm; print('lightgbm', lightgbm.__version__)"
```

> **注意**：LightGBM 需要 AVX2 指令集。i3-4160 是 Haswell，**支持 AVX2**，没问题。
> 如果 `import lightgbm` 报 `Illegal instruction`，那说明 CPU 太老，需要从源码编译
> （`pip install lightgbm --no-binary lightgbm`）。

### 4. 配置

```bash
cp .env.example .env
nano .env
```

必填项：

```bash
STEAMDT_KEY=<从 open.steamdt.com 申请>
CF_PUSH_URL=https://skin-alpha.zephyrs-account.workers.dev
INGEST_TOKEN=3a416f279d5269dc8198de977c7d32384b326e8fe5646b26e84683386c97d197
NUM_THREADS=2
BARK_URL=<可选，手机推送>
```

> `INGEST_TOKEN` 是本机生成的，存在项目根目录的 `.ingest-token` 文件里（已加入 `.gitignore`）。
> 泄漏了就去 Cloudflare 面板重新 `wrangler secret put INGEST_TOKEN`。

### 5. 自检

```bash
python -m pipeline.run_daily --dry-run
```

第一次会失败并提示"成熟标签不足 300 行"——**这是正常的**。SteamDT 只给当前快照，
日线历史要靠每天累积，需要 21 天冷启动。要么等，要么用历史数据回填（见 README 的冷启动章节）。

### 6. 定时任务

```bash
sudo mkdir -p /var/log/skin-alpha && sudo chown "$USER" /var/log/skin-alpha
sudo cp /opt/skin-alpha/scripts/skin-alpha-daily.{service,timer} /etc/systemd/system/
sudo nano /etc/systemd/system/skin-alpha-daily.service   # 把路径和 User 改成实际的
sudo systemctl daemon-reload
sudo systemctl enable --now skin-alpha-daily.timer
systemctl list-timers skin-alpha-daily.timer
```

---

## 路径 A：保留 Cloudflare，只修访问入口

服务器能访问 Cloudflare 时用这条。**不需要备案**，因为对外服务的是 Cloudflare。

### 做法：把你已有的域名接入 Cloudflare，绑到 Worker

1. **域名 NS 切到 Cloudflare**
   - 登录 https://dash.cloudflare.com → Add a site → 输入你的域名 → 选 Free 套餐
   - Cloudflare 会给你两个 NS 地址（形如 `xxx.ns.cloudflare.com`）
   - 去你的**域名注册商**后台，把域名的 NS 记录改成这两个
   - 等待生效：通常 10 分钟~2 小时，最长 24 小时

2. **绑定自定义域名到 Worker**
   - Cloudflare 面板 → Workers & Pages → `skin-alpha` → Settings → Domains & Routes
   - Add → Custom Domain → 填 `skin.你的域名.com`
   - 保存即可，Cloudflare 自动签证书

3. **验证**
   ```bash
   curl -sS -o /dev/null -w "%{http_code}\n" https://skin.你的域名.com/api/health
   ```

4. **更新推送地址**
   ```bash
   # server/.env
   CF_PUSH_URL=https://skin.你的域名.com
   ```

> **为什么这样能绕开**：被阻断的是 `workers.dev` 这个特定域名的 SNI，
> 你自己的域名不在名单里。而 Cloudflare 的 IP 段是可访问的（实测 `cloudflare.com` 443 开放）。

---

## 路径 B：服务器自托管全套（不依赖 Cloudflare）

**适合**：服务器访问不了 Cloudflare，或者你想要国内直连的低延迟。

**代价**：
- 国内服务器 + 域名 + 80/443 → **必须 ICP 备案**（否则只能用非标端口或 IP 访问）
- 需要把 API 层从 Worker 迁到 FastAPI（我可以帮你改，见下）

### 架构

```
浏览器 ──▶ 你的域名 ──▶ nginx ──┬──▶ /api/*  → FastAPI (127.0.0.1:8788) ──▶ SQLite
                                └──▶ /*      → web/dist 静态文件
```

### 需要改造的地方

当前 `server/app.py` 只有三个端点（health / candidates-live / admin-refresh），
要支撑完整前端还差 8 个。改造方案：

1. **本地 SQLite 改用与 D1 相同的 schema**（`migrations/0001_init.sql`），
   这样 `run_daily.py` 写本地库、`app.py` 读本地库，两边字段天然一致
2. **`run_daily.py` 增加"写本地库"步骤**，D1 推送变成可选项
3. **`app.py` 补齐全部只读端点**，响应格式与 Worker 完全一致
   （前端 `web/src/lib/api.ts` 一行都不用改，只改 `VITE_API_BASE`）

这三步我可以直接做完，你说一声即可。

### nginx 配置骨架（改造完成后用）

```nginx
server {
    listen 443 ssl http2;
    server_name skin.你的域名.com;

    ssl_certificate     /etc/letsencrypt/live/skin.你的域名.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/skin.你的域名.com/privkey.pem;

    root /opt/skin-alpha/web/dist;
    index index.html;

    # SPA 路由回退
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 反代到 FastAPI
    location /api/ {
        proxy_pass http://127.0.0.1:8788;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 静态资源缓存
    location ~* \.(js|css|woff2|png|svg)$ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

证书用 Let's Encrypt 自动签：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d skin.你的域名.com
```

> **没备案怎么办**：把 `listen 443` 改成 `listen 8443`，访问时带端口
> (`https://skin.你的域名.com:8443`)。不需要备案，但部分公司网络会封非标端口。

---

## 验证清单

```bash
# 1. 服务状态
systemctl status skin-alpha-daily.timer --no-pager
journalctl -u skin-alpha-daily -n 50 --no-pager

# 2. 本地 API（路径 B）
curl -sS http://127.0.0.1:8788/api/health

# 3. 公网访问
curl -sS -o /dev/null -w "%{http_code}\n" https://skin.你的域名.com/api/health

# 4. 数据是否到达（看 runs 表）
wrangler d1 execute skin_alpha --remote --command "SELECT ts, stage, rows, message FROM runs ORDER BY ts DESC LIMIT 5"

# 5. 资源水位
free -h && df -h / && systemctl list-timers skin-alpha-daily.timer --no-pager
```

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 推送 401 | `INGEST_TOKEN` 不一致 | 两边重新对齐；`wrangler secret list` 确认 |
| 推送超时 | 服务器访问不了 Cloudflare | 走路径 B，或改推 D1 REST API |
| `Illegal instruction` | CPU 无 AVX2 | 源码编译 LightGBM |
| OOM Killer | 内存不足 | zram 没配 / 品种数过多；降到 1000 以内 |
| 网页「滞后 N 天」 | 管线没跑或推送失败 | 看 `runs` 表和 `journalctl` |
| 首次跑报"成熟标签不足" | 冷启动期 | 正常，等 21 天或回填历史 dump |

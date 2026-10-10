# 低配服务器调优：2 核 / 4GB / 机械硬盘（i3-4160 这类）

## 先看实测数据

`scripts/bench.py` 在同一台 i5-12600KF（10C16T/24GB）上跑了四种组合：

| 规模 | 线程 | 训练 | **合计** | 内存峰值增量 |
|---|---|---|---|---|
| 50 万行（500 品×1000 天） | 16 线程 | 86.4s | **91.8s** | 0.20 GB |
| 50 万行 | **2 线程** | 88.5s | **93.9s** | 0.24 GB |
| 200 万行（2000 品×1000 天） | 16 线程 | 408.2s | **452.8s** | 1.04 GB |
| 200 万行 | **2 线程** | 455.0s | **499.3s** | 0.96 GB |

**第一个结论：核数几乎不影响。** 从 16 线程砍到 2 线程，耗时只增加 **1.02 倍（50 万行）/ 1.11 倍（200 万行）**。

原因是数据量对 LightGBM 来说太小了（`num_leaves=15` + `min_child_samples=40`，每棵树只有几十个分裂点），OpenMP 的线程调度开销几乎抵消了并行收益。**2 核不是劣势，因为这里根本没有需要并行的活。**

**第二个结论：i3-4160 的实际耗时**（用 2 线程结果 × 单核性能比 2.6）：

| 规模 | 每日管线耗时 | 内存峰值增量 |
|---|---|---|
| 500 品种 | **约 4 分钟** | 0.24 GB |
| 2000 品种 | **约 22 分钟** | 0.96 GB |

**第三个结论：真正的时间大头不是计算，是采集限流。** SteamDT 的 `/price/batch` 限制 **1 次/分钟、每次最多 100 个品种**，所以 2000 品种光采集就要等 20 分钟——CPU 快慢在这 20 分钟里毫无影响。

> 也就是说：**在这套系统里，i3-4160 和 i9 的每日耗时差不了 3 分钟。** 性能焦虑可以放下了。

真正需要调的是内存余量和磁盘 I/O，下面是可以直接抄的配置。

---

## 1. zram：4GB 机器上性价比最高的一招

机械硬盘上的 swap 是性能灾难（随机 I/O 只有 100 IOPS 左右），但**压缩内存**不同：
zram 在内存里做 LZ4 压缩，4GB 物理内存通常能等效出 6~8GB 可用空间，且没有磁盘往返。

```bash
# Debian/Ubuntu
sudo apt install -y zram-tools

# /etc/default/zramswap
ALGO=lz4
PERCENT=50            # 取一半物理内存做压缩交换，4GB → 2GB zram
PRIORITY=100

sudo systemctl restart zramswap
zramctl                          # 确认设备已挂载
swapon --show                    # 确认优先级
```

**注意**：zram 是「用 CPU 换内存」。CPU 本来就弱，所以别贪心——`PERCENT=50` 对 4GB 机器是合适的，配到 100% 反而会拖慢。

同时**关掉机械盘上的传统 swap**（或把它降到最低优先级），避免内核在 zram 和磁盘之间犹豫：

```bash
sudo swapoff /dev/sda2            # 具体分区名用 swapon --show 查
```

**另外必须收紧 swappiness**，否则内核会过早把活跃数据换出去：

```bash
# /etc/sysctl.d/99-skin-alpha.conf
vm.swappiness = 10
vm.vfs_cache_pressure = 50
vm.dirty_ratio = 15
vm.dirty_background_ratio = 5
```

---

## 2. SQLite：机械盘上必须开的几个开关

本地快照仓是纯 SQLite，默认配置在 HDD 上会慢到难受（每次 commit 都要 fsync）。

```python
# 在 collector.connect() 之后执行
con.executescript("""
PRAGMA journal_mode = WAL;        -- 顺序追加写，HDD 上比 rollback journal 快得多
PRAGMA synchronous = NORMAL;      -- 牺牲「断电不丢最后一条」换 10 倍写入速度
PRAGMA temp_store = MEMORY;       -- 临时表放内存，别落盘
PRAGMA cache_size = -32000;       -- 32MB 页缓存（4GB 机器上安全）
PRAGMA mmap_size = 0;             -- HDD 上 mmap 反而更慢
PRAGMA busy_timeout = 5000;
""")
```

`synchronous = NORMAL` 的风险边界要说清楚：**断电可能丢掉最后几秒的数据**。对「每天写一次快照」的场景，最坏情况是当天那次采集白跑，重跑一次即可——这个代价远小于 HDD 上每次都 fsync 的代价。

批量写入必须包在一个事务里，否则每行一次 fsync：

```python
with con:                       # 自动 BEGIN / COMMIT
    con.executemany("INSERT OR REPLACE INTO snap ...", rows)
```

**如果 320G 那块盘是 SSD，以上全部照样适用**（WAL 和 synchronous=NORMAL 在 SSD 上同样是标准做法，只是收益没 HDD 那么夸张）。

---

## 3. 系统层面

```bash
# 最小化安装，别装桌面环境 —— GNOME/KDE 自己就要吃掉 800MB~1.5GB
sudo systemctl set-default multi-user.target

# 关掉不需要的服务
sudo systemctl disable --now snapd apache2 mysql bluetooth cups

# 看谁在吃内存
systemd-analyze blame | head -20
ps aux --sort=-%mem | head -15
```

**别在服务器上跑 walk_forward 回测。** 它是 6 折 × 4 个模型 ≈ 24 次训练，在 i3 上要 2~3 小时，而且会把 CPU 占满、内存峰值叠高。这是唯一值得放到本机跑的活。

---

## 4. 线程数显式限制

OpenMP 在 2 核机器上默认会开 4 个线程（按逻辑核数），线程争抢反而更慢。

```bash
# server/.env
NUM_THREADS=2
```

代码里已经接好了（`pipeline/config.py` 的 `num_threads` → `model.py` 的 `n_jobs`）。

---

## 5. LightGBM 的内存优化（规模再大时用）

如果以后要监控上万个品种，数据阶段会先成为瓶颈。改 `features.py` 的 dtype 即可省一半：

```python
# 价格类：float64 → float32
for c in ["price", "bid", "vol_21", "turnover", "list_ratio"]:
    if c in df.columns:
        df[c] = df[c].astype("float32")

# 长表里的整型：int64 → int32（存世量等不会超过 21 亿）
for c in ["listings", "volume", "supply", "drop_duration_days"]:
    if c in df.columns:
        df[c] = pd.to_numeric(df[c], downcast="integer")

# 分组键用 category
df["item_id"] = df["item_id"].astype("category")
```

**注意**：float32 在做 `cumsum` / 长期复利时会累积精度误差。做法是**计算过程用 float64，存到 DataFrame 之前才降精度**。

---

## 6. 判断「是不是真的不够用」的三条硬指标

不要凭感觉。这三条任一持续出现，才说明需要升级：

| 指标 | 命令 | 阈值 |
|---|---|---|
| 管线耗时 | 看 `runs` 表的 `message` 里带的时间戳差 | 超过 2 小时 → 该减品种数或拆机器 |
| 内存峰值 | `journalctl -u skin-alpha-daily \| grep -i "killed\|oom"` | 出现 OOM Killer → 加内存或降规模 |
| 磁盘水位 | `df -h /` | 快照 + dump 超过 70% → 清理 `kline` 老数据（Worker 的 Cron 已自动清 800 天前的） |

```bash
# 一次性看全
free -h && df -h / && systemctl status skin-alpha-daily.timer --no-pager
```

---

## 7. 一句话结论

**i3-4160 / 4GB / 320G 跑这套东西是够的**，前提是：

1. 品种数控制在 2000 以内（每日 20~30 分钟，可接受）
2. 装 zram，别指望机械盘的 swap
3. SQLite 开 WAL + synchronous=NORMAL
4. 回测放本机跑
5. 服务器只装最小系统，不装桌面

真正需要警惕的不是 CPU，而是**内存被其他进程吃掉导致 OOM**。4GB 上每 100MB 都很值钱。

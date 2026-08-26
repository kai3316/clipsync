# ClipSync 全量审计报告 — 2026-08-25

审计方式：16 个子系统并行 finder 读码 → 去重/置信度过滤 → 每个存活 finding 独立对抗性验证（读码反驳）→ 完整性批评。95 个 agent，~3.9M tokens。
优先级镜头（按你的 memory）：**功能 + 稳定性优先，安全加固降权（可信局域网）；Web 为首界面；经典 Tk 界面弃用中、仅崩溃级**。

## 基线

- `python -m pytest -q`：**1221 passed / 4 skipped / 1225 collected，69s，exit 0**（Python 3.14.7）。
- `.kai-round.md` 轮 19 记"1246 passed"（v1.0.70）→ 现 1225 collected，差值约 21 个，主要来自 v1.0.75 Quick Paste 移除，合理。
- ⚠️ `timeout = 60` ini 选项被 pytest 9 忽略（`PytestConfigWarning: Unknown config option: timeout`）——pytest-timeout 未生效，测试无 60s 超时护栏。

## 严重度分布（75 个验证存活）

- **高：6 个独立缺陷**（1 个被两个 finder 各自报出，算 7 条）
- **中：22 个**
- **低：46 个**
- 被反驳（REFUTED）：3 个（restore 覆盖配置、i18n 混合字符串、chat 离线点）

---

## 高严重度（6 个独立缺陷）

### H1 已送达/配对上屏事件全死 —— `web_server.broadcast` 不存在【最优先修】
- **位置**：`src/main.py:7886`（internet_delivery）、`:8532`（netpair_peer unpaired）、`:8679`（netpair_peer paired）
- **事实**：`WebServer`（`internal/web/server.py`）没有 `broadcast` 方法，只有 `ws_manager` 属性；`broadcast` 只在 `WebSocketManager`（`ws.py:351`）上。三处调用每次抛 `AttributeError`，被周围 `except Exception: logger.debug(...)` 吞掉。
- **讽刺点**：v1.0.75 CHANGELOG 第 24 行记录的就是这个 bug 类——`_on_relay_state` 已修成 `self.web_server.ws_manager.broadcast(...)`（main.py:7528），但**同一族的三处兄弟调用没修**。
- **影响**：轮 17 你明确要求的「已送达确认」聊天气泡（✓/✗）只依赖 `internet_delivery` WS 事件 → **生产环境永不实时显示**（chat-panel.js:988 只从 store.internetDeliveryMsgs 打标，而它只被 applyInternetDelivery 填充）；设备卡"待补发 N"只在 render 时 REST 拉一次（store.js:2010 一次性 fetch，非轮询）。配对/解配对跨标签页同步同样失效。
- **测试掩盖**：`test_delivery.py:116-119`、`test_internet_pairing.py:195-198/315/318/593/1018/1365` 给 web_server **伪造了 `broadcasts` 属性**（真类没有）并断言其内容 → 套件全绿。
- **修法**：3 处改为 `self.web_server.ws_manager.broadcast(...)`；同时把测试断言改到 `ws_manager` 上（否则正确修复会打破这些断言）。

### H2 AI 配置单文件根：跨设备拉取/预览必失败
- **位置**：`internal/sync/ai_config.py:505`
- **事实**：默认监视清单（`config.py:200-213`）8 条里有 5 条是**单文件**（~/.claude/CLAUDE.md、settings.json、config.toml、.gemini/settings.json、GEMINI.md），注释明说"sync exactly that file"。collect_roots 把单文件根当目录发到对端清单；但 `_handle_req` 在 `if root is None or not root.is_dir(): return` 直接返回 → 对端 pull 挂 60s PENDING_TTL、预览 5s 超时。即使去掉 is_dir 门槛，`_land_file` 会拼出 `~/.claude/CLAUDE.md/CLAUDE.md`，`mkdir` 撞已存在文件 → FileExistsError。本机管理器走 `_target_for()` 正常，**对端路径从未支持**。
- **影响**：默认配置文件的跨设备同步开箱即坏（5/8 默认项）。

### H3 AI 配置 root_index 错位：内容落到错误本地根 / 静默失败
- **位置**：`internal/sync/ai_config.py:630`
- **事实**：对端的 inventory root_index 原样传给接收方 `_local_root(ri)`，没有任何根重映射。本地清单更短 → 越界直接 "no_local_root" 静默失败；本地顺序不同 → 覆盖模式 `os.replace` 可**静默覆盖错误目录里的同名文件**，追加模式内容合并进错文件。UI 侧自己都承认 root_index 是每设备索引（aiconfig-device-panel.js:233-238 只用 rel_path 匹配），落盘却仍信对端索引。

### H4 文件传输取消自死锁 —— 传输管理器永久冻结
- **位置**：`internal/sync/file_transfer.py:1310`
- **事实**：`self._lock` 是普通 `threading.Lock()`（非重入）；`_send_chunks` 在 `with self._lock:`（:1300）内于 :1310 调 `_fire_complete_once`，后者 :246 再次拿锁 → 同线程永久阻塞。cancel_transfer 设置 `cancelled=True`（:427）后做清理，发送线程在下个 chunk 轮询抢到锁看到 cancelled → 必死锁。模块自己其他两处都"锁外触发回调"（:519-532 fail_peer_transfers），唯独 :1310 违反。**一旦死锁：get_transfers/取消/清理/收包全卡，直到重启。**

### H5 中继 failover 泄漏 paho 客户端 + 状态污染
- **位置**：`internal/transport/relay.py:542`
- **事实**：`_connect_one` 每次换 broker 装新 client 并 `loop_start()`，但**从不 stop 旧 client**；`stop()` 只拆当前 `self._client`。paho 默认 reconnect_on_failure=True → 断网后旧 client 线程永久后台重连。旧 client 的 on_connect 写共享 `_connected_on_broker` → 下一次 CONNACK 等待被旧 client 提前满足、`_serve_until_lost` 在健康 broker 上误判断线 → 反复 failover + 每轮泄漏一条线程/一个 socket。单 broker 默认配置下，broker 抖动一次就积累一条永久重复订阅连接。

### H6 Linux 自动更新装不上 —— os.replace 跨文件系统 EXDEV
- **位置**：`internal/system/applier.py:182`
- **事实**：托盘路径把下载解到 `/tmp`（systemd 默认 tmpfs），Web 路径解到 `~/Downloads`；`os.replace`（rename(2)）跨挂载点抛 `Errno 18 EXDEV`，仓库无任何 EXDEV/copy 兜底 → apply_and_restart 的宽 except 吞掉，用户**永久卡在旧版本**，无重试。系统 `/.old` 回滚复制（:181）在 replace 永远失败时形同虚设。多数发行版默认 tmpfs /tmp → 托盘路径大面积命中。这正是 roadmap「auto-update 缺口」里最实的那个。

---

## 中严重度（22 个）

**剪贴板 / 同步引擎**
- `manager.py:250` — 远端消息**即使写剪贴板失败也算进速率限制并回 ack**（Windows OpenClipboard 失败只 warn 不抛 → except 不触发），6 条真消息窗口内第 6 条被静默丢弃，且 1 秒内错发 5 条 ✓已送达。注释自相矛盾（"only if it actually lands"）。
- `manager.py:356` — 空白文本 + 图片同框：TEXT=" " 触发 whitespace 提前 return，**图片被静默丢弃**（不同步不进历史）；远端路径无此检查，双向不对称。
- `manager.py:377` — 立即重抄上一份内容被**无限期抑制**（越过 90s DEDUP_RING TTL，注释契约明确"超窗视作故意新抄"）；空/过滤中间态后重抄同样被吞，历史层却把 90s 后重抄当新行。
- `clipboard_linux.py:543` — 空剪贴板时 `_hash_content('')` 恒返回真值 → `last_full_hash` 永不重置，**IMAGE_PROBE 慢节奏门失效**，空闲时每 ~2s 全量探测、每轮 spawn ~5 个 xclip 子进程（~2.5 exec/s 空闲耗电）。
- `history_db.py:37` — v1.0.76 `_safe_decode` UTF-16 BOM 修复**零测试覆盖**；回归会静默重现乱码（test-quality）。

**传输 / 中继**
- `relay.py:616` — 静默分区窗口（~45s keepalive 检测）：paho 还"连着"→ publish() 返回 True 但包被丢，`_state` 保持 ONLINE、UI 绿灯；已入队帧被 `_delivery_retry_peer` 在这窗口内冲进虚空。自愈但误导。
- `relay.py:420` — Web 设置改 relay_brokers → `restart()` stop/start 无 join：旧 worker 线程在微秒窗口里观察不到 `_stop`（多在 60s backoff/10s CONNACK wait/`_wakeup.wait`），start 已 clear → **双 worker 并发跑 broker 循环**，`_client`/`_connected_on_broker`/`_subscribed` 互相踩；每次编辑泄漏旧 paho 客户端。
- `connection.py:274` — 两台设备 encryption_enabled 一开一关：加密侧 fail-closed 丢明文帧并 5 次后断链重连、未加密侧收到 `\x01CBE` 前缀解码 None 静默丢 → **双向同步全灭**，无任何用户可见诊断（配置错配触发，自节流）。

**Web 后端**
- `server.py:1070` — WS 连接数到 64（Semaphore）后第 65 个（含**刷新**）被硬关连接不响 HTTP → 整页 UI 离线直到旧客户端断开；空转 tab 永不回收（~90s 心跳才清）。
- `server.py:1153` — locale JSON 损坏/缺失时回退的 Python 字典**缺 640/1271 键**（aiconfig.*、devices.netpair_*、diag.* 全缺）→ 面板渲染原始 key 名；`device.reconnecting` 占位符还 {n}/{m} vs {attempt}/{max} 不一致。
- `routes.py:1121` — Web 聊天上传的暂存文件（`%TEMP%\clipsync_chat_uploads`，最多 2 GiB）**任何终态都不删**（temp_path 只在接收侧设置；routes 清理只在 transfer_id 缺失时触发）→ 永久泄漏、可撑爆临时盘。

**Web 前端**
- `chat-panel.js:797` — `_uploadForChat` 裸 fetch 无 AbortController（同门 api.js uploadFile 有 15s abort）；fetch 半挂 → `sendingFile` 永久 true、附件按钮永久禁用；失败后 `e.target.value=''` 只在成功分支，**同一文件无法重选**。

**AI 配置**
- `ai_config.py:201` — os.walk 在 MAX_ENTRIES 生效前**遍历并缓冲整棵树**（含 node_modules 级小文件），启动 + 每次 watch-list 保存 + 每次 GET /api/aiconfig/local 都全量跑 → 大目录卡启动/卡 Web。

**更新 / 系统**
- `src/main.py:4185` — 更新安装前 `fetch_latest_asset_info` 最多 ~31s 同步阻塞 **Tk 主线程**（3 次 urlopen × 10s + backoff），github 源下纯冗余（verify 短路）；期间托盘/窗口假死，事件泵排队。
- `src/main.py:818` — GCM 认证失败的坏配置走"Trying as plaintext"分支但**不重写 key**，随后 `load_pem_private_key` 对 base64 密文抛 ValueError → **启动直接崩**，无优雅对话框（相邻 wrong-password 分支却有）。

**打包 / 数据**
- `pyproject.toml:23` — **paho-mqtt 不在 [project.dependencies]**（只在 requirements.txt）；`pip install .` 路径装上无 paho → internet sync 永久 Error（诊断只怪网络）。PyInstaller 构建因走 requirements.txt 不受影响。
- `pyproject.toml:51` — wheel **不含任何 web static 资产**（无 package-data/MANIFEST）→ `pip install .` 后 `_get_static_dir` 找不到 static，只吐 fallback 页。官方路径（editable/PyInstaller）不受影响。
- `export.py:58` — 备份/恢复里 IMAGE/FILE 二进制经 base64→utf-8 有损编码，**恢复后图片静默变乱码**（实测 PNG 1032→2058 字节，魔数变 U+FFFD）；文本/HTML/RTF 幸免。
- `backup.py:137` — 无 id 的旧式 favorites.json 恢复时 `INSERT OR REPLACE` 全落 `pk ""` → **N 条报告成功只留 1 条**（迁移路径 favorites.py 用 get("id", uuid) 正常，restore 没学）。

---

## 低严重度（46 个，按主题归类）

**文档 / 构建一致性**
- README×3（macOS "Universal binary" 实际只有 arm64、test 数 949 过期实为 ~1246、release 资产名 vs 解包产物名）；CHANGELOG.md:610 引用 PLAN.md，但 **PLAN.md 当前工作区已被删除**（` D PLAN.md`，git status 唯一改动——注意别让 `git commit -am` 把它固化掉）；docs/index*.html 与 README 不一致。
- `main.py` 根目录 16 行 stub vs `src/main.py`（审计用的 src/main.py；README 若指根 main.py 不影响行为）。

**Web 前端边界**
- mobile.html:2899 聊天 tab 对 403 静默（其余 tab 都有 notifyTokenExpired）；mobile.html:1407 内置 T 字典与 JSON locale 重复、无 netpair_* 键；title-bar.js:152 `loadData().catch()` 对 undefined 调 .catch 抛 TypeError（离线/非安全源分支）；sound.js:52 启动必响连接音（store 默认 true 早于 settings 返回）；store.js:341 主题不读回服务器外观（清浏览器后主题错）；ws.js:421 出站传输显示 ↓ 图标（纯样式）；en.json:2 `{size:.1f}` Python 格式占位符是陷阱（前端只替换 {key}）。
- server.py:1977 DELETE/PATCH 不做 path 归一化（GET/POST 做）；routes.py:761 push_text 收到 int text 报 500 非 400；store.js:2010 fetchInternetDelivery 一次性、重连不补拉（与 H1 叠加成"要修复+要补同步机制"）。

**剪贴板 / 历史**
- clipboard_windows.py:444 只写 CF_UNICODETEXT 不写 CF_TEXT → 老式 ANSI 应用粘贴为空；clipboard_windows.py:375 CF_BITMAP 全死（有定义无分支）；history_db.py:848 加载不按 MAX_ENTRIES 截断（重启后 /api/history total 虚高，下次捕获自愈）；history_db.py:205 坏库隔离中途 rename 失败则永久内存态；history_db.py:845 加载按发送端时钟排序（重启后时钟偏差端条目错位、trim 反向误杀——方向已纠正）。

**聊天 / 文件传输**
- nearby_chat.py:1539 重复 chunk 索引导致 received_bytes 超算 → 健康传输误报 size_mismatch（需异常 peer）；nearby_chat.py:1383 同 transfer_id 二次 offer 覆盖状态 → 卡 600s + 泄漏 .part（需异常 peer）；file_transfer.py:1688 测速只测单向墙钟（含 40ms sleep 地板，2.5G/10G 饱和 ~125MB/s）；file_transfer.py:1412 发送端 60s 截止 vs 接收端 3 轮×30s → 持续丢包下发送端先放弃造成单侧假失败；file_transfer.py:1024 文件名 NUL → ValueError 断连接（.part 泄漏，120s 自愈）。

**系统 / 更新**
- hotkey.py:1121 macOS CFMachPort/CFRunLoopSource 双 +1 引用永不 CFRelease/Invalidate（每开关一轮泄漏，tap 进程生命周期内残留）；webview_window.py:180 `taskkill /F /T` 同步最多卡 10s（主线程在重置/关停时）；server.py:2097 stop() 不 server_close()（靠 GC 关 socket，重启+活动连接时 Linux EADDRINUSE/Windows 双监听）；routes.py:428 `/api/update/check` 同步阻塞 worker 线程 ~25s（critic 补的镜像面）。
- settings_window.py:956 每次开设置同步 `getaddrinfo`（冷缓存，Tk 主线程冻结；dashboard 已特意离线程）；main.py:4008 web "restart" 分支不更新托盘 web_enabled → Show Web QR 整会话消失；main.py:707 六端口全占时首启引导被跳过（下次启动自愈）；config.py:299 `clipboard_poll_interval=0` CPU 忙转 / `port=70000` OverflowError 启动崩（手改/坏备份触发，Web API 路径已 clamp）；backup.py:563 手改备份 `hotkeys:{}` 静默清空全部快捷键绑定；main.py:423 `_on_send` 未包异常，Timer 回调异常丢一次捕获；backup.py:256 create_backup 无锁迭代 cfg.peers（配对插入并发 → RuntimeError 500，重试即可）。

**配对 / 安全（降权）**
- pairing.py:252 同一张卡上 8 位配对码与 SAS 指纹**来自不同 hash 输入**（16499178 vs 241F-771B，两码都对端匹配、互不相等，纯 UX 困惑）；pairing.py:258 每次重连刷新 PENDING 时间戳 → 单侧确认后 B 端配对卡永不超时、Web reject 被下轮重连推翻；encryption.py:113 帧密钥只绑定两个指纹中较短者（≥32B 后较长侧零贡献——TLS 之上纵深，无功能破坏）；encryption.py:56 口令校验非常量时间 + 100k vs 600k 迭代不一致。

**测试质量**
- test_delivery/test_internet_pairing 伪造 `web_server.broadcasts`（见 H1，**修 H1 会打破这些断言**）；relay.py:294 probe_relay_endpoint（v1.0.75 SSL 修复点）零测试；sw.js/v-cloak（v1.0.77 两个修复）零测试；test_internet_pairing.py:519/832 fixture 装饰器套在测试函数名上被 :1427/1438 同名函数覆盖 → 死代码（真断言在 :1427 跑，无覆盖损失）；i18n 键一致性测试只比 en/zh-CN JSON，**从不跨 Python 字典 vs Web JSON**（当前 640 键缺口 + 11 个占位符差异无感）；`.kai-round.md` 声称 1246 已不实。

**其他**
- connection.py:1012 mDNS 消失时用 hashed id 调 disconnect_peer，`_peers` 却按真实 id 建键 → 半开连接残留 ~30-120s（旁边代码都解析 hash，唯独这里没有）；ws.py:383 broadcast 串行 sendall，单个不读的客户端最多拖慢一次 ~5s 后自逐出（比声称的 90s 轻）；server.py:1869 Web 上传非法文件名（`|`/`:`/NUL/尾点）open 抛 OSError 杀线程（现代 Win11 下 CON/PRN 不抛，但类真实）；web 上传缺 _sanitize_file_name（P2P 路径有）。

---

## 交叉问题（完整性批评，未逐一验证）

1. **互联网配对聊天传文件结构性不可能**：中继镜像拒绝 file_chunk（main.py:7811，为双通道去重），文件字节只能走 LAN；但聊天附件按钮对纯互联网 peer 照常提供，OFFER/ACCEPT 帧却过中继 → 接收方接受了一个永远收不到字节的 offer，发送方首块报误导性 'peer_offline'，接收方卡到 600s sweeper。要么禁用该按钮、要么提示，要么支持分块中继。**【已修 v1.0.83】**：中继放行 file_chunk（qos=1 at-least-once），纯互联网对端用小分块（RELAY_CHUNK_SIZE=224KiB，封包 < 256KiB 中继上限）并设 5MB 文件上限，双通道对端仍走 LAN 原格式不重复投递；前端附件按钮联网态改名提示 + 客户端预校验，测速对纯联网对端改报"仅局域网"。详见文末"v1.0.83 修复记录"。
2. **Web 实时态无重连兜底**：已送达/配对/中继态/AI 结果/会话全靠 WS 事件；审计证明一整族（internet_delivery/netpair_peer）在发行版已死，唯一 REST 兜底是一次性 fetch、重连不补拉。
3. **unpair/forget 不清送达账本**：`_netpair_unpair` 清了 secret/别名却不清 `_delivery_queue`/`_delivery_ledger`/relay_pending.json → 已断关系的队列继续烧 5 次重试、base64 载荷在盘上留几分钟。
4. **聊天账本只在中继路径记账**：LAN P2P 送达的聊天文字不进账本，对端 relay_ack 被当 unknown msg_id 丢弃 → 同一 peer 中继送达有 ✓、LAN 送达永远无 ✓。
5. **测试 mock 错边界**：正确的 H1 修复会让 test_delivery/test_internet_pairing 报红（套件会标修复、不标它所消灭的 bug）。
6. **更新检查阻塞 Web worker 线程**：`/api/update/check` 同步 ~25s，与 H7 的 Tk 冻结同时发生时可两条线程一起堵在 GitHub。

---

## 建议修复顺序（按 性价比 × 用户价值）

1. **H1**（3 处一行改 + 测试改断言）→ 恢复轮 17 你点名的「已送达确认」实时能力。
2. **H4 文件传输死锁**（锁外触发回调，模块内已有先例）→ 中途中止即可触发，直接冻结管理器。
3. **H2 + H3 AI 配置**（对端路径支持文件根 + root_index 重映射）→ 默认清单 5/8 开箱即坏 + 落错目录可覆盖。
4. **H6 Linux 更新 EXDEV**（os.replace 加 copy 兜底）→ 正好是 roadmap「auto-update 缺口收口」里最实的。
5. **H5 中继 client 泄漏**（_connect_one 先 stop 旧 client）。
6. **M 级里先挑**：routes.py:1121 暂存文件泄漏、chat-panel.js:797 上传卡死、backup.py:137 收藏恢复塌缩、export.py:58 二进制乱码、server.py:1070 64 连接断网。
7. **测试面补齐**（H1 断言修正、_safe_decode、probe、sw.js/v-cloak、i18n 跨系统 parity）。

> 说明：安全类（低）基本是纵深加固/UX，按你的「可信局域网 + 功能/稳定性优先」未上调；Tk 界面条目（settings_window/webview/fonts）保持仅崩溃级关注。

---

## 修复状态（同日，21 个并行 worker + 集成回归）

全部按文件分组并行修复，**全量 pytest 1231 passed / 4 skipped（exit 0）**。改动 48 文件，+1624/−633。

- **6 个高严重度全部落地**：H1 三处 `web_server.broadcast` → `ws_manager.broadcast`（并修正了 2 个测试文件的伪造 `web_server.broadcasts` 断言）；H2/H3 AI 配置单文件根 + root_index 重映射（`_land_target` 按 rel 归属校验）；H4 文件传输取消死锁（回调移出锁外）；H5 中继 failover 旧 client 先停再换；H6 Linux 更新 EXDEV 加 copy 兜底。
- **22 个中、46 个低按清单全部处理**（含 unpair 清送达账本、聊天 LAN 路径补账本、`/api/update/check` 后台化、WS 传输方向、上传暂存清理、收藏/导出数据修复、locale 回退、端口/轮询钳制等）。
- **测试更新（集成期）**：7 个测试因行为修正而调整——聊天丢弃帧改断言 `False`（2 个）；历史 DB 改戳本地接收时间后 trim/age-prune 期望集更新（2 个 trim + 2 个 age-prune 改用冻结时钟注入真实年龄）；文件传输超时测试补 `RETRANSMIT_TIMEOUT` 猴补丁。另修复 2 处 `_delivery_clear_peer` 调用点未 getattr 防御（SimpleNamespace 测试桩崩溃）。
- **新增回归测试**：`_safe_decode` UTF-16、`probe_relay_endpoint`、sw.js shell-v2/v-cloak、i18n 跨系统 parity、AI 配置单文件根拉取（test_relay +84 / test_aiconfig +54 / test_history +94 / test_web_api +16）。
- **注意事项**：
  - `encryption.py` 帧密钥改为绑定双方完整指纹（修复 frame(X,Y)==frame(X,Z)）——**密码保护帧的密钥会变，配对设备两边都升级到同版本后需重新配对一次**；at-rest 存储密钥未动，存量数据可读。
  - `PLAN.md` 在工作区被删（` D PLAN.md`），按要求**未触碰**，别被 `git commit -am` 顺手固化。
  - 移动端 mobile.html 聊天附件对纯互联网 peer 仍可见（与 chat-panel.js 相同问题，本轮按清单只修了 Web 端）；`pytest-timeout` 未装、60s 超时护栏无效（存量）。
  - 未提交任何 commit。

---

## v1.0.83 修复记录（2026-08-26，交叉问题 #1）

修复交叉问题 #1（互联网配对聊天传文件）：`_relay_publish_to_peer` 曾硬拒 `file_chunk` 帧（main.py:7927 旧行，双通道去重遗留），导致纯互联网对端能收到 offer/accept 控制帧但永远收不到文件字节 → 卡 0% / 假报"速度很慢"。

- **nearby_chat.py**：新增 `ChatFileTooLarge`；类常量 `RELAY_CHUNK_SIZE=224KiB`（带 46B 头封包 < 256KiB 中继上限）、`RELAY_FILE_CAP=5MB`；send_file 按发送闭包上标记的 `chunk_size`/`internet_cap` 算总块数并提前拒绝超限；offer 载荷新增 `chunk_size` 字段（旧接收端忽略未知字段）。
- **src/main.py `_chat_send_fn`**：对"不在已连接 LAN 列表 + 互联网可达"的对端，把 `_send.chunk_size`/`_send.internet_cap` 打成函数属性；LAN/双通道对端保持 256KiB 原格式（线协议字节不变，无旧 LAN 对端回归）。
- **`_relay_publish_to_peer`**：移除 file_chunk 禁令，best-effort 解码嗅探 chunk 帧走 `qos=1`（broker at-least-once），文本/控制帧仍 qos=0；解码失败不阻断（不误杀无法解码的帧）。
- **relay.py**：`publish()` 新增 `qos` 参数（默认 0），主/镜像 client 均透传。
- **前端**：chat-panel.js 附件按钮纯互联网对端改名"发送文件（互联网中继，上限 5 MB）"+ 客户端 5MB 预校验 + `internet_file_cap` 错误区分；store.js 测速对纯联网对端报"仅支持局域网"而非误导的"很慢"。
- **i18n 四镜像**：`chat.attach_internet`、`chat.err_internet_file_cap`、`transfer.speed_test.lan_only` 同步 en/zh-CN JSON + Python _EN/_ZH。
- **测试**：relay 桩 publish 增 `qos` 形参；中继文件块测试改断言 qos=1 + 文本 qos=0；新增 `TestInternetRelayFileTransfers` 4 例（跨中继收发、小分块、超 5MB 拒绝、bogus chunk_size 回退）。全量 **1273 passed / 4 skipped（exit 0）**。
- **版本**：v1.0.83（CHANGELOG 同步）。

> 已知边界：新版发送端 → 旧版纯互联网接收端仍失败（旧接收端连 offer 都收不到字节，修复前即全坏，非回归）；新→新全通。旧 LAN 对端不受影响。

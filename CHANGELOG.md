# Changelog

All notable changes to ClipSync are documented in this file.

## [1.0.93] — 2026-08-27

- **修复设备页「连接」按钮谎报成功**：在「已发现」里点连接，之前无论结果如何都弹「连接成功」——但 `{ok:true}` 只代表连接已**发起**、不代表连上了；若对端直接拒绝（它的用户曾在另一台设备移除本机，它收到连接后发回拒绝标记），连接静默失败、UI 毫无反馈，看起来就是「提示连接成功然后没有任何反应」。现在：① 点连接后的即时提示改为「正在连接…」，真正的成功以设备卡移入「已连接」区为准；② 对端明确拒绝时，传输层通过新增的 `set_on_connect_rejected` 回调（携带 name/peer_id、线程安全、绝不抛出）把 `connect_rejected` 广播给 Web UI，弹出「{name} 拒绝了连接 — 该设备可能已将你移除」。设备页的「已发现」卡片上，一个被对端移除的设备从此不再是「假成功 + 无反应」，而是有明确、诚实的反馈。
- 新增 2 个 locale 键（`device.connect_started` / `device.connect_rejected`）中英全量同步。对应测试：`tests/test_connection.py`（回调触发 / 无回调静默 / 回调抛错不传播）与 `tests/test_devices_ui.py`（连接 toast 诚实化、WS 处理 `connect_rejected`、后端接线、locale 镜像、node --check）。

## [1.0.92] — 2026-08-27

- **修复自动更新检查崩溃**：`internal/system/updater.py` 的 `download_latest_release` 把内建函数 `callable` 误当类型写进参数注解（`progress_cb: callable | None`）。在应用所用的 Python 3.11（注解在函数定义时立即求值）下，懒导入该模块的瞬间即抛 `TypeError: unsupported operand type(s) for |`——自动更新检查每轮都失败并刷 ERROR 日志（启动不报错，因为该模块是定时任务才懒加载；测试环境是 Python 3.14 的惰性注解，也一直没暴露）。改为正确的 `typing.Callable | None`；Python 3.11 / 3.14 双解释器导入与注解访问均验证通过，`tests/test_update.py` 51 例全绿。

## [1.0.91] — 2026-08-27

- **五个 Web 页面整体走查（概览 / 历史 / 设备 / 文件 / 软件壳）**：以「真实缺陷 + 诚实状态」为尺，修掉一批误导性 UI、后端不一致与假成功提示。各页要点如下。
- **概览页**：统计卡片在进入 tab 时立即填充（此前要等第一次轮询推送才从 0 跳数）；切页时取消未完成的数字滚动动画，不再对已卸载组件发起 rAF 回调；手动开关同步会同步清掉本地的「定时暂停」倒计时（与服务端行为一致），不再残留「⏸ N 分钟后恢复」行；「复制 URL」改为复用 store 的手机连接链接（跟随实际服务协议 http/https、指向轻量 mobile 页），此前写死 http、在 TLS 部署下复制出来的链接连不上。后端概览的「已连接」统计改为只算「已配对 + 实时同步会话」，与前端连接数、环形图口径一致；移除已死的 `transfer_bytes` / `recent_activity` 字段与相关的 transfer 统计卡片标签错位。
- **历史页**：搜索有结果时才显示「共 N 条」统计行——空结果时只留空态提示，不再和「0 条」行打架；「加载更多」改用与首页一致的 `web_history_limit` 页大小（此前写死 20，与首页配置页大小不齐、偏移量会算错）；清空/置顶/收藏/删除批量操作在后端返回 `{ok:false}` 时如实 toast 失败原因（此前一律报成功）；清空历史同时清掉多选框选，批量操作条不再悬在空列表上。卡片获得焦点时 Delete/Backspace 删除该条（应用级快捷键对卡片焦点是关掉的）；复制动作收敛到共享的 `store.pasteHistoryItem`，并修正了两处声称「右键菜单也走该 helper」的失实注释（右键菜单对精确点击行保留自己的内联 pasteRich）。
- **设备页**：被移除的旧设备即使仍在局域网广播，也不会再以「已发现」重新冒出来（发现扫描按已移除归档去重）——它只存在于「已移除」区等恢复/彻底删除；生成/输入配对码的控件只在互联网同步开启时显示（关着时是死 UI，输码本来就会被拒绝），改为单一「开启同步」提示；过期的配对请求标记为「已过期」而非误导的「已确认 · 等待中」；「无设备」空态等到「已移除」区也清空才显示。🌐 互联网配对徽标抽成 `<netpair-badge>` 组件（原来内联三份）、配对码复制抽成共享 `_copyToClipboard`、连接测试收敛到 `store.testPeerConnection`（局域网设备卡与互联网配对行共用）；本机设备卡不再提供备注编辑（后端静默丢弃本机 id 的备注，保存是假动作）；删掉一批死 CSS。
- **文件页**：删除死端点 `POST /api/transfer`、`/api/transfer/accept`、`/api/transfer/reject` 及其路由（向对端发起/接受 P2P 文件发送是桌面壳的界面，Web 端没有对应按钮，前端无调用方）。上传卡死保护从固定 15s 改为「15s + 每 MB 5s」——慢但正常的几十 MB 上传不再被中途掐死，真卡死的连接对小文件仍能快速失败。传输快照同步收敛为 `store.refreshTransfers` 一处实现（初始加载、操作后刷新、WS 完成、定时轮询共用，不再两处漂移）；每 5s 的传输对账轮询只在有活动传输且页面可见时跑，并加并发闸。暂停/恢复/取消/重试/全部取消在后端返回 `{ok:false}` 时如实报错（此前乐观置位 + 谎报成功）；「对端离线」提示不再被自动选择目标瞬间盖掉；新增 `transfer.retry_failed` 文案（中英）。
- **软件壳**：tab→面板映射单点化——app.js 的 `PANEL_COMPONENTS` + 宽/窄两套布局各一个动态 `<component :is="panelComponent">`，替换两套手维护且已漂移的 `v-else-if` 挂载链；清掉一批死 CSS（被组件块整体顶掉的 `.favorites-panel` / `.history-panel__header`、`.transfer-card__*` 进度/图标块、`≤1200px/≤480px` 里冗余的 max-width 覆盖、与面板切换动画重复的双重淡入）。顶栏主题切换改走 `store.selectAppearanceTheme`（与设置页同一持久化路径：本地应用并 POST `appearance_mode`，跨设备生效），此前只改本地不持久化；右键菜单「标为已读」对空会话加 `(targetSession || {})` 保护，不再在 target 为空时崩模板。
- 对应新增/更新测试：新增 `tests/test_devices_ui.py`（13 个用例：发现去重、netpair 门控、过期徽标、空态、徽标/复制/连接测试单点、本机备注隐藏、死 CSS、locale 镜像、node --check），并更新 `test_aiconfig_ui.py` / `test_diagnostics.py`（面板挂载断言改为统一 dispatcher）、`test_history.py`（Web locale 镜像 gap 693→692，因移除 web-only 死键 `history.merged`）、`test_web_api.py`（test 连接收敛到 store）。Web locale 移除 22 个死键、新增 1 个键，中英 + Python 全量同步。

## [1.0.90] — 2026-08-27

- **AI 配置同步彻底重构：从「原始路径列表」升级为「AI 工具档案」**。此前该功能维护一份监视根路径列表、界面到处是 R0/R1 这类 `root_index` 编号、对端清单映射到本机时报「模糊根 / 无本地根」——心智模型混乱。现在「配置」tab 是一个统一面板：**顶部设备条**（本机 + 各已配对设备，每台标注差异总数徽标）、**中部按工具档案分组的清单**（Claude Code / Codex / Cursor / Gemini + 自定义路径，每行对比徽标）、**本机管理子视图**（预览 / 编辑保存留 .bak / 回收站 / 打开文件夹）。旧的本机文件管理器占 2/3、跨设备浏览被挤到底部的陈旧布局已删除。
- **内置 4 个工具档案取代手工路径**：claude_code（`CLAUDE.md`、`settings.json`、`skills/`）、codex（`config.toml`）、cursor（`rules/`、`commands/`）、gemini（`settings.json`、`GEMINI.md`），另保留用户自定义路径兜底。配置字段 `ai_config_paths` → `ai_config_tools` + `ai_config_custom_paths`；旧配置**自动迁移**（路径命中档案即启用对应工具，其余归为自定义路径），已在测试中锁定。设置页改为档案勾选 + 自定义路径，前端不再硬编码预置表（唯一事实源在后端 `/api/aiconfig/profiles`）。
- **skills 文件夹真正支持整文件夹递归拉取**：勾选一个技能/命令文件夹即从对端清单递归展开该文件夹下全部文件，一次批量 pull，逐文件独立哈希校验原子落盘，进度条实时显示「N/M」，失败可单独重试。此前文件夹只是只读元数据、无法勾选拉取。
- **一键迁移向导**：选源设备 → 按工具分组的差异清单（缺失 / 本地新 / 远端新，默认全选差异）→ 一键应用，冲突默认「跳过已有」（可选「覆盖留 .bak」/「另存副本」），批量进度 + 逐文件结果。主视图的「迁移」按钮直接进向导并预选全部差异。
- **协议升级为 v2（`root_index` → `tool` 维度），旧版兼容保留**：清单条目改按 `{tool, rel_path, sha256, size, mtime, is_dir}` 描述，落盘按工具档案解析、不再扫描所有本地根猜 `ambiguous_root`；对旧版对端的清单仍可只读浏览与预览（标记「旧版只读」），文件夹拉取与迁移仅对新版对端可用。对比键 = `(tool, rel_path)`，哈希相同→相同、否则按 mtime 判本地新/远端新、单边→缺失。
- **前端重复逻辑收敛**：`aiconfig-device-panel.js` 删除，设备条 / 对比徽标 / 格式化的重复实现抽为公共 `js/aiconfig-helpers.js`（纯函数，node 测试直接驱动）；对比徽标在本地清单未加载时返回「无徽标」而非误报「缺失」（修复一个徽标闪烁前误报的隐患）。
- 对应新增/重写测试：`tests/test_aiconfig.py`（75 个后端用例：档案展开、tool 落盘、文件夹展开拉取、batch 结果、v2/legacy 兼容、profiles 端点、迁移辅助）、`tests/test_aiconfig_ui.py`（38 个前端用例：统一面板结构、迁移向导、store/api/ws、locale 全量镜像、node --check、无硬编码预置）。修复 1 个后端缺陷：同名的顶层文件根优先于同名目录根命中（否则拉取 `CLAUDE.md` 会误报「模糊根」）。

## [1.0.89] — 2026-08-26

- **更新流程改造：下载进度可见 + 下载完提示手动运行（Windows 不再自动重启）**。之前的自动更新会在部分 Windows 机器上触发 PyInstaller 的父进程校验崩溃（`parent process has different executable`）——现在 Windows 下载更新时设置页显示实时进度条（百分比随分块推进），下载完成并校验后把可运行的新版 `clipsync.exe` 解到 `下载\clipsync-update\`，弹出提示 + 设置页显示「新版本就绪」卡片（版本 / 文件路径 / 「打开所在文件夹」按钮），由你手动退出当前应用、双击运行新版；剪贴板历史与设备数据都留在本机，新版直接使用。Linux 保持自动替换重启，macOS 保持自动打开文件夹，均不变。
- **Windows 自动替换/重启机制整体下线**：删除 `_build_windows_bat` / `_apply_windows` / `/api/update/install` 端点及配套前端「安装更新」按钮、`on_update_install` 回调链、更新 bat 测试。
- **新增 6 个 locale 键**（`settings_window.update_downloading_progress` / `update_ready` / `update_ready_hint` / `update_open_folder`、`tray.update_ready_prompt`）+ 微调 `tray.update_install_prompt` / `settings_window.update_hint` 文案，中英 + Python 全量同步（镜像 gap 保持 677）。

## [1.0.88] — 2026-08-26

- **设备页大改：「已连接」只认「已配对 + 实时同步会话」**。设备状态改为六分栏互斥模型：🟢已连接（`connected && paired`）、🟣临时连接（新，`connected && !paired`——聊天拉起的会话不再混进「已连接」）、🟠已配对·离线、🔍已发现、🗑已移除设备（新）、🌐互联网配对。点「连接/断开」不再乐观置位（`{ok:true}` 只代表已发起握手），真实状态由后端广播 ≤3s 收敛——握手失败不再假亮「已连接」。
- **新增「已移除设备」归档**：被移除（forget）的设备不再直接蒸发，归档到配置里持久保存，可「恢复」（保留配对标记 + 按归档地址尽力重连回同步）或「彻底删除」（不可撤销）。旧配置无归档字段自动兼容。
- **设备信息及时更新**：设备页广播门控从粗粒度 `name+状态` 字符串改为**全量快照指纹**——任何渲染字段变化（连接/断开、重连进度、改名、地址、备注、归档）都触发 `devices_updated`，页面 ≤3s 收敛；改备注立即同步到其他标签页。
- **每一栏都可折叠、默认全展开**：本机 / 配对请求 / 已连接 / 临时连接 / 已配对·离线 / 已发现 / 已移除 的栏标题都可点击折叠/展开（chevron 与互联网配对一致），折叠后标题与计数徽标仍在。
- **传输目标 / 状态栏 / tab 徽标只认同步连接**：聊天临时连接不再计入「已连接」计数，也不可选作传输目标。
- **toast 去重修复**：同一操作的重复成功/失败 toast 不再叠加。
- 新增 11 个 locale 键（`device.temporary_connected` / `device.restore` / `device.purge` / `devices.removed_*` / `devices.restore_*` / `devices.purge_*`）中英 + Python 全量同步（镜像 gap 保持 677）。

## [1.0.87] — 2026-08-26

- **中继设置拆分为「免费匿名」与「需登录的私有」两部分**：远程同步设置里的中继服务器列表一分为二——「匿名免费中继」（公共 broker，无需账号，每行一个地址）与「需登录的私有中继」（用下方用户名/密码登录，每行一个地址）。旧的合并式单列表移除。
- **私有中继优先、免费中继兜底**：连接时先尝试私有中继，全部不可达再回退到免费公共中继；免费中继同时作为镜像，让落在不同 broker 上的两端设备仍能互通。broker 凭据只在连接私有端点时下发，匿名公共 broker 永远不会收到。
- **默认已预置需登录的中继**：默认配置已带一个需登录的中继（地址 + 账号密码已预填），开启互联网同步即可直接用，无需手动输入；同一地址出现在两个分区时自动去重，不会重复连接。
- 新增 4 个 locale 键（`settings_window.relay_free_label` / `relay_free_hint` / `relay_private_label` / `relay_private_hint`），移除 2 个旧键（`relay_brokers_label` / `relay_brokers_hint`），中英 + Python 全量同步（镜像 gap 由 679 变为 677）。

## [1.0.86] — 2026-08-26

- **中继支持私有 broker 账号鉴权**：远程同步设置里新增「Broker 用户名 / 密码」输入（可留空保持匿名，公共中继行为不变）。保存后立即生效——正在运行的中继会热切换凭据并重连，无需重启；密码永不回显，只显示「已设置」状态，可用「清除」按钮显式移除。凭据随配置持久化（设置了应用密码时随配置整体加密），并随备份/恢复一同迁移。
- **中继传输按 URL scheme 自动选择**：之前所有中继地址都被强制走 WebSocket+TLS，私有 broker 的明文/原生 MQTT 监听端口无法使用。现在 `wss://`（默认）走 TLS WebSocket、`ws://` 走明文 WebSocket、`mqtt://` 走原生 TCP、`mqtts://`/`ssl://`/`tls://` 走原生 TCP+TLS；端口缺省时按协议取默认值（原生 MQTT 1883、WebSocket 8884）。设置页的中继服务器列表校验与提示同步放宽。
- **私有 broker 设为默认**：中继默认服务器改为用户的私有 broker（`mqtt://mqttyyc.top:1883` + `ws://mqttyyc.top:8083/mqtt`，两者互为故障切换），broker 账号密码已预填为默认值——开启互联网同步即可直接使用，无需再手动输入账号密码。
- 新增 5 个 locale 键（`settings_window.relay_username_label` / `relay_password_label` / `relay_password_placeholder` / `relay_password_clear` / `relay_password_hint`）中英 + Python 全量同步（镜像 gap 保持 679）。

## [1.0.85] — 2026-08-26

- **加密开关措辞简化**：设置里的开关标签去掉「（所有三项功能）」字样，提示语也不再列举三处功能，只说明「在 TLS 之上再叠加一层密码加密」。
- **统一密码恢复强度要求**：设置加密密码和之前设置配对密码一样**必须满足**：至少 12 个字符（不超过 200），且同时包含大写字母、小写字母、数字和特殊字符。服务端强制校验（改请求体也无法绕过），Web 设置页输入时实时显示逐条勾选清单并给出缺项提示。
- 新增 7 个 locale 键（`settings_window.password_rules` / `password_rule_*` ×5 / `password_invalid`）中英 + Python 全量同步。

## [1.0.84] — 2026-08-26

- **密码统一为一个**：互联网配对不再有单独的配对密码，**一个加密密码**同时保护局域网传输、本地存储和互联网配对（`encryption_password` 作为唯一密码，配对码只做路由+盐，AES 密钥由这一个密码派生）。设置里删掉了「互联网配对密码」输入框（连带 19 个失效 locale 键），旧版已保存的配对密码自动兼容（保留为升级回退，重新设置加密密码后即完全切换）。加密开关/密码变更时**实时重建**加密管理器并重派生配对频道密钥，无需重启。
- **通知声音语义统一**：`声音`开关现在是纯粹的「是否播放提示音」开关，只受「通知」总开关节制——关掉通知后声音也随之关闭；聊天提示音与后端所有通知一致，不再被声音开关单独吞掉。
- **互联网配对自愈（确认身份后收起幽灵条目）**：输码后的临时 base32 标签条目在首次收到对端真实身份的 hello 时自动重键为真实设备 id（状态列表同步跳过标签键条目），不再出现「配对成功但列表里挂着一条打不开的幽灵设备」；配对成功的广播同步带上在线/最近活跃时间，设备页立即显示绿点而非误导性的离线状态。
- **配对码重复生成清理**：重新生成码时先清掉旧待确认码，杜绝旧码残留导致的配对状态错乱。
- **设备测试连接结果诚实化**：中继不可达（发布失败）逐通道报「中继未在线」而非笼统超时；无可用通道时 toast 明确显示「无可用通道」原因（空结果数组在 JS 里为真值的判断 bug）。
- **设置里改中继服务器列表真正生效**：`restart()` 之前先把新列表写进中继客户端（此前重启读的仍是旧列表，改完等于没改）。
- **中继连接日志去噪**：broker 不可达的 `TimeoutError/OSError` 只记一行警告，不再打整条堆栈；过期的 on_connect 回调不再顶替当前连接。
- **断开连接清理完整**：删除设备时同时清掉按哈希存的发现条目（此前只弹真实 id，删除后设备还会出现在「已发现」区）。
- **局域网发现恢复一致性**：暂停再恢复浏览后，消失的旧对端在 4 秒宽限期内未重新宣告即报告离线（此前恢复后永久幽灵在线）；跳过无 `device_id_hash` 的畸形 mDNS 服务。
- **帧级容错**：单个坏帧（解码/处理异常）只丢弃该帧并记录，不再整体断开连接触发重连风暴；重连计数读写纳入锁保护。
- **移动设备 / 地址变更重连**：对端换了 IP 重宣告时清除在途自动连接去重标记，能立刻连到新地址；自动连接只对「已配对」对端发起。
- **前端小修**：配对请求卡片去重（重复推送不再叠卡）；互联网配对成功后清掉「我的配对码」显示（已用完）；过期（5 分钟）的配对请求不再显示确认按钮并给出「请求已过期」提示；「与自身配对」错误识别覆盖「cannot pair with this device」。
- 新增 1 个 locale 键（`device.test_relay_offline`）中英同步。

## [1.0.83] — 2026-08-26

- **互联网模式聊天文件传输真正打通（修复"能发起但卡在 0% / 提示速度很慢"）**。根因：`file_chunk` 二进制帧以前被明确拦在中继之外（仅文本/控制帧可跨网），所以互联网配对的对端只收到文件 offer、永远等不到数据块 → 卡死。现在聊天文件字节可以跨中继（`_relay_publish_to_peer` 放行，LAN 优先的 `_chat_send_fn` 保证双连接对端不会收到两遍；`chat_file_offer/accept/complete` 握手本就走中继，收方最终落盘由 `chat_file_complete` 触发，全链路闭合）。
- **为互联网对端切到中继安全分块 + 5 MB 上限**。互联网专属对端（无实时局域网连接）文件按 224 KiB 分块（46B 二进制头后总帧长 < 中继 256 KiB 载荷上限，全量 256 KiB 分块会超限被拒）；文件超过 5 MB 时**明确拒绝**并提示"互联网模式上限 5 MB"（Web 前端在选文件时客户端预检 + 服务端二次校验），不再无声挂起。局域网/双连接对端保持原 256 KiB 分块，线上格式逐字节不变，旧版局域网对端无回归。
- **文件分块（QoS 1）过中继**，文本/控制帧仍 QoS 0：公共中继尽力而为，数据块丢包由 broker 重投，收方信封去重 + 逐块去重吸收重复投递；`chat_file_complete` 终帧兜底校验大小不匹配。
- **聊天附件按钮对互联网对端放行**（原来直接禁用），悬停提示"互联网中继，上限 5 MB"；发送端选择 >5 MB 文件即时 toast 说明。
- **测速提示诚实化**：速度测试只测局域网吞吐，只有互联网对端在线时明确提示"测速仅支持局域网"，不再误报"未连接设备"或通用失败。
- 新增 3 个 locale 键（`chat.attach_internet` / `chat.err_internet_file_cap` / `transfer.speed_test.lan_only`）中英 + Python 全量同步。

## [1.0.82] — 2026-08-26

- **修复互联网配对「配对成功但 0 台在线」的分裂 bug（镜像发布）**。根因：MQTT broker 之间不联邦（不同 broker 上收不到彼此的帧），而中继是单连接 failover——两台配对设备各自断线后换到**不同** broker 时，A 发到 broker#1 的帧在 broker#2 上根本不存在，对方永远收不到。现在每条帧会**同时发布到所有已配置 broker**（主连接负责订阅/接收不变，其余 broker 各开一条只发布不订阅的镜像连接），对方无论 failover 到哪个 broker 都能收到；主连接断线重连换 broker 时镜像集自动跟着换（新 broker 不再重复发布，避免对方收到两遍）。单 broker 配置行为与旧版逐字节一致。
- **远程同步设置页新增只读「当前中继服务器」行**。显示本机中继此刻实际连着的 broker 地址（在线且有值时显示，诊断定位用，不可编辑）；两台设备都开时一眼能看出是否落在同一 broker。实时跟随连接状态，与「状态」行同源。

## [1.0.81] — 2026-08-26

- **新增「配对加密密码」（设置项，分层加密）**。互联网配对码流程完全不变（仍自动生成 12 位码 / 输码），密码全部放设置界面：设置里没设密码 → 一切照旧（现有 35 位模型，向后兼容）；设置了密码 → 分层加密——配对码只做**路由 + 盐**，真正的 AES 密钥由密码派生（码只决定收在哪个频道，穷举码拿不到密钥）。两台设备设相同密码即可正常配对；已配对的两台设备设相同密码后无缝升级（同一公式重派生，无需重新配对）；设了密码的和没设密码的设备解不开对方，无法通信。密码强度强制（长度 ≥12 且大写 + 小写 + 数字 + 特殊符号四类齐全），保存时服务端校验，设置界面实时提示缺哪一项；密码值永不下发前端（只暴露「已设置」布尔）。
- **设备卡新增「测试连接」按钮**。局域网卡片和互联网配对列表里的设备都能一键测双向通信，结果用 Toast 展示：在线显示各通道（局域网 / 中继）的往返延迟，失败逐通道列出原因（设备离线 / 中继未在线 / 超时未回复 / 无可用通道）。
- **所有通知改成 toast 样式**。软件的 ~30 处通知（聊天邀请/消息、URL 打开/发送、更新检查/下载/拒绝、托盘失败等）全部走内置 toast，不再发系统通知；设置里的「通知」总开关同时关掉这些应用内 toast。
- **所有可折叠模块默认展开**。设置里互联网配对等所有列表式模块打开时默认不折叠，内容多了再手动折叠；首次引导第 7-8 步的「外观/偏好」总开关也一并移除，主题/动画、语言/开机自启等直接展开显示（不再被主开关折叠隐藏）。
- **修复 macOS 粘贴崩溃**（含非 UTF-8 文件路径时 `POST /api/paste-rich` 返回 500、粘贴静默失败）：原子写 FILE 分支的 `decode("utf-8")` 现加保护，失败落到安全的 osascript 写文件路径。

## [1.0.80] — 2026-08-26

- **首次引导页从 3 步扩到 9 步，把最重要的设置做成一页一个的总开关（默认全部关）**。前三步不变（欢迎/命名、配对、手机访问），新增第 4-9 步：**内容过滤、应用过滤、远程同步、外观、偏好设置、声音**。每一步只有一个主开关 + 一句解释，不弹按钮；其中**外观**打开后原地展开主题/动画选择，**偏好设置**打开后原地展开语言/开机自启选择。向导完成（或跳过）时把这四个真实功能开关（内容过滤/应用过滤/互联网同步/声音）的最终状态写回后端——全新安装若没手动打开就是**默认关闭**；外观/偏好主开关是纯界面总开关（存 localStorage），它们下面的子设置仍走各自的设置键。
- **设置面板同步加主开关**：**外观**页变成"外观"总开关，打开才显示主题 + 动画；**偏好设置**页变成"偏好设置"总开关，打开才显示语言/系统（开机自启、界面模式），**通知区保持常显**（声音开关不受偏好总开关影响）。老用户已有自定义（深色主题/开机自启）时主开关自动为开，避免已有配置被折叠隐藏。
- 新增 13 个 web-only locale 键（`onboarding.enable` + 六个步骤的 title/body）中英同步。

## [1.0.79] — 2026-08-26

- **设置界面彻底重构分组**（12 标签 → 13 标签，每页只讲一件事）。**外观**只剩主题 + 动画（语言、开机自启、界面模式挪走）；新增**偏好设置**页（语言、开机自启、界面模式、声音 + 全部通知开关——从外观/安全挪来，且通知开关从"点保存才生效"改为**即时保存**，与开机自启同款交互）；**网络**只剩局域网（端口/服务类型/本机地址），互联网同步单独拆成**远程同步**页（开关、实时状态、配对管理入口、代理列表/测试，与「远程访问」并排）；**安全**只剩加密/密码 + 可信设备证书；**危险区域**（重启/恢复出厂）并入**数据管理**。设备页"去开启互联网同步"跳转改到远程同步页。新增 2 个 web-only locale 键（`settings_nav.remote_sync`/`settings_window.system_title`）中英同步。

## [1.0.78] — 2026-08-26

- **全面代码审计修复**（48 文件，+1624/−633）。高严重度：修复已送达/配对上屏事件全死（`web_server.broadcast` → `ws_manager.broadcast` 三处，并把伪造 `broadcasts` 属性的测试断言一并修正）、AI 配置单文件根跨设备拉取/预览、AI 配置 root_index 落错目录、文件传输取消自死锁、中继 failover 泄漏 paho 客户端、Linux 更新跨文件系统 EXDEV 加 copy 兜底。中/低：剪贴板写失败不计速率并正确回 ack、空白文本不再吞图片、重抄尊重 90s 去重窗、Linux 空剪贴板慢节奏、历史时间戳改本地接收时间（修复发送端时钟偏差重排/误裁）、聊天丢弃帧不再误回已送达、聊天文件回调移出锁外、Web 聊天上传暂存清理、收藏/导出数据不损、中继静默分区状态、加密帧密钥绑定双方完整指纹（配对设备两边升级后需重新配对一次）、存储密钥未动存量可读、端口/轮询值钳制、`pip install .` 补 paho-mqtt 与 web static 打包等。
- **Web 前端全面审计重构**（25 文件，+829/−235）。核心：**放开全局右键**（三处 preventDefault + `user-select:none` 全处理）——聊天/历史/诊断/AI 配置等文字可选中、输入框可右键粘贴；**聊天消息气泡/文件卡片新增右键「复制」菜单**；各面板布局/按钮/文案/无障碍修复（设备页配对码可复制、设置"保存后又提示未保存"、历史"删除中"文案、传输未知文件显示真实名、AI 配置 tab 可滚动、移动端浅色主题修复、聊天 IME 回车守卫等）。24 个新 locale 键中英同步。
- **互联网配对折叠区改版**：折叠开关从"像功能开关的 checkbox"改为**chevron 手风琴按钮**（▸/▾ 旋转指示展开态），去开启按钮改为行内 accent 幽灵按钮，不再浮到最右。

## [1.0.77] — 2026-08-25

- **Fix stale cached web pages after an update** ("重启后打开的好像还是 quickpaste 页面"). The dashboard's service worker cached static assets **cache-first** and fell back to the cache on a navigation 404, so after an update removed `quickpaste.html`, the browser kept serving the old cached copy — and updated JS/CSS were never picked up. The cache is now versioned `shell-v2` (old v1 caches — including any cached `quickpaste.html` — are purged on activation) and shell assets are **network-first** (the server is local, so the extra round-trip is negligible; updates take effect immediately, cache is only the offline fallback).
- **De-flake the queued-dialog response-window test** (was intermittently failing on the macOS CI runner: margins of ~0.05 s against a loaded runner). The queued-dialog test now uses ~1 s margins and passes consistently.
- **No more garbled flash on refresh** ("刷新会闪一下引导页，全是代码"). Before the Vue bundle mounts, the raw HTML — including the `v-if` onboarding overlay — was briefly painted unstyled. The app root now carries `v-cloak` so the shell stays hidden until the app mounts, then renders fully controlled by state.
- **Paired-device AI-config inventories moved out of the Devices page** into the「配置」(AI Config) tab, under the local config manager — device configs belong with the config manager, not buried at the bottom of the device list.
- **Device AI-config files are now grouped by directory.** Instead of one flat table of every nested file, a device's inventory is a tree grouped by its top-level folders (skills/commands/rules/…) with subdirectories collapsible (▸/▾); search still returns flat matches. Applies to the local manager too (same treatment from v1.0.75).
- **Internet pairing moved to the bottom of the Devices page, collapsed behind a toggle.** It no longer sits at the top as a wall of pairing prompts — the section header now has an on/off switch and the detail (relay state, generate/enter code, paired list) only shows when expanded; the verbose three-step guide is gone (just a one-line "sync off → 去开启" when disabled). It auto-expands once a paired internet device actually appears.
- **AI config: folders can now be trashed too.** The local config manager's 🗑 previously only appeared on files — a folder entry now also trashes (moves the whole directory tree to the recoverable recycle-bin trash) and the list drops every entry under it.

## [1.0.76] — 2026-08-25

- **Fix Windows auto-update relaunch crash.** After replacing `clipsync.exe`, the update helper launched the new exe with `start ""` from a batch file and then exited — so the relaunched exe's parent `cmd` was already gone when PyInstaller's onefile bootloader validated it, killing the app with `Security validation failure: failed to obtain executable path for parent process`. The helper now runs the new exe **directly**, keeping itself alive as the parent for the app's whole lifetime, so the validation always resolves and the app auto-reopens after updating. (`clipsync.exe.old` next to the exe is the intended rollback backup.)
- **Defensive fix for garbled clipboard history.** The history decode helper only tried UTF-8 and single-byte CJK encodings; any entry stored as wide (UTF-16 with BOM) text — very old data, or a peer that didn't normalize — fell through and rendered as mojibake. `_safe_decode` now detects a UTF-16 BOM first in both the SQLite and JSON history backends.
- **Fix macOS update check** (`Update check failed after 3 attempts: [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate`): the updater's `urllib` requests used the default SSL context, which on macOS / frozen builds has no CA store. Both the release-info fetch and the asset download now use a certifi-backed context (same root cause as the v1.0.75 relay fix).

## [1.0.75] — 2026-08-25

- **Fix macOS internet sync** (relay `wss://` brokers all failed with `SSL: CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`): macOS / frozen builds have no OS trust store in OpenSSL's default paths, so every public-broker handshake was rejected. The relay client and the connectivity-test probe now pin certifi's bundled CA bundle (`certifi` added as a dependency; PyInstaller bundles its `cacert.pem` automatically). Windows / Linux keep their system trust store.
- **Fix "relay_state WS broadcast failed" `AttributeError`** on every internet-sync toggle: `_on_relay_state` called `web_server.broadcast(...)` (no such method); now routes through `web_server.ws_manager.broadcast(...)`.
- **AI-config local listing is now a tree.** A skill's nested files (e.g. `R2-aeon/references/*.md`) no longer dump flat onto the list — folders collapse/expand on click (▸/▾), double-click or the 📂 button opens the folder in the OS file manager, and the search box still returns flat matches.
- **Remove the Quick Paste feature entirely** (never used). Deleted `quickpaste.html`, the tray/hotkey entry (`Ctrl+\``), the `/api/quickpaste/done` route + host instance management, the `page=quickpaste` manifest variant, and all related i18n keys, tests and docs. This also removes the class of bug where a stale `quickpaste.html` popup could appear after a factory reset / restart on Windows.
- **Devices page: chat actions.** Connected/paired device cards gain a 💬 聊天 button and the device right-click menu gains "打开聊天" — starts a chat session with that device and switches to the Chat tab (works for LAN and internet-paired peers).
- **Chat no longer triggers a pairing prompt.** Starting a chat with an unpaired LAN device auto-generated a shared pairing code on the receiving side, flashing a pairing-code notification/card before the chat invite (milliseconds later) cleaned it up — "聊天还会弹配对码，很混乱". The pairing notification is now debounced ~1.2s and the chat invite cancels it before it ever shows; real pairings still notify (the code is stored immediately).
- **Pairing codes pinned to the top of the Devices page.** The 「配对请求」 section now renders at the top (above Connected/Offline/Discovered), and the two codes — the 8-digit pairing code and the SAS fingerprint — sit side by side in prominent badges so both can be compared across devices at a glance.
- **Onboarding guide no longer re-shows on every refresh / app open.** The web wizard's "fresh install" flag was derived from `language_chosen`, which stays False forever when no language is ever explicitly picked — so `__CLIPSYNC_FRESH__` was true on every page load and the guide reappeared constantly. It is now a one-shot marker written at factory reset and consumed once; afterwards the browser's own `clipsync_onboarded` flag governs.

## [1.0.74] — 2026-08-25

- **Tray timed-pause tests skip on headless Linux** (`TestTrayPause` needs a display to build a `pystray.Icon`; CI runners have no X server). CI test gate is now green on Windows / macOS / Linux.

## [1.0.73] — 2026-08-25

- **Fix headless-Linux import crash** (second site): `systray._build_full_menu` also annotated `-> pystray.Menu` eagerly; now lazy, so importing the module on a display-less Linux no longer crashes.
- **AI-config local manager: skill / command folders are now visible.** The local file manager lists each subdirectory (e.g. `~/.claude/skills/my-skill/`, `~/.cursor/commands/search/`) as an openable 📁 folder entry — click it (or the 📂 button) to open the folder in the OS file manager. The peer-inventory exchange still advertises files only.
- **AI-config refresh button no longer wraps to two lines** (`white-space: nowrap`).

## [1.0.72] — 2026-08-25

- **Fix startup crash** (regression from v1.0.71): `Application._create_services` referenced `_history_db` after the dedup-wiring edit dropped its import — the app could not start. Now re-imports `history_db` before `set_max_age_days`; regression test added.
- **Fix headless-Linux import crash**: `systray.py` annotated a method return `-> pystray.Menu` which was evaluated eagerly and blew up when `pystray` is unavailable (headless CI); annotation is now lazy.
- **Fix macOS hotkey tests**: the parse tests asserted Windows `ord()` key codes; they now expect the platform's actual code (macOS Carbon kVK vs ASCII ord elsewhere).
- **Docs / landing page**: the GitHub Pages site now defaults to Chinese (`index.html`), English at `index_en.html`; README (中文 + English) updated with diagnostics, relay connectivity test, AI-config presets / local manager, internet pairing management, chat-over-internet, delivery confirmation & offline queue.
- **Web default language**: the SPA i18n fallback and static `<html lang>` now default to Chinese (matching `config.language` default); the server-injected locale still overrides per the user's configured language.

## [1.0.71] — 2026-08-25

### Code consistency (staged-development cleanup)
- **Single sources of truth** for four formulas that were copy-pasted across layers: the mDNS device-id hash (`internal/transport/ids.peer_id_hash`, used by transport/discovery/dashboard/web), the persisted content-type label table (`dedup.CONTENT_TYPE_LABELS` — the wire label stays protocol-fixed), the history dedup key (`dedup.make_dedup_key`, JSON and SQLite backends now agree, `DEDUP_ALGO` honours the simple/md5 switch in both), and HTML→text preview (`format.strip_html`).
- **Removed the dead `relay_url` field** end-to-end (config, backup schema, both settings UIs, locales) — it was replaced by `relay_brokers` but kept being persisted and editable.
- **Default hotkeys single-sourced** in `config.DEFAULT_HOTKEYS` (the manager re-exports it as `DEFAULT_SHORTCUTS`).
- **Local IP detection converged** onto `discovery.get_all_local_addresses` / `_get_local_address` — the dashboard card and web server no longer run their own (divergent) detection.
- **Timed sync-pause is single-owner**: the web `/api/sync/pause|resume` delegates to the host's one resume timer instead of arming a second one.
- **Web API error shapes unified**: `/api/import` failures now return real 4xx (was 200), history-item and placeholder errors carry `ok: false`, and the settings front-end surfaces the backend reason.
- **Web-companion toggling is instant from the desktop settings** too (previously "restart needed"), via the same host action the dashboard/web-settings use.
- **First-run language picker no longer double-appears** in webview mode (the web SPA's own wizard owns it).
- Renamed the misleading `ai_config.MAX_FILE_SIZE` → `MAX_CONFIG_FILE_SIZE`; removed ~8 dead functions/aliases.
- Diagnostics helper cleanup: the probe path and relay helpers share the platform config dir via `config.config_dir`.

### Web UX
- **Test server connectivity** button in Settings → Network: probes every configured relay broker with a light TCP/TLS handshake (parallel, ~latency per broker, never disturbs the live relay) via the new `POST /api/internetpair/test`.
- **History list sort toggle** (newest / oldest) in the history filter bar.
- **Favorites gain a "push to desktop clipboard" button** (the old copy only wrote the phone's own clipboard).
- **History right-click menu gains "open link in browser"** for link-type items (`/api/nav`, URL-safe).
- **Chat file cards gain "open / show in folder"** for completed inbound files.

### Tests
- **Reorganized the test suite by feature instead of development round**: 59 round-named files → 25 feature-named files (`test_chat`, `test_pairing`, `test_internet_pairing`, `test_aiconfig`, `test_aiconfig_ui`, `test_diagnostics`, `test_history`, `test_relay`, `test_delivery`, `test_devices`, `test_web_server`, `test_web_api`, `test_codec`, `test_clipboard`, `test_sync`, `test_update`, `test_hotkey`, `test_config`, `test_file_transfer`, `test_connection`, `test_chat_api`, `test_chat_ui`, `test_desktop_ui`, `test_integration`, `test_pairing`). Multi-round files were split to their feature homes; shared web-UI scaffolding is suffixed per round to avoid shadowing. Full suite stays at **1246 passed / 4 skipped**.

### AI config
- **Syncing exactly the real AI-tool config files.** The default watch list now covers the actual locations used by Claude Code, Codex, Cursor and Gemini CLI (`.claude/CLAUDE.md`, `.claude/settings.json`, `.claude/skills`, `.codex/config.toml`, `.cursor/rules`, `.cursor/commands`, `.gemini/settings.json`, `.gemini/GEMINI.md`) — files *and* folders are both valid watch entries, so credentials like `auth.json` stay out. The paths dialog gains a ✨ one-click "add common AI config paths".
- **Know which device is newer.** The cross-device config browser marks every remote file with a badge: ✓ same / this device newer / remote device newer / missing here — compared by hash then modified time, with a tooltip showing both sides' size and time, so you can decide the pull direction before anything is overwritten.
- The AI-config tab is now purely the local manager; the cross-device browse/pull moved to the Devices page as its own section.

### Diagnostics
- **Comprehensive diagnostics.** The Diagnostics page is now a set of grouped cards covering every module — system (version/uptime/data dir), network (LAN IP/port/mDNS/web/firewall), internet sync (toggle/relay state/brokers/netpair pairs/pending sends), AI config (watch roots/entries/last collect/trash), chat, transfers, and filesystem (history DB size/disk free) — each item with ok/warn/fail status, detail and a fix hint. The old flat response shape is preserved for the mobile card.

### AI config
- **Local config manager — works with no paired devices.** The Config tab's new "Local config" section is a small file manager over your watch list: browse files (path/size/mtime, searchable, multi-root), preview text, **edit & save** (the original file is auto-backed up as `.bak`), **remove to a trash folder** (never hard-deleted — files land in `aiconfig_trash/` with a timestamp, recoverable manually), **open the containing folder**, and manage the watch paths themselves. Every write is path-locked to the watch roots, size-capped, and refuses binary content. The cross-device browse/pull section stays as before.

### Internet sync
- **Delivery confirmation for relay chat.** Messages sent over the internet relay now carry a per-bubble mark — ✓ delivered / ✗ not delivered / … sending — via an end-to-end ack: the receiver replies on the same paired topic, and the sender updates the bubble. Clipboard items keep working as before; confirmation rides the same ack.
- **Offline queue for internet clipboard.** If the relay or the peer is unreachable at send time, the clip lands in a persisted local queue (`relay_pending.json`) and is retried automatically when the relay comes back online or the peer is seen again (up to 5 attempts). The Devices page shows a small "N pending" badge per internet-paired device plus the last send result.

### Internet sync
- **Chat now works across the internet.** Start a conversation with an internet-paired device even when it isn't on your network — text, typing indicator and session state travel through the relay. Delivery is LAN-first, so a device reachable both ways gets each message exactly once.
- **One device, one card, both worlds.** Chat targets and the devices panel deduplicate LAN + internet entries: a machine on both networks appears once, with a 🌐 badge and (for LAN cards) your alias shown instead of its raw name. The chat picker lists internet-only devices with their live online state.
- Removed the dead "relay URL" field from web settings (the editable broker list is the real setting); un-pairing now broadcasts so every open tab drops the device immediately.
- Integration audit pinned the interactions: dual-path delivery dedupes to one history entry, paused/timed-paused sync halts internet mirroring too, and internet-arrived content follows the identical notification/history path as LAN.

### Internet sync
- **Internet pairing moved into the Devices page** as a first-class device-management section (no longer buried in Settings → Network): a numbered guide, generate/copy/regenerate the pairing code, type-to-pair with inline errors, and a per-device list with **rename-to-alias** (persisted), **online / last-synced status**, and **unpair** (single-sided, LAN pairing unaffected).
- **Internet-presence badges everywhere a device appears**: the local device shows a 🌐 "internet online" badge when the relay is up, and any LAN device that is also internet-paired carries a 🌐 badge (online/offline). One device card, both reachability indicators.
- The relay now **unsubscribes** channels on unpair/refresh — an unpaired peer can no longer keep delivering frames because its topic stayed subscribed.
- Settings → Network keeps just the toggle and live relay status, with a link to pairing management on the Devices page.

### Internet sync
- **Internet pairing code — two devices that have never shared a network can now pair over the internet.** One side generates a 12-character code (`XXXX-XXXX-XXXX`), the other types it in; both derive a private channel from it directly, so the old "must have met on the LAN first" limitation is gone. The code carries a device tag plus a checksum (typos fail loudly), and the real device identity is confirmed over the channel before the pair is stored.
- **The web settings section is now a guided flow**: three numbered steps (switch on / generate a code / enter it), an actionable message instead of a bare relay error ("can't reach the public relay — check your network, or update the app"), and a "pair a device" button whenever internet sync is on with no paired device yet.

### Reliability
- Backups now carry the internet-sync and AI-config settings too (relay brokers, secrets, watch list) — restoring a backup no longer silently drops them.

### Devices & connectivity
- The desktop pairing dialog now shows the same safety code as the web dashboard, so the match-the-code check works whichever interface you pair from.

### AI config sync (new)
- **Browse and migrate AI tool configs across your own devices.** A new "Config" tab shows what each paired device has on its watch list (CLAUDE.md, memory notes, skills, `.mcp.json` — the list is user-editable), letting you preview any file and pull it over, choosing **overwrite / save as a copy / append to Markdown** per pull. Nothing is ever overwritten silently; append only applies to text/Markdown. Inventories carry metadata only (path, hash, size, time) — file content moves only when you request it, over the same encrypted channel you already trust.
- Watch-list edits broadcast to paired devices automatically; inventories refresh on connect and on change.
- Defensive by design: requests are checked against the peer's own inventory, paths are resolved so `../` traversal can't escape a watch root, served content is hash-verified, and oversized (» 1 MB) files are truncated with a clear flag.

### Internet sync (new)
- **Sync across networks — office ↔ home — with zero cost, zero signup, and no extra software.** The new "Internet sync" setting mirrors clipboard traffic over free public MQTT relays (broker list editable, defaults to three well-known free services with automatic failover). Both ends stay end-to-end encrypted: the relay only ever sees ciphertext and an unguessable per-pair topic derived from device secrets exchanged over the encrypted LAN channel.
- **Pairing now shows a safety code.** During pairing confirmation both devices display the same short fingerprint code (e.g. `3A2F-91C4`) — verify they match before confirming, which closes the pairing-code man-in-the-middle window that mattered little on a trusted LAN but matters on the internet.
- Live connection status in web settings (off / connecting / online / error) pushed over WebSocket; the broker list can be edited while running and applies immediately.
- LAN delivery stays primary: internet sync adds a mirror path for paired peers outside the network, and the existing duplicate-merge logic collapses double deliveries.

### Web dashboard
- **Timed sync pause from the web**: the overview quick controls gain ⏸ 15 min / 30 min / 1 hour buttons with a live countdown and instant resume — same persisted deadline as the tray, so it even survives a restart.
- **Settings search box on web**: type to filter setting groups with match counts, Enter jumps to the first hit (mirrors the desktop window, works in Chinese and English).
- **Mobile catches up with the desktop dashboard**: clipboard history gains pin / push-back-to-PC / favorite / delete / clear-all; transfers gain cancel-all and one-tap retry of failed uploads; chat gains mute-per-conversation, close conversation, and 📎 file attachments; settings gains a diagnostics scan card. All touch-friendly via bottom-sheet action menus.
- Fixed: after restoring a backup or picking a language, the first-run wizard didn't appear until a manual refresh — restore now broadcasts the fresh-state flag over WebSocket immediately, and choosing a language in web settings properly records it.

### Chat
- **Typing indicator**: see the other side composing in real time in the web dashboard and on mobile ("typing ●●●"). Reporting is throttled, the state self-clears 4 s after they stop (no background threads), vanishes the moment their message arrives, and old app versions simply ignore it.
- Fixed a double-send race where quickly pressing Enter / tapping send twice created duplicate message bubbles (desktop and mobile).
- On mobile, typing a draft no longer gets wiped when a background refresh re-renders the conversation mid-keystroke.
- Malformed chat payloads (non-string text) are rejected cleanly instead of raising inside the manager.

### Reliability
- **Timed sync pause survives restarts.** Previously a restart landing inside the pause window — e.g. an auto-update reboot — left syncing permanently off with nothing to re-enable it; the deadline is now persisted and re-armed on startup (an already-expired deadline resumes syncing).
- **Factory reset now removes SQLite WAL sidecars and corrupt-file quarantine copies** — previously "deleted" clipboard history could resurrect from the write-ahead log when the database was recreated next to its stale `-wal` file.

### Devices & connectivity
- **One stalled device can no longer delay everyone else.** TLS handshake + identity exchange moved off the single accept thread onto a per-connection thread — previously one hung client could queue every other peer's incoming connection for up to ~25 s ("devices flicker in and out" during simultaneous reconnects).
- **Reconnects no longer give up permanently.** After the fast-retry budget ran out (~3 minutes) the saved address was cleared and the peer was abandoned until an app restart; now it keeps the address and retries once every 30 s, so longer Wi-Fi/router outages self-heal. The reconnect indicator also stops showing attempts past its maximum.
- **Devices behind the same NAT no longer hijack each other's identity** (e.g. phone-hotspot topologies): the incoming hash→device mapping is derived exactly from the announced id instead of guessed from the shared source IP.
- Fixed a socket-handle leak on timed-out handshakes and a hot spin when accept kept failing.

### Transfers
- **Live speed & ETA on active transfers**: progress rows show the real current rate over a rolling window instead of an average diluted by waits — paused/awaiting states show no misleading numbers, and the value reappears within a second of resuming.
- Duplicate/replayed `file_request` messages are now ignored (previously they created orphan `.part` handles Windows couldn't delete, reset the receive window, and popped a second accept dialog).

### Backup
- Backups now include global hotkey bindings — restoring a backup no longer silently resets custom hotkeys to defaults.

### Interface
- **Settings search box**: type a keyword to filter setting groups (match counts per group), highlight matching labels in the open panel, Enter jumps to the first match, Esc clears — works with Chinese and English text alike.
- The history list no longer blanks out on Tk builds that reject double-`destroy()`, quickly closed settings windows no longer throw stray Tcl errors from a lingering timer, and the settings LAN-IP lookup runs off-thread with a short cache instead of stalling the window.

### Tray
- **Pause sync for 15 minutes / 30 minutes / 1 hour from the tray.** While paused, the menu shows a live "paused · ~N min left" line plus an immediate "Resume sync" item; sync resumes automatically (with a notification) when time is up. Any manual toggle — hotkey, tray checkbox, web settings, quick controls — cancels the pending auto-resume so the two never fight.

### Web dashboard
- **Export all favorites** as Markdown (grouped by folder) or plain text from the dashboard Favorites page and the mobile view; the file lands in Downloads and a toast reports count and path.
- Fixed a JavaScript syntax error that left the desktop dashboard Chat page blank since v1.0.54.
- The favorites context menu closes on outside click / Esc again (its lifecycle hooks had been nested inside `methods` where Vue never calls them).
- Upload hardening: a truncated transfer can no longer be stored as a complete file, a missing closing boundary is rejected with a clear message, text files keep their trailing newline, filenames containing `;` or `=` parse correctly, zero-byte files are accepted, and aborted request bodies surface as clean errors instead of socket stack traces.

### Clipboard & history
- **Re-copying the same text no longer downgrades rich content.** Within 90 s of the original capture, a plain-text re-copy refreshes the existing history entry (clipboard formats merged) instead of stacking a duplicate plain-text entry over the rich one; conversely, HTML arriving just after the text upgrades the entry instead of being dropped as a duplicate.
- Notifications now work without a tray icon — Linux falls back to `notify-send` instead of silently dropping them.

### Auto-update
- **P2P-received update packages are verified before install**: SHA-256 against the official GitHub release digest plus a version check — a mismatched or stale package pushed by a peer is dropped with a notification instead of being installed; when release info is unavailable the P2P path refuses and falls back to the official download (which verifies itself).
- **Install reentrancy guard**: a GitHub download and a P2P transfer finishing around the same time install exactly once.
- **"Automatically check for updates" switch** (web settings and desktop About, on by default): turning it off means zero background requests.
- **Manual rollback copy**: Windows/Linux updates keep `<exe>.old` next to the binary for manual restore.
- **Check & install from the web dashboard**: the About panel gains "Check now" (`GET /api/update/check`) and, when a new version exists, "Download & install" (`POST /api/update/install`) through the same verified pipeline as the tray.

### Chat & pairing reliability
- **Failed outgoing texts get a ⟳ Resend button.** A text that failed to send now shows as a red-outlined "not delivered" bubble in the web dashboard and mobile view instead of looking like a normal message; one click re-sends it through the same rate-limited path (`POST /api/chat/resend`). The desktop dashboard matches: failed bubbles carry the same red "not delivered" marker and ⟳ Resend button, with an inline hint when the retry is rejected.
- **Chat invites fail honestly.** An invite the transport refuses no longer leaves a phantom "inviting" conversation pinning a slot for five minutes; accepting over a broken link keeps an honest, retryable state instead of a fake active chat; shutdown no longer leaves sender threads parked for minutes.
- Duplicate or late pairing confirmations no longer demote an already-paired device (no spurious re-confirm prompts); expired pairing requests clean up their pending state.
- Malformed sync frames (non-string message type, NaN/garbage timestamps) are dropped at decode instead of tearing down the whole connection.
- The Windows self-update helper gives up after 5 minutes and deletes itself instead of looping forever when antivirus quarantines the staged file.
- All chat file-transfer completion callbacks now fire outside the manager lock (a slow UI/WS callback can no longer freeze the receive thread), and repeated WS pushes for the same chat message merge fields instead of being dropped as duplicates — so resend status updates actually reach already-open tabs.

### Hotkeys
- **Dead hotkeys are now explained.** Shortcuts that fail to register (combo occupied by another app, invalid combination, two actions bound to the same keys) are reported by name after startup — previously completely silent. Conflicting bindings are rejected at registration time.
- Toggling hotkeys off→on in settings restores them immediately (previously every hotkey silently vanished until restart); re-binding a combo releases the old OS registration instead of leaking it; teardown runs on the listener thread so combos can't stay held by a zombie window; punctuation combos like `Ctrl+/` resolve to the right key on Windows/macOS.

### Interface
- Combo boxes, dropdown menus and scrollbars now follow the app theme in light and dark mode; Firefox dashboard windows no longer pass dead size flags.

### Transfers
- **Failed file transfers are no longer invisible.** Every failure path (disk error, peer offline, timeout, size mismatch, rejection, stale sweep, …) now lands in transfer history with a machine-readable reason and the target device, and failed outbound rows get a ⟳ Retry button in the web dashboard (double-click guarded).
- **Cancel-all**: one click clears every active transfer from the Transfers panel (`POST /api/transfer/cancel-all`).
- Fixed a sender-thread crash when a paused receiver resumes and late chunk retransmits hit a closed file handle (transfer stuck at "finalizing" until timeout).

### Devices & connectivity
- **Offline devices show reconnect progress** ("Reconnecting N/M") in the device panel and desktop dashboard instead of a bare offline state, across REST snapshots and WS broadcasts.
- **Network switches self-heal**: the mDNS advertisement is rebuilt when the local address set changes (Wi-Fi ↔ wired / subnet change) — previously peers couldn't find this device until an app restart.

### Clipboard & history
- **Sync as plain text only** (new setting): strips rich formatting (HTML/RTF) from synced clips on both the sending and receiving side — other devices always paste plain text, while this machine's clipboard and history keep full fidelity. Images and file lists are content and still sync.
- **History retention by age**: new "keep history for N days" setting (0 = unlimited); unpinned rows older than that are pruned automatically after startup and each capture.
- Fixed Linux captures silently missing non-plain-text clipboards (RTF / file-list / URL-only): the monitor hashed through methods it didn't have, so only the first such copy was ever detected.
- JSON/CSV import no longer rewrites the whole database per row (O(n²) on large imports); imports keep original order, protect pinned items, and round-trip `source_app`/`source_title`.
- **Markdown export** joins JSON/CSV in Data Management (grouped by day, atomic write). CSV exports gain `time_iso`/`source_app`/`source_title`/`byte_size` columns; old CSVs still import.
- Exports/imports round-trip `source_app`/`source_title`; imports keep original order and protect pinned items from over-limit trimming.
- Local clipboard capture no longer silently drops items when another app holds the clipboard briefly (read-side retry budget now matches write side).
- A single corrupted `types` JSON row no longer aborts loading the whole history; clearing history now reclaims disk space (`VACUUM`).
- Batch pin/delete tolerates string vs numeric ids from web clients (previously silent no-ops).
- **RTF payloads are scanned for secrets too.** Card numbers, tokens or e-mails hidden in an RTF body were invisible to the TEXT/HTML-only sensitive-data scan and rode to peers unredacted; a matching RTF is now dropped instead, and an RTF-only clip still syncs as redacted plain text.

### Reliability
- Desktop settings window reaches parity with the web UI: "history retention days" input, plain-text-only switch, Markdown in the dashboard export dropdown (JSON/CSV/Markdown), failed transfer rows show their reason with a ⟳ Retry button, and the tray shows the live connected-device count.
- Source-app tracking fixes: leaked file handle on Linux; truncated window titles on macOS when the title contains ", ".
- P2P update install no longer runs inside the network receive thread (half-finished updates after `sys.exit` in a worker thread); exit now marshals through the Tk main loop so shutdown hooks run.
- A transient exception can no longer permanently kill the peers-status daemon loop (tray/device state froze on first error).
- Conflicted global hotkeys clean up their dead mappings and are reported once at startup instead of silently never firing.
- Overview "Transfers" card always showed 0 (wrong stats field); dashboard edge-snapping ran twice per resize event.

### Web UI
- **Stackable toasts** (up to 4, with leave animations) replace overlapping notifications; pairing-reject and chat send failures surface errors instead of being swallowed.
- **Keyboard navigation in history list**: ↑/↓ move, Enter copies the full item (rich paste incl. images), Del deletes; multi-select push-to-desktop now fetches full text instead of pushing truncated previews.

## [1.0.50] — 2026-08-23

### Desktop (Windows)
- **No more black console box flashing on startup.** The packaged Windows build (a `console=False` one-file exe) still spawned the console-mode `powershell.exe` while enumerating network-interface priorities at launch, and `taskkill.exe` when tearing down the web window / Quick Paste popups — each spawn briefly flashed a black console window before the app opened. Those spawns now pass `CREATE_NO_WINDOW`, matching the existing `netsh` firewall calls, so the exe stays fully silent on startup and shutdown.

## [1.0.52] — 2026-08-23

### Chat & devices
- **Unified device/session state.** Devices are reported through one deduplicated view (`get_device_states`) with explicit paired/pairing/connected state, keyed by canonical device id — so a device no longer appears twice, and "pairing" is no longer shown as "connected".
- **Chat conversations no longer vanish.** A session id adopted to a new id (mutual invite / peer restart) no longer blanks the conversation; the UI falls back to the peer id.

### Clipboard & filtering
- **Windows clipboard robustness.** Retry `OpenClipboard` on the read side, and fix the ANSI file-list reader reading one byte out of bounds.
- **Single-character copies are no longer dropped** (only whitespace-only noise is skipped).
- **Redaction is less aggressive.** Credit-card digits now require a Luhn checksum, and bare `secret`/`token`/`key` are word-bounded.
- **Clipboard from an unknown app is allowed** (the app-filter whitelist no longer blocks it).
- `source_title`/`source_app` are now encrypted at rest.

## [1.0.51] — 2026-08-23

### Auto-update
- **One-click and LAN-distributed updates.** Periodic version check, download with SHA-256 verification, and per-platform install (Windows self-replace, Linux in-place, macOS hand-off). Devices cache the installer and serve it to lagging peers over the LAN via mDNS version advertisement + a chunked `kind="update"` transfer, falling back to GitHub.

### Security
- **Pairing requires typing the code** (no longer a single "match" click), the TLS certificate is bound to the app-layer identity certificate, and encryption labels no longer overstate at-rest protection when no password is set.

### Desktop
- **Startup no longer races the one-file temp directory on the first codec import.** The single-instance lock is now claimed at the very start of `main()` instead of after `_start_services()`, closing the window where two launches could both pass the stale-lock check and race their `_MEI` extraction dirs. Common text codecs — including the locale default (`cp936`/`gbk` on zh-CN Windows) and the stdio codecs — are pre-loaded up front, so a later `write_text(encoding="ascii")` can never trigger a lazy `encodings.*` import from a deleted `base_library.zip`.

### Updater & release
- **Downloads are checksum-verified.** The release downloader now verifies the SHA-256 (from the GitHub asset `digest`) in addition to size, so a corrupted or tampered asset is rejected before it is offered as an installer.
- **macOS ships Apple Silicon only.** The retired Intel (`macos-13`) build leg is removed; the release asset and download page are `clipsync-macos-arm64.zip` only, while Linux keeps x86_64 and ARM64.

### Clipboard
- **Linux now detects non-text clipboard changes.** The listener only hashed plain text or an image, so HTML-only / RTF-only / file-list / URL copies were silently never synced. It now probes all those formats.
- **Pasting a multi-file entry on macOS no longer drops all but the last file.** The atomic ctypes write overwrote the same pasteboard type per file; multi-file entries now fall through to the `writeObjects:` path that appends every file.
- **Restoring an image from history keeps its format.** The desktop paste path dropped `image_fmt`, so non-PNG images (TIFF/BMP) were re-encoded as PNG and corrupted.

### Sync & transfer
- **Cancelling a transfer now actually completes it.** The completion callback fired after the transfer was removed, so it never fired — leaking the temp zip and leaving the web panel stale.
- **Clipboard file transfers now verify the sender.** Inbound transfers never recorded their peer, so the "reject bytes from a non-owner" guard was dead code.
- **Dedup respects its TTL and clears on restore.** The local send path never TTL-pruned (its `in` check was a no-op against the tuple ring), so a repeated copy after the window was still suppressed, and a history restore within 90 s was dropped.

### Transport
- **Connection health now detects clean remote close.** The EOF probe used `MSG_PEEK`, which `SSLSocket` rejects, so a cleanly-closed peer looked alive and could win the reconnect race over a fresh connection. A recv-loop flag now reports it correctly.

## [1.0.49] — 2026-08-23

### Mobile companion (phone page)
- **The phone page grows from 3 to 7 tabs** (scrollable tab bar) — it is no longer just history/send/files:
  - **💬 聊天**: a remote UI for the desktop's nearby-chat — browse devices, start conversations, exchange text messages, accept/decline incoming files, all live-polled every 5 s just like the other tabs.
  - **📤 传输管理**: watch the desktop's active P2P transfers with progress/speed/ETA, accept or reject incoming requests right from the phone (new `POST /api/transfer/accept|reject`), and cancel/pause/resume; completed transfers list with specific failure reasons.
  - **⭐ 收藏**: browse desktop favorites, tap to copy one to the desktop clipboard, or delete it.
  - **⚙ 设置**: device name, web address, language, a dark/light theme toggle for the page, and a link to the full web dashboard. `/api/status` now reports the app version too.
- **Desktop**: the phone-companion QR dialog gains a **「发送文件到手机」** button — pick a local file and it lands in the phone-visible folder, so the phone can download it from its Files tab (~5 s) without pairing.

## [1.0.48] — 2026-08-23

### Desktop
- **Factory reset / restart no longer crashes.** In a packaged (PyInstaller one-file) build the reset path spawns a fresh instance and exits; the two processes' `_MEI` temp-directory cleanups could race and delete a live extraction, failing on the next lazy import (`base_library.zip` not found) — seen right after choosing the language in the first-run dialog. The spawned instance now extracts into a private temp directory, isolated from the exiting process's cleanup.

## [1.0.47] — 2026-08-23

### Web & mobile
- **Phone companion & Quick Paste pages no longer render blank.** The server's HTML interpolation globally replaces the `__CLIPSYNC_I18N_LOCALE__` placeholder with the locale string, but those two pages used the placeholder as a JavaScript property name (`window.__CLIPSYNC_I18N_LOCALE__`), which produced `window."zh-CN"` — a SyntaxError that aborted the page's inline script and left only the static title. They now use the non-interpolated `__I18N_LOCALE__` alias for the property (matching `index.html`), so the locale value lands without breaking the script.
- **All phone-scan QR entry points now open the phone companion page** (`mobile.html`: history / send / files) instead of the full desktop dashboard — the desktop Overview card, the Settings web panel, the tray "Web QR" dialog and the web UI's QR cards are now consistent.

## [1.0.46] — 2026-08-23

### History (cursor convention — no remaining drift)
- **The calibration branches now route through the shared cursor helper too** — the throttle, raced-abandon and failure paths of `calibrateHistory` no longer hand-write the offset/hasMore formula, so a future change to the convention can't silently miss three sites.
- **The phone page carries its own copy of the same convention** (`_setHistCursor`), used by the calibration write-back and the throttle pin, keeping the visible-length cursor rule in step with the desktop helper (mobile has no shared store object).
- **The cursor-convention regression test now covers the dashboard's Load More handler** — the original `offset + items.length` bug site — and asserts no calibration branch re-introduces the hand-written formula.

## [1.0.45] — 2026-08-23

### History (cursor convention — single source)
- **The load-more cursor now has one implementation.** A new `store.setHistoryCursor(total)` aligns the cursor with the visible list (offset = length, pinned to `total`, `hasMore` = length < total), and the desktop's hand-inlined sites — page-1 reload, WebSocket wholesale + paged paths, ghost calibration write-back, and the dashboard's Load More — route through it. The phone page keeps its own local copy of the same convention (it has no shared store object). The raw-vs-visible formula can no longer drift across sites (it flip-flopped between v1.0.43 and v1.0.44 for exactly that reason).

### Tests
- Cursor-convention guard updated to assert the shared helper. Full suite 433 passed / 3 skipped.

## [1.0.44] — 2026-08-23

### History (cursor convention unified)
- **Every wholesale replace now sets the load-more cursor from the visible (post-null-filter) list length** — the invariant every cursor-shrink path (delete, clear, calibration) depends on. The page-1 reload, WebSocket wholesale path, and calibration write-back all agree; a filtered null slot (if one ever arrives from a future serializer) is simply re-requested once and deduped.
- `_rowDiffer`'s doc now accurately describes the two-directional compare (stronger than the merge path's one-directional check) and warns against storing client-only fields on history rows.

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.43] — 2026-08-23

### History (null safety & change detection)
- **`_rowDiffer` now compares every key** (for-in, like the merge path) instead of a hard-coded field list — a new field added by the server can't silently fall outside the reconcile guard.
- **`replaceHistory` skips the rebuild when the snapshot is unchanged** — no more reactive list churn on identical reconnects; it still bumps the guard only on real changes.
- **Every load-more / calibration append now skips malformed null rows** — desktop load-more, the WebSocket wholesale path's cursor, and the phone calibration write-back all guard nulls, and the cursor uses the raw (post-null) length so a filtered null slot is never re-fetched.
- The `removeHistoryItems` docstring is back where it belongs (it had been orphaned onto the compare helper).

### Tests
- The wholesale change-detection guard is strengthened to assert the helper's actual behavior (no bump on unchanged, bump + rebuild on change). Full suite 433 passed / 3 skipped.

## [1.0.42] — 2026-08-23

### History (write-path consolidation)
- **Every history wholesale-replace now flows through one shared `store.replaceHistory`** — the page-1 reload, the WebSocket `history_updated` path, and the ghost-calibration write-back all share it. It filters malformed null rows (a null could never reach the renderer again, on ANY path), detects change across every user-visible field (key-order independent), and bumps the reconcile guard only for real changes.
- The WebSocket wholesale path's hand-rolled change-detection loop is gone (subsumed by the helper), removing the duplicated logic and the gaps where a null row slipped through.

### Tests
- Source-guard regressions updated to the consolidated structure. Full suite 433 passed / 3 skipped.

## [1.0.41] — 2026-08-23

### History (reconcile — change detection)
- **The page-1 reload change detection compares explicit fields** instead of `JSON.stringify` (which is key-order sensitive — rows merged in-place by `mergeHistoryFresh` can carry extra keys and would false-positive on every reconnect, aborting a calibration and burning its budget). Any of the user-visible fields (paste_count, timestamp, source metadata, pin, preview) still counts as a change.
- **A malformed null row is skipped, never stored** (mirroring the WebSocket merge path) — a null in the response no longer crashes the history renderer.

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.40] — 2026-08-23

### History (reconcile — change detection)
- **The page-1 reload's change detection is now full-field** (mirroring the WebSocket merge path): a reconnect that changes paste_count, timestamp, or source metadata — not just pin/preview — bumps the reconcile guard, so an in-flight calibration can't write back stale data over fresher rows. A malformed (null) row in the response is treated as a change instead of throwing.
- The store module header's description of the calibration timeout now matches the code and tests (it unwedges the lock without advancing the generation).

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.39] — 2026-08-23

### History (pin helpers — polish)
- **The page-1 wholesale reload only bumps the reconcile guard when the snapshot actually changed** — a reconnect that applied an identical list no longer invalidates an in-flight calibration or burns its throttle budget (aligned with the WebSocket merge path).
- `setPinned` / `setPinnedBatch` get proper docs, and batch-pin now uses a hash-set membership test (O(n+m)) with a null/empty guard, matching `removeHistoryItems`.
- The stale test docstring describing the calibration timeout as advancing the generation now matches the corrected contract.

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.38] — 2026-08-23

### History (pin reconcile — consolidated)
- **Every pin path now goes through one place.** New `store.setPinned` / `setPinnedBatch` helpers re-find rows by id, apply the flag, and bump the reconcile guard **unconditionally** — the server committed a change, so an in-flight calibration's pre-change snapshot must never write back, even when the toggled row left the loaded window during the round-trip (the v1.0.37 gap).
- Single-pin (history item + context menu), batch-pin, and the page-1 wholesale reload on reconnect all route through the guard, so no pin path can silently re-open the calibration race.
- Stale calibration comments corrected (the timeout still does not advance the generation).

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.37] — 2026-08-23

### History (pin/delete race fixes)
- **Toggling pin no longer writes to the wrong row.** A pinned item jumps to the top when the server broadcast's wholesale replace lands before the HTTP response — the response now re-finds the row by `entry_id` instead of using a stale captured index, so an unrelated item can't silently get pinned.
- **The reconcile guard now covers every pin path**: single-pin (history item + context menu) and batch-pin all bump the mutation tick, so an in-flight calibration can't revert a just-applied pin.
- **Batch delete shrinks the pagination cursor by the rows actually removed**, not the pre-confirm selection size — a WS broadcast that already removed (and shrunk) the rows no longer over-shrinks the cursor.
- Dead index computations removed from the context-menu delete and pin paths; the calibration comment block now matches the timeout contract.

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.36] — 2026-08-23

### History (reconcile)
- **Pin toggles now bump the reconcile guard.** An in-flight calibration could write back a stale snapshot that reverted a pin the user had just toggled (same-id pin changes don't always reach the broadcast comparator). Toggling pin marks the list mutated, so the calibration abandons its write-back.
- **The calibration docblock now matches the code** (the timeout is the one terminal state that does not advance the generation; stale comments no longer claim it does).
- **The context-menu delete is fully consolidated**: it shrinks the load-more cursor by the number of rows actually removed (not a stale pre-confirm index) and relies on the shared helper to prune the selection.

### Quick Paste
- **A mid-removal `FileNotFoundError` is no longer mistaken for "already cleaned"** — the entry is kept for the sweep to finish, while a genuinely-absent profile is still treated as cleaned.

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.35] — 2026-08-23

### History (reconcile)
- **A slow-but-valid reconcile response is no longer discarded.** v1.0.34's 16s timeout also advanced the generation counter, so any fetch slower than the timeout was thrown away — on a slow link the ghosts could never heal. The timeout now only unwedges the lock and consumes the back-off budget; a response that settles later still passes the generation guard and writes back (staleness vs. newer data remains the mutation tick's job).
- **The last hand-written history delete now goes through the consolidated helper** (context-menu's splice), and it shrinks the "load more" cursor like every other delete path — no more skipping the item that shifts into a deleted slot.

### Quick Paste
- **A missing profile directory is treated as already-cleaned** (no spurious "incomplete cleanup" keep-entry when an external temp cleaner already removed it).
- **Shutdown no longer promises a retry it can't keep** — the instance dict dies with the process, so a profile that can't be removed at shutdown is dropped with a visible path warning (the OS temp cleaner will reclaim it), and the retry sleep is only between attempts.

### Tests
- Calibration-semantics regression updated to the corrected timeout contract. Full suite 433 passed / 3 skipped.

## [1.0.34] — 2026-08-23

### History (pagination reconcile — consolidated)
- **The mutation contract is now one place.** All history mutations flow through `store.removeHistoryItems` / `store.clearHistory` / `store.mergeHistoryFresh`, which bump the reconcile guard centrally — a future delete/clear path can no longer silently re-open the ghost-resurrection race.
- **The reconcile back-off is consistent**: every terminal state (success, failure, timeout, race-abandon) consumes the throttle budget and advances the generation counter, so a busy clip stream can't trigger an unlimited full-history download, and a timed-out calibration's late response can never write back.
- **In-place content updates (pin, edits) on the first page now count as mutations** too, so a reconcile can't revert a just-applied change.

### Quick Paste
- **The `--app` window paste path is guarded as well** — a successful paste sets the pasted flag before the auto-close block, so the retry-exhaustion banner never covers a confirmed paste.
- **The abandoned-profile sweep never evicts a live popup** (process check first), keeps a partially-removed profile for a bounded retry (3 attempts), and the done-path only forgets an instance after its profile is actually removed — no more permanent partial-profile leaks or unbounded zombie entries.
- App-shutdown profile reclaim retries once and hands the rest to the next startup.

### Tests
- 10 new/updated regressions. Full suite 433 passed / 3 skipped.

## [1.0.33] — 2026-08-23

### History (pagination reconcile)
- **The ghost-calibration throttle only counts successful reconciles** — a fetch aborted by a racing deletion no longer blocks the next attempt for 30s.
- **The mutation guard covers every delete path**, not just the WebSocket broadcasts: the dashboard's own delete/clear/batch-delete now bump the mutation tick, and a `history_updated` merge that actually adds data bumps it too. An in-flight reconcile can no longer resurrect a locally-deleted row or clobber a just-arrived clip.
- **The reconcile lock can't wedge forever** — a fetch that never settles (old WebView without AbortController) is unwedged by a 16s fallback timer, and a superseded fetch can neither clear a newer calibration's lock nor write back.
- **The phone's reconcile is throttled like the dashboard** (30s), so a failing calibration no longer re-downloads the entire history every 5-second poll.

### Quick Paste
- **A successful paste is never overwritten by the close-retry banner** — if the done POST keeps failing, an already-pasted popup gets a light toast, not a "could not auto-close" screen replacing the confirmation.
- **An empty instance id is treated as "missing"** (accepted, no-op) instead of a 400, matching the intended legacy-client behaviour.
- **App shutdown closes popups in parallel** (one shared wait round instead of serial per-popup timeouts), and the abandoned-profile sweep keeps an entry for a later retry when a lingering child still holds profile locks.

### Tests
- 9 new regressions. Full suite 428 passed / 3 skipped.

## [1.0.32] — 2026-08-23

### Quick Paste
- **The popup closes on every platform now.** v1.0.31 stored the instance id as an int but the page sent it as a JSON string, so the close signal never matched and no popup ever closed. The id is now normalised to an int on the way in, invalid ids are rejected with a 400 (never a 500), and the regression test posts the real string form.
- **Closing a popup on macOS/Linux no longer kills the whole app.** The Chromium child was launched in the app's own process group, so the group-signal teardown signalled ClipSync itself; it now starts in its own session.
- **Abandoned popups are cleaned up** — dead instances and their temp profiles are swept before each new open and on app shutdown.
- **A done signal with no/unknown instance is a no-op**, never "close the most recent popup" (a stray POST could previously kill a popup it didn't come from).
- **Pasted-state confirmation survives a failed done POST** — the safety net retries up to 3×, then shows a persistent "close this window manually" notice instead of silently leaking.

### History
- **Ghost calibration can't resurrect deleted rows.** A deletion/clear that lands while the reconcile fetch is in flight is now respected (a mutation tick guards the write-back), the reconcile is throttled to once per 30s, and the dashboard + phone share one implementation instead of three drifting copies.
- The phone's reconcile failure path now pins the cursor and re-renders like the dashboard.

### Tests
- 6 new regressions (string-id close, invalid-id 400, process-group isolation, instance sweep/cleanup, None no-op). Full suite 419 passed / 3 skipped.

## [1.0.31] — 2026-08-23

### Quick Paste
- **The popup close mechanism actually works now.** The v1.0.30 Chromium `--app` launch handed the URL off to an already-running browser instance (the spawned process exited, so closing it was a no-op) — and with no browser running, closing killed the whole browser. Each popup now launches its own private Chromium instance (`--user-data-dir=<temp>`), tracks it by an instance id, and closes only that instance's process tree (`taskkill /T /F` / `killpg`), cleaning up its profile. The done signal (paste, Esc, X, and a 60s safety net) carries the instance id, so an abandoned popup can never close a different one.
- **A pasted Quick Paste becomes a terminal state** — the confirmation can't be replaced by a re-pastable live list.
- The done callback is wired through the normal dispatch parameters instead of a module-level slot, so a stale registration can't survive an app restart.

### History
- **Ghost entries no longer evict live ones.** The previous `total`-trim assumed a deleted entry is always the oldest row, but history is pinned-first ordered — trimming the tail could drop a live clip while the ghost stayed. When the loaded list exceeds the server `total`, clients now fetch the authoritative list and replace wholesale, then recompute the cursor.
- **Phone deletions heal again.** With no WebSocket on the phone, a deletion beyond the first page is now caught by the 5-second poll (which triggers the same authoritative reconcile when `total < loaded`).

### Misc
- Chat file-offer expiry is surfaced on the desktop too (the accept path returns the `None` sentinel, and the dashboard shows "request expired" instead of silently doing nothing).
- `postDone` uses the page's own timeout helper instead of a bare fetch.

### Tests
- 10 new / 6 updated regressions. Full suite 413 passed / 3 skipped.

## [1.0.30] — 2026-08-23

### Quick Paste
- **The popup can actually close itself now.** Browser tabs opened with `webbrowser.open_new` can't be closed by script, so the v1.0.29 affordances were still dead on desktop. The host now launches Quick Paste in a Chromium `--app` window when one is available, and the page signals a paste via `POST /api/quickpaste/done` so the app closes the window for real (X / Esc also work there). Without Chromium it falls back to a plain tab, which degrades to a "✓ Pasted — close this tab" state instead of pretending.
- **Keyboard shortcuts are back on plain desktop tabs.** Listbox focus, 1-9 / Arrow / Enter, and the "Press 1-9 to paste" hint are gated on touch *hardware* (via `pointer: coarse`) rather than whether the page was script-opened — so a keyboard user who bookmarked the page keeps working keys, while auto-close behaviour stays tied to the app-opened window.

### History (pagination)
- **No more 5-second list collapse on the phone.** The background poll prunes only when you haven't scrolled past the first page; once you've loaded more, it merges without pruning, and the server's `total` is used to trim genuinely-deleted ghost rows at the tail.
- **Desktop cursor is calibrated against `total`.** If the WebSocket missed a deletion broadcast, the loaded list can hold ghost rows that inflated the pagination cursor — "load more" would skip live entries. The page-1 merge now trims past `total` before recomputing the offset.

### Misc
- Accepting a chat file offer that just expired now shows "request expired" instead of a generic failure (the offer state is popped under the lock, so a racing accept can't observe a half-dead offer).
- Removed dead `peer_completed` bookkeeping in the chat file sender.

### Tests
- 9 new regressions. Full suite 402 passed / 3 skipped.

## [1.0.29] — 2026-08-23

### Quick Paste
- **Popup closing is keyed to how the page was opened, not the hardware.** The v1.0.28 fix gated close behaviour on `IS_TOUCH`, which is true on any touch-capable laptop even when using a mouse — so the popup still refused to close there. The host now opens Quick Paste with `?auto_close=1`, and auto-close / Esc / the X button are enabled only for script-opened popups. A plain browser tab (bookmark, copied link) keeps the X hidden instead of showing a button that can't close.

### History (pagination consistency)
- **Live-inserted entries no longer skip history.** The merge path advanced the pagination cursor by the number of freshly-inserted rows, which overruns the real position when de-duplication drops duplicates — later "load more" pages then silently skipped entries. The cursor is now recomputed from the list length (dashboard and phone).
- **Phone background polling prunes deleted entries.** Once a phone had scrolled past the first page, the merge kept every row that wasn't in the fresh snapshot — deleted clips lingered as ghosts forever. The page-1 poll now reconciles (removes missing ids, empties on clear).

### Misc
- Reverted an incomplete "unconfirmed delivery" chat-file status that nothing rendered; file sends report plain success again.
- `DELETE /api/files` matches filenames exactly (no trimming), so a name with leading/trailing spaces can't delete a different file or become undeletable.
- `web_history_limit` help text now says 1–500 (matching the accepted range and the 30 default) instead of 1–20.
- The chat stale-receive sweeper fires its callbacks outside the lock, like its sibling sweeper, so a slow WebSocket can't freeze the chat state.
- Phone delete-file and send share one error-handling helper (consistent 403 → re-scan-QR messaging).

### Tests
- New regressions: auto-close URL, cursor recompute, mobile prune, exact-filename delete, chat-file success status, deferred lock-out callbacks. Full suite 392 passed / 3 skipped.

## [1.0.28] — 2026-08-23

### Quick Paste
- **The desktop popup actually closes again.** The inline close handler referenced an IIFE-local function (a `ReferenceError`), and every close path was gated on `window.opener` — which is `null` for pages opened via `webbrowser.open`. The button is now bound via `addEventListener`, and closing (auto-after-paste, Esc, and the X button, which is visible on desktop) is gated on touch input instead. Paste-then-walk-away popups work as intended.
- The paste push now uses the same timeout as the history fetch, so a hung server can't leave the "pasting…" state forever.

### Phone companion (mobile.html)
- **History has pagination** — scroll to the bottom loads more (offset-based, de-duplicated), and background refreshes merge into the already-loaded pages instead of collapsing them back to the first 30.
- **Files can be deleted from the phone** — new `DELETE /api/files` endpoint (path-confined to the receive dir) plus a delete button with confirmation.
- **Token expiry is never silent**: the send path and the background polling both surface "re-scan the QR code" when a 403 comes back (throttled so polling doesn't spam).
- Upload pre-check leaves room for multipart overhead, matching the server's `Content-Length` limit.

### Dashboard
- **Startup no longer loads everything twice** — the WS `connected` event is the single load entry point (with a fallback timer in case the socket never opens).
- **WS reconnects keep your pagination** — history that was "load more"-ed is merged, not replaced, so a network blip doesn't drop you back to page 1.
- Overview fetches are de-duplicated in flight (5s timer + WS-triggered refresh share one request).

### Tests
- New regressions for the file-delete endpoint (path traversal, absolute paths, missing/deleted, reject-directory) and the Quick Paste / pagination guards. Full suite 386 passed / 3 skipped.

## [1.0.27] — 2026-08-23

### Web companion (convergence round)
- **Concurrent dialogs no longer overwrite each other.** Two server-pushed dialogs arriving together (a file transfer request next to a pairing prompt) used to fight over one slot — the first silently timed out after 2 minutes. Dialogs now queue client-side and pop one at a time.
- **Delete / pin / clear now reach every web client.** A history deletion, pin, or clear performed on one client (dashboard, phone) previously left other connected clients showing stale rows until a manual refresh. New `history_item_deleted` / `history_clear` WebSocket events keep every client in sync.
- **WebSocket heartbeats.** The server now pings clients and drops any that have been silent for ~90s, so a phone that went to sleep or lost its network no longer leaves a zombie "connected" entry inflating the device list and eating a slot.

### Web & API polish
- History pagination offset tracks live-inserted entries (no more drifting "load more" cursor after real-time pushes).
- Pin toggles surface failures; details view shows `entry_id` 0 correctly (no more "N/A" on the first clip).
- `AbortController` feature-detected (no crash on ancient WebViews); `uploadFile` gets the same timeout as other API calls; an abort during response parsing is reported as a timeout, not a phantom "HTTP 200".
- Dialog delivery is judged by actual sends, and a dialog queued while no client was attached gets a full response window from the moment it appears.
- Overview refresh is debounced (500 ms) — rapid copying no longer fires a full HTTP overview fetch per keystroke.

### Desktop
- **Windows webview shutdown cleans up child processes** (`taskkill /T /F`) instead of leaving GPU/renderer stragglers behind.
- First-run onboarding Tab/Shift+Tab navigation works even when focus is inside a card's label.

### Tests
- 14 new web regressions (dialog queue, delete/pin/clear broadcast, WS heartbeat + stale-client drop, dialog delivery/queue timing). Full suite 374 passed / 3 skipped.

## [1.0.26] — 2026-08-23

### Security
- **Clipboard file-transfer chunks now verify their sender.** `file_chunk` frames pass the unpaired-peer gate (chat file bytes ride them), so a chunk that doesn't match the transfer's peer is dropped — an unpaired or newly-unpaired device that learned a transfer_id can no longer inject bytes into a clipboard download it doesn't own.

### Web chat (first audit round)
- **Failed text sends are no longer swallowed.** A send rejected by the backend (peer offline, flood control) now keeps the draft and shows "send failed" instead of silently clearing the composer while the peer never receives anything.
- **Changing the file-receive directory no longer breaks chat-file downloads** — the chat manager's receive dir is updated too, and downloads confine against the chat dir (with a fallback to the web upload roots for older files).
- **No more per-message full reload or unread-badge flash** — an incoming message no longer triggers a redundant REST refetch while you're looking at the conversation.
- **File cards show real terminal states** (declined / failed / cancelled) instead of "Preparing…" or a false "Sent" — the wire statuses map to labels, and a sender whose receiver rejects the file marks it declined, not done.
- **Fast session switching can't mix conversations** — a stale message-list response is ignored if the active session changed.
- **Inviting an unreachable / rate-limited device gives feedback** — the API distinguishes "connecting in background" from "can't connect", and the UI shows the timeout error instead of a 2-second "Connecting…" with nothing after.
- **Chat file sends no longer trigger a "received file" notification, sound, or a Files entry** — chat uploads go to a staging area via `purpose=chat` and are cleaned up if the send fails.
- **Accept / decline / close / file actions surface failures** instead of silently doing nothing.
- **Closed sessions read as "Closed"**, not "Offline"; **incoming invites raise the unread badge** so a chat request isn't missed until you open the panel.

### Phone pages & dashboard
- **Phone companion pages are installable PWAs** — `mobile.html` and `quickpaste.html` ship a manifest (`?page=` variant, distinct app identity) and register the service worker, so "Add to Home Screen" yields a standalone, offline-capable app like the dashboard.
- **Chat sidebar unread badge memoized** — no redundant Tk repaints every fast refresh tick.

### Tests
- Web chat regressions added (send-failure, receive-dir, invite feedback, chat-upload staging + cleanup). Full suite 360 passed / 3 skipped.

## [1.0.25] — 2026-08-23

### Security & transport (regression round)
- **The anonymous-connection gate actually fires now.** v1.0.24's check matched `device_id.startswith("__anon__")`, but anonymous connections carry `device_id = "unknown"` (the `__anon__{ip}:{port}` string was only the `_peers` dict key) — the gate was dead code and chat-invite floods from fresh TLS connections could still reach the UI. Anonymous connections now carry an explicit `is_anonymous` flag that the transport gate honours.
- **A torn rejection marker is treated as a rejection** instead of being discarded (which sent the client into an invalid-frame → reconnect loop on congested LANs).
- **A rejected peer is no longer added to the inbound-reject set** — a forgotten device can still reach you again after the user re-pairs.

### Nearby chat
- **Session-id adoption migrates the text-rate buckets** — adopting a peer's new session id no longer orphans the old buckets (memory) or resets the flood budget (abuse).
- **`_file_sender` can't raise `NameError`** when the stale-transfer sweeper pops the send state while a blocked `sendall` returns — sid/tid are bound up front.
- **Multi-line messages are preserved**: the incoming-text sanitizer strips control/bidi characters but keeps `\n`/`\t` (the sender's transcript and receiver's view no longer diverge).
- **Accepting a file rolls back its receive state if the accept frame can't be sent** — no more "Receiving…" stuck for minutes with an open temp-file handle after the peer vanished.
- **The per-peer send_fn cache evicts the least-recently-used entry**, not merely the oldest-inserted one.

### Desktop
- **macOS autostart toggle reads the key the app actually writes** (`ProgramArguments`, not `Program`) — the "enabled at login" switch no longer always shows Off. It still verifies the binary exists.
- **The chat unread badge is only cleared while the chat panel is on screen** — switching to another panel no longer silently zeroes incoming-message indicators (which the dashboard-visible notification suppression would otherwise swallow).

### Tests
- Updated to match intended semantics (stall-sweep test uses a working accept send_fn; accept-rollback is the new contract). Full suite 351 passed / 3 skipped.

## [1.0.24] — 2026-08-23

### Nearby chat (deep-audit round)
- **Chat sends actually work again.** The transport's `send_to_peer`/`broadcast` returned `None` (fire-and-forget), which the chat layer read as "nothing delivered" — every text message showed as failed and file transfers could not start at all. The transport now returns a real bool: delivered vs. peer-not-connected vs. send-failed, so nothing is falsely marked sent and real failures are surfaced.
- **Session ids stay in sync after a peer restarts its session** — a re-invite carrying a new session id is adopted instead of leaving both sides "active" with mismatched ids and silently dropping every later message.
- **Anonymous connections can no longer spam chat invites.** The transport gate drops all application frames from identity-less `__anon__` connections, closing the flood-by-new-TLS-connection dialog/notification DoS (each fresh connection used to get a fresh invite-rate budget).
- **Incoming chat text is sanitized** (control / bidi-override characters stripped) before it reaches OS notifications and session previews — the invite-string sanitization now covers the message body too.
- **Rate-limit bookkeeping no longer grows without bound**; text flood control is now per-direction (a peer flooding in no longer halves your send budget) and failed sends roll their quota back.
- **Stalled file transfers are swept after 10 minutes** — the state entry and the half-written `.part` temp file are cleaned up; offline detection no longer kills a session mid-transfer on a slow (TCP-retransmit) link.

### Platform & tooling
- **Updater retries transient network failures** (3 attempts with backoff) instead of failing on one blip.
- **`notify-send` failures are logged** (return code checked, stderr captured) — honouring the v1.0.21 "notification failures are logged" contract.
- **macOS autostart toggle verifies the plist still points at a real binary** — a relocated portable app no longer shows a dead "enabled" state.
- **PyInstaller spec's hidden-import fallback** now includes the chat, updater and history-db modules.
- **Docs**: README (zh + en) — new "Nearby Chat" section, corrected architecture tree and content-filter docs, test count updated; PLAN.md entry path (`src/main.py`) and Python 3.12 requirement corrected.

## [1.0.23] — 2026-08-23

### Core reliability
- **Re-copying recent content no longer gets silently swallowed.** The dedup ring is now time-bounded (90s TTL): past the window, a repeated hash is treated as a deliberate new copy and goes into history and broadcasts again.
- **Pause-sync race closed.** A capture already in flight when the user pauses no longer reaches history or the network — two post-capture re-checks honour the pause before any write or broadcast.
- **Bare email addresses are no longer redacted by default.** The sensitive-content filter treated any email as a credential and replaced it with `[FILTERED]`, corrupting ordinary clips (signatures, pasted correspondence) on the receiving side. Emails are now an opt-in "email" category; real credential patterns (tokens, passwords, keys) stay default-on.
- **Corrupt history database self-heals.** A broken `clipboard_history.db` is quarantined (`*.corrupt-<ts>`) and a fresh database started, instead of silently degrading to memory-only history that vanishes on restart.

### Transport & protocol
- **Rejection probe no longer breaks the frame stream or stalls 2s.** The post-handshake check now uses a brief timed probe and replays any bytes that weren't actually the reject marker — outbound connects no longer pay a flat 2s and no longer eat the first application frame ("connected then instantly dropped").
- **Anonymous TLS connections are reaped after 60s.** A LAN device that completes TLS but never sends an identity frame can no longer hold threads/fds forever and block legitimate pairing.
- **App-layer encryption fails closed.** When encryption is enabled, unencrypted frames are dropped instead of passed through, and repeated decrypt failures close the connection so reconnect rebuilds key state.
- **mDNS instance names are unique.** A short id-hash suffix prevents two devices sharing an 8-char prefix from colliding on one mDNS name; registration failure now retries under an altered name.
- **Peer-supplied names are sanitized** (control chars stripped, 64-char cap) before reaching logs, notifications or the UI.
- **Decoder no longer crashes the receive loop** on valid-but-non-object JSON payloads or deeply nested frames.
- **Device rename no longer reports a ghost offline;** `confirm_pairing` enforces pairing-code expiry; `forget_peer` stops every matching connection, not just the last; a `FILE_COMPLETE` wait timeout is reported as `error_timeout` instead of a misleading "peer offline".

### Web & mobile
- **Regenerating the web token no longer lands the dashboard on the "link expired" dead end** — the new token is echoed to the authenticated caller so the page can rewrite its URL before reloading.
- **Multi-select "Add to favorites" keeps the full clip** (was silently truncated to a 200-char preview) and inserts incrementally instead of delete-and-reinsert-whole-table (which could roll back favorites edited elsewhere mid-batch).
- **Settings API validates numeric ranges server-side** (port 1024–65535, history limits, debounce, …) so a bad LAN request can't leave the network layer unable to start.
- **WebSocket handshake sends the snapshot before subscribing** — a racing broadcast can no longer be clobbered by the stale snapshot's wholesale replace.
- **Transfer history shows specific failure reasons** (`error_timeout` / `error_internal`) and the completion toast no longer celebrates failed or cancelled transfers.
- **Mobile history renders BMP/TIFF image clips with the correct MIME** instead of breaking as `image/png`.
- **"Remote access" off now gates DELETE/PATCH too** (it previously only guarded GET/POST), so LAN clients really are cut off.
- **Dashboard inline language follows the app locale**, not the browser's.
- **Discovery no longer hides a genuinely new device** whose name merely extends a shorter known name.

### Desktop UX
- **Webview transfer/retrust/peer-pick dialogs no longer block the Tk main thread for up to 120s** — they run off the worker thread and marshal results back via `after(0, …)`; the send-URL flow had a fourth instance of the same bug.
- **A corrupt config file is archived** (`config.json.corrupt-<ts>`) and fields validated instead of silently resetting device identity and overwriting the file; hotkey parsing defends against non-string values.
- **macOS hotkeys require an exact modifier match** — no more accidental paste when an extra modifier is held.
- **Backups restore paired peers** (public keys re-exchanged on reconnect); **export and backup writes are atomic** (`.part` + replace); backups validate `history.json` before packaging.
- **Dashboard LAN-IP lookup moved off the UI thread** (30s cache) — the periodic 5s stutter is gone.
- **QR / phone-guide dialogs release their grab on close**, so other windows stay clickable.
- **"Port in use" shows the right command per platform** (`netstat -ano | findstr :{port}` on Windows, `lsof` elsewhere).
- **CSV import tolerates empty/non-numeric cells**; the language dropdown shows names (English / 简体中文); Linux sound fallback honours exit codes; the tray-startup-failure notice is localized.

### Nearby Chat（附近聊天）
- **全新「附近聊天」功能：与同一局域网内的设备直接通信，无需预先配对。** 在主页左侧边栏新增「💬 附近聊天」入口（沿用现有核心功能风格），可对已发现但未配对的设备发起聊天、收发文字与文件；已配对设备同样支持聊天，并断线自动重连。
- **严格的双向同意模型，未接受邀请不泄露任何内容。** 对方必须显式接受邀请后才能开始；未配对设备弹窗展示短证书指纹供线下核对，已配对设备自动接受；邀请按设备限速（5 次/5 分钟）且同时最多 3 个待处理邀请，文字按会话防洪（30 条/10 秒）。
- **文件传输复用现有分块通道**（256KB 分块、2GiB 上限），每个收件文件单独确认；文件名消毒、路径穿越拦截、重名自动改名、磁盘写入字节与预期大小双重校验。
- **会话全生命周期护栏：** 心跳保活 + 离线检测（45s ping / 150s 静默判离线）、互邀确定性收敛、并发上限、死会话自动清扫、取消配对即关闭会话；断线/失败状态在会话中可见，不再静默丢失。

## [1.0.22] — 2026-08-23

### Fixed
- **Copying no longer pops a "No devices connected" notification on every copy.** v1.0.21 added a transient warning when there were no connected peers, but with a 5-second throttle it was effectively an alert on every copy for frequent copiers. The copy now simply broadcasts to the (empty) peer set with no nag.

## [1.0.21] — 2026-08-23

### macOS & Linux platform (Round 3)
- **macOS tray menu is live again.** The tray runs in a subprocess whose menu used to be frozen — the peer list always showed "No devices" and the Web-QR item never appeared. The parent now pushes peers/web/sync state over the pipe and the child rebuilds its menu.
- **macOS can no longer go headless.** If the tray subprocess dies, it's now restarted automatically (with backoff) instead of leaving the app with no Dock icon, no tray, and no way to quit. A `_shutting_down` guard prevents a stray restart during quit.
- **`_hide_dock` uses the correct activation policy (Accessory, not Prohibited)** so CTk dialogs (settings, retrust, QR) can come to the front on macOS.
- **HiDPI / fractional scaling support** on Linux & Windows: the UI now detects the display scale and applies CTk widget/window scaling (clamped 1.0–2.5), so a 2x or GNOME-fractional display no longer renders the desktop at half size.
- **Dashboard & settings remember their window geometry** across hides and restarts (sidecar JSON per window), so Linux no longer loses position on every tray reopen.
- **Linux clipboard-tool check is backend-aware**: Wayland requires `wl-copy`/`wl-paste`, X11 requires `xclip` — the startup warning now names exactly which tool is missing instead of silently failing.
- **Linux `.desktop` autostart Exec is XDG-spec-quoted** (double quotes, not shlex single quotes) so a path with spaces or metacharacters actually launches.
- **Hotkey Accessibility detection is direct**: macOS `AXIsProcessTrusted` is probed so the "enable Accessibility" prompt fires even when the event tap is created but not trusted; the failure now also surfaces as a desktop notification.
- **Linux tray has a fallback**: if the AppIndicator backend can't start, the app logs and notifies instead of dying silently; notification failures (no daemon / no notify-send) are logged instead of swallowed.

### Transfer flow
- **"Finalizing on the receiving device…" state** replaces the confusing "Sending… 100%" while the receiver writes the file to disk; the web panel shows it with a spinner.
- **Sending to a peer that drops now fails fast with "peer went offline"** instead of hanging 120s in "awaiting-ack" — the disconnect path resolves the hashed discovery id to the real device id (the earlier wiring passed the hash and matched nothing) and fails matching transfers immediately.
- **`awaiting_ack` / `finalizing` transfers are cancellable in the classic dashboard** (previously no cancel button).
- **Copying with no peer connected gives a desktop notification in classic mode** instead of only a web toast that silently dropped when no web client was attached. *(Removed in v1.0.22 — see above — as it nagged on every copy.)*
- **Sensitive-content filtering is no longer silent**: the sender gets a throttled "Sensitive content was not synced" notice, and history/favorites items containing `[FILTERED]` show an explanatory note ("Some sensitive content was replaced with [FILTERED]").
- **Clipboard write failures surface** a "Clipboard write failed" notification instead of being swallowed.
- **Transfer sounds respect the notifications master switch** (no more dings with notifications off).
- **Web transfer history shows the specific failure reason** (disk full, size mismatch, timeout, peer offline, rejected, cancelled) instead of a generic "Failed".
- **Classic dashboard polls faster (800ms) during active transfers** so progress advances smoothly instead of lurching every 5s.

### Robustness (regressions found in self-review, fixed)
- macOS tray pipe sends are now serialized under a lock — the state-sync writer and the notification sender share one pipe and could otherwise interleave frames (desync, or a blocked send freezing the UI).
- The macOS notification pipe-sender thread is stopped before a tray restart instead of leaking and competing for the pipe.

## [1.0.20] — 2026-08-23

### Interaction & accessibility (Round 2)
- **Web dashboard is now keyboard-usable**: the right-click context menu is a real `role="menu"` with arrow-key navigation, Enter to activate, Escape to close with focus restored — and the advertised Ctrl+C / Del shortcuts now actually work (guarded so they never fire through a modal dialog). History and favorite cards activate with Enter/Space, the settings dialog is `role="dialog"` with a focus trap, peer-picker rows are proper radios with arrow-key selection, and emoji-only action buttons everywhere got accessible labels.
- **Async operations give feedback instead of silence**: overview toggles (sync / discovery / visibility / web companion) disable and show a busy state during the request and toast the real reason on failure; Show QR / Send URL no longer swallow errors; device-refresh failures show a distinct "failed to load — retry" state instead of a misleading "No devices found"; history "load more" and multi-select batch actions get busy states and failure toasts.
- **Settings dialog unsaved-changes awareness**: toggles that need a Save are now marked with an "unsaved" badge, closing with staged changes prompts for confirmation, and focus is trapped and restored.
- **Mobile-friendly polish**: toggle switches have 44px touch targets, the overview status bar wraps instead of clipping, cards get `:active` press feedback, and clip text is selectable (it's a clipboard manager — you should be able to drag-select a portion).

### Core flows — transfer failure taxonomy
- **The file-transfer failure reason is no longer discarded.** Every terminal state now carries a stable reason (`error_disk`, `error_size_mismatch`, `error_missing_chunks`, `error_security`, `rejected`, `peer_offline`, `timeout`, `cancelled`) from the transfer engine through history and into both UIs, so "Disk full" and "Peer went offline" no longer both read as a generic "File transfer failed".
- **A user-initiated cancel is reported as "Cancelled", not "Failed"** — in the notification and the history record.
- **Incoming transfer dialogs can no longer outlive the transfer**: if a request is cleaned up or the sender cancels while the prompt is open, answering shows a clear "transfer no longer available" instead of silently doing nothing.
- **Cancel can't double-notify**: the send loop and `cancel_transfer` share a once-guard so a cancel races the mid-send thread without firing two callbacks.

### Desktop (CTk) window behavior
- **⌘Q / Ctrl+Q now actually quits** the app instead of just closing the dashboard window and leaving a zombie in the tray.
- **"System" appearance mode is honored**: it resolves to the OS light/dark preference (Windows registry, macOS defaults, Linux gsettings) instead of silently rendering Light; re-opening a window re-reads the current system theme.
- **Escape no longer closes a window while you're typing** in the dashboard history search or any settings text field.
- **All themed dialogs accept Enter (default) and Escape (cancel)**, focus their default button, and give buttons a real hover state.
- Settings window closes with Escape/⌘W; the first-run language picker is now a proper modal with Tab/Enter/Escape support; the "About" tray item opens the Settings About panel instead of an ephemeral notification; hotkey-registration failures (macOS Accessibility) surface a one-time actionable dialog; toggling sync notifies "Sync active/paused"; pairing codes are grouped (1234 5678) and expired pairing requests are surfaced.

### Web companion & PWA
- **Regenerating the access token no longer bricks installed PWAs or open phone pages**: the manifest gets a stable `id`/`scope` and a "link expired — re-scan the QR" page instead of raw 403 JSON; a stale-token phone page shows guidance instead of a wrong "check your Wi-Fi" diagnosis.
- **Token is URL-encoded** when embedded in QR codes / copy-URLs, so a custom token with reserved characters can't break the flow.
- **iOS "Add to Home Screen" now yields a standalone app** (apple-mobile-web-app metas on all pages) and the quick-paste page hides its dead close button on normal browser tabs.
- **Phone pages follow the app's configured language** instead of the phone's browser language; quick-paste history fetches get an 8s timeout with a Retry state; mobile tap targets are ≥44px; the iOS auto-zoom-on-focus bug is fixed (16px textarea).
- **Static assets are always revalidated** (`no-cache` + ETag/304) so an app update never serves stale JS/CSS, while unchanged assets stay cheap over LAN.

## [1.0.19] — 2026-08-22

### Platform UX (macOS / Linux) — typography & theme
- **Cross-platform UI font resolution.** CustomTkinter defaults every font to "Roboto", which is missing on most macOS/Linux installs — so the whole desktop UI fell back to Tk's dated default. A new `internal/ui/fonts.py` resolves the best actually-installed UI font per platform (SF Pro / Helvetica on macOS, Segoe UI on Windows, Noto Sans / Ubuntu / Cantarell on Linux, with CJK fallbacks like PingFang SC / Microsoft YaHei / WenQuanYi) and applies it to every widget via a small `CTkFont` patch — no per-widget churn. Tk's default font is aligned too.
- **Web UI font stack is now system-native on every OS.** `--clipsync-font` lists each platform's UI font with interleaved CJK fallbacks (PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, WenQuanYi) so Chinese text — the app's default UI — renders crisply instead of falling back to an unrelated font, especially on Linux.
- **Custom aurora CTk theme.** The desktop windows now use a cyan→violet theme (`assets/themes/clipsync.json`) matching the web UI's brand palette instead of CustomTkinter's stock blue. Headers, sidebars, buttons and dialog accents were recolored to the same cyan/violet family.
- **Firefox scrollbar styling.** `::-webkit-scrollbar` is Chromium-only; Firefox (common on Linux) now gets matching thin styled scrollbars via `scrollbar-width`/`scrollbar-color`.
- **macOS keyboard shortcuts.** The dashboard now binds ⌘W (hide) and ⌘Q (close) on macOS instead of only Ctrl+W/Ctrl+Q.
- **macOS webview window size.** Chrome-based `--app` windows on macOS now pass `--window-size`, so the dashboard opens at the requested 960×720 instead of a browser-default size.

## [1.0.18] — 2026-08-22

### Fixed (regressions found by the v1.0.17 self-review)
- **Rejected incoming transfers no longer leak a direction entry** in memory (the per-transfer direction map grew one entry per rejected request forever).
- **Changing the receive directory now moves web/phone uploads too** — the Files list, download and delete now follow the same directory, so an uploaded file no longer appears missing after a receive-dir change.
- **Web-cancelled transfers show "Cancelled" immediately** — the WebSocket completion broadcast now carries the `cancelled` flag (the frontend's handler previously checked a field the backend never sent, so cancels briefly flashed "Failed" before a refetch corrected them).
- The **transfer target selector no longer silently re-aims** at a different device the instant the chosen peer goes offline — the "peer offline" hint shows and the send buttons disable instead.
- The web **speed test shows "Connect a device first"** when no peer is connected (was a generic "failed to start").

## [1.0.17] — 2026-08-22

### File transfer & speed test
- **Web pause/resume/cancel now actually reach the peer** — they previously dropped the control frame, so a web-paused transfer made the sender keep sending and then fail after 60s.
- **Speed test refuses to run with no connected peer** (previously reported a bogus "fast" result), and the desktop panel's speed unit is corrected to **MB/s** (was labeled Mbps, an 8× underestimate).
- **Receiver-side failures** (disk full / unwritable receive dir) now surface a notification instead of silently vanishing.
- Transfer completion notifications are **direction-aware**: the receiver is no longer told "File sent successfully", and a failed incoming transfer reports "File receive failed".
- **Sending to a peer that just went offline now fails loudly** (was a silent "success" with the file left on the sender).
- **Cancelled transfers are distinguished from failures** in transfer history (web + desktop), not lumped into "Failed".
- The incoming-transfer dialog shows the **sender's device name** instead of no identity.
- Phone/web uploads **honor the configured receive directory** and return a clear "file too large (max 128 MB)" instead of the cryptic "no file field".

### Desktop chrome
- Settings → Danger Zone **"Factory Reset" now actually wipes the data** — the exiting process previously re-created the config it had just deleted.
- Web **"Restart App" no longer intermittently quits with "another instance is already running"** (the single-instance lock is released before the new instance spawns).
- The tray **"Show Dashboard" reopens immediately** after closing the window (previously dead for up to 8 seconds), so tray QR/Send-URL actions aren't dropped.
- The per-event **transfer notification toggle now gates send confirmations** (was bypassed by direct calls).
- Dismissing the **startup encryption-password prompt exits cleanly with a message** instead of continuing with broken encryption.
- Settings "Restart App"/factory reset no longer print a Tk traceback; **TCP sync-port saves now show a restart-required note**.
- Web transfer **pause/resume/cancel button state reflects reality** (optimistic update + server reconcile), upload errors surface their real reason, and a stale target device is cleared when the peer goes offline.

## [1.0.16] — 2026-08-22

### Fixed (regressions found by the v1.0.15 self-review)
- **Regenerating or clearing the web token no longer lands on a raw 403 page.** The backend now returns the fresh token; the page rewrites the token in its URL and reloads seamlessly. (v1.0.15's "reload after regenerate" carried the now-stale URL token and hit a 403 dead-end with the SPA gone.)
- **Import is confined again.** v1.0.15's path relaxation let a token holder read arbitrary files off the disk by importing them into history and reading them back; import is now limited to the Downloads folder and the ClipSync data directory, and JSON items must carry ClipSync export fields to be accepted.
- **mobile.html**: a text clip no longer leaves a stale image on screen from a previously opened image clip, and the 5-second poll skips a tick while a request is still in flight (no overlapping, out-of-order renders on slow networks).
- **First-run wizard** now also appears on touch-primary desktop convertibles (it's gated on the local webview host in addition to the pointer type), instead of being silently suppressed by the `(pointer: fine)` check.
- The **restore summary toast is no longer immediately overwritten** by the restart-required note — both are shown together.

## [1.0.15] — 2026-08-22

### First-run & onboarding
- The **language picker now reappears on every launch until a language is actually chosen** — previously, closing it (or the config file already existing from identity bootstrap) permanently stranded English-only users in the default Chinese UI. Option cards also respond to clicks anywhere on them now, not just the thin border.
- The **first-run wizard's step 3 ("Use it on your phone") is reachable and real**: it shows the phone connect URL and a "Show QR Code" button (previously the step was dead code and the third progress dot lied).
- **Empty device names are rejected inline** with an error instead of being saved silently; the wizard now only appears on desktop-like clients (a phone opening the dashboard gets the normal app, not a "rename your PC" prompt); pressing Enter in the name field advances.
- Pairing codes render **grouped (1234 5678)** with an "expires in 5 minutes — reconnect to retry" hint.

### Mobile & phone pages
- `mobile.html` now **polls the active tab every 5s** (paused when the page is hidden), so new history/file entries appear without manually switching tabs.
- **Phone file uploads get a live progress bar, a cancel button, multi-file support** (uploaded sequentially), and a clear pre-flight error when a selection exceeds the 128 MB server cap (previously a silent "no file field").
- **Image clipboard items render in the phone modal** with tap-to-zoom (previously "(empty)").
- Phone fetches get an **8-second timeout with "same Wi-Fi / ClipSync running" guidance** instead of an indefinite shimmering skeleton.
- **Rich history actions (favorite/translate/paste-to-device/view) are reachable on iPhone** via touch long-press (iOS never fired the right-click menu); favorites can be **reordered with up/down arrows on touch** (HTML5 drag-and-drop doesn't work on phones); the quick-paste confirmation says **"Sent to computer"** instead of the misleading "Pasted!"; the quick-paste header respects the safe-area inset and localizes its tooltip/aria labels.

### Settings & data
- **Export JSON/CSV writes a real file to Downloads** (path shown in the confirmation) instead of deleting the temp file before the user could use it.
- **Import accepts any real file path** with clear localized errors (10 MB cap + content validation) instead of rejecting everything outside the private data directory.
- **Restore now auto-creates a backup of the current state first**, its confirmation states the real semantics (settings overwritten, history *merged*, not undoable), and a "restart required" toast appears when restored settings need one.
- **"History items shown" now actually persists** (the raw `v-model` string was being rejected by the backend type guard); the **theme choice persists to the app config**; **regenerating/clearing the web token warns first** and reloads the page with the fresh token so the session recovers.
- **Update downloads use a 120s timeout** (no more false "failed" on slow downloads) and **surface the real reason on failure** (e.g. "no release asset for this platform") instead of a generic error.
- The classic UI's **factory reset now deletes the full data set** (history/favorites databases included), matching the web path; the web **"Restart App" no longer reports a false failure** (exit is scheduled after the response flushes).
- Web/Data settings that need a restart now say so in the save confirmation.

### Regressions fixed from v1.0.14 (self-review pass)
- **Image items copied from the context menu paste the actual image again** (v1.0.14 changed "Copy" to a local text copy, which degraded pure-image items to a truncated text preview).
- Server-pushed dialog **focus is restored from the very first dialog** (not just the second), and a dialog **can no longer be double-submitted** while its response is in flight.
- **Cancelling a password change no longer leaks the encryption toggle** into a later unrelated save.

## [1.0.14] — 2026-08-22

### Desktop UI (fixed)
- **Saving Security settings can no longer wipe the pre-shared encryption password.** The password field always started blank and a save unconditionally copied it over the stored password, clearing the password hash and leaving the device identity key undecryptable after a restart. The field is now only applied when a new password is typed (with a confirmation explaining the private key is re-encrypted); a blank field leaves the stored password untouched.
- The dashboard's 5-second refresh no longer accumulates: every `show()`/re-show cancels the previous timer chain and the poll only re-arms while the window is actually visible, so repeated tray "Show Dashboard" clicks can't stack N refresh loops (a source of hidden-window CPU/battery drain and stutter).
- Uptime now measures app lifetime, not time since the window was last built — it no longer resets on every window rebuild.
- Copy-URL, note-editing, and other timer/dialog callbacks that could fire against a destroyed widget are now guarded (`winfo_exists` / try-except), fixing latent `TclError` crashes when a window is closed mid-action.
- The **first-run language picker** is no longer "consumed" by dismissing it: closing it with × returns to the picker on the next launch; only an actual language choice marks onboarding complete.
- The **Enable desktop notifications** toggle now applies live (no restart), the header theme toggle and Appearance radios stay in sync, and the receive-directory field expands `~` before validation so the documented `~/Downloads/ClipSync` placeholder is accepted.
- Enabling **Remote access** from desktop Settings now clearly states it takes effect after a restart instead of silently leaving the server off.
- The tray's sync checkbox is reconciled with the real sync-manager state instead of flipping optimistically, and non-Windows tray menu rebuilds are marshaled onto the tray thread (avoiding a context-menu race).
- A failed "Launch at login" toggle now reverts the switch and explains itself instead of silently sticking.

### Web dashboard (fixed)
- **Settings → Remote access "Copy" now appends the auth token** (the copied URL previously opened "Invalid token" on a phone, while Overview's copy worked) and the shown URL is derived live from the port/LAN-IP fields, so editing the port is immediately reflected.
- **Multi-select "Push to Desktop" actually pushes to the desktop** (via the server clipboard) instead of silently copying the merged text to the phone's browser clipboard.
- **Regenerating the web token no longer leaves the session and Overview Copy-URL on the dead token**; the stale token is cleared from the running session.
- **History pagination survives background clipboard syncs**: a LAN broadcast used to replace the loaded list with page 1 and discard everything the user had loaded via "Load more"; it now merges the broadcast in place (upsert by id, newest-first), preserving loaded pages.
- A failing dialog response (pairing/transfer/url-input) is no longer silent — the dialog stays open and a failure toast is shown instead of vanishing while the server keeps it pending.
- Server-pushed dialogs are now dismissible with **Esc** (blocking progress/confirm excepted), and Esc while typing in a search box no longer wipes selection/preview.
- The speed-test spinner can no longer stay spinning forever; WebSocket listeners are removed on teardown (no duplicate-handler stacking); the `web_history_limit` setting now applies to the first history fetch; a server dialog arriving over a pending local confirm no longer leaves the confirm promise stuck.
- **No more silent failures**: reordering favorites, renaming the device, and pausing/resuming/cancelling transfers all report success or failure; **Translate** now uses the full clip text instead of only its first 200 characters.
- **Clear all history** resets the pagination cursor like the other delete paths (no skipped items on later "Load more").

### Accessibility & mobile
- Pinch-zoom restored on all three pages (removed `user-scalable=no` / `maximum-scale=1`); `prefers-reduced-motion` handling added to the two phone pages (they previously ran infinite aurora/pulse animations for vestibular-sensitive users).
- The `lang` attribute is set at runtime to the actual UI language, so screen readers pronounce the text correctly.
- Settings toggles now expose `role="switch"` + `aria-checked` + accessible names (~27 controls); the quick-paste listbox is keyboard-focusable with `aria-activedescendant`; focus is returned to the triggering element after closing dialogs/modals; keyboard users can no longer tab into invisible (hover-only) history actions.
- Touch targets on the dashboard grow to ≥44 px on coarse-pointer devices; the app shell uses `100dvh` so the status bar isn't hidden behind mobile browser chrome; the quick-paste toast no longer overflows narrow phones; the status bar wraps instead of clipping.
- Muted meta-text contrast raised to ~5:1 on both themes.

### Localization & consistency
- Newly localized: desktop uptime/ETA, "QR err", log-export notifications, and placeholder hints; quick-paste relative-time strings and "(empty)" previews now render in the page language; the missing transfer pause/resume/cancel tooltip keys were added to both locales.
- Clipboard-type icons (URL/file/RTF/image) now come from one shared helper instead of three drifted per-tab mappings.
- Removed conflicting duplicate CSS rules (filter-chip background was silently overridden; duplicated media-query blocks), dead code, and a "Ungrouped" context menu whose only actions did nothing.

## [1.0.13] — 2026-08-22

### Security
- **Fixed a cross-origin token leak in the web companion.** The auth token was embedded in every static asset and those assets were served with `Access-Control-Allow-Origin: *`, so any web page could `fetch('http://127.0.0.1:<port>//index.html')` and read the token. Path aliases (`//index.html`, `/./index.html`) are now normalized to the canonical route, token-bearing static files are no longer CORS-readable, and all responses send `Referrer-Policy: no-referrer` + `X-Content-Type-Options: nosniff`.
- **Fixed stored XSS via the device name.** `device_name` / `device_id` are interpolated into an inline `<script>` literal, and `json.dumps` does not escape `<`, so a name containing `</script>` could execute arbitrary JS on the companion origin (exfiltrating the live token). Inline-script values are now escaped (`<` / `>` / `&`).
- The diagnostics "request permission" route (`/api/diagnostics/request`) is wired to its callback again, so the Firewall / Local Network permission buttons work from the web companion.
- `/api/logs` now redacts locally-sensitive strings (user home, config dir, web token) before serving them to web clients.
- Web API routes reject non-object JSON bodies with a clean 400 instead of a 500 plus a full stack trace.

### Sync & Clipboard
- Fixed a race in the sync manager where two debounced reads could run in parallel (a rich-content capture takes ~1.4s), broadcasting stale clipboard content out of order or dropping the newest copy — reads are now serialized.
- Windows: the clipboard reader and writer no longer race on `OpenClipboard`, so an incoming sync is no longer silently dropped mid-read; writes retry briefly, and a non-UTF-8 peer text decodes with `errors="replace"` instead of wiping the clipboard.
- Content filtering preserves the image format hint, so a filtered BMP/TIFF copy is no longer corrupted on Linux/macOS receivers.
- Linux: clipboard writes now check the tool's exit code and fall through to the secondary tool instead of silently "succeeding" on failure.
- History: `find_by_id` is type-tolerant (int vs str ids), fixing `/api/history/item` always returning 404; dashboard Copy/Delete act on the entry id so a search filter can no longer target the wrong entry; paste-to-top ordering is consistent between memory and the database so a just-pasted entry is not trimmed away on restart.

### File Transfer
- A late `file_chunk_ack` (e.g. after pause/resume) is now honored while waiting for `FILE_COMPLETE`, instead of being dropped after the first 3 seconds.
- Cancelling an incoming transfer no longer leaks the open temp handle / `.part` file, and stale `.part` files from a crash are swept on startup.
- Received files are verified against the total bytes actually written (`received_bytes`), not just the final file size, so a hole left by a short middle chunk is now caught.
- Release downloads stream to a `.part` file and are verified against the release size before rename, so an interrupted download never leaves a truncated installer at the final path.

### UI
- Transfers panel progress/state no longer freezes — the change-detection key now uses the real transfer fields.
- Fixed a Windows tray crash risk: the sync toggle no longer calls `update_menu()` (DestroyMenu) while the context menu is open.
- Dashboard `after()` timers (chunked history renderer, search debounce, copy-URL reset) are cancelled on hide/close so they can't fire against destroyed widgets.
- Dashboard network detection (`netsh` / `powershell`, up to ~15s) now runs once in a background thread instead of freezing the UI on every window build.

### First-run onboarding
- New bilingual **Choose Language** step on the very first launch: every label and option is shown in both 简体中文 and English, so anyone can complete it regardless of which language they read. The choice is remembered and changeable anytime in Settings → Appearance.

### Pairing & device lifecycle (interaction layer)
- **Pairing is now a true two-sided handshake.** Confirmation no longer happens in a vacuum: after you confirm, your device shows "已确认 · 等待对方确认…" and tells the peer; when the peer confirms (or rejects / un-pairs), you get a notification. Every state has a clear bilingual prompt.
- **Asymmetric confirmation handled**: one side confirming first puts the other side's card into a "对方已确认配对,请在此设备确认" prompt; both sides confirming completes the pairing; a never-confirmed request **expires after 5 minutes** with a "请求已过期" notice.
- **Reject / unpair propagated**: rejecting a pending request tells the peer ("对方已拒绝配对"); unpairing a paired device tells it too ("对方已取消配对"), and a device that was unpaired on the far side is notified on reconnect.
- **Device certificate changes** (reinstall / reset) now raise a friendly **重新信任 / 保持不配对** dialog instead of silently dropping the connection — both at startup (one dialog listing affected devices) and at runtime.
- **Rich pairing card UX**: the pairing code is larger, a guidance line explains to compare the code on both devices, and a verify-the-code confirmation dialog appears before accepting. Device cards show a clear status chip (已连接 / 已配对 · 离线 / 未配对 · 已发现) on both desktop and web.
- Backup **restore now validates every field** (types/ranges/enums) and persists immediately, so a malformed backup can no longer crash the transport on next start or silently vanish.

### Privacy & LAN exposure
- **No data reaches an unpaired peer**: the transport drops inbound app frames from unpaired peers and `broadcast()` skips them, so nothing (clipboard, history, files) is obtainable before both devices are paired.
- **mDNS advertisement tightened**: only RFC 1918 private LAN addresses are advertised (no public / VPN / virtual-adapter IPs), capped at 10, so the device exposes minimal network topology.
- macOS: fixed an unbounded Objective-C memory leak in the clipboard monitor's 0.4s pasteboard poll (autorelease pools around every ctypes→ObjC bridge call).

### Housekeeping
- Removed duplicate i18n dictionary keys and unused variables; applied ruff import-sorting / unused-import cleanups.

## [1.0.12] — 2026-08-22

### Fixed
- macOS: crash on launch (`*** CFHash() called with NULL ***` / SIGTRAP) in the global hotkey manager — the run loop mode is now passed correctly instead of as NULL
- Paired devices now auto-reconnect after a reboot/restart instead of sitting at "waiting for pairing"
- Windows: firewall setup no longer errors with `'NoneType' object has no attribute 'strip'`
- Logs tab and Diagnostics page now work on macOS/Linux (logs were read from the wrong directory; the diagnostics summary crashed with `UnboundLocalError`)
- Global hotkeys are now **off by default**, with a new settings toggle to re-enable them

### Settings
- Moved **Launch at login** out of the Network section into Preferences (applies immediately on toggle)

## [1.0.9] — 2026-08-21

### Diagnostics
- New standalone **Diagnostics** page in the left sidebar (Overview / History / Devices & File Transfer / Favorites / Diagnostics): one-click scan that reveals each check one by one (✓/✗) with a translated detail + actionable guidance line, then a final summary. Moved out of Settings → Advanced; the overview network-health chip now jumps straight to the page
- Checks cover the TCP server port, mDNS discovery, network advertising, web companion, network classification, firewall and permissions (macOS Local Network), plus Linux-specific checks (ufw/firewalld, avahi-daemon, xclip/wl-paste)
- The firewall and permissions checks carry a **Request permission / open settings** button: macOS opens the relevant System Settings pane; Windows re-applies the firewall allow rule or opens the firewall settings page
- Fixed the button previously failing with "Failed to open permission settings" (the `/api/diagnostics/request` backend route was missing)
- Diagnostics detail and guidance are now fully localized in English and Chinese, falling back to the server text when no translation exists

### Web Companion & onboarding
- PWA support: the web companion is now installable (app manifest, apple-touch-icon, token-safe service worker with offline app-shell caching)
- First-run onboarding wizard: name this device → pair a device → open it on your phone (skippable, persisted)

### Settings
- New **Logs** tab: view the log tail, refresh and export
- **Security** tab now lists trusted devices / certificate fingerprints
- Per-event notification toggles (device connect, transfer, pairing, sync)
- **Update download** button that fetches the platform release artifact into ~/Downloads
- i18n pass: replaced the remaining hardcoded user-facing strings across the frontend and backend dialogs

### UI
- Tray menu follows the app language and was redesigned (emoji icons, cleaner grouping)
- Title bar gained a **Refresh** button that reloads all data in one click
- History panel: filter chips, item count, sort and clear-all merged into a single combined sticky bar
- Modern app icon; toast text wrapping fixed; high-count badge / history layouts hardened
- Fixed square-corner glass inconsistencies, stale-data refreshes after idle, and a Settings → Advanced crash; unified remaining icons

## [1.0.8] — 2026-08-21

### Settings
- Translation configuration moved out of the Web Companion section into its own **Translation** tab
- Settings panel is now complete: added **Launch at login**, **mDNS service type**, **App Filter** (enable / black/whitelist mode / app list), **Clipboard behavior** (paste-to-top, low-memory mode, retry capture, dedup method, source-app tracking), and **Data locations** (data dir, favorites path)
- Almost every setting now takes effect **immediately** when saved, instead of only after a restart: auto-start, web companion on/off, sync debounce, retry capture, poll interval, low-memory mode, dedup method, reconnect attempts, transfer timeout, receive directory (the few that genuinely need a restart — TCP/mDNS/web ports, UI mode, encryption password, data paths — are clearly labelled)
- Fixed `dedup_method` never being wired (and crashing on "simple"); it now maps to a real hash (sha256 / md5)
- `paste_to_top` is now functional: re-using a history item surfaces it at the top

### Overview page
- Richer data from the backend: today's copies, pinned items, image count, completed transfers, total bytes transferred, connected-device names, discovered count, a recent clipboard activity feed, and the app version
- Redesigned overview: animated stat counters, a live network-map ring (connected / paired / discovered), connected-device neon chips, a recent-activity feed with staggered entry animations, and the device hero now shows version + OS
- Overview now reports the real network type (Wi-Fi / Ethernet + interface name) instead of a hard-coded "LAN"

### More
- **Global hotkey editor** added to Advanced settings (all 13 shortcuts, restart required)
- Config schema version added: old configs with `filter_enabled_categories: []` are migrated to `None` so existing installs keep content redaction ON, while a fresh save of `[]` stays a deliberate "disable all"
- Panel design aligned with the new overview: history filter chips and history items are now frosted-glass (no more flat white band), the transfer panel's send-file / folder buttons and speed test got proper glass layouts, and the overview "Connected Devices" card gained a count badge, a nicer empty state and solid quick-action buttons

## [1.0.7] — 2026-08-21

### Fixed
- Dashboard window no longer multiplies: opening is now idempotent (a live WebSocket client means the window is already open, and a short grace period covers page load), so repeated tray clicks / settings actions can't spawn duplicate browser windows or accumulate processes
- Closing the dashboard window is detected reliably, and quitting the app tells open dashboard windows to close themselves (no orphaned browser processes)
- App startup no longer fails in webview mode (the web server referenced `threading` without importing it)

### Changed (visual)
- Quick Paste (mobile/QR page) unified with the dashboard: cyberpunk aurora background with a slow drift, frosted-glass header and toast, pulsing title glow, and a cycling neon border on the selected item
- Dashboard: frosted-glass title bar, status bar, and toasts (backdrop blur + saturation over the aurora background)

## [1.0.6] — 2026-08-21

### Security
- Clipboard is now only broadcast to / accepted from **paired** peers — an unpaired TLS peer can no longer read or inject the local clipboard (pastejacking), open arbitrary URLs, or spawn transfer dialogs
- `nav_url` from peers is restricted to `http`/`https` (no more `file://` / custom-scheme launch)
- zlib frame decompression is capped (zip-bomb / OOM fix); /api/download rejects Windows drive-relative escapes
- Web settings save re-encrypts the device private key at rest; backup/export files are chmod 0600 and export temp files cleaned up
- Web token no longer logged to the (previously world-readable) log file
- Factory reset is no longer defeated by shutdown re-saving the deleted config
- Single-instance lock works for PyInstaller-frozen builds

### Fixed
- macOS web UI opens reliably even when Chrome/Edge is already running (browser binary launched directly with `--app`)
- Pairing: pairing requests now survive dashboard-closed (polled via /api/devices), Connect no longer claims success before the handshake, hashed-id reconnects still enforce cert pinning, pairing-code rate limit can't be reset by reconnecting
- File transfer: retransmission actually completes (finalizing flag reset), missing middle-chunk gaps detected, chunks stream to disk instead of buffering the whole file in RAM, 2 GiB size cap, paused transfers no longer auto-cancelled, web pause/resume no longer crash
- WebSocket: slow/stalled clients can no longer freeze clipboard sync; shutdown doesn't deadlock
- Clipboard: first copy after empty-clipboard start is no longer dropped, HTML/RTF-only changes detected, FILE/URL content dedups, image re-encode no longer re-broadcasts duplicates, Linux idle polling spawns far fewer subprocesses
- Web UI: transfers panel populates on load, redundant double-fetches removed, settings (sound/animation/language) persist, "Open file" and "Restart App" actually work, full clipboard text loads on copy/favorite instead of truncated preview
- Sensitive-content redaction is ON by default with broader matchers (tokens, keys, emails, JWT, AWS/GitHub/Slack secrets)
- CTk dashboard breath animation no longer re-queries everything at 5 fps; QR + LAN IP are cached

### Build / CI
- Linux hotkeys restored (`pynput` added to requirements)
- Release tag is verified to match `internal/version.py`; artifact smoke tests catch missing web UI; releases now run the test suite
- macOS bundle is ad-hoc signed with version keys; dead `pyobjc_framework_Cocoa` hiddenimport removed
- `upx` disabled (risky on macOS/arm64); customtkinter data bundled explicitly

## [1.0.5] — 2026-08-21

### Added
- "Check for Updates" (GitHub releases) and a versioned About dialog in the tray
- App icon (.ico) for Windows builds
- Quick actions (Show QR / Send URL) in the Overview panel
- Richer telemetry across the UI: status bar, overview stats, device cards, history, transfers

### Changed
- "Aurora Cyber" theme: unified cyan/violet/pink palette across light and dark, glass-morphism surfaces, aurora background glow
- Static assets served with `no-cache` so UI updates appear immediately after restart

### Fixed
- Settings panel and translate modal never registered (bare `t()` calls)
- Device unpair/forget/connect not refreshing the device list
- Tray actions (QR / send-url / settings) dropped when the webview window was not yet open
- Status bar showing stale "sync paused" / wrong connected-device count
- History pagination cursor after batch delete; transfer history missing direction/timestamp
- i18n keys missing from JSON locale files (sort control, update checker)
- Cross-platform: macOS tray wiring for check-update/about, Linux pynput dependency, color-mix fallback, spec icon placement
- macOS: web UI now launches the Chrome/Edge/Brave/Chromium binary directly with `--app`, so the dashboard opens even when the browser is already running (previously `open -a … --args --app` just activated the existing window and showed the browser start page instead of the app)
- Packaging: the web UI static files (`internal/web/static`) are now bundled by explicit filesystem path; `collect_data_files("internal.web")` silently skipped them during the spec's isolated package check, so packaged builds only ever served the minimal fallback page instead of the full dashboard

## [1.0.4] — 2026-08-21

### Added
- Modern web-based UI (WebView) with an in-app UI mode switch (Modern / Classic)
- Favorites panel in the web companion
- Per-device connect/disconnect controls
- Clipboard de-duplication and source tracking
- Durable SQLite-backed clipboard history database
- Send retry for failed clipboard syncs
- Global hotkey support

### Changed
- Version now has a single source of truth (internal/version.py) — pyproject, native About dialog, and web About panel all derive from it

## [1.0.0] — 2026-05-03

### Added
- Cross-platform clipboard sync (Windows, macOS, Linux)
- mDNS/Zeroconf automatic device discovery on LAN
- TLS 1.3 encrypted transport with Ed25519 certificates
- AES-256-GCM app-layer encryption per peer-pair
- At-rest encryption for private keys and clipboard history
- Optional pre-shared password for additional key entropy
- Trust-on-first-use (TOFU) device pairing with 8-digit codes
- System tray application with sync toggle and device status
- Dashboard with Overview, Devices, History, and Transfers panels
- Settings window with Network, Content Filter, Security, Advanced, Logs, and About sections
- **Web Companion** — built-in HTTP server for mobile phone access on the same LAN
  - QR code scanning to connect (no app install needed)
  - View clipboard history, push text to desktop, transfer files
  - PWA support with app icon for "Add to Home Screen" on iOS/Android
  - Pin/unpin and delete history items from the web page
  - File upload/download between phone and desktop
  - iOS install banner with instructions
  - Animations (fade-in cards, refresh spin, push button pulse)
- File transfer between paired devices with progress tracking
- Speed test for measuring LAN throughput
- Content filtering for sensitive data (credit cards, SSNs, API keys, etc.)
- Clipboard history with search, copy, delete, pin/unpin, and pinned-first sorting
- Dark mode support (light/dark/system)
- Auto-start on system login
- Desktop notifications for connect/disconnect and sync events
- Log viewer and export within the app
- PyInstaller standalone builds for all platforms
- Factory reset and restart buttons in advanced settings

### Security
- PBKDF2 password verification (password never stored in plaintext)
- Certificate pinning with change detection (potential MITM alert)
- Rate-limited pairing code attempts (5 per 5-minute window)
- Path traversal prevention in file transfers

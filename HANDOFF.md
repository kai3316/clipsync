# ClipSync 待办交接说明

本文档供新会话直接接手。所有位置都经过实测或逐行阅读确认，**不确定的地方都标了"未验证"**，
不要把它们当成结论。

---

## 0. 项目与发布流程（必须照做）

- 仓库：`D:\copyboard`。桌面端 `desktop/`（Tauri 2 + Vue 3），侧车 `internal/`（Python）。
- 版本号来源：`internal/version.py` 的 `__version__`。当前 **1.0.70**，GitHub Latest 也是 1.0.70。
- 发布步骤：
  1. 改 `internal/version.py`
  2. 改 `.github/workflows/build.yml` 里 release notes 那一段（`What is new in this release:` 之后、
     `Still true from 1.0.34` 之前）
  3. `git add` **具体文件名**（不要 `git add -A`，会把草稿文件一起提交）
  4. commit → `git push origin master` → `git tag -a vX.Y.Z` → `git push origin vX.Y.Z`
  5. 等 `desktop.yml` 与 `build.yml` 两个 workflow 都 success，确认 release 有 **8 个资产**
- **发布前必须全绿**：
  - `python -m ruff check .`
  - `python -m pytest tests -q --timeout=180 -p no:cacheprovider`
  - `desktop/`：`npm run typecheck`、`npm test`、`npm run test:e2e`
  - `desktop/src-tauri/`：`cargo test --locked`（cargo 在 `C:\Users\sukai\.cargo\bin`，不在 PATH 里，需要手动加）
- 当前基线（1.0.70）：pytest **1511 passed / 1 skipped**，`npm test` **252**，e2e **19**，
  `cargo test` **72**，ruff / typecheck clean。

### 环境注意事项（踩过的坑）

- 控制台是 GBK：Python 一行命令里打印中文会 `UnicodeEncodeError`；`python -c` 里带嵌套引号或中文
  容易坏。**用 `write` 工具写临时 `.py` 跑，然后删掉**。有时 `python -c` 会报 `spawn EPERM`，换临时文件即可。
- `edit` 工具要求**同一个会话里先 `read` 过该文件**，否则拒绝。
- `write` 工具偶尔对某些临时路径报 "file no longer exists"，换个文件名。
- e2e 如果报缺 Playwright 浏览器，跑一次 `npx playwright install chromium`。
- **前端 shell 文件里（含注释）不能出现 `t()` 之外的中文字面量**，`desktop/tests/i18n.test.ts` 会失败。
  注释用英文写。

### 用户的偏好（重要）

- **要证据，不要推断。** 这个项目里已经发生过两次"把推断写成结论"的错误（v1.0.67 的发布说明归因、
  以及一次修错位置导致测试没失败）。**任何归因都要能指出日志或代码行。**
- 反向验证：**新测试必须实际反向改一次、确认它会失败**，并在报告里给出失败原文。
- 不要声称验证过没有验证的东西。**没有真机跑过的路径必须写明"未验证"**。
- 用户会提供设备日志，放在 `C:\Users\sukai\Downloads\ClipSync-logs\`。日志**不轮转**，
  一份文件可能横跨好几天，读的时候注意时间戳。

---

## 1. 待办 A：把"设备名字"合并成一个

### 现状（已确认）

同一台设备的名字存在**两套字段**里，读的地方又各不相同：

| 存储 | 写入者 | 读取者 |
|---|---|---|
| `peer.notes`（`config.py:43`，注释写的是 "user-assigned alias or memo"）| 设备列表右键重命名 → `set_device_note`（`lan.py:4448`）| `deviceLabel`（`device-row.ts:137`，`note \|\| name`）、中继行的 `note`、`known_device_names`（`config.py:438-446`）|
| `config.netpair_aliases[peer_id]` | 互联网配对页重命名 → `internet_pairing.rename`（`internet_pairing.py:866`）| **只**在中继行的 `alias`（`lan.py:4277`）|

### 两个已确认的缺陷

1. **中继行恒发 `"note": ""`**（`lan.py:4281`，注释说 *"Notes live on a saved LAN peer"*）。
   所以互联网配对设备的名字，设备列表里看不到。
2. **`renameDeviceRow`（`App.vue:961`）按 `device.paired` 分支**：已配对走 `set_device_note`，
   否则走 `renameRelayDevice`。**而中继行必然是 `paired: true`**，而 `renameRelayDevice` 只被
   `device.relay` 为真的行调用（`App.vue:838`）。于是**中继行的右键重命名写的是 `notes`，
   而它显示的是 `alias`** —— 改了等于没改。
   并且 `set_device_note` 在 `config.peers` 里找不到设备时**直接报 NOT_FOUND**（`lan.py:4453`），
   而 `_persist()`（`lan.py:4010`）只写"配对管理器已知的对端"，**互联网配对是否一定产生
   `PeerInfo` 我没有验证**。

### 用户对这个问题的原始描述

> "设备名有的时候会变成'试试'……历史记录里下面显示的名字不对。"
> （实测：本机配置里 `483fa196a05a` 的 `notes = "试试"`，而 `device_name = "Kais-MacBook"`。
> 设备列表读 notes 显示"试试"，历史行/中继行读 `_peer_name`（不读 notes）显示"Kais-MacBook"。）

### 用户已确认的修法

> "合并成'一个名字'，就是右键重命名的这个功能。"

即：**右键"重命名"是唯一入口**，它写的名字要在**设备列表、历史行、中继行、网页面板**全部生效；
互联网配对页的重命名也要写同一个地方。

### 建议实现

1. **统一读取顺序**（一个函数，所有地方都用）：
   `netpair_aliases` → `peer.notes` → 对端自报的 `device_name` → 设备 id
   - 后端：`config.known_device_names`（`config.py:400` 附近）、`LanRuntime._peer_name`（`lan.py:4482`）、
     局域网行的 `note`（`lan.py:4211`）、中继行的 `name`/`note`/`alias`（`lan.py:4277`、`4281`）
   - 前端：`deviceLabel`（`device-row.ts:137`）、排序（`App.vue:2041`）、
     `deviceSubtitle`（`App.vue:5484`、`5674`）
2. **两条重命名路径写同一组字段**：`set_device_note` 写 `peer.notes` 时也写/清
   `netpair_aliases[pid]`，反之亦然（注意 `internet_pairing.rename` 在 `netpair_secrets` 里
   找不到 peer 时会报 NOT_FOUND，`internet_pairing.py:867`）。
3. **中继行也要带上名字**：现在 `"note": ""`，要么让它发真实名字，要么让前端只读统一的字段。
4. 改完必须新增测试，并**实际反向验证**。

---

## 2. 待办 B：设备页"附近聊天"按钮有时卡住

### 已确认的机制

```python
# lan.py:2735
def chat_invite(self, peer_id, peer_name):
    # Blocking: the dial below can hold this for CHAT_CONNECT_TIMEOUT.
    return self._command(self._chat_invite, peer_id, peer_name, blocking=True)

# lan.py:315
CHAT_CONNECT_TIMEOUT = 15.0
```

拨号在这里发生（`lan.py:2755`）：

```python
if pid not in self.transport.get_connected_peers() and self._address(pid) is not None:
    connected = self._connect_and_wait(pid, no_auto_pairing=True)   # 最多 15 秒
```

**触发条件很常见**：设备在 mDNS 上看得见（`_address` 有值）但链路尚未建立 → 点一次等满 15 秒。

### 三处放大

1. **`blocking=True` 占住整个串行化队列**（`_command(..., blocking=True)`），
   这 15 秒内**其他设备操作也一起卡**，不只这个按钮。
2. **拨的可能是过期地址**。`lan.py:2770` 的注释自己写着：`_address` 会回退到缓存的、
   再回退到持久化的地址，**而拨号失败不会清除它们**，所以换了网络的设备会被反复拨旧地址。
3. **前端没有忙状态也没有超时**（`App.vue:977` `chatWith`，直接 `await bridge.inviteChat(...)`）。
   这 15 秒里按钮看起来就是坏的。
   （行内按钮有 `:disabled="busy"`（`App.vue:5497`、`5539`），但是 `chatWith` 自己不设忙标志，
   所以连点会重复发起。）

### 接口本身是"先返回、后拨号"的设计

`rpc.py:784` 的注释明确说：*"No session yet means the link is still being dialed, not that the
invite was refused — the runtime publishes `chat.connect_timeout` when the peer never answers."*
**`blocking=True` 把这个设计作废了。**

### 建议实现

1. 前端：点下去立刻显示"正在连接 {name}…"，进入忙状态，禁止重复点击，并且**有超时**。
2. 后端：让 `chat.invite` **不再阻塞**（`connecting: true` 立即返回），拨号结果通过
   `chat.connect_timeout` / 会话建立事件回来——即恢复 `rpc.py:784` 描述的设计。
   **注意这会改变调用时序，必须检查 web 面板（`companion.py:79` 也用了 `runtime.chat_invite`）**
   和所有依赖返回值的调用方。
3. 拨号失败后清除缓存/持久化地址，避免反复拨旧地址。
4. 新增测试并反向验证。

---

## 3. 待办 C：概览首屏显示"网络需注意"→点一下才变"网络正常"

### 已确认的机制

```python
# report.py:620
def summarize(checks):
    if any(not c["ok"] for c in checks if c["id"] in SUMMARY_CRITICAL_IDS): return "fail"
    if any(not c["ok"] for c in checks): return "warn"     # 任意一项不 ok 即 warn
    return "ok"

# report.py:58
def classify_network(lan_ip):
    if not lan_ip or lan_ip.startswith("127."):
        return (False, "No LAN address detected", ...)      # ok=False
```

**启动早期 `lan_ip` 为空 → 网络检查 `ok=False` → summary="warn" → 界面显示"网络需注意"。**
局域网地址发现之后才变 "ok"。

前端每 **8 秒**才重读一次报告（`OverviewView.vue:130` `healthTimer`），
所以"点一下别的东西就变正常"实际上是**等到了下一次重读**。

### 建议实现

**"等待中"不等于"失败"。** 可选做法（任选，但要说明选择理由）：
- 局域网地址尚未探测到时，该检查项报"等待/未知"而不是 `ok=False`，且不参与 `summarize` 的 warn 判定；
- 或者首屏在报告真正就绪前不下结论（显示"检查中"）。
- **注意 `summarize` 的 warn 是所有检查项的总和，改一处会影响别的项**——先读 `build_groups` /
  `build_checks` 全表，把哪些项可能在启动早期不可用列出来，不要只改网络那一项。

---

## 4. 待办 D：设备版本不实时更新

### 用户描述

> "某个设备，在线显示的版本是旧版本。我把它重新升级后，打开 B 设备看这个设备，还是显示旧版本。"

### 已确认：代码路径是完整的（所以这**不是**简单的一个 bug）

- mDNS 同时注册了 `("Added", "Updated")` 两种事件（`discovery.py:1517`）。
- `discovery.py:1638-1643` 明确把**版本**算进"变了"，注释：*"a peer that upgrades re-announces
  from the same address and port, and that re-announcement is the only word this machine gets that
  the update landed. Skipping it here left the device list showing the old version until something
  else moved."*
- `_on_peer_found` 更新 `_discovered[pid]`（`lan.py:3854`）后调用 `_refresh()`（`lan.py:3869`），
  而 `_refresh`（`lan.py:4079`）会广播设备列表变化。

### 最可能的原因（**未验证，需要日志**）

广播是**周期性**的：`ANNOUNCE_EVERY_ROUNDS = 6` × `PRESENCE_INTERVAL_SECONDS = 10` = **每 60 秒一次**。
所以 A 升级后，B 最多要等 60 秒才听到新的版本号。**"打开 B 看还是旧版本"可能就是还没听说。**

### 要做的第一步（先取证，不要先改）

向用户要 **B 设备**在"A 升级后、打开 B 查看"那段时间的日志，看有没有：

```
Discovered peer: <A> at <ip>:<port> (candidates: ...)
```

- **出现了但版本仍是旧的** → `_discovered` 更新链路有 bug，按代码查。
- **没出现** → 是广播/接收问题（丢失、等待下一轮），不是读取 bug。

### 顺带一个已确认的独立缺陷（与版本无关，但同属"过期地址"）

`lan.py:2770` 注释：`_address` 回退到缓存→持久化地址，**拨号失败不清除**，
所以换了网络的设备会被反复拨旧地址。这条和待办 B 的第 3 点相关，**可以一起修**。

---

## 5. 待办 E（用户新提的需求）：设备信息刷新要有"应答"

### 用户原话

> "对方接到这个刷新后要应答更新消息。这样行吗？"

### 可行性：可行，而且有现成机制

侧车已有 `device_ping` / `device_pong`（`LanRuntime._handle_device_probe`，`lan.py:5260`），
是"问一句、回一句"的请求/应答通道，且已同时支持局域网与中继（`lan.py:5260` 的 `via_relay`）。
把设备信息（版本、平台、架构、名字、在线状态）放进应答是**增量改动**，不需要新协议族。

### 为什么值得做

mDNS 广播是**周期性**的（60 秒一轮），**这正是待办 D 的成因**。
应答是**按需、即时**的：打开设备页或点刷新时问一句，立刻得到权威答案，**不用等广播周期**。

### 必须注意的取舍（写进设计说明，不要跳过）

1. **这会成为同一个事实的第二来源**。mDNS TXT 记录已经承载版本/平台/架构
   （`discovery.py:738` 的 `b"v": __version__`，以及 `os`/`arch`/`app`）。
   **两个来源就会不一致** —— 必须明确优先级，并在应答里带一个能判断新旧的依据
   （例如时间戳，或规定"应答覆盖缓存"）。**不要留下"mDNS 说 1.0.66、应答说 1.0.70"的可能。**
2. **应答必须是经过身份绑定的**。局域网帧走 TLS 且证书已钉住；中继帧的来源绑定在频道上
   （`lan.py:3698-3725`，*"Bind the frame's self-declared source to the channel it arrived on"*）。
   **不要把设备信息做成任何人都能伪造的广播**——只应答已配对的对端，或只接受来自已绑定频道的应答。
3. **不要放大流量**。设备页每次刷新都问一轮是合理的；**不要**把应答机制接到 4 次/秒的
   `_refresh` 上（`lan.py:3573` 的注释说 `_refresh` 一秒跑四次）。
4. **要有超时与去重**，否则一台不回答的设备会让刷新一直挂着（这正是待办 B 的教训）。

### 建议实现顺序

1. 扩展 `device_ping`/`device_pong` 的载荷，带上版本/平台/架构/名字。
2. 收到应答时**覆盖** `_discovered`（或新增一个"权威信息"表），并按上面第 1 点定好优先级。
3. 在"打开设备页"和"点刷新"时主动发一轮 ping（`scan_devices` 已经在做 announce + query + 等回包，
   可以复用它的入口）。
4. **补测试**：应答能更新一个比 mDNS 缓存更新的版本；未配对来源的应答被忽略；不回答时不挂住。

---

## 6. 明确不要做的事

- **不要把待办 A/B/C/D/E 一次性全改完再发布。** 用户已明确同意"一项一项做完、每项跑全量测试、
  最后统一发版"。**半成品比不改更糟**，而且 A 和 B 都涉及多处读取路径，历史上已经因为
  "改错位置"导致过测试没失败。
- **不要在没有日志证据的情况下归因待办 D。**
- **不要因为某个测试通过就认为修好了**——先反向验证一次。
- **不要把发布说明写成"已修复"除非真的修了。** 这个项目已经出现过一次发布说明声明了未实现的修复
  （v1.0.68 的草稿里有一条"设备名字各处一致"，在发布前被删掉了）。

---

## 7. 已经修好并发版的（不要重复修）

| 版本 | 内容 |
|---|---|
| 1.0.64 | 互联网配对设备复制文件可下载；局域网线格式不变 |
| 1.0.65 | 中继到达的文件在接收前询问 |
| 1.0.66 | 历史行名字（含 `netpair_names` 持久化）；升级后名字不丢 |
| 1.0.67 | 侧车下载缓存 release 签名；更新缓存清理签名与重复安装包；e2e 修复 |
| 1.0.68 | **中继上传超 48 KiB 的剪贴板帧改为分块**，且不再"报成功"（"mac 复制 win 看不到"的根因） |
| 1.0.69 | 中继可达的设备不再在自己的历史行显示"当前不在线" |
| 1.0.70 | 主机把"为什么没安装更新"写进日志（`Update install decision: stage=...`） |

### 仍未解决、且已加了诊断的

**macOS 从对端更新时只弹访达、不自动安装。** 证据（`zzz MacBook-20261009-195755.log`）：

```
18:52:32  Received update blob ClipSync.app.tar.gz from 483fa196a05a
18:52:36  Cached update asset + Cached update signature
18:53:24  clipsync-companion-stop / clipsync-lan-stop      <- 安装路径执行了
18:53:30  Clipboard backend: Darwin ...                    <- 回来还是旧版本
```

1.0.70 起，每个失败分支都会写 `Update install decision: stage=<stage> version=<v> detail=<...>`，
候选值见 `desktop/src-tauri/src/main.rs::install_staged_update`：
`no_staged_update` / `no_embedded_key` / `manifest_signature_mismatch` / `no_manifest_entry` /
`no_usable_signature` / `staged_file_unreadable` / `signature_or_version_refused` /
`bundle_swap_failed` / `installing_offline`。

**下一步是向用户要那次失败的新日志，读出 `stage=` 的值，再改对应的那一处。**
（macOS 的解包与 bundle 替换在 `main.rs::install_macos_from_bytes` 和 `replace_app_bundle`。）

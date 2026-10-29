/**
 * English translations for the desktop shell, keyed by the Chinese source
 * string (see `./index.ts` for why).  A missing key falls back to the Chinese
 * source, so this file can lag the UI without breaking it — but
 * `tests/i18n.test.ts` fails on any untranslated literal in `src/`, so it
 * cannot lag silently.
 *
 * Placeholders use the same `{name}` syntax as the source strings.
 */
export const EN: Record<string, string> = {
  // ── App shell, navigation and status ─────────────────────────────────
  // ── Click confirmations ──────────────────────────────────────────────
  // The window's own answers to its own buttons, for the clicks that change
  // nothing a reader can see — see `toast` in `stores/application.ts`. The
  // first line of each is the notice's title (the page the click was on).
  "已刷新 · {count} 条记录": "Refreshed · {count} records",
  "已刷新 · {count} 台设备": "Refreshed · {count} devices",
  "已刷新 · {count} 条收藏": "Refreshed · {count} favourites",
  "已刷新 · 收藏库是空的": "Refreshed · the library is empty",
  "已刷新 · {count} 个进行中的传输": "Refreshed · {count} transfers in progress",
  "已刷新 · 暂无进行中的传输": "Refreshed · nothing in progress",
  "已刷新 · {count} 个会话": "Refreshed · {count} conversations",
  "已刷新 · 暂无会话": "Refreshed · no conversations",
  "已刷新 · {count} 个备份": "Refreshed · {count} backups",
  "同步已开启": "Sync is on",
  "同步已关闭": "Sync is off",
  "已刷新互联网配对状态": "Internet pairing state refreshed",
  "已重新检测 · {ok}/{total} 项通过": "Re-checked · {ok}/{total} checks passed",
  "已复制收藏内容": "Favourite copied",
  "已置顶这条记录": "Record pinned",
  "已取消置顶这条记录": "Record unpinned",
  "已删除这条记录": "Record deleted",
  "已删除这条收藏": "Favourite deleted",
  "主导航": "Main navigation",
  "剪贴板历史": "Clipboard History",
  "历史记录": "History",
  "文件传输": "File Transfer",
  "附近聊天": "Nearby Chat",
  "无法连接到 {name}，设备可能已离线。": "Could not connect to {name}. The device may be offline.",
  // Toast titles for the runtime events the shell shows as notices.
  "错误": "Error",
  "配对请求": "Pairing request",
  "配对失败。验证码可能已过期。请重新连接。": "Pairing failed. The code may have expired. Try connecting again.",
  "无法读取本机剪贴板，可能被其他程序占用。":
    "Could not read this machine's clipboard; another program may be holding it.",
  "无法写入本机剪贴板，同步的内容没有粘贴过来。":
    "Could not write to this machine's clipboard, so the synced content is not there to paste.",
  "剪贴板内容太大，本次同步已跳过。": "The clipboard was too large and this sync was skipped.",
  "历史记录写入失败，本机磁盘可能已满。":
    "Writing to the history failed — this machine's disk may be full.",
  "配对请求没有发出去，请确认设备仍在同一网络。":
    "The pairing request did not go out. Check that the device is still on this network.",
  "中继收到一条无法解析的消息，已丢弃。":
    "The relay received a message it could not parse, and dropped it.",
  "局域网同步的内部处理出错，本次操作未完成。":
    "Local network sync hit an internal error and this operation did not finish.",
  "同步引擎遇到错误。": "The sync engine ran into a problem.",
  // The one good answer the internet-pairing card has to give, and it arrives
  // from the other machine rather than from anything this window did.
  "已与 {name} 完成互联网配对": "Internet pairing with {name} is complete",
  "收到网址": "Received URL",
  "设备已连接": "Device connected",
  "设备已断开": "Device disconnected",
  "剪贴板同步": "Clipboard sync",
  "在浏览器打开": "Open in browser",
  // A refused connection: the other device only sends the refusal for a peer
  // its user removed, so this names the state and the repair.
  "{name} 已将本机移除 — 需要重新在对方配对":
    "{name} has removed this device — pair again from that device",
  // The other refusal the peer can send: it has this device pinned but could not
  // verify who this is. Usually an older build on this machine, which is why the
  // sentence names the upgrade as the likely fix as well as the re-pair.
  "{name} 无法证明自己的身份 — 请在「设备」页对它重新配对（该设备若为旧版本，升级后即可正常连接）":
    "{name} could not prove its identity — pair again with it from the Devices page (if that device is on an older version, updating it also restores the connection)",
  "找不到 {name} — 请确认该设备已开启 ClipSync 且在同一网络":
    "Can't find {name} — make sure it is running ClipSync and on the same network",
  // The certificate-change prompt. Wording follows the legacy dialog's.
  "设备身份变更": "Device identity changed",
  "设备“{name}”的证书已变更（可能已重装或重置）。":
    "Device “{name}” has a new certificate (likely reinstalled or reset).",
  "这是您信任的设备吗？": "Do you trust this device?",
  "该设备需要再次连接后才能信任新证书。":
    "The device has to connect to this machine again before its new certificate can be trusted.",
  "重新信任": "Trust again",
  "保持不配对": "Keep unpaired",
  "敏感内容未同步": "Sensitive content was not synced",
  "收藏库": "Favorites",
  "设置": "Settings",
  "赞助": "Sponsor",
  "赞助 ClipSync": "Sponsor ClipSync",
  "首次设置": "First-time setup",
  "欢迎使用 ClipSync": "Welcome to ClipSync",
  "先选好语言和外观，再开始使用。两项之后都能在设置里修改。": "Choose your language and look first. You can change both later in Settings.",
  "选择语言": "Choose a language",
  "界面会立即切换，选你觉得顺手的。": "The interface switches immediately — pick whichever feels right.",
  "选择外观": "Choose a look",
  "点一下即可预览，进入软件后再慢慢调也可以。": "Click to preview; you can fine-tune it later in the app.",
  "选好语言后，点击下一步。": "Pick a language, then continue.",
  "下一步": "Continue",
  "上一步": "Back",
  "按下面的顺序做，完成后主界面就会显示设备和剪贴板历史。": "Follow these steps; the main window will show your devices and clipboard history afterwards.",
  "完成，进入 ClipSync": "Finish and open ClipSync",
  "正在保存…": "Saving…",
  "关于": "About",
  "关于 ClipSync": "About ClipSync",
  // The shortcut sheet, folded under 关于: every one of these already worked,
  // and none of them was written down anywhere a reader would look.
  "快捷键": "Keyboard shortcuts",
  "切换左侧页面": "Switch the page in the sidebar",
  "在当前页搜索": "Search the page in front of you",
  "打开设置": "Open settings",
  "保存在设置页或 AI 配置页上改过的东西": "Save what was changed on the settings or AI configuration page",
  "↑ ↓": "↑ ↓",
  "在历史列表中移动": "Move through the history list",
  "复制选中的记录": "Copy the selected record",
  "Delete": "Delete",
  "删除选中的记录": "Delete the selected record",
  "全选当前页的记录": "Select every record on the page",
  "清空搜索框": "Clear the search box",
  "重启": "Restart",
  "重启应用": "Restart app",
  "重启 ClipSync？": "Restart ClipSync?",
  "应用会退出并自动重新启动，同步会短暂中断。":
    "The app will quit and restart automatically; syncing is briefly interrupted.",
  "停止服务": "Stop service",
  "启动服务": "Start service",
  "正在停止": "Stopping",
  "正在启动": "Starting",
  "正在处理": "Working",
  "正在运行": "Running",
  "已停止": "Stopped",
  "等待解锁": "Waiting for unlock",
  "解锁": "Unlock",
  "解锁 ClipSync": "Unlock ClipSync",
  "密码": "Password",
  "连接失败，请重试": "Connection failed, please try again",
  "后台进程不可用，请重试": "The background process is unavailable — please try again",
  "数据目录正被旧版 ClipSync 占用。请关闭旧版应用后重试。":
    "The older ClipSync application is still using this data folder. Close it, then try again.",
  "谁占用了？": "Who is holding it?",
  "数据目录被占用": "The data folder is in use",
  "后台进程起不来，是因为同一个数据目录正被另一份 ClipSync 用着。最常见的来源是旧版还开着：关掉窗口不等于退出，它会留在托盘里继续同步。":
    "The background process cannot start because another copy of ClipSync is using the same data folder. The usual culprit is an older build still running: closing its window does not quit it, and it stays in the tray syncing.",
  "Windows：在托盘里找到 ClipSync 图标选「退出」；或用任务管理器「详细信息」，找进程名里带 clipsync 的那几个。":
    "Windows: pick Quit from the ClipSync icon in the tray, or open Task Manager → Details and look for process names containing clipsync.",
  "macOS：在菜单栏图标里退出；或在「活动监视器」里搜 ClipSync。":
    "macOS: quit from the menu bar icon, or search ClipSync in Activity Monitor.",
  "Linux：运行 ps -ef | grep -i clipsync，看还有哪些进程在跑。":
    "Linux: run ps -ef | grep -i clipsync to see what is still running.",
  "这里不会替你结束进程：结束错了可能把正在写的数据弄坏。确认旧实例都退出之后，点「重新连接」再试。":
    "Nothing here will end a process for you — ending the wrong one can corrupt data being written. Once the old copy is gone, press Reconnect to try again.",
  "后台进程已退出，正在重新启动（第 {attempt} 次）…":
    "The background process exited — restarting it (attempt {attempt})…",
  "操作未完成，请刷新后重试": "The action did not complete — refresh and try again",
  "操作未完成": "The action did not complete",
  "操作失败": "The action failed",
  // The other half of a page that can fail in two ways: a refused action and a
  // read that could not reach the runtime.
  "刷新失败": "Refresh failed",
  "所选记录已变化，请取消并重新选择。": "The selection changed — cancel and select again.",
  "请在 ClipSync 桌面窗口中打开": "Open this in the ClipSync desktop window",
  "跨平台剪贴板共享，支持 Windows、macOS 和 Linux。":
    "Cross-platform clipboard sharing for Windows, macOS and Linux.",
  "在设备之间实时共享剪贴板内容与文件。":
    "Share clipboard content and files between devices in real time.",

  // ── Header / status line ─────────────────────────────────────────────
  "同步运行中": "Syncing",
  "同步已暂停": "Sync paused",
  "同步引擎未启动": "Sync engine not started",
  "连接中": "Connecting",
  "重连中 {attempt}/{max}": "Reconnecting {attempt}/{max}",
  "重连中 {attempt}": "Reconnecting {attempt}",
  "{count} 条未读消息": "{count} unread messages",
  "连接异常": "Connection problem",
  "连接状态未知": "Connection status unknown",
  "在线": "Online",
  "离线": "Offline",
  "已连接": "Connected",
  "已配对": "Paired",
  "正在同步": "Syncing",
  "未配对": "Not paired",
  "已发现": "Discovered",
  "已移除的设备": "Removed device",
  "此设备": "This device",
  "对方平台": "The platform the other device runs",
  "{platform} · {arch}": "{platform} · {arch}",
  "同一网络里的设备打开 ClipSync 后会自己出现在这里；不在同一网络的设备，在“互联网同步（中继）”里用配对码添加。":
    "Devices on this network appear here on their own once ClipSync is running on them; for one that is not on it, add it with a pairing code under Internet sync (relay).",
  "设备": "Devices",
  "设备名称": "Device name",
  "正在向 {name} 索取安装包…": "Asking {name} for its installer…",
  "{name} 保留的安装包（{asset}）不比本机新，所以没有可发送的更新；它需要先自己升级一次":
    "The installer {name} kept ({asset}) is not newer than this build, so it has no update to send; it needs to upgrade itself first",
  // "本机还没有可发送的安装包…" was here for the row's 发送更新 button while that
  // button was dimmed rather than absent.  The row says the same thing in its
  // own sentence now, and offers no button at all when there is nothing to
  // send -- see `deviceVersionNote`.  `i18n.test.ts` fails on a catalog entry
  // nothing in the shell uses, which is why it is gone rather than kept for the
  // button that might come back.
  // "设备备注" and "保存设备备注失败" were here for the row's own note field, which
  // was removed.  `i18n.test.ts` fails on a catalog entry nothing in the shell
  // uses, which is why they are gone rather than left for a feature that might
  // come back: the rename dialog writes the same value under its own strings.
  // One device's share of the clipboard.  "Sync clipboard" rather than "send
  // clipboard", because the switch is read by a user deciding whether this
  // machine's copies belong on that machine at all.
  "同步剪贴板": "Clipboard",
  "向此设备同步剪贴板": "Sync the clipboard to this device",
  "关闭后，本机复制的内容不会发送到这台设备（局域网与中继都不发）。":
    "When off, what you copy here is not sent to this device — over the local network or the relay.",
  "设置剪贴板同步失败": "Could not change the clipboard setting",
  "刷新设备": "Refresh devices",
  "正在读取设备": "Reading devices",
  "暂无设备": "No devices",
  "连接设备": "Connect device",
  "断开连接": "Disconnect",
  "重新连接": "Reconnect",
  // The folded half of an error band: the sidecar's own token, kept because a
  // bug report needs it and folded because it is not an answer to "what now".
  "详情": "Details",
  // The send-URL dialog's two refusals, said under the box rather than on the
  // band at the top of the window.
  "只支持 http 或 https 开头的网址": "Only http and https addresses can be sent",
  "请输入完整网址，例如 https://example.com": "Enter a full address, such as https://example.com",
  "复制诊断信息": "Copy diagnostics",
  // The clear-everything button names how much it would clear; the count travels
  // through the same `{count}` placeholder as every other count in the shell.
  "测试连接": "Test connection",
  "配对": "Pair",
  "与该设备配对": "Pair with this device",
  "该设备当前不可达，无法发起配对；等它上线或检查两端的网络连接":
    "This device cannot be reached right now, so pairing cannot start. Wait for it to come online, or check the connection at both ends.",
  "生成配对码": "Generate pairing code",
  "提交配对码": "Submit pairing code",
  "配对码已生成": "Pairing code generated",
  "配对码：": "Pairing code:",
  "本机配对码": "This device's pairing code",
  "把这串码给对方，让它在自己的设备上输入。":
    "Give this code to the other device to enter on its own screen.",
  "复制配对码": "Copy pairing code",
  "尚未生成": "Not generated yet",
  "重新生成": "Regenerate",
  "重新输入": "Enter it again",
  "对方生成了一串码，输在这里提交，两台设备即通过中继配对。":
    "If the other device made a code, type it here and submit; that pairs the two devices over the relay.",
  "互联网配对的设备": "Internet-paired devices",
  "互联网同步": "Internet sync",
  "配对与连接": "Pairing and connections",
  "同一网络里的设备；互联网配对的设备在「互联网同步」页管理。":
    "Devices on this network; internet-paired devices are managed on the Internet sync page.",
  "跨网络配对与设备连接": "Pairing and device connections across networks",
  "局域网设备": "Local network devices",
  "互联网同步已关闭：下面这些设备现在都联系不上，在线状态是关闭前最后一次读取的结果；打开开关后恢复。":
    "Internet sync is off: none of the devices below can be reached right now, and their online state is the last reading from before the switch was turned off. Turning it back on restores them.",
  "还没有互联网配对的设备。上面两个方向任选一个，两台设备就能隔着网络配对。":
    "No internet-paired devices yet. Either direction above pairs two devices across the network.",
  "输入对方配对码": "Enter the other device's pairing code",
  "安全代码：": "Security code:",
  "确认配对": "Confirm pairing",
  "等待对方确认": "Waiting for the other device",
  "等待确认": "Waiting for confirmation",
  "对方已确认": "The other device confirmed",
  // Not 已取消: a request nobody answered reads as one the other side refused,
  // which is the case the reader has to act on differently.
  "已过期": "Expired",
  "请求超过五分钟未处理会过期，设备行会显示“已过期”，重新配对即可。":
    "A request left unanswered for five minutes expires; the device row reads “Expired” and pairing again is all it takes.",
  // A code that has been sent but whose partner has not answered yet. Not the
  // same sentence as "submitted": this one is about the other machine, and it
  // is the only thing the card can honestly say in that window.
  "等待对方确认…": "Waiting for the other device to confirm…",
  "等待 {name} 确认…": "Waiting for {name} to confirm…",
  "配对码已提交，对方通过中继确认后即会出现在上方设备列表中。":
    "The code has been sent. Once the other device confirms over the relay it appears in the device list above.",
  "提交于 {when}": "Sent {when}",
  "撤销": "Undo",
  "配对码无效，请核对后重新输入。":
    "That pairing code is not valid — check it and enter it again.",
  "本机还未连上中继，暂时无法配对。请稍后重试。":
    "This device is not on the relay yet, so it cannot pair right now. Try again in a moment.",
  "互联网同步已关闭，无法配对。": "Internet sync is off, so pairing is not possible.",
  "配对码未能保存，请重试。": "The pairing code could not be saved. Please try again.",
  "提交配对码失败": "Could not submit the pairing code",
  "解除互联网配对失败": "Could not remove the internet pairing",
  "请核对两台设备上的代码，仅在一致时确认。":
    "Compare the code on both devices and confirm only if they match.",
  "同步与聊天只接受指纹一致的设备，发现不一致请撤销信任后重新配对。":
    "Sync and chat only accept devices whose fingerprints match. If they differ, revoke trust and pair again.",
  "撤销信任": "Revoke trust",
  // What the archived list never said: whether a removed device is still on
  // record, and whether anything will eventually clean it up by itself.
  "移除只是从这台机器的列表里收起来：记录仍在本机，也不会自动清理，随时可以恢复。彻底删除才会把它从本机抹掉。":
    "Removing only takes the device out of this list: its record stays on this machine, nothing purges it on a timer, and it can be restored at any time. Only a permanent delete erases it.",
  // The row's three unbind entries, prefixed with the route each of them ends.
  // A row can hold two pairings, so a reader looking at 撤销信任 and 解除互联网配对
  // side by side had no way to tell which one ended what — and the third,
  // 移除设备, ends both.  The prefix is the only difference between them, so it is
  // in the name rather than in a tooltip.  See U121.
  "局域网：撤销信任": "LAN: revoke trust",
  "中继：解除配对": "Relay: unpair",
  "全部：移除设备": "Both routes: remove device",
  "撤销设备信任？": "Revoke this device's trust?",
  "还有 {count} 条内容在等它上线后补发；撤销后不再补发。": "{count} items are waiting for it to come back; revoking drops them.",
  "本机保留着它的剪贴板历史与聊天记录，撤销只断开同步。": "Its clipboard history and conversation stay on this machine; revoking only ends the sync.",
  "要重新连上，得两台设备再配对一次。": "To reconnect, the two devices have to pair again.",
  "移除设备": "Remove device",
  "移除这台设备？": "Remove this device?",
  "彻底删除": "Delete permanently",
  "彻底删除这台设备？": "Delete this device permanently?",
  "证书指纹": "Certificate fingerprint",
  "查看证书指纹": "View certificate fingerprint",
  "已固定证书指纹": "Pinned certificate fingerprints",
  // The dialog shows the fingerprint whole, in groups of four, and offers it as
  // text: a value that cannot be compared character by character cannot do the
  // one job it has.  The short form stays beside it, labelled.
  "完整指纹：与对方屏幕上的逐个字符核对":
    "Full fingerprint: compare it, character by character, with the one on the other screen",
  "复制完整指纹": "Copy the full fingerprint",
  "复制 {name} 的指纹": "Copy the fingerprint of {name}",
  "复制全部指纹": "Copy all fingerprints",
  "短号 {short}": "Short form {short}",
  "暂无已固定的设备证书": "No pinned device certificates",
  "已打开本地网络权限设置": "Opened the local network permission settings",
  "无法打开系统设置": "Could not open system settings",
  "打开本地网络权限": "Open local network permission",
  "无可用通道": "No available channel",
  "超时": "Timed out",
  "正在检测…": "Checking…",
  "重新检测": "Check again",
  "局域网": "LAN",
  "局域网发现": "LAN discovery",
  "启用局域网发现": "Enable LAN discovery",
  "隐藏本机": "Hide this device",
  "隐藏本机（附近设备看不到此设备）": "Hide this device (invisible to nearby devices)",
  "同步引擎未运行，无法修改发现设置。":
    "The sync engine is not running, so discovery settings cannot be changed.",
  "同步引擎未运行，推送文本与发送网址不可用。":
    "The sync engine is not running; pushing text and sending links are unavailable.",
  "任何人可直接发来消息和文件": "Anyone nearby can send messages and files",
  "关掉以后，附近设备要先经过你同意，才能发消息和文件给你。":
    "Turn this off and a nearby device has to be allowed by you before its message or file arrives.",
  "任何人可直接向我发送文件": "Anyone nearby can send me a file",
  "关掉以后，别人发来的文件会先问你是否接收。聊天的收发不受这个开关影响。":
    "Turn this off and an incoming file asks you first. Chat is unaffected either way.",
  "设备日志": "Device logs",
  "允许其他设备取走本机日志": "Let other devices take this device's log",
  "开着时，网络上任何设备（无需配对）向你索取日志都会得到一份，其中已隐去本机路径、令牌和配对密码；关着时一律回复无法提供。":
    "While this is on, any device on the network — paired or not — that asks for this device's log gets a copy, with this machine's paths, tokens and pairing passwords taken out. While it is off, every such request is answered with a refusal.",
  "收集所有设备日志": "Collect every device's log",
  "打开日志文件夹": "Open the log folder",
  "收来的日志存在「下载/ClipSync-logs」，文件名带设备名和收取时间。":
    "Collected logs are saved under Downloads/ClipSync-logs, each file named for the device it came from and when it was taken.",
  "已向 {count} 台设备索取日志": "Asked {count} devices for their logs",
  "没有可索取日志的设备": "No device to ask for a log",
  "收集日志失败": "Could not collect logs",
  "打开日志文件夹失败": "Could not open the log folder",
  "索取日志失败": "Could not ask for the log",
  "已向 {name} 索取日志，收到后放进「下载/ClipSync-logs」":
    "Asked {name} for its log; it will be filed under Downloads/ClipSync-logs",
  "获取日志": "Get log",
  "请该设备把它的日志发过来，存到「下载/ClipSync-logs」；对方关闭日志共享时会说明":
    "Ask this device to send its log, to be filed under Downloads/ClipSync-logs. A device with log sharing off says so instead.",
  "已收到 {name} 的日志": "Received the log from {name}",
  "{name} 没有共享日志": "{name} is not sharing its log",
  "{name} 的日志没能保存到本机": "The log from {name} could not be saved here",
  "手机 Companion": "Phone Companion",
  "手机伴侣": "Phone Companion",
  "手机访问与远程中继": "Phone access and remote relay",
  "设备与工具": "Devices & tools",
  "同一网络": "Same network",
  "让手机通过浏览器访问这台电脑的剪贴板、文件和聊天。":
    "Let a phone reach this computer's clipboard, files and chat in its browser.",
  "手机和电脑在同一个 Wi-Fi 时，扫二维码就能直接打开。":
    "On the same Wi-Fi, scan the QR code to open it directly.",
  "手机不在同一网络时，通过中继访问；流量端到端加密，但会经过第三方 MQTT 服务器。":
    "On another network, reach it through the relay. Traffic is end-to-end encrypted but passes through a third-party MQTT server.",
  "手机访问地址": "Phone access address",
  "手机服务端口": "Phone service port",
  "手机服务未启动": "Phone service is not running",
  // The card is filled in when the page opens, so this button is a refresh and
  // says so; the old label promised a read that had already happened.
  "刷新服务状态": "Refresh status",
  "正在读取手机服务状态…": "Reading the phone service status…",
  "手机 Companion 二维码": "Phone Companion QR code",
  "网页二维码": "Web QR code",
  "显示二维码": "Show QR code",
  "放大二维码": "Enlarge QR code",
  "启动服务后显示二维码": "The QR code appears after the service starts",
  "开启远程访问后显示二维码": "The QR code appears after remote access is turned on",
  "正在生成二维码…": "Generating QR code…",
  "二维码不可用": "QR code unavailable",
  "发送文件到手机": "Send a file to the phone",
  "正在发送…": "Sending…",
  "已发送到手机：{name}": "Sent to the phone: {name}",
  "网页伴侣未开启": "Web companion is not enabled",
  "用手机相机扫描，打开手机 Companion 页面。":
    "Scan with your phone camera to open the phone Companion page.",
  "旧访问链接将失效，手机需要使用新地址重新连接。":
    "The old link stops working; the phone must reconnect with the new address.",
  "更换访问令牌": "Rotate access token",
  "更换令牌": "Rotate token",
  "更换手机访问令牌？": "Rotate the phone access token?",
  "清除访问令牌": "Clear access token",
  "清除令牌": "Clear token",
  "清除手机访问令牌？": "Clear the phone access token?",
  "清除后手机不再需要令牌，任何能访问该端口的设备都可以直接连接。":
    "After clearing, the phone no longer needs a token, and any device that can reach the port connects directly.",
  "访问令牌已清除，任何能访问该端口的设备都可以直接连接":
    "Access token cleared — any device that can reach this port can connect directly",
  "远程访问已关闭": "Remote access is off",
  "远程访问已开启": "Remote access is on",
  "远程访问设置已保存": "Remote access settings saved",
  "远程令牌已更换，旧二维码已失效": "The remote token was rotated; the old QR code no longer works",
  "远程权限": "Remote permissions",
  "中继服务器": "Relay servers",
  "按顺序尝试的中继服务器": "Relay servers, tried in order",
  "移除中继 {broker}": "Remove relay {broker}",
  "移除中继": "Remove relay",
  "还没有中继服务器，添加一个后再保存。": "No relay servers yet; add one before saving.",
  "添加中继服务器地址": "Add a relay server address",
  "加入": "Add",
  "按顺序尝试，某个中继不可用时自动换下一个；列表顺序就是尝试顺序。公共中继免费且不保证可用，不稳定时可以换成自己的服务器。":
    "Tried in order, moving on when one is unavailable; the list order is the try order. Public relays are free and not guaranteed, so switch to your own server if they keep failing.",
  "恢复默认中继": "Restore default relays",
  "只读（推荐）": "Read-only (recommended)",
  "完整（允许推送、删除、发送聊天）": "Full (allows push, delete, chat)",
  "更换远程令牌": "Rotate remote token",
  "最近访问：{time}": "Last access: {time}",
  "暂无": "None",
  "与手机直接聊天": "Chat directly with the phone",
  "手机：{text}": "Phone: {text}",
  "远程访问 · 与手机直接聊天": "Remote access · chat directly with the phone",
  "无法发送消息": "Could not send the message",
  "手机对话": "Phone conversation",
  "输入消息…": "Type a message…",
  "中继已连接：{broker}": "Relay connected: {broker}",
  "中继未连接（会自动重试）": "Relay not connected (retrying automatically)",
  "手机 Companion 已停止：远程二维码仍然可用，但手机只会看到「电脑端伴侣没有在运行」。先在上面点「启动服务」。":
    "Phone Companion is stopped: the remote QR code still works, but the phone will only see \"Phone Companion is not running on the computer\". Start it with \"Start service\" above first.",
  "手机在线": "Phone online",
  "请先开启远程访问": "Turn on remote access first",
  "请先填写手机页面地址并保存": "Enter and save the phone page address first",
  "访问令牌已清除，无法生成远程二维码":
    "The access token is cleared, so a remote QR code cannot be generated",
  "远程访问二维码": "Remote access QR code",
  "手机不在同一网络时（4G/5G 或其他 Wi-Fi），用相机扫描这个二维码。":
    "When the phone is on another network (4G/5G or a different Wi-Fi), scan this code with its camera.",
  "浏览器预览": "Browser preview",
  "本地数据": "Local data",
  "附近设备": "Nearby devices",
  "版本 {version}": "Version {version}",
  "本机地址 {ip}": "Local address {ip}",
  "已连接 {count} 台": "{count} connected",
  "已配对 {count} 台": "{count} paired",
  "网页伴侣运行中（端口 {port}）": "Web companion running (port {port})",
  "移除于": "Removed",
  "未固定": "Not pinned",
  "连接测试：": "Connection test: ",
  "发送网址到 {name}": "Send a URL to {name}",
  "已发送网址到 {name}": "Sent the URL to {name}",
  "项目主页": "Project home",
  "显示行数": "Lines shown",
  "待投递消息：": "Pending deliveries: ",
  // Relay send states.  These name the same four transitions the ledger
  // records, so the words have to travel with the status, not with a panel.
  "已送达": "Delivered",
  "未送达": "Not delivered",
  "待补发": "Queued",
  "发送中": "Sending",
  // A file's own state inside a conversation.  The words the phone and the web
  // panel already use, so one transfer reads the same wherever it is watched.
  "已发送": "Sent",
  "已接收": "Received",
  "发送中…": "Sending…",
  "接收中…": "Receiving…",
  "准备中…": "Preparing…",
  "等待接受": "Awaiting accept",
  "昨天": "Yesterday",
  "昨天 {time}": "Yesterday {time}",
  // The transcript's own two landmarks.  The day divider itself is an `Intl`
  // date rather than a word, because `昨天 {time}` above already names
  // yesterday on every row under it.
  "未读消息": "Unread messages",
  "跳到最早未读": "Jump to the first unread",
  // Deleting one message deletes this machine's copy of it, and only that.
  // The dialog says so before it happens rather than after.
  "从本机删除": "Delete from this device",
  "从本机删除这条消息？": "Delete this message from this device?",
  "它只会从这台设备上消失，对方仍然能看到它。": "It disappears from this device only — the other side still has it.",
  "这条消息没有删除": "That message was not deleted",
  "删除消息失败": "Could not delete the message",
  "待补发 {count}": "{count} queued",
  "对方离线时内容暂存，上线后自动补发": "Held while the device is offline; sent when it returns",
  "已选择 {count} 条": "{count} selected",
  "一次最多选 {count} 条": "{count} is the most a batch can name",
  "{count} 台局域网设备": "{count} local network devices",
  "与附近设备进行会话": "Conversations with nearby devices",
  "删除": "Delete",
  "删除所选的 {count} 条记录？": "Delete the {count} selected records?",
  "共 {count} 条记录将被移除，此操作无法撤销。":
    "All {count} records are removed; this cannot be undone.",
  "{name} 将无法继续同步，重新连接需要双方再次配对。":
    "{name} can no longer sync; reconnecting needs a fresh pairing on both devices.",
  "{name} 将停止同步并从此列表消失，可在“已移除的设备”中恢复。":
    "{name} stops syncing and leaves this list; restore it from “Removed devices”.",
  "{name} 的移除记录将被永久删除，此操作无法撤销。":
    "{name}'s removal record is deleted permanently; this cannot be undone.",
  "放弃修改并切换": "Discard changes and switch",
  "放弃修改并关闭": "Discard changes and close",

  // ── History ──────────────────────────────────────────────────────────
  "搜索历史记录": "Search history",
  "刷新": "Refresh",
  "正在读取历史": "Loading history",
  "暂无历史记录": "No history yet",
  "当前数据目录中没有可显示的记录": "No records to show in the current data directory",
  "没有匹配的记录": "No matching records",
  "多个关键词用空格分隔，需同时包含；也可以只留一个词再试。": "Spaces separate terms and every one has to match — or try leaving just one word.",
  "桌面窗口连接后显示本地记录": "Local records appear once the desktop window connects",
  "全选当前页": "Select all on this page",
  "复制记录": "Copy record",
  // The right-click menu.  Its wording is the legacy menu's own, catalog key
  // for catalog key, because these are the same five objects it was opened on
  // and a reader who knew the old menu should recognise every line of this one.
  "复制": "Copy",
  "置顶": "Pin",
  "取消置顶": "Unpin",
  "在浏览器中打开链接": "Open link in browser",
  "查看详情": "View Details",
  "复制设备 ID": "Copy Device ID",
  "复制文件名": "Copy file name",
  "复制文件路径": "Copy file path",
  "重命名": "Rename",
  "重命名设备": "Rename device",
  "重命名 {name}": "Rename {name}",
  // The single door onto a device row's less-used actions, now that they are no
  // longer printed inline on every row.
  "更多操作": "More actions",
  "名称只保存在这台设备上，对方看到的仍是自己的名字。":
    "The name is stored on this device only; the other side still sees its own.",
  "已重命名为 {name}": "Renamed to {name}",
  "重命名失败": "Could not rename the device",
  "打开聊天": "Open chat",
  // The device row's menu entry that names the machine before the file, so the
  // transfers page starts on it instead of asking which device again.
  "发送文件…": "Send files…",
  "把文件发给这台设备：在传输页选好文件再发送":
    "Send files to this device: pick them on the transfers page, then send",
  "{name} 当前不在线，无法接收文件": "{name} is offline and cannot receive files",
  // The row's own chat button.  Short because it shares a line with 连接设备 and
  // 断开连接, and the device row is the only place it appears.
  "聊天": "Chat",
  // The two update entries, as one sentence.  The direction of the installer is
  // in the words here because it was the one thing the two arrows could not say.
  "版本不一致：该设备是 {version}，比本机旧":
    "Version mismatch: that device is on {version}, older than this one",
  "版本不一致：该设备是 {version}，比本机旧；本机没有可发送的安装包，可以让它自己检查更新":
    "Version mismatch: that device is on {version}, older than this one, and this one has no installer to send — it can check for updates itself",
  "版本不一致：该设备是 {version}，比本机新":
    "Version mismatch: that device is on {version}, newer than this one",
  // The same two, for a peer that advertises no version at all: the sidecar can
  // still say which build is ahead, and the row must not go quiet about it just
  // because there is no number to name.
  "版本不一致：该设备比本机旧":
    "Version mismatch: that device is older than this one",
  "版本不一致：该设备比本机旧；本机没有可发送的安装包，可以让它自己检查更新":
    "Version mismatch: that device is older than this one, and this one has no installer to send — it can check for updates itself",
  "版本不一致：该设备比本机新":
    "Version mismatch: that device is newer than this one",
  "标记为已读": "Mark as read",
  "复制失败": "Failed to copy",
  "未知": "Unknown",
  // The two lines the conversation menu reports a mute with.  The row's bell
  // says it by its own shape; the menu needs words, because its click changes
  // nothing on screen that the reader is looking at.
  "已静音此设备的消息通知": "Notifications muted for this device",
  "已恢复此设备的消息通知": "Notifications enabled for this device",
  "已复制！": "Copied!",
  "类型：{type} | 来源：{source} | ID：{id}": "Type: {type} | Source: {source} | ID: {id}",
  "收藏": "Save",
  "加入收藏夹": "Add to favorites",
  // The toolbar's pin buttons used to be worded 收藏/取消收藏, which collided
  // with 加入收藏夹 directly beside them: two controls, two different effects,
  // one word.  The context menu already said 置顶, so the toolbar says it too.
  "批量置顶": "Pin selected",
  "批量取消置顶": "Unpin selected",
  "合并推送到电脑": "Merge and push to desktop",
  "已合并推送 {count} 条到本机剪贴板": "Merged and pushed {count} clips to this machine's clipboard",
  "没有可合并的记录": "Nothing to merge",
  "合并后内容过长，无法推送": "The merged clips are too long to push",
  "删除记录": "Delete record",
  "删除所选记录": "Delete selected records",
  "删除这条记录？": "Delete this record?",
  "所选记录将从本地历史中移除，此操作无法撤销。":
    "The selected records are removed from local history; this cannot be undone.",
  "记录将从本地历史中移除，此操作无法撤销。":
    "The record is removed from local history; this cannot be undone.",
  "清空历史": "Clear history",
  "清空全部历史": "Clear all history",
  "清空全部历史记录？": "Clear all history?",
  "批量删除": "Delete selected",
  "上一页": "Previous",
  "下一页": "Next",
  "正在读取完整内容": "Loading full content",
  "记录来源应用": "Source app",
  "来源应用过滤": "Source app filter",
  "推送文本": "Push text",
  "推送": "Push",
  "推送文本到剪贴板并同步": "Push text to the clipboard and sync",
  "要推送的文本": "Text to push",
  "请输入要推送的文本": "Enter the text to push",
  "文本会写入本机剪贴板；同步开启时会同时发送到所有设备。":
    "The text is written to this device's clipboard and, with sync on, sent to all devices.",
  "已推送到本机剪贴板并同步到所有设备":
    "Pushed to the local clipboard and synced to all devices",
  "已推送到本机剪贴板（同步未开启，未广播）":
    "Pushed to the local clipboard (sync is off, so it was not broadcast)",
  "发送网址": "Send URL",
  "发送网址不可用": "Sending a URL is unavailable",
  "没有已连接的设备可以发送。": "No connected devices to send to.",
  "目标设备": "Target device",
  "要发送的网址": "URL to send",
  "网址": "URL",
  "请输入有效的网址": "Enter a valid URL",
  "对方将在默认浏览器中打开该 http(s) 网址。":
    "The other device opens this http(s) URL in its default browser.",
  "发送失败": "Could not send",
  "{name} 请求配对 — 代码：{code}": 'Device "{name}" wants to pair — code: {code}',
  "{name} 已连接": "{name} is now connected",
  "{name} 已断开": "{name} has disconnected",
  // A nearby-chat message, in full: the sender, then what they said.  The
  // notice for an attachment has no text to carry, so it names the file.
  "{name}：{text}": "{name}: {text}",
  "{name} 发来文件：{file}": "{name} sent a file: {file}",
  "文本": "Text",
  "内容": "Content",
  // A history row's kind, and the badges the row carries.  The wire names
  // (TEXT, IMAGE_PNG) are not labels, so the kinds the panel named are named
  // here too; anything else falls back to the type it is.
  "图片": "Image",
  "矢量图": "Vector Image",
  "富文本": "Rich Text",
  "文件": "File",
  "链接": "Link",
  "1 次粘贴": "1 paste",
  "{count} 次粘贴": "{count} pastes",
  // The chip row above the list: the same five kinds, the sort toggle and the
  // empty state that names the kind a chip filtered down to.
  "全部": "All",
  "按类型筛选历史记录": "Filter history by kind",
  "该类型暂无内容": "Nothing of this kind yet",
  "还没有{type}": "No {type} yet",
  "换个过滤条件，或清除过滤查看全部内容。": "Pick another kind, or clear the filter to see everything.",
  "最新优先": "Newest first",
  "最旧优先": "Oldest first",
  "切换历史排序（最新 / 最旧）": "Toggle history ordering (newest / oldest)",
  "标题": "Title",
  "分组": "Group",
  "位置": "Position",
  "未分组": "Ungrouped",
  "未命名": "Untitled",
  "全部正常": "All good",
  "存在故障": "Failure",
  "存在警告": "Warning",
  "失败": "Failed",
  // A diagnostics section's verdict, and a shortcut for the words a check
  // reports about itself.  "Failed" above is the same word the legacy panel's
  // verdict used, so only the other two are new.
  "正常": "OK",
  "警告": "Warn",
  "已保存": "Saved",
  "未保存": "Unsaved",
  "放弃未保存的更改，恢复为已保存的设置":
    "Discard the unsaved changes and restore the saved settings",
  // The save bar's own two remarks about the form.
  "有未保存的更改": "Unsaved changes",
  "所有更改都已保存": "All changes saved",
  // Saved *when*: a reader who looked away and came back could not tell whether
  // the line was written a second ago or before they left.
  "所有更改都已保存 · {time}": "All changes saved · {time}",
  // Refused, said at the button rather than only in the band at the top.
  "保存失败，更改还没有写入": "The save was refused; your changes are not stored yet",
  "已取消": "Cancelled",
  "已关闭": "Closed",
  "已拒绝": "Declined",
  "待确认": "Pending",
  "邀请中": "Inviting",
  "聊天中": "Chatting",
  "关闭": "Close",
  "关闭提示": "Dismiss this notice",
  // Said on the welcome flow's second page: × on this window hides it to the
  // tray rather than quitting, and a reader who learns that by hunting for the
  // off switch has already been surprised by it.
  "关闭这个窗口不会退出 ClipSync：它会留在托盘里继续同步剪贴板。要真正退出，请用托盘菜单里的「退出」。":
    "Closing this window does not quit ClipSync: it stays in the tray and keeps syncing the clipboard. To quit for real, use Quit in the tray menu.",
  "从 {name} 下载这个文件（经互联网，比局域网慢）": "Download this file from {name} (over the internet, slower than on the same network)",
  "关闭会话": "Close conversation",
  "会话": "Sessions",
  "附近没有可聊天的设备。": "No devices nearby to chat with.",
  "会话未关闭": "The session is still open",
  "会话已关闭": "This conversation is closed",
  "消息记录还在，可以重新发起。": "The messages are kept; you can start it again.",
  "重新发起": "Start again",
  "{name} 拒绝了邀请": "{name} declined the invitation",
  "可以再发一次。": "You can ask again.",
  "对方当前离线，消息暂时发不出去。": "That device is offline, so a message cannot go out right now.",
  "对方当前离线": "That device is offline",
  "对方离线，暂时发不出去": "That device is offline, so this cannot be sent yet",
  "选择已配对设备": "Choose a paired device",
  "查看设备": "Device",
  "尚未读取该设备的配置": "That device's config has not been read yet",
  "（{count} 项不同）": "({count} differ)",
  "该设备还没有可同步的配置项": "That device has no config files to sync yet",
  "测试中继连接": "Test relay connections",
  "正在测试…": "Testing…",
  "{total} 个中继中 {reachable} 个可达": "{reachable} of {total} relays reachable",
  "{ms} 毫秒": "{ms} ms",
  "不可达": "Unreachable",
  "（只能浏览）": " (browse only)",
  "该设备版本过旧，只能浏览，不能作为迁移来源": "That device runs an older version; its files can be browsed but not migrated from",
  "选择一个会话": "Choose a conversation",
  "或者点左边的设备，开始一段新对话。": "Or pick a device on the left to start a new one.",
  "暂无聊天会话": "No conversations",
  // The three empty states that used to say 暂无 and stop: each now names what
  // the page is for and offers the one action that fills it.
  "去设备页": "Go to devices",
  "会话在设备之间建立：挑一台在线的机器，或在手机伴侣上发起。":
    "Conversations start between devices: pick one that is online, or start one from the phone companion.",
  "收藏是长期保存的片段：在历史记录里点星标存一条，或者在这里直接写一条。":
    "Favourites are clips kept for good: star one in the history, or write a new one here.",
  "把这个文件发给一台已配对的设备，收发的每一笔都会记在这里。":
    "Send a file to a paired device — every transfer in and out is recorded here.",
  "暂无消息": "No messages",
  "还没有消息": "No messages yet",
  "系统消息": "System message",
  "聊天消息": "Chat messages",
  "发送消息": "Send message",
  "输入消息，按 Enter 发送": "Type a message, press Enter to send",
  "重发消息": "Resend message",
  "刷新聊天": "Refresh chat",
  "发送附件": "Send attachment",
  "打开": "Open",
  "接受": "Accept",
  "拒绝": "Decline",
  "发送": "Send",
  "取消": "Cancel",
  "接收": "Receive",
  "静音": "Mute",
  "取消静音": "Unmute",
  "对方正在输入…": "The other device is typing…",
  "请核对指纹后决定。": "Compare the fingerprint before deciding.",
  "消息未送达": "Message not delivered",
  "无法读取聊天会话": "Could not load conversations",
  "无法读取聊天消息": "Could not load messages",
  "无法打开附件": "Could not open the attachment",
  "无法打开附件所在文件夹": "Could not open the folder containing the attachment",
  "附件发送失败": "Could not send the attachment",
  // The one attachment refusal a reader can act on: the app's own 2 GiB cap.
  // It reached the window as "LAN operation failed" until the runtime started
  // raising `CHAT_FILE_TOO_LARGE` for it.
  "文件超过 2 GB：聊天一次只能发这么大的单个文件，更大的内容请拆开或压缩后再发。":
    "The file is over 2 GB: that is the most a single chat attachment can be, so split or compress anything larger.",
  "{file} 发送失败：{reason}": "{file} could not be sent: {reason}",
  "附件操作失败": "Attachment action failed",
  "附件操作未完成": "The attachment action did not complete",
  "发送消息失败": "Could not send the message",
  "发送邀请失败": "Could not send the invitation",
  "处理邀请失败": "Could not handle the invitation",
  "已接受聊天邀请": "Chat invitation accepted",
  "已拒绝聊天邀请": "Chat invitation declined",
  "关闭会话失败": "Could not close the conversation",
  "设置静音失败": "Could not change the mute state",
  "重发消息失败": "Could not resend the message",
  "正在等待对方接受邀请…": "Waiting for the other device to accept…",
  "正在连接对方，连接上以后会话会出现在列表里。": "Connecting to the other device; the conversation appears in the list once it is up.",
  "正在连接 {name}…": "Connecting to {name}…",
  "连接超时，请重试": "Connection timed out, please try again",

  // ── Transfers ────────────────────────────────────────────────────────
  "传输历史": "Transfer history",
  "已暂停": "Paused",
  "正在写入对方设备…": "Finalizing on the receiving device…",
  "等待对方接受…": "Waiting for the peer to accept…",
  "已完成": "Completed",
  "传输失败": "Transfer failed",
  "传输已取消": "Transfer cancelled",
  "对方已拒绝接收": "The recipient declined the transfer",
  "对方设备已离线": "The other device went offline",
  "传输超时": "Transfer timed out",
  "接收设备磁盘空间不足": "Not enough disk space on the receiving device",
  "文件大小校验失败，传输可能已损坏": "File size check failed — the transfer may be corrupted",
  "传输数据不完整": "Transfer data was incomplete",
  "传输因安全原因被拒绝": "Transfer rejected for security reasons",
  "未知文件": "Unknown file",
  "未知设备": "Unknown device",
  // A failed transfer's own account of itself — the block behind 详情, and the
  // text 复制诊断 puts on the clipboard.  Short labels because they are listed
  // one per line, five lines deep, in a row.
  "复制诊断": "Copy diagnostics",
  "方向": "Direction",
  "对端": "Peer",
  "大小": "Size",
  "时间": "Time",
  "状态": "Status",
  "原因": "Reason",
  "路径": "Path",
  "刷新传输": "Refresh transfers",
  "发送文件": "Send file",
  "发送目标": "Send to",
  "选择接收设备": "Choose a device…",
  "先选择接收设备": "Choose a device first",
  "没有已连接的设备": "No device is connected",
  // The picker has devices but none it can send to — every one is offline, or
  // merely discovered.  The old sentence ("no device is connected") was wrong
  // the moment the list held an internet-paired device the page could not offer.
  "没有可用设备": "No device can be reached",
  "文件发送与接收": "File sending and receiving",
  "文件发送失败": "Could not send the file",
  "打开所在文件夹": "Open containing folder",
  "打开文件": "Open file",
  "无法打开所在文件夹": "Could not open the containing folder",
  "无法读取传输状态": "Could not load transfer status",
  "暂无传输记录": "No transfers",
  "暂无进行中的传输": "No transfers in progress",
  "进行中的传输": "Transfers in progress",
  "接受文件": "Accept file",
  "拒绝文件": "Decline file",
  "取消传输": "Cancel transfer",
  "暂停传输": "Pause transfer",
  "恢复传输": "Resume transfer",
  "重试传输": "Retry transfer",
  "删除传输记录": "Delete transfer record",
  "速度测试": "Speed test",
  "全部取消": "Cancel all",
  "已取消 {count} 个传输": "Cancelled {count} transfers",
  "没有可取消的传输": "Nothing left to cancel",
  // The title is the legacy catalog's own ("Clear Transfer History"); the line
  // under it is the shell's, and says what the legacy confirm did not — that a
  // running transfer is not a record and so is not touched by this.
  "清除传输历史": "Clear Transfer History",
  "共 {count} 条传输记录将被移除，正在进行的传输不受影响，此操作无法撤销。":
    "{count} transfer records will be removed. Transfers in progress are not affected, and this cannot be undone.",
  "已清除 {count} 条传输记录": "Cleared {count} transfer records",
  "没有可清除的传输记录": "No transfer records to clear",
  // "Send Folder" is the legacy catalog's own name for the panel's second send
  // button.  The line beside it is the shell's, and stands in for the panel's
  // archive progress dialog: the archiving happens before the transfer exists,
  // so there is no row to show it on yet.
  "发送文件夹": "Send Folder",
  "正在打包文件夹…": "Archiving the folder…",
  "正在打包 {count} 个文件…": "Archiving {count} files…",
  // The same line once the sidecar has counted what it is packing: the totals
  // before the first member goes in, then the count that is in.  Two sentences
  // because they answer two questions — how much is this, and how far along.
  "正在打包 {count} 个文件 · 共 {size}": "Archiving {count} files · {size} total",
  "已打包 {done}/{total}": "Archived {done}/{total}",
  // A file dropped on the window answers the picker, not the target, and the
  // veil over the window says which of the two the drop is about to answer.
  "松开后在“文件传输”页选择设备": "Drop to choose the device on the File Transfer page",
  "拖放不会自动发送，设备仍由您指定": "Dropping does not send: you still name the device",
  "已拖入 {count} 个文件": "{count} files dropped",
  "点击测试局域网速度": "Click to test LAN speed",
  "测速失败": "Speed test failed",
  "空文件": "(empty file)",

  // ── Favorites ────────────────────────────────────────────────────────
  "搜索收藏": "Search favorites",
  "刷新收藏": "Refresh favorites",
  "正在读取收藏": "Loading favorites",
  "暂无收藏": "No favorites",
  "收藏库暂不可用": "Favorites are unavailable",
  "收藏条目": "Favorites",
  // The page's two tabs: the library, and the editor opened out of it.
  "收藏列表": "Favorites",
  "编辑器": "Editor",
  "收藏页分区": "Favorites sections",
  "编辑器里有未保存的修改": "The editor has unsaved changes",
  "没有正在编辑的收藏": "Nothing is open in the editor",
  "在收藏列表里点一条右边的铅笔来编辑它，或者在这里新建一条。": "Pick a favorite's pencil in the list to edit it, or start a new one here.",
  "新建": "New",
  "新建收藏": "New favorite",
  "编辑": "Edit",
  "编辑收藏": "Edit favorite",
  "删除收藏": "Delete favorite",
  "删除收藏？": "Delete this favorite?",
  "复制收藏": "Copy favorite",
  // The pull modes, each with the one thing it does to this machine's files.
  // The bare names were four characters each and are remembered between
  // sessions, so the explanation is what makes the choice a choice.
  "创建副本（原有的另存为副本）": "Create copies (existing files kept as copies)",
  "覆盖本机配置（用对方版本替换）": "Overwrite local config (replaced by the peer's)",
  "追加到本机配置（接在文件末尾）": "Append to local config (added at the end of the file)",
  "副本不改动本机已有文件：本机没有的直接写入，已有的另存为带设备名的副本。":
    "Copies leave what is already here alone: a file this machine lacks is written in place, one it has is written beside it with the peer's name attached.",
  "覆盖会用对方版本替换本机同名文件；本机原文件另存为 .bak。":
    "Overwrite replaces this machine's copy with the peer's; the file it replaces is kept beside it as a .bak.",
  "追加把对方内容接到本机文件末尾，本机原有内容保留；两端都必须是文本文件。":
    "Append adds the peer's content to the end of this machine's file and keeps what was there; both sides have to be text.",
  "保存": "Save",
  "正在保存": "Saving",
  "导出全部收藏": "Export all favorites",
  // Two formats, one button: Markdown is for reading, JSON is the one that can
  // come back — which is what 导入 reads, and why the entry says so.
  "选择导出格式": "Choose an export format",
  "导出为 Markdown": "Export as Markdown",
  "导出为 JSON（可再导入）": "Export as JSON (can be imported again)",
  "导入收藏": "Import favorites",
  "从 ClipSync 导出的 JSON 文件导入收藏": "Import favorites from a JSON file ClipSync exported",
  "这个文件里没有收藏": "That file has no favorites in it",
  "没有新增：{count} 条收藏都已经在库里": "Nothing new: all {count} favorites are already here",
  "已导入 {count} 条收藏": "Imported {count} favorites",
  "已导入 {added} 条收藏，跳过 {skipped} 条已在库中的": "Imported {added} favorites, skipped {skipped} already here",
  // Selecting rows.  Tidying up twenty rows one at a time was twenty deletes
  // and twenty confirmations, so the list gets a mode that collects them.
  "批量选择收藏": "Select favorites",
  "勾选要处理的收藏": "Tick the favorites to work on",
  "已选 {count} 条": "{count} selected",
  "全选本页": "Select this page",
  "取消全选": "Clear the selection",
  "删除所选": "Delete selected",
  "退出选择": "Done selecting",
  "选择 {title}": "Select {title}",
  "删除所选的 {count} 条收藏？": "Delete the {count} selected favorites?",
  "这 {count} 条收藏将被永久删除。": "These {count} favorites will be deleted for good.",
  "已删除 {count} 条收藏": "Deleted {count} favorites",
  "收藏上一页": "Favorites previous page",
  "收藏下一页": "Favorites next page",
  "全部分组": "All groups",
  "收藏分组": "Favorite groups",
  "新建分组": "New group",
  "分组名称": "Group name",
  "新分组名称": "New group name",
  "重命名分组 {name}": "Rename the group {name}",
  "分组操作 {name}": "Group actions for {name}",
  "重命名或删除分组": "Rename or delete this group",
  "删除分组": "Delete group",
  "删除分组？": "Delete this group?",
  // Deleting a group keeps its favourites: they are moved out of the group,
  // not deleted with it.  The dialog says so because the alternative is what
  // a reader would reasonably fear.
  "分组 {group} 中的 {count} 条收藏会移到未分组，收藏本身不会被删除。": "{count} favorites in {group} will move to Ungrouped. The favorites themselves are kept.",
  "分组 {group} 和其中的 {count} 条收藏都会被删除，此操作不可撤销。": "{group} and the {count} favorites in it will be deleted. This cannot be undone.",
  "连同其中的 {count} 条收藏一起删除": "Delete the {count} favorites in it as well",
  "删除分组和收藏": "Delete group and favorites",
  "已删除分组 {name} 及其中 {count} 条收藏": "Deleted the group {name} and the {count} favorites in it",
  "分组已重命名为 {name}": "Group renamed to {name}",
  "已新建分组 {name}": "Created the group {name}",
  "已删除分组 {name}": "Deleted the group {name}",
  "复制路径": "Copy path",
  "复制导出路径": "Copy the export's path",
  "已复制路径": "Path copied",
  "无法复制到剪贴板": "Could not copy to the clipboard",
  // Saving a body that is already in the library asks rather than merges: the
  // same snippet under two titles is deliberate often enough that neither a
  // silent second copy nor a silent refusal is the right answer.
  "收藏库里已有一条内容完全相同的收藏：{title}": "The library already has a favorite with exactly this body: {title}",
  "已存在相似条目，仍要新建？": "A similar entry already exists — create this one anyway?",
  "打开那一条": "Open that one",
  "仍然新建": "Create it anyway",
  "再改改": "Change it first",
  "上移": "Move up",
  "下移": "Move down",
  "收藏编辑器": "Favorites editor",
  "关闭收藏编辑器": "Close the favorites editor",
  "关闭编辑器": "Close the editor",
  "放弃修改": "Discard changes",
  "继续编辑": "Keep editing",
  "放弃未保存的修改？": "Discard unsaved changes?",
  "当前收藏的修改尚未保存。": "This favorite has unsaved changes.",
  "收藏已在其他位置更新。": "This favorite was updated elsewhere.",
  "重新读取": "Reload",
  "收藏字段超出限制或记录无效": "A favorites field exceeds its limit or the record is invalid",
  "收藏操作失败，请重试": "The favorites action failed — please try again",
  "收藏未删除": "The favorite was not deleted",
  "收藏未复制": "The favorite was not copied",

  // ── Settings: appearance and behaviour ───────────────────────────────
  // The settings page's own chrome: the first card's heading.
  "常规": "General",
  "外观与语言": "Look and language",
  "启动与提示": "Startup and feedback",
  "选完立即生效": "Applies as soon as it is picked",
  /* The group's name, and not the card's: `.settings-nav-label` sits beside the
     chips it introduces now, so 通用 and 常规 next to each other as two copies of
     "General" would read as the same word twice.  The group is the common part;
     the card under it is still the general one.  See U37. */
  "通用": "Common",
  "连接": "Connectivity",
  "数据": "Data",
  "系统": "System",
  "设置分区": "Settings sections",
  "主题": "Theme",
  "跟随系统": "System",
  "浅色": "Light",
  "深色": "Dark",
  "暖色": "Sepia",
  "午夜": "Midnight",
  "高对比": "High contrast",
  "语言": "Language",
  "中文": "Chinese",
  "日本語": "Japanese",

  "启用动画": "Enable animations",
  "启用声音": "Enable sounds",
  "同步": "Sync",
  "恢复同步": "Resume sync",
  "恢复同步失败": "Could not resume sync",
  "暂停同步失败": "Could not pause sync",
  "定时暂停同步": "Pause sync for",
  "15 分钟": "15 minutes",
  "30 分钟": "30 minutes",
  "1 小时": "1 hour",
  "已暂停 · 剩余 {minutes} 分钟": "Paused · {minutes} min left",
  "立即恢复": "Resume now",
  "同步已暂停 {minutes} 分钟": "Sync paused for {minutes} minutes",
  "同步已恢复": "Sync resumed",
  "全局快捷键": "Global hotkeys",
  "启用全局快捷键": "Enable global hotkeys",
  "目录不存在": "no such directory",
  "没有可同步的文件": "nothing syncable in it",
  "这些路径已由勾选的工具包含，不会重复同步：{list}":
    "Already covered by a tool ticked above, so they are not synced twice: {list}",
  "远程配置预览": "Remote config preview",
  "关闭预览": "Close preview",
  "内容已截断": "Truncated",
  "正在读取…": "Reading…",
  "组合键写法如 Ctrl+Shift+V。留空表示不绑定；被别的程序占用时这一行会标红。ClipSync 窗口在前台时，这些键仍归窗口自己用。":
    "Write combinations like Ctrl+Shift+V. Leave a row empty to unbind it; a row turns red when another application already owns that combination. While the ClipSync window is in front, these keys stay the window's own.",
  "呼出窗口": "Show the window",
  "不想手打就点右边的键盘按钮，然后按一次想要的组合键；Esc 取消。手打照样可以。":
    "Instead of typing, press the keyboard button on the right and then press the combination once; Esc cancels. Typing still works.",
  "按下组合键…": "Press a combination…",
  "录制 {label} 的组合键": "Record the shortcut for {label}",
  "按一次组合键": "Press a combination once",
  "正在录制：按下想要的组合键，Esc 取消。":
    "Recording: press the combination you want, or Esc to cancel.",
  "这个键不能单独设成全局快捷键：至少要带一个修饰键（Ctrl、Alt、Shift 或 Cmd），键本身要是字母、数字、F1–F24 或方向键一类。":
    "That key cannot be a global shortcut on its own: it needs at least one modifier (Ctrl, Alt, Shift or Cmd), and the key itself has to be a letter, a digit, F1–F24, an arrow key and so on.",
  "暂停或恢复同步": "Pause or resume sync",
  "复制最近一条（纯文本）": "Copy the latest as plain text",
  "复制为纯文本": "Copy as plain text",
  "已复制为纯文本": "Copied as plain text",
  "这一条没有文字可以复制": "This clip has no text to copy",
  "复制第 {n} 条": "Copy item {n}",
  "已复制第 {n} 条": "Copied item {n}",
  "已复制最近一条（纯文本）": "Copied the latest as plain text",
  // A shortcut pressed for a row the history does not have yet: an empty
  // clipboard history, or a ninth over three clips.
  "还没有第 {n} 条记录": "There is no item {n} yet",
  "还没有可复制的记录": "There is nothing to copy yet",
  "未绑定": "Not set",
  "未生效": "Not registered",
  "仅同步纯文本": "Sync plain text only",
  "复制后置顶": "Pin after copy",
  "开机自动启动": "Start on login",
  "应用过滤模式": "App filter mode",
  "启用应用过滤": "Enable app filter",
  "过滤模式": "Filter mode",
  "排除名单中的应用": "Excluded apps",
  "仅允许名单中的应用": "Allowed apps only",
  "应用过滤进程列表": "App filter process list",
  "进程名列表": "Process name list",
  "每行一条，按名称精确匹配（可用 * 和 ?）：Windows 与 Linux 写进程名（chrome.exe、firefox），macOS 写应用名（Google Chrome）。写错不会报错，只会匹配不上。":
    "One per line, matched against the name exactly (* and ? allowed): process names on Windows and Linux (chrome.exe, firefox), app names on macOS (Google Chrome). A mistake is silent — the line simply never matches.",
  "{count} 条带着目录，匹配不到任何进程：{list}。这里要的是进程名，不是路径。":
    "{count} lines contain a folder and match no process: {list}. This field wants a process name, not a path.",
  "每行一个路径": "One path per line",
  "这里只决定哪些文件参与：配置不会自动同步。要拿到另一台电脑上的配置，去「其他设备」对比后拉取。":
    "This only decides which files take part: config is never synced on its own. To get another computer's config, compare it under “Other devices” and pull.",
  "{count} 个路径没读到内容：{list}": "{count} paths gave nothing: {list}",
  "查看本机配置（{count}）": "See this machine's config ({count})",
  "敏感内容过滤": "Sensitive content filter",
  "银行卡号": "Bank card number",
  "身份证号": "Resident ID number",
  "手机号": "Mobile number",
  "API 密钥": "API key",
  "电子邮箱": "Email address",
  "私钥": "Private key",
  "加密密码": "Encryption password",
  "密钥已设置": "Key is set",
  "未设置密钥": "No key set",
  "保存密钥": "Save key",
  "清除密钥": "Clear key",
  "留空表示不修改": "Leave blank to keep unchanged",
  "保存设置": "Save settings",
  "保存设置失败": "Could not save settings",
  "配置已保存": "Settings saved",
  "历史记录上限": "History entry limit",
  "历史保留天数（0 为不限）": "History retention in days (0 = unlimited)",
  "数据备份": "Data backup",
  "创建备份": "Create backup",
  // What the backup card says while one of its six long calls is running.  The
  // sidecar reports no progress for any of them, so this is a clock and a
  // stage, not a fraction: "elapsed" is something the window knows to be true.
  "正在创建备份": "Creating the backup",
  "正在恢复备份": "Restoring the backup",
  "正在导入历史": "Importing the history",
  "正在导出历史": "Exporting the history",
  "已用时 {seconds} 秒": "{seconds}s elapsed",
  "这一步要读写整个数据目录，可能要几十秒；完成前请先不要关窗。":
    "This reads and writes the whole data directory and can take tens of seconds; please leave the window open until it finishes.",
  "刷新备份": "Refresh backups",
  "可用备份": "Available backups",
  "暂无备份": "No backups",
  "选择备份恢复": "Choose a backup to restore",
  "恢复备份": "Restore backup",
  "恢复备份 {name}": "Restore backup {name}",
  "恢复": "Restore",
  "正在恢复": "Restoring",
  "确认恢复": "Confirm restore",
  "确认恢复备份？": "Restore this backup?",
  "此操作会修改当前数据。恢复过程中请勿退出应用。":
    "This changes your current data. Do not quit the app while it runs.",
  "打开数据文件夹": "Open data folder",
  "打开备份文件夹": "Open backup folder",
  "导入历史": "Import history",
  "导出 CSV": "Export CSV",
  "导出 JSON": "Export JSON",
  "导出 Markdown": "Export Markdown",
  "先把上面的开关打开，这些组合键才会生效；这里填过的内容会保留。":
    "Turn the switch above on and these keys take effect; anything typed here is kept.",
  "16–19 位银联/银行卡号，如 6222 0212 3456 7890":
    "A 16–19 digit UnionPay/bank card number, e.g. 6222 0212 3456 7890",
  "18 位居民身份证号，如 11010519491231002X":
    "An 18-character mainland resident ID, e.g. 11010519491231002X",
  "中国大陆 11 位手机号，如 138 0013 8000":
    "An 11-digit mainland mobile number, e.g. 138 0013 8000",
  "常见令牌前缀，如 sk-…、ghp_…、AKIA…": "Common token prefixes, e.g. sk-…, ghp_…, AKIA…",
  "形如 name@example.com 的地址": "An address such as name@example.com",
  "-----BEGIN … PRIVATE KEY----- 开头的整段": "A whole block starting -----BEGIN … PRIVATE KEY-----",
  "形如 password=… / 密码：… 的赋值行": "An assignment line such as password=… or 密码：…",
  "勾选的类别在同步到其他设备前会被替换为 [FILTERED]。本机记录不受影响，也不会被改写；默认全部关闭。":
    "A ticked category is replaced with [FILTERED] before it syncs to another device. The record on this machine is untouched. All off by default.",

  // ── Diagnostics and logs ─────────────────────────────────────────────
  "系统诊断": "Diagnostics",
  "诊断与维护": "Diagnostics & maintenance",
  "运行诊断": "Run diagnostics",
  "暂时无法获取诊断信息": "Diagnostics are unavailable right now",
  "总体状态：": "Overall status:",
  "修复防火墙": "Fix firewall",
  "已请求修复防火墙规则": "Requested the firewall rule repair",
  "应用日志": "App log",
  "查看日志": "View log",
  "日志行数": "Log lines",
  "日志内容": "Log contents",
  "按级别筛选日志": "Filter the log by level",
  "问题": "Problems",
  // "全部" is already translated above — the log filter reuses that entry.
  "没有需要排查的日志": "Nothing to troubleshoot",
  "暂无日志": "No log entries",
  "正在读取日志…": "Loading log…",
  "导出日志…": "Export log…",
  "正在导出…": "Exporting…",

  // ── Updates ──────────────────────────────────────────────────────────
  "软件更新": "Software update",
  "自动检查更新": "Check for updates automatically",
  "改完立即生效，不用按「保存设置」": "Takes effect at once; no need to press Save settings",
  "每约 6 小时检查一次 GitHub 是否有新版本，同时留意同一网络、同一平台的其他设备；发现更新的版本后会在「软件更新」里提示，点「下载并安装」才会下载、校验并安装。关闭后后台不再发起任何更新请求。":
    "Checks GitHub for a new version about every 6 hours, and also watches for other devices on this network and platform. A newer version is reported in Software update; the download, verification and install begin when you confirm them there. When off, no update requests are made in the background.",
  "立即检查更新": "Check for updates now",
  "正在检查…": "Checking…",
  "已是最新版本": "Up to date",
  "最新版本": "Latest version",
  "发现新版本：": "New version available: ",
  "正在下载…": "Downloading…",
  "正在安装更新，完成后应用会自动重启。":
    "Installing the update. The app restarts on its own when it finishes.",
  "新版本 {version} 已就绪": "Version {version} is ready",
  "已核对来源设备声明的摘要：本机连不上发布服务器，无法与官方摘要比对。":
    "Checked against the checksum the sending device declared: this machine cannot reach the release server, so the published checksum could not be compared.",
  "更新失败": "Update failed",
  // What to do about it, in the reader's language: the fragment that follows is
  // the updater's own English, folded for a report rather than read as advice.
  "可以稍后重试；网络受限时检查代理设置。": "Try again later; if the network is restricted, check the proxy settings.",
  "更新下载失败": "Update download failed",
  "更新安装失败": "Update install failed",
  "下载更新": "Download update",
  "检查更新": "Check for updates",
  "安装更新": "Install the update",
  "失败于：{stage}": "failed while {stage}",
  "再试一次": "Try again",
  "重新检查": "Check again",
  "手动下载": "Download by hand",
  "下载并安装": "Download and install",
  "立即安装": "Install now",
  "下载并安装最新版本，完成后应用会自动重启。":
    "Downloads and installs the latest version, then restarts the app.",
  "自动下载最新版本，下载完成后提示你手动替换旧版本。":
    "Downloads the latest version automatically and then asks you to replace the old one manually.",
  "无法连接更新服务器": "Could not reach the update server",
  "检查更新失败": "The update check failed",
  "请退出当前应用，然后用下方文件替换旧版本。剪贴板历史与设备仍保留在本机。":
    "Quit the app, then replace the old version with the file below. Your clipboard history and devices stay on this machine.",

  // ── A file that lives on another device ──────────────────────────────
  // Its row carries a name and a size, never a path, so the only thing to do
  // with it is ask the machine that has it.  The refusals come back as codes
  // and are worded here, for the peer's window.  下载文件 rather than 下载:
  // the row's neighbours are 复制记录/翻译记录/删除记录, and a bare 下载 reads
  // as the page's own download rather than this row's.
  "下载文件": "Download file",
  "从 {name} 下载这个文件": "Download this file from {name}",
  // A conversation opened from a device row: the row is behind the reader, and
  // this is the line that says which one and how to get back to it.
  "返回设备": "Back to devices",
  "从 {name} 的「聊天」进来": "Opened from {name}",
  // The row itself cannot be asked for: its offer named no entry this build can
  // read, so there is no id to send and no honest click to offer.
  "这条记录无法下载": "This record cannot be downloaded",
  "已请求下载，正在等待那台设备": "Download requested — waiting for that device",
  "已请求下载，对方开始发送后可在文件传输里查看":
    "Download requested — it will appear under File transfers once that device starts sending",
  "{name} 当前不在线": "{name} is offline",
  "{name} 没能发送这个文件：{reason}": "{name} could not send this file: {reason}",
  "那台设备当前不在线": "That device is offline right now",
  "已与那台设备解除配对": "That device is no longer paired",
  "那台设备上已经没有这条记录了": "That record is gone from that device",
  "这个文件不在那台设备上": "The file is not on that device",
  "这条记录不是文件": "That record is not a file",
  "文件已被移动或删除": "The file was moved or deleted",
  "文件太多，请分成几次下载": "Too many files — download them in smaller batches",
  "文件太大，无法传输": "The file is too large to transfer",
  "文件夹里没有可发送的文件": "The folder has no files to send",
  "那台设备没能把文件发出来": "That device could not send the file",
  // The fallback, for a code from a peer newer than this window.  Said rather
  // than shown: a code is not something a reader can act on.
  "请稍后重试": "Please try again shortly",
  // The download button no longer asks the row whether the device looks online:
  // that reading is a 90-second in-process window and is empty after a restart,
  // while the sidecar decides the request anyway.  The hint says what the row
  // cannot confirm, and the request is still offered.
  "{name} 可能不在线，仍可尝试下载":
    "{name} may be offline — you can still try the download",

  // ── A dial that failed, and a pairing that ended ─────────────────────
  // A connect/join click starts a dial that outlives the request; these are the
  // transport's own codes, worded so a reader knows whether to wait, check the
  // network, or pair again.
  "无法连接到 {name}：地址不可达，设备可能不在线":
    "Could not reach {name}: the address is not answering, so the device may be offline",
  "连接 {name} 超时，设备可能不在线":
    "Connecting to {name} timed out — the device may be offline",
  "与 {name} 建立加密连接失败":
    "Could not set up the encrypted connection to {name}",
  "{name} 断开了连接": "{name} closed the connection",
  "{name} 的身份验证没有通过，连接已拒绝":
    "{name} did not prove its identity; the connection was refused",
  "与 {name} 的加密设置不一致，连接已拒绝":
    "{name} uses a different encryption setting; the connection was refused",
  "无法连接 {name}：连接端口被拒绝":
    "Could not connect to {name}: the port was refused",
  "连接 {name} 失败": "Could not connect to {name}",
  // Said by the window when the engine has not answered a dial within its own
  // window: the row's 连接中 would otherwise be the only thing on screen.
  "连接 {name} 仍未完成，设备可能不在线":
    "Connecting to {name} has not finished — the device may be offline",
  // The two ways a pairing request ends without pairing.  Both also leave a word
  // on the row for a minute; the notice is the same news where the reader was
  // looking when the card went.
  "{name} 的配对请求已过期，请重新配对":
    "{name}'s pairing request expired; pair again",
  "{name} 的配对请求已取消": "{name}'s pairing request was cancelled",
  // The non-destructive identity alert: the certificate presented is the one
  // already pinned, so nothing is re-pinned and nothing is unpaired.
  "无法验证 {name} 的身份，本次连接已拒绝；它的证书与已保存的一致。":
    "{name} could not prove its identity, so the connection was refused. Its certificate is the one already saved.",
  "设备身份未能验证": "Device identity unproven",
  "通知": "Notice",
  // Two devices hold different encryption passwords, so a relay channel they
  // both believe in cannot be opened and every frame is dropped in silence.
  "与 {name} 的加密密码不一致，无法同步；请让两端使用同一个加密密码":
    "{name} uses a different encryption password, so syncing cannot work; use the same password on both devices",

  // ── Waiting for a pairing code ───────────────────────────────────────
  // Both halves of a code exchange now have a deadline on the card: the code
  // this machine made can be typed only until it expires, and a code this
  // machine entered gives up if nobody answers.  Without the countdowns the
  // deadline was discovered by the code vanishing.
  "配对码约 {seconds} 秒后过期，过期后需要重新生成":
    "The pairing code expires in about {seconds} seconds; generate a new one after that",
  "配对码已过期，请重新生成": "The pairing code expired — generate a new one",
  "对方没有确认配对码，本次配对已超时，请重新生成或输入配对码":
    "The other device did not confirm the pairing code, so this pairing timed out; generate or enter a new code",
  "{name} 没有确认配对码，本次配对已超时":
    "{name} did not confirm the pairing code, so this pairing timed out",
  "对方没有确认配对码，本次配对已超时":
    "The other device did not confirm the pairing code, so this pairing timed out",
  "配对码已提交，但对方没有确认；请重新输入配对码，或让对方重新生成。":
    "The code was submitted but the other device did not confirm; enter the code again, or ask that device to generate a new one.",
  "若对方未确认，本次等待约 {seconds} 秒后结束。":
    "If the other device does not confirm, this wait ends in about {seconds} seconds.",

  // ── Refreshing the device list ───────────────────────────────────────
  // The scan is a blocking round and answers with the discovery switches; a
  // scan with browsing off is an empty round, not a network with no devices.
  "局域网发现已关闭，未扫描网络（已重新读取 {count} 台设备）":
    "LAN discovery is off, so nothing was scanned; re-read {count} devices",
  "刷新设备失败": "Could not refresh the device list",

  // ── Translation ──────────────────────────────────────────────────────
  "翻译": "Translate",
  "翻译文本": "Translate text",
  "翻译记录": "Translate this record",
  "记录原文": "The record's own text",
  "这条记录没有可翻译的文本": "This record has no text to translate",
  "记录过长，只读取并翻译了前 {count} 个字符": "The record is long; only its first {count} characters were read and translated",
  "输入需要翻译的文本": "Enter the text to translate",
  "翻译源语言": "Source language",
  "翻译目标语言": "Target language",
  "源语言": "Source language",
  "目标语言": "Target language",
  "翻译服务地址": "Translation service URL",
  "翻译 API 密钥": "Translation API key",
  "自动检测": "Detect automatically",

  // ── Internet pairing and relay ───────────────────────────────────────
  "互联网配对": "Internet pairing",
  // The relay's own name, in the four places that carry it: the device page's
  // tab and card heading, and the settings page's fieldset.  Pairing and syncing
  // are two halves of one switch, and calling it by two names made a reader
  // check twice whether their change had taken.
  "互联网同步（中继）": "Internet sync (relay)",
  "AI 配置分区": "AI config sections",
  // The relay link's own line, above the peers it carries.
  "本机中继": "This machine's relay",
  "当前中继：{broker}": "Current relay: {broker}",
  "对方在线与否以中继连接为准；本机中继不可用时，所有设备都会显示为离线。":
    "Whether a device is online is read off the relay connection; while this machine's relay is down, every device reads as offline.",
  // The way out of that line: the broker test lives on the settings page's own
  // card, and without this the page reported a fault nobody could investigate.
  "查看原因": "Find out why",
  // The two routes a device row can be reached by, one chip each — plus the
  // third a history row can carry, which names no peer route at all: the clip
  // was pushed to this machine from its own web panel.
  "本地连接": "Local link",
  "本地": "Local",
  "互联网": "Internet",
  "互联网中继": "Internet relay",
  "网页推送": "Web push",
  "网页": "Web",
  "最后在线 {when}": "Last seen {when}",
  "尚未连接过": "Never connected",
  "XXXX-XXXX-XXXX": "XXXX-XXXX-XXXX",
  "启用互联网同步": "Enable internet sync",
  "已开启": "On",
  "刷新互联网配对": "Refresh internet pairing",
  "关闭后设备之间不再通过中继配对或同步，剪贴板也不离开局域网。":
    "With this off, devices no longer pair or sync over the relay and the clipboard stays on the local network.",
  // Asked once, before the relay is ever switched on, on whichever of the two
  // doors the reader came through.  It is the only setting in the window that
  // sends clipboard text off the LAN, and it used to be the one that said
  // nothing about doing so.
  "开启互联网同步？": "Turn on internet sync?",
  "开启后剪贴板内容会经互联网在设备之间同步，数据由第三方中继服务器转发，不再只在这台路由器下的局域网里。":
    "With this on, clipboard content syncs between devices over the internet: the data is forwarded by a third-party relay server and no longer stays on the local network behind this router.",
  "中继负责转发端到端加密后的数据，无法解密内容，也不会拿到你的中继密码。配对仍需双方确认。":
    "The relay only forwards end-to-end encrypted data — it cannot decrypt the content and never sees your relay password. Pairing still needs both sides to confirm.",
  "随时可以关掉这个开关：关掉后设备之间不再通过中继配对或同步，剪贴板不再离开局域网。":
    "You can turn this off at any time: devices then stop pairing or syncing over the relay and the clipboard no longer leaves the local network.",
  "我了解，开启": "I understand, turn it on",
  "发送更新": "Send update",
  "把本机的安装包发送给该设备": "Send this device's installer to it",
  "获取更新": "Get the update",
  "从该设备获取新版本安装包并安装": "Fetch the newer installer from that device and install it",
  "该设备版本较旧，可以发送更新": "This device is on an older version and can be updated",
  "对方软件版本": "The version the other device is running",
  // Why a device row offers no update action in either direction.  The sidecar
  // sends the code and the shell says it in words.  None of them is about which
  // application the peer runs: the sidecar stopped deciding on that.
  "对方版本过旧，本机不能直接给它发送安装包；先让它自己检查更新升级一次":
    "That device is on too old a version to be sent an installer directly; let it run its own update check once",
  "对方与本机不是同一个平台，双方的安装包都用不了":
    "That device is not on this machine's platform; neither one's installer would work on the other",
  "两台设备版本相同，没有需要发送的安装包":
    "Both devices are on the same version; there is no installer to send",
  "已把更新发送给 {name}": "Sending the update to {name}",
  "本机还没有安装包可发送：本机只保留自己升级时下载的那一个，对方可自行检查更新":
    "Nothing to send: this machine only keeps the installer its own upgrade downloaded, and has none. The other device can run its own update check",
  "已向 {name} 索取安装包，收到后会自动安装并重启":
    "Asked {name} for its installer; this app installs it and restarts on its own once it arrives",
  "发送更新失败": "Could not send the update",
  "获取更新失败": "Could not fetch the update",
  "{name} 上没有可发送的安装包，请在本机检查更新":
    "{name} has no installer to send; run an update check here to download it",
  "{name} 的版本不比本机旧，不需要这个更新":
    "{name} is not on an older version — it does not need this update",
  "{name} 与本机不是同一个平台，装不了这个安装包":
    "{name} is on a different platform and cannot install this package",
  "对方没有可以发送的安装包": "The other device has no installer to send",
  "已通知 {name}，但对方没有回应":
    "Told {name}, but it did not answer — its build may be too old to reply",
  "{name} 有新版本 {version}，可在设备列表向它获取":
    "{name} has version {version}; get it from that device in the device list",
  "{name} 正在把新版本 {version} 发送过来":
    "{name} is sending version {version} over",
  "解除互联网配对": "Remove internet pairing",
  "解除互联网配对？": "Remove this internet pairing?",
  "{name} 将不再通过中继与这台设备同步，恢复需要重新配对一次。":
    "{name} will stop syncing with this device over the relay; bringing it back needs a new pairing",
  // A file to an internet-paired device now goes over the relay (the chunks are
  // cut to a broker message), so the device is a target — and slower than the
  // same file on the local network.  The warning appears with the selection.
  "经互联网中继传输，速度较慢。":
    "Sent over the internet relay, which is slower.",
  // The picker's chosen device stopped being a target — it went offline or lost
  // its route — so the page let go of it and says which one it was.
  "{name} 已无法接收文件，请重新选择接收设备。":
    "{name} can no longer receive files — pick a device again.",
  "中继": "Relay",
  "中继用户名": "Relay username",
  "中继密码": "Relay password",
  // The broker's own per-message ceiling.  Kilobytes, because that is the unit
  // a broker's limits are published in; the row says so, and says that the
  // relay reads it at startup.
  "单条消息大小上限": "Max message size",
  "单位 KB。填中继服务器允许的单条消息上限，传输文件时每个数据块都会按它切分——超出会被服务器直接丢弃。免费中继常见 64 KB，公共中继为 256 KB。修改后需重启生效。":
    "In KB. Enter the largest single message your relay server accepts; file transfers cut every chunk to fit under it, since the server silently drops anything larger. A free tier commonly allows 64 KB, the public relays 256 KB. Takes effect after a restart.",
  // The rate half of the same setting: the ceiling a file transfer paces
  // itself at, and the number a faster private relay is told it can carry.
  "发送速率上限": "Max send rate",
  "单位 KB/s。中继单块没超限也可能丢弃：连发太快时，服务器会悄悄丢掉其中大部分。传输文件时按这个速率匀速发送，逐块确认对方已收到，丢块时自动降速重传。免费中继约 64 KB/s，私有中继更快可调高。修改后对新发起的传输生效。":
    "In KB/s. A relay can drop chunks that are under its size limit too: sent back to back, most of them are quietly discarded. File transfers pace themselves at this rate, confirm each chunk with the other device, and slow down and resend on their own when chunks go missing. A free relay carries about 64 KB/s; raise this for a faster private one. Applies to transfers started after the change.",
  "中继离线": "Relay offline",
  "公共中继地址": "Public relay addresses",
  "私有中继地址": "Private relay addresses",
  "每行一个地址": "One address per line",
  // The two hints under the relay lists, from the legacy panel's own copy.
  "兜底通道：私有中继不可达时启用，并作为镜像让落在不同中继的设备仍能互通。公共中继接受任何客户端，也绝不会拿到你的中继密码。": "Fallback path: used when the private relay is unreachable, and as a mirror so devices sitting on different relays still reach each other. The public relays accept any client and never see your relay password.",
  "主通道：剪贴板数据优先走这些端点，用下方用户名和密码登录。留空则只使用上面的公共中继。": "Primary path: clipboard data goes through these first, logging in with the username and password below. Leave empty to use only the public relays above.",

  // ── AI config ────────────────────────────────────────────────────────
  "AI 配置": "AI config",
  "AI 工具": "AI tools",
  "本机配置": "This machine",
  "其他设备": "Another device",
  "请选择设备": "Choose a device",
  "选择一台已配对设备后，可以对比并拉取它的 AI 工具配置。":
    "Choose a paired device to compare with and pull its AI tool configuration.",
  "读取、编辑本机与已配对设备的 AI 工具配置":
    "Read and edit the AI tool configuration here and on a paired device",
  "正在读取本机可用的 AI 工具…": "Reading the AI tools on this machine…",
  "本机没有可同步的 AI 工具。": "This machine has no AI tools to sync.",
  "勾选要同步的 AI 工具；工具不认识的目录写在下面，每行一个路径。":
    "Tick the AI tools to sync; directories the tools do not know about go below, one path per line.",
  "保存 AI 配置": "Save AI config",
  "AI 配置编辑器": "AI config editor",
  "AI 配置远程设备": "AI config remote device",
  "AI 配置拉取方式": "AI config pull mode",
  "自定义配置路径": "Custom config paths",
  "浏览目录…": "Browse folder…",
  "选一个目录加进上面的列表，不用手打路径。":
    "Add a folder to the list above instead of typing the path.",
  "这个目录已经在列表里：{path}": "That folder is already in the list: {path}",
  "已加入 {path}：保存后才会参与扫描。":
    "Added {path}; it is scanned once the list is saved.",
  "重试失败的 {count} 项": "Retry the {count} that failed",
  "拉取": "Pull",
  "拉取方式": "Pull mode",
  "读取本机配置": "Read local config",
  "读取远程库存": "Read remote inventory",
  "预览": "Preview",
  "保存配置": "Save config",
  "这是 JSON 文件：保存前会先校验格式，不成立就不写入。":
    "This is a JSON file: the format is checked before saving, and nothing is written if it does not parse.",
  "保存后由对应的工具读取；它拒绝时会在这里说明是哪一行。":
    "Saved as text; the tool that reads it loads it next, and if it refuses, this shows which line it refused.",
  "这不是有效的 JSON，保存前请先修正。":
    "This is not valid JSON. Fix it before saving.",
  "这不是有效的 JSON：第 {line} 行第 {column} 列附近有问题，保存前请先修正。":
    "This is not valid JSON: something around line {line}, column {column} is wrong. Fix it before saving.",
  "保存失败": "Save failed",
  "移入回收区": "Move to trash",
  "移入回收区？": "Move to trash?",
  "配置已移入回收区": "Config moved to trash",
  "覆盖本机配置？": "Overwrite local config?",
  "追加到本机配置？": "Append to local config?",
  "确认拉取": "Confirm pull",
  "已请求更新，等待对方返回库存": "Refresh requested; waiting for the other device's inventory",
  "未发送任何配置拉取请求": "No config pull request was sent",
  "AI 配置操作失败": "The AI config action failed",
  "文件超过编辑器读取上限，请使用系统编辑器打开，避免保存不完整内容。":
    "The file exceeds the editor's read limit — open it in your system editor so an incomplete save cannot happen.",
  "上一页本地配置": "Previous local config page",
  "下一页本地配置": "Next local config page",
  "上一页远程配置": "Previous remote config page",
  "下一页远程配置": "Next remote config page",
  "本地配置": "Local config",
  "本地配置分页": "Local config pagination",
  "远程配置分页": "Remote config pagination",
  "请选择 1 至 100 条有效记录": "Please choose 1 to 100 valid records",

  // ── Action results (toasts and status lines) ─────────────────────────
  "已加入收藏夹 {count} 条": "Added {count} to favorites",
  // Batch results.  A dialog that closes on the click that commits has to say
  // how many rows it covered, and a drag already painted in its new place has
  // to say whether the new order was kept.
  "已删除 {count} 条": "Deleted {count} items",
  "已置顶 {count} 条": "Pinned {count} items",
  "已取消置顶 {count} 条": "Unpinned {count} items",
  "顺序已保存": "Order saved",
  "保存顺序失败": "Failed to save the order",
  "已清空 {count} 条历史记录": "Cleared {count} history entries",
  "已导出：{path}": "Exported: {path}",
  "已在浏览器打开：{url}": "Opened in your browser: {url}",
  "已读取 {count} 个配置项": "Read {count} config entries",
  "已读取 {count} 个远程配置项": "Read {count} remote config entries",
  "这台设备已不在列表里，无法读取。": "This device is no longer in the list, so nothing can be read from it.",
  "对方当前离线，读不到它的配置；等它上线再试。": "That device is offline right now, so its configuration cannot be read; try again when it is back.",
  "对方没有返回配置清单——可能是它的版本较旧，或还没开启 AI 配置。": "That device returned no configuration list — its build may be older, or it has not turned on AI configuration yet.",
  "已发送 {count} 个拉取请求，等待文件接收":
    "Sent {count} pull requests; waiting for the files",
  "；部分请求失败：{errors}": "; some requests failed: {errors}",
  "正在接收 {done} / {total} 个文件": "Receiving {done} of {total} files",
  // A file arriving over the internet relay rather than over the same network.
  // The row asks the reader before the file is taken; the notice words the same
  // route for the arrival that lands while the transfers page is not on screen.
  "此文件经互联网传输，速度比同一网络慢。确定要接收吗？":
    "This file travels over the internet, which is slower than on the same network. Receive it?",
  "{file} 经互联网传输，速度比同一网络慢，请在文件传输页确认接收。":
    "{file} travels over the internet, which is slower than on the same network; confirm receiving it on the File Transfer page.",
  "拉取完成：{count} 个文件已更新": "Pull complete: {count} files updated",
  "拉取完成：{ok} 个成功、{failed} 个失败":
    "Pull complete: {ok} succeeded, {failed} failed",
  // Why a pulled file did not land, one per reason the sidecar reports.  These
  // four and the reasons suffix below were added to the shell without entries
  // here, which the catalog test catches: a reader running in English saw the
  // Chinese sentence.
  "不是文本文件，无法追加合并": "Not a text file; append merge does not apply",
  "本地文件备份失败": "The local file could not be backed up",
  "本地文件写入失败": "The local file could not be written",
  "本机没有与之匹配的监控目录": "No watched folder on this machine matched it",
  "：{reasons}": ": {reasons}",
  "备份已创建：{path}": "Backup created: {path}",
  // Settings that reached the file but not the running services: the panel used
  // to say "saved" for both halves, so a failed rebind looked like a success.
  "设置已保存，但未能立即生效，重启后才会使用新配置":
    "Saved, but not applied yet — the new settings take effect after a restart",
  // The factory-reset dialog's last exit: after the reset the history, the
  // favourites and the pairings are gone, and this is the only place a reader
  // can still ask for a copy of them.
  "想留一份副本的话，现在导出备份，重置后还能恢复回来。":
    "If you want to keep a copy, export a backup now — it can be restored after the reset.",
  "先创建备份": "Create a backup first",
  "正在备份…": "Backing up…",
  // Typed confirmation, in the reader's own word for what the button does.
  "输入「{word}」以确认": "Type “{word}” to confirm",
  "已打开：{path}": "Opened: {path}",
  // A restore answers with what it brought back, named rather than dumped:
  // the shell used to show the whole payload here, JSON and all.
  "恢复完成 · {parts}": "Restore complete · {parts}",
  // Parts of that line, in the order they are joined.
  "历史 {count} 条": "{count} history entries",
  // The half the old payload hid: a restore whose rows landed but whose
  // settings could not be persisted still reads as a success without it.
  "有 {count} 项没有完成": "{count} parts did not finish",
  "导出完成：{name}": "Export complete: {name}",
  "导入完成 · {count} 条记录": "Import complete · {count} records",

  // ── Counted / interpolated strings ───────────────────────────────────
  "{count} 条记录": "{count} records",
  "{count} 条收藏": "{count} favorites",
  "打开 {path}": "Open {path}",
  "拉取 {path}": "Pull {path}",
  "移入回收区 {path}": "Move {path} to trash",
  "编辑 {path}": "Edit {path}",
  "预览 {path}": "Preview {path}",
  "预览图": "Preview image",
  "文件夹": "Folder",
  "已不存在": "no longer there",
  "共 {count} 个文件": "{count} files in all",
  "{width} × {height} 像素": "{width} × {height} px",
  "选择记录 {id}": "Select record {id}",
  "选择 {path}": "Select {path}",
  "选择文件夹 {path} 下的全部文件": "Select every file under {path}",
  "与对方不同：缺失 {missing}、对方较新 {remote}、本机较新 {local}":
    "Differs from the peer: {missing} missing, {remote} newer there, {local} newer here",
  "拉取选中项（{count}）": "Pull selected ({count})",
  "选择缺失项（{count}）": "Select missing ({count})",
  "将写入以下 {count} 个配置项：": "These {count} entries will be written:",
  "删除收藏 {title}": "Delete favorite {title}",
  "复制收藏 {title}": "Copy favorite {title}",
  "编辑收藏 {title}": "Edit favorite {title}",
  "{name} 进度": "{name} progress",
  "{label} · 安全码 {code}": "{label} · Security code {code}",
  "第 {index} 条，共 {total} 条：{text}": "Item {index} of {total}: {text}",
  "ClipSync 诊断报告": "ClipSync diagnostics report",
  "版本": "Version",
  "总体状态": "Overall status",
  "复制诊断结果": "Copy diagnostics",
  "诊断结果已复制，可以直接粘贴到反馈里。":
    "Diagnostics copied — paste them straight into a report.",
  "三步开始": "Start in three steps",
  "在另一台设备上装 ClipSync": "Install ClipSync on the other device",
  "同一个局域网里最省事：装好后两台会互相发现。":
    "The same network is easiest: once it is installed the two find each other.",
  "在这台设备上显示二维码让对方扫，或输入对方的配对码；确认之后两台才算互相信任。":
    "Show a QR code on this device for the other to scan, or type its pairing code; the two trust each other only after the confirmation.",
  "复制一次试试": "Copy something to try it",
  "在任何应用里复制一段文字，两台设备的历史里都会出现同一条。":
    "Copy a piece of text in any application and the same item shows up in both machines' history.",
  "这台设备还没有配对的机器，也没有任何历史——上面三步做完，这一页就会显示它们。":
    "This device has no paired machines and no history yet — once the three steps above are done, this page shows both.",
  "安全码是这台设备的身份摘要：两台设备上的码一致，才说明对面确实是它。":
    "The security code is a summary of that device's identity: the same code on both machines is what says the other end really is it.",
  "该设备不支持核对": "This device cannot be checked",
  "测速中 {sent}/{total}": "Testing {sent}/{total}",
  "速度 {mbps} MB/s": "Speed {mbps} MB/s",
  // The panel's three words for a finished speed test, on its own thresholds.
  // It also owned a fourth reading — a run that ended with no measurement is the
  // failure below, not a speed of zero.
  "快速": "Fast",
  "良好": "Good",
  "慢": "Slow",
  "速度测试失败": "Speed test failed",
  // The line under the history card's header: how many records there are, how
  // they ended, and how much has moved.  The panel wrote the same three clauses
  // with the same omission rule — a part with nothing to say is left out, so a
  // list without failures does not report zero of them.
  "已完成 {count} 个": "{count} completed",
  "{count} 成功": "{count} succeeded",
  "{count} 失败": "{count} failed",
  "已导出 {count} 条收藏 → {path}": "Exported {count} favorites → {path}",
  "{title} 将被永久删除。": "{title} will be permanently deleted.",
  "{name} 邀请你聊天": "{name} invites you to chat",
  "{who}：{name}": "{who}: {name}",

  // ── The settings page's own search ───────────────────────────────────
  // The placeholder and the no-match line are the legacy Settings window's own
  // strings; the count line says 分区 rather than the legacy 分组, because in
  // this shell a 分组 is a rail heading and a 分区 is one of the cards.
  "搜索设置…": "Search settings…",
  "清除搜索": "Clear search",
  "{count} 个分区匹配“{query}”": '{count} sections match "{query}"',
  "没有匹配“{query}”的设置项": 'No settings match "{query}"',
  // The quick search: one box over the history and the library.  The two
  // counts carry no noun on purpose — "Found 3" needs no plural form, and the
  // alternative was a `|one` twin for a sentence that can say the same thing
  // without one.
  "搜索": "Search",
  "搜索历史与收藏": "Search history and favorites",
  "搜索历史与收藏…": "Search history and favorites…",
  "搜索范围": "Search scope",
  "输入关键词，在剪贴板历史和收藏里一起找。": "Type a word to look through the clipboard history and the favorites.",
  "正在搜索…": "Searching…",
  "搜索失败，请重试": "Search failed. Try again.",
  "没有找到“{query}”": 'Nothing found for "{query}"',
  "找到 {count} 条": "Found {count}",
  "在{scope}中查看全部 {count} 条": "Show all {count} in {scope}",
  "搜索全部历史与收藏": "Search all history and favorites",

  // ── Network & advanced settings ──────────────────────────────────────
  "网络与高级": "Network & Advanced",
  // The favourites editor's folded field: the sort number is real, but it is not
  // one of the three things a favourite is, and it used to sit between 分组 and
  // 内容 as if it were.
  "高级": "Advanced",
  "位置是从 0 开始的排序号：数字小的排在前面。拖动列表里的手柄会改写它。":
    "The position is a 0-based sort number: lower numbers come first. Dragging a handle in the list rewrites it.",
  // The four group names are the legacy Advanced window's own card titles.
  "剪贴板与同步": "Clipboard & Sync",
  "日志与通知": "Logging & Notifications",
  "TCP 端口": "TCP port",
  // Field-level errors on the settings form: the box is marked and the sentence
  // sits under it, instead of the value being clamped into something the reader
  // did not type.  See U51/U52.
  "TCP 端口要填 1024–65535 之间的整数；1024 以下留给系统。":
    "The TCP port must be a whole number from 1024 to 65535; anything lower is reserved for the system.",
  "数据目录要填绝对路径（例如 D:/ClipSync）；留空则用默认位置。":
    "The data directory must be an absolute path (for example D:/ClipSync); leave it empty to use the default.",
  "{field}里有 {count} 个地址不成立：{list}。每行一个 host:port，或用逗号分隔。":
    "{count} entries in {field} are not addresses: {list}. Put one host:port per line, or separate them with commas.",
  "有 {count} 个字段还不能保存": "{count} fields cannot be saved yet",
  "中继单条消息上限": "Relay message ceiling",
  "中继发送速率": "Relay send rate",
  "{field}不能留空：可填 {min}–{max}，留空会被当成没填、存下原来的值。":
    "{field} cannot be empty: it takes {min}–{max}, and an empty box is treated as not filled in and keeps the old value.",
  "有字段还不能保存，改好之后才能保存": "Some fields cannot be saved yet; fix them first",
  "去修改": "Go fix it",
  "（1024–65535，需重启）": "(1024–65535, restart required)",
  "局域网发现标识": "LAN discovery name",
  "同一标识的设备才能互相发现。保持默认即可，改了要重启。":
    "Only devices with the same name find each other. Keep the default; changing it needs a restart.",
  "显示历史条数": "History items shown",
  "网页上显示最近多少条剪贴板记录（1–500）": "How many recent clipboard items the web page shows (1–500)",
  "同步发送间隔（秒）": "Gap between sends (seconds)",
  "连续复制时，两次同步之间至少隔这么久。调大可以减少发送次数（0.05–10 秒）。":
    "When you copy one item after another, syncs wait at least this long. Raise it to send less often (0.05–10 s).",
  "检查剪贴板的间隔（秒）": "Check the clipboard every (seconds)",
  "多久检查一次剪贴板有没有新内容。调大会更省电，但新内容出现得慢（0.1–60 秒）。":
    "How often the clipboard is checked for something new. Longer saves power but new items show up later (0.1–60 s).",
  "接收目录": "Receive directory",
  "留空使用默认": "Leave empty for the default",
  "传输超时（秒）": "Transfer timeout (seconds)",
  "文件传输超过此时间视为失效（5–3600 秒）": "A file transfer longer than this is considered stale (5–3600 s)",
  "最大重连次数": "Max reconnect attempts",
  "日志级别": "Log level",
  "低内存模式（更少预览、更慢轮询）": "Low memory mode (fewer previews, slower polling)",
  "重试剪贴板捕获": "Retry clipboard capture",
  "去重方式": "Dedup method",
  "SHA-256（内容哈希）": "SHA-256 (content hash)",
  "简单（快速）": "Simple (fast)",
  "数据目录": "Data directory",
  "选择目录…": "Choose folder…",
  "选好的目录会填进上面；能不能写入要等保存后侧车试过才知道。":
    "The folder you pick is put in the field above; whether it is writable is only known after it is saved.",
  "配置 / 历史 / 收藏的存储位置。重启后生效。": "Where config / history / favorites are stored. Applies after restart.",
  "部分更改将在重启后生效。": "Some changes take effect after a restart.",

  // ── Security settings ────────────────────────────────────────────────
  "安全": "Security",
  "启用端到端加密": "Enable end-to-end encryption",
  "加密设备之间的剪贴板、文件与聊天流量。设置密码后，重启应用需要输入密码解锁。":
    "Encrypts clipboard, file and chat traffic between devices. Once a password is set, the app asks for it on every restart.",
  "更换加密密码": "Change encryption password",
  "设置加密密码": "Set encryption password",
  // "加密密码" is already in the filter-category block above.
  "确认加密密码": "Confirm encryption password",
  "显示密码": "Show the password",
  "隐藏密码": "Hide the password",
  "两次输入一致": "Both boxes match",
  "两次输入不一致": "The two boxes differ",
  "长度至少 12 位": "At least 12 characters",
  "包含大写字母": "Contains an uppercase letter",
  "包含小写字母": "Contains a lowercase letter",
  "包含数字": "Contains a digit",
  "包含特殊字符": "Contains a special character",
  "密码会随“保存设置”一起提交，并同时用作设备配对的通道密钥；剪贴板历史的密钥在重启后更新。":
    "The password is submitted with “Save settings” and also derives the device-pairing channel keys; the clipboard history key is updated after a restart.",
  "已设置加密密码": "Encryption password is set",
  "未设置加密密码": "No encryption password",
  "本机文件已加密：剪贴板历史与设备私钥用该密码存放。":
    "The files on this machine are encrypted: the clipboard history and the device key are stored under that password.",
  "清除加密密码": "Clear encryption password",
  "危险区域": "Danger zone",
  "清空历史只会删除剪贴板记录；恢复出厂设置还会删除收藏、配对和设备身份，并重新启动应用。两者都无法撤销。":
    "Clearing history removes clipboard records only; factory reset also removes favorites, pairings and this device's identity, then restarts the app. Neither can be undone.",
  "恢复出厂设置": "Factory reset",
  "清除加密密码？": "Clear the encryption password?",
  "清除后重启不再需要密码，加密流量改用设备身份派生密钥。":
    "After clearing, restarts no longer ask for a password and encrypted traffic uses a key derived from the device identity.",
  "清除": "Clear",
  "筛选路径…": "Filter paths…",
  "展开或折叠 {name}": "Expand or collapse {name}",
  "迁移向导": "Migration wizard",
  "AI 配置迁移向导": "AI config migration wizard",
  "把来源设备的配置与技能带到本机：先对比，再选冲突策略，最后一次拉取。":
    "Bring the source device's config and skills here: compare, choose a conflict strategy, then one pull.",
  "来源设备": "Source device",
  "迁移来源设备": "Migration source device",
  "先选择一台已配对设备": "Choose a paired device first",
  "只补缺失（默认）": "Only what is missing (default)",
  "全部拉取，已有的另存为副本": "Pull everything, keep existing files as copies",
  "全部拉取，覆盖本机文件": "Pull everything, overwrite local files",
  "冲突策略": "Conflict strategy",
  "本机已有的配置一个都不动，只把缺的补上。":
    "Nothing already on this machine is touched; only the gaps are filled.",
  "本机已有的同名文件保留，拉下来的另存一份。文件会变多，但谁都不会被覆盖。":
    "Files already here are kept and the incoming ones are saved beside them. The disk gains files; nothing is overwritten.",
  "本机的同名文件会被替换，替换前留一份 .bak。这是三个选项里唯一会改动已有内容的。":
    "Files already here are replaced, and a .bak copy is kept. This is the only one of the three that changes what is already here.",
  "将处理这些配置项（{count} 个）：": "These entries will be handled ({count}):",
  "{count} 项 · {files} 个文件": "{count} entries · {files} files",
  "没有需要迁移的配置项": "Nothing to migrate",
  "将补齐本机缺少的 {count} 个配置项": "Will fill in what is missing from {count} config entries",
  "将拉取 {count} 个不同的配置项，本机已有的另存为副本":
    "Will pull {count} differing config entries, saving local copies of the ones that exist",
  "将覆盖 {count} 个不同的配置项，被覆盖的原文件保留为 .bak":
    "Will overwrite {count} differing config entries, keeping the replaced originals as .bak",
  "（{count} 个文件）": "({count} files)",
  "开始迁移（{count}）": "Start migration ({count})",
  "搜索本机配置": "Search local config",
  "搜索远程配置": "Search remote config",
  "没有匹配的配置项": "No config entries match",
  "本机还没有可同步的配置项：勾选的工具可能尚未安装，或还没有生成配置文件。":
    "Nothing syncable here yet: a tool ticked above may not be installed, or may not have written its config files yet.",
  "与对方一致": "Matches the peer",
  "缺失": "Missing",
  "对方较新": "Newer there",
  "本机较新": "Newer here",
  "尚未读取本机配置，暂时无法对比版本差异":
    "This machine's config has not been read, so the versions cannot be compared yet",
  "清除选择": "Clear selection",
  "选择本页": "Select page",
  "选择本页全部远程配置": "Select every remote entry on this page",
  "恢复出厂设置？": "Factory reset?",
  "全部历史、收藏、配对和设备身份都会被删除，应用随即重新启动。此操作无法撤销。":
    "All history, favorites, pairings and the device identity are deleted and the app restarts immediately. This cannot be undone.",
  "正在重置": "Resetting",
  "确认重置": "Reset everything",
  "修复数据": "Repair data",
  "数据修复": "Data repaired",
  "已将损坏的文件移到一旁：{files}": "Damaged files were moved aside: {files}",
  "修复数据目录？": "Repair the data folder?",
  "剪贴板历史、设备身份或配置已损坏，应用无法启动。修复会把这些文件改名移到一旁（不会删除），并在原位置新建配置。":
    "The clipboard history, device identity or configuration is damaged, so the app cannot start. Repair renames those files and moves them aside — nothing is deleted — and creates a fresh configuration in their place.",
  "移开的文件可以手动改回原名恢复。":
    "The moved files can be restored by renaming them back.",
  "移开的文件留在同一个数据目录里，文件名后面加上 .broken 和时间戳。":
    "The renamed files stay in the same data folder, with .broken and a timestamp appended to the name.",
  "先打开数据文件夹留一份": "Open the data folder and take a copy first",
  "正在修复": "Repairing",
  "确认修复": "Repair now",
  "本机文件未加密": "Files on this machine are not encrypted",
  "没有设置加密密码时，密钥由公开的设备指纹派生：任何能读取本机文件的程序或人都能解开剪贴板历史与设备私钥。":
    "Without a password the key is derived from the device fingerprint, which is public: anything that can read these files can open the clipboard history and the device's private key.",

  // ── The overview page ────────────────────────────────────────────────
  // The dashboard the window opens on: what this machine is doing, the four
  // switches that change it, and the way in to the three pages its rows lead
  // to.  Most of its words are the legacy panel's own (快速控制, 网络地图,
  // 最近活动, the three network-health verdicts); the ones that differ are
  // noted where they sit.
  "概览": "Overview",
  "跳到主内容": "Skip to the content",
  "概览暂不可用": "Overview unavailable",
  "同步引擎未运行，无法读取概览。": "The sync engine is not running, so the overview cannot be read.",
  "正在读取概览": "Reading the overview",
  "读取概览失败": "The overview could not be read",
  "本机状态与最近活动": "This device's state and recent activity",
  "以太网": "Ethernet",
  "运行时间": "Uptime",
  "网络正常": "Network healthy",
  "网络需注意": "Network needs attention",
  "网络异常": "Network problem",
  "检查网络": "Check the network",
  "打开网络诊断": "Open network diagnostics",
  "编辑设备名称": "Edit device name",
  "设备名称已更新": "Device name updated",
  "设备 ID": "Device ID",
  "平台": "Platform",
  "本地地址": "Local address",
  "复制地址": "Copy address",
  "快速控制": "Quick controls",
  "发现": "Discovery",
  // The overview's switches, named the way the settings card names them.
  // "在局域网可见" is the one half of that pair this catalogue did not have:
  // the settings card says the same thing turned inside out ("隐藏本机").
  "在局域网可见": "Visible on the LAN",
  "远程访问": "Remote access",
  "已撤销未保存的更改": "Undid the unsaved changes",
  "共 {count} 台已配对": "{count} paired in total",
  "今天": "today",
  "已置顶": "pinned",
  "连不上后台服务（{time} 起）。下面显示的是当时读到的内容，可能已经过时。":
    "Cannot reach the sidecar (since {time}). What is shown below is what it last returned, and may be out of date.",
  "已固定 · Esc 关闭": "Pinned · Esc closes",
  "传输": "Transfers",
  "网络地图": "Network map",
  "已配对（离线）": "Paired (offline)",
  "已连接设备": "Connected devices",
  "还没有已连接的设备——复制内容并配对一台设备即可开始同步。":
    "No devices are connected yet — copy something and pair a device to start syncing.",
  "发送链接到设备": "Send a link to a device",
  "最近活动": "Recent activity",
  "查看全部": "View all",
  "复制到剪贴板": "Copy to clipboard",
  "已复制": "Copied",
  // The status line's answer to a press that arrived while the previous one was
  // still in flight — the press is dropped, not queued, and this says so.
  "正在处理上一步…": "Still finishing the last action…",
  "复制文本、图片和文件时，剪贴板活动将显示在这里。":
    "Clipboard activity appears here as you copy text, images and files.",
  "（无内容）": "(no content)",
  "刚刚": "just now",
  "{minutes} 分钟前": "{minutes} min ago",
  "{hours} 小时前": "{hours} h ago",
  "{days} 天前": "{days} d ago",
  "另有 {count} 台未在此列出": "{count} more not listed here",
  "「安全」里两次输入的加密密码不一致，改好之前不能保存。":
    "The two encryption passwords on the Security card do not match — fix that before saving.",
  "「安全」里的加密密码还不满足下面的要求，改好之前不能保存。":
    "The encryption password on the Security card does not meet the rules below yet — fix that before saving.",
  "当前版本的服务端不支持诊断。": "This version of the sidecar cannot run diagnostics.",
  "同步引擎未运行，无法运行诊断。": "The sync engine is not running, so diagnostics cannot run.",
  "只显示最近 {count} 条消息": "Showing the newest {count} messages only",
  // The transfer history's fold (U134): the list holds every record it has ever
  // made, and these two lines are the way to see past the first fifty.
  // The sync card's fold (U186): the relay boxes are for a relay that is not
  // the shipped one, so they wait behind a line of their own.
  "地址和凭据都不用改：留空就用公共中继。要接私有中继、或中继限速时再展开。":
    "The addresses and credentials need no changes: leave them empty and the public relay is used. Open this for a private relay, or when a relay limits the rate.",
  "高级：中继地址与限速": "Advanced: relay addresses and rate limits",
  "收起中继的高级设置": "Hide the advanced relay settings",
  "只显示最近 {count} 条": "Showing the newest {count} only",
  "显示其余 {count} 条": "Show the other {count}",
  "加载更早的 {count} 条": "Load {count} earlier",
  "{count} 条新消息": "{count} new messages",
  "本页 {rows} 条，超过一次可选的 {count} 条，请逐条勾选。": "This page holds {rows}; a batch may name {count}, so tick rows one by one.",

  // ── Plurals ──────────────────────────────────────────────────────────
  // English needs “1 record” where Chinese says “1 条记录”, and the catalog
  // carried the difference as “(s)” in thirty-odd entries — “transfer(s)”,
  // “file(s)”, “part(s)” — which is not English.  A count now picks a form:
  // `t()` looks for `"<key>|one"` and `"<key>|other"` and falls back to the
  // unsuffixed entry, so the strings that do not need this keep one form.
  // Only entries whose noun is countable are here; “Deleted {count}” and the
  // like read the same at any number and stay where they were.  See U95.
  "已刷新 · {count} 条记录|one": "Refreshed · {count} record",
  "已刷新 · {count} 条记录|other": "Refreshed · {count} records",
  "已刷新 · {count} 台设备|one": "Refreshed · {count} device",
  "已刷新 · {count} 台设备|other": "Refreshed · {count} devices",
  "已刷新 · {count} 条收藏|one": "Refreshed · {count} favourite",
  "已刷新 · {count} 条收藏|other": "Refreshed · {count} favourites",
  "已刷新 · {count} 个进行中的传输|one": "Refreshed · {count} transfer in progress",
  "已刷新 · {count} 个进行中的传输|other": "Refreshed · {count} transfers in progress",
  "已刷新 · {count} 个会话|one": "Refreshed · {count} conversation",
  "已刷新 · {count} 个会话|other": "Refreshed · {count} conversations",
  "已刷新 · {count} 个备份|one": "Refreshed · {count} backup",
  "已刷新 · {count} 个备份|other": "Refreshed · {count} backups",
  "{count} 条未读消息|one": "{count} unread message",
  "{count} 条未读消息|other": "{count} unread messages",
  "已向 {count} 台设备索取日志|one": "Asked {count} device for its log",
  "已向 {count} 台设备索取日志|other": "Asked {count} devices for their logs",
  "{count} 台局域网设备|one": "{count} local network device",
  "{count} 台局域网设备|other": "{count} local network devices",
  "{count} 条记录|one": "{count} record",
  "{count} 条记录|other": "{count} records",
  "{count} 条收藏|one": "{count} favorite",
  "{count} 条收藏|other": "{count} favorites",
  "已删除 {count} 条|one": "Deleted {count} item",
  "已删除 {count} 条|other": "Deleted {count} items",
  "已置顶 {count} 条|one": "Pinned {count} item",
  "已置顶 {count} 条|other": "Pinned {count} items",
  "已取消置顶 {count} 条|one": "Unpinned {count} item",
  "已取消置顶 {count} 条|other": "Unpinned {count} items",
  "已清空 {count} 条历史记录|one": "Cleared {count} history entry",
  "已清空 {count} 条历史记录|other": "Cleared {count} history entries",
  "历史 {count} 条|one": "{count} history entry",
  "历史 {count} 条|other": "{count} history entries",
  "导入完成 · {count} 条记录|one": "Import complete · {count} record",
  "导入完成 · {count} 条记录|other": "Import complete · {count} records",
  "共 {count} 个文件|one": "{count} file in all",
  "共 {count} 个文件|other": "{count} files in all",
  "已导出 {count} 条收藏 → {path}|one": "Exported {count} favorite → {path}",
  "已导出 {count} 条收藏 → {path}|other": "Exported {count} favorites → {path}",
  "{count} 条新消息|one": "{count} new message",
  "{count} 条新消息|other": "{count} new messages",
  "只显示最近 {count} 条消息|one": "Showing the newest {count} message only",
  "只显示最近 {count} 条消息|other": "Showing the newest {count} messages only",
  "已取消 {count} 个传输|one": "Cancelled {count} transfer",
  "已取消 {count} 个传输|other": "Cancelled {count} transfers",
  "已清除 {count} 条传输记录|one": "Cleared {count} transfer record",
  "已清除 {count} 条传输记录|other": "Cleared {count} transfer records",
  "已拖入 {count} 个文件|one": "{count} file dropped",
  "已拖入 {count} 个文件|other": "{count} files dropped",
  "有 {count} 项没有完成|one": "{count} part did not finish",
  "有 {count} 项没有完成|other": "{count} parts did not finish",
  "{count} 个分区匹配“{query}”|one": "{count} section matches \"{query}\"",
  "{count} 个分区匹配“{query}”|other": "{count} sections match \"{query}\"",
  "{count} 个路径没读到内容：{list}|one": "{count} path gave nothing: {list}",
  "{count} 个路径没读到内容：{list}|other": "{count} paths gave nothing: {list}",
  "{count} 条带着目录，匹配不到任何进程：{list}。这里要的是进程名，不是路径。|one":
    "{count} line contains a folder and matches no process: {list}. This field wants a process name, not a path.",
  "{count} 条带着目录，匹配不到任何进程：{list}。这里要的是进程名，不是路径。|other":
    "{count} lines contain a folder and match no process: {list}. This field wants a process name, not a path.",
  "正在打包 {count} 个文件…|one": "Archiving {count} file…",
  "正在打包 {count} 个文件…|other": "Archiving {count} files…",
  "正在打包 {count} 个文件 · 共 {size}|one": "Archiving {count} file · {size} total",
  "正在打包 {count} 个文件 · 共 {size}|other": "Archiving {count} files · {size} total",
  "已发送 {count} 个拉取请求，等待文件接收|one": "Sent {count} pull request; waiting for the files",
  "已发送 {count} 个拉取请求，等待文件接收|other": "Sent {count} pull requests; waiting for the files",
  "拉取完成：{count} 个文件已更新|one": "Pull complete: {count} file updated",
  "拉取完成：{count} 个文件已更新|other": "Pull complete: {count} files updated",
  "已读取 {count} 个配置项|one": "Read {count} config entry",
  "已读取 {count} 个配置项|other": "Read {count} config entries",
  "已读取 {count} 个远程配置项|one": "Read {count} remote config entry",
  "已读取 {count} 个远程配置项|other": "Read {count} remote config entries",
  "将写入以下 {count} 个配置项：|one": "This {count} entry will be written:",
  "将写入以下 {count} 个配置项：|other": "These {count} entries will be written:",
  "共 {count} 条传输记录将被移除，正在进行的传输不受影响，此操作无法撤销。|one": "{count} transfer record will be removed. Transfers in progress are not affected, and this cannot be undone.",
  "共 {count} 条传输记录将被移除，正在进行的传输不受影响，此操作无法撤销。|other": "{count} transfer records will be removed. Transfers in progress are not affected, and this cannot be undone.",
  "将拉取 {count} 个不同的配置项，本机已有的另存为副本|one": "Will pull {count} differing config entry, saving a local copy of the ones that exist",
  "将拉取 {count} 个不同的配置项，本机已有的另存为副本|other": "Will pull {count} differing config entries, saving local copies of the ones that exist",
  "将覆盖 {count} 个不同的配置项，被覆盖的原文件保留为 .bak|one": "Will overwrite {count} differing config entry, keeping the replaced original as .bak",
  "将覆盖 {count} 个不同的配置项，被覆盖的原文件保留为 .bak|other": "Will overwrite {count} differing config entries, keeping the replaced originals as .bak",
  "将补齐本机缺少的 {count} 个配置项|one": "Will fill in the {count} config entry this machine is missing",
  "将补齐本机缺少的 {count} 个配置项|other": "Will fill in the {count} config entries this machine is missing",
};

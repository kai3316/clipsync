# ClipSync 现代化改造计划

> 参考 QuickClipboard，将 ClipSync 桌面 UI 迁移到 Vue 3 + Tailwind CSS，后端 100% 保留。

---

## 一、背景与目标

**ClipSync 现状**：核心后端（P2P 加密同步、mDNS 发现、文件传输、剪贴板监控）稳定可用。但 UI 基于 customtkinter，美观上限低、动画能力弱、交互细节粗糙。

**QuickClipboard**：Tauri 2.0 (Rust + React + Tailwind CSS) 构建的 Windows 剪贴板管理器，1538 stars。功能极度丰富，UI 精致。

**目标**：将桌面 UI 迁移到 Web 前端技术栈，同时保留 Python 后端 100% 不变。实现 QuickClipboard 的核心功能特性，达到同等甚至更好的用户体验。

---

## 二、参考代码库

### QuickClipboard 源代码
```
路径: C:\Users\sukai\Desktop\quickclipboard_analysis
Repo: https://github.com/mosheng1/QuickClipboard
Stars: 1538 | 技术栈: Tauri 2.0 (Rust + React + Tailwind CSS)
```

实施过程中可直接查阅的关键文件：

| 要实现的功能 | 参考文件 |
|---|---|
| **CSS 设计令牌** | `src/shared/styles/index.css` — `:root` 变量 + `.theme-dark` 覆写 |
| **动画系统** | `src/shared/styles/animations.css` — slide/fade/bounce/dropdown 全套动画 |
| **剪贴板捕获** | `src-tauri/src/services/clipboard/monitor.rs` — 多轮重试 + SHA256 去重 |
| **粘贴处理** | `src-tauri/src/services/paste/paste_handler.rs` — 全格式还原 + Ctrl+V 模拟 |
| **粘贴选项** | `src-tauri/src/services/paste/options.rs` — PasteAction 枚举 + 智能默认 |
| **键盘模拟** | `src-tauri/src/services/paste/keyboard.rs` — Ctrl+V/Shift+Insert + Alt 冲突处理 |
| **来源追踪** | `src-tauri/src/services/system/app_filter.rs` — ClipboardFormatListener + 进程检测 |
| **内容处理器** | `src-tauri/src/services/clipboard/processor.rs` — 文本/HTML/文件/图片分类 |
| **内容类型** | `src/shared/utils/contentType.js` — getPrimaryType / hasType |
| **数据库模型** | `src-tauri/src/services/database/models.rs` — 完整表结构 (ClipboardItem/FavoriteItem/GroupInfo) |
| **设置模型** | `src-tauri/src/services/settings/model.rs` — 100+ 配置项参考 (含截图/AI/外观/快捷键) |
| **快捷键系统** | `src-tauri/src/services/system/hotkey.rs` — 18+ 快捷键注册/注销/重载 |
| **快捷粘贴窗口** | `src/windows/quickpaste/App.jsx` — 滚轮选择 + 松开粘贴 + 序号+类型标签 |
| **主界面 App** | `src/windows/main/App.jsx` — 标签切换 + 窗口动画 + 焦点管理 |
| **历史列表** | `src/windows/main/components/ClipboardList.jsx` — react-virtuoso 虚拟滚动 + 按需加载 |
| **历史条目** | `src/windows/main/components/ClipboardItem.jsx` — 悬停预览 + 来源图标 + 拖拽 |
| **筛选按钮** | `src/windows/main/components/FilterButton.jsx` — 芯片组 + active 动画 |
| **收藏标签** | `src/windows/main/components/FavoritesTab.jsx` — 搜索防抖 + 滚动控制 |
| **悬浮工具栏** | `src/windows/main/components/FloatingToolbar.jsx` — 拖拽移动 + 淡入淡出 |
| **多选操作栏** | `src/windows/main/components/MultiSelectActionBar.jsx` — Ctrl/Shift 多选 + 批量操作 |
| **标题栏搜索** | `src/windows/main/components/TitleBarSearch.jsx` — 搜索框展开/收起动画 |
| **一次性粘贴** | `src/shared/services/oneTimePaste.js` — localStorage 状态管理 |
| **i18n** | `src/shared/locales/zh-CN.json` — 1132 行中文翻译可供参考 key 命名 |

### ClipSync 核心代码
```
路径: C:\Users\sukai\Desktop\copyboard
分支: feat/webview-ui (当前)
稳定分支: master
```

---

## 三、竞品分析：QuickClipboard vs ClipSync

| 功能领域 | ClipSync | QuickClipboard | 差距 |
|---|---|---|---|
| **端到端加密** | ✅ P2P TLS 1.3 + AES-256-GCM | ❌ 裸 TCP | 领先 |
| **跨平台** | ✅ Win/Mac/Linux | ❌ Windows only | 领先 |
| **设备发现** | ✅ mDNS 自动 | ❌ 手动 IP | 领先 |
| **Web 伴侣** | ✅ 内置 HTTP + 二维码 | ❌（仅有 Android APK） | 领先 |
| **UI 美观度** | ★★☆ customtkinter | ★★★★★ Tailwind CSS | 落后 |
| **动画效果** | ❌ 仅有呼吸动画 | ✅ 完整动画系统 | 落后 |
| **搜索 + 筛选** | ❌ | ✅ | 落后 |
| **收藏系统** | ❌ | ✅ | 落后 |
| **快捷粘贴窗口** | ❌ | ✅ 标志性功能 | 落后 |
| **多选批量操作** | ❌ | ✅ | 落后 |
| **多格式粘贴** | ⚡ 单一格式 | ✅ 全格式 | 落后 |
| **来源应用追踪** | ❌ | ✅ | 落后 |
| **数字快捷键** | ❌ | ✅ `Ctrl+1~9` | 落后 |
| **截屏 OCR** | ❌ | ✅（闭源） | 不计划 |

---

## 三、架构设计

```
┌─────────────────────────────────────────┐
│  桌面 WebView 窗口 (系统原生)            │
│  ├─ index.html        (主界面)          │
│  ├─ settings.html     (设置页)          │
│  └─ quickpaste.html   (快捷粘贴浮窗)    │
│                                         │
│  技术: Vue 3 CDN + Tailwind CSS CDN     │
│        零构建工具，零 npm                │
├─────────────────────────────────────────┤
│  HTTP + WebSocket Server (Python)       │
│  ├─ GET  /                     静态页面 │
│  ├─ GET  /api/history           历史列表│
│  ├─ GET  /api/devices           设备状态│
│  ├─ POST /api/push              推送内容│
│  ├─ POST /api/paste             粘贴到本机│
│  ├─ GET  /api/favorites         收藏列表│
│  ├─ WS   /ws                   实时推送  │
│  └─ GET  /api/qrcode           二维码    │
├─────────────────────────────────────────┤
│  现有后端 (不变)                         │
│  剪贴板监控 / P2P 同步 / 加密 / 文件传输  │
│  Config / i18n / systray / 快捷键       │
└─────────────────────────────────────────┘
```

### 关键决策

| 决策 | 选择 | 原因 |
|---|---|---|
| 前端框架 | **Vue 3 CDN** | 组件化、响应式、中文生态最好、零构建 |
| CSS 框架 | **Tailwind CSS CDN** | QuickClipboard 同款、设计令牌天然支持、暗色模式 |
| WebView | **系统原生 WebView** | Windows=Edge WebView2, macOS=WKWebView, Linux=WebKitGTK |
| 实时通信 | **WebSocket** | 设备状态、传输进度、剪贴板变更实时推送 |
| 前端文件 | **独立 .html 文件** | 不内嵌 Python 字符串，直接 serve 静态文件 |
| 后端 API | 扩展现有 `internal/web/server.py` | 已有点框架，加路由和 WebSocket |
| 过渡策略 | **双轨并行** | CTk 界面保留，通过 `ui_backend` 设置项切换 |

---

## 四、Vue 3 组件树

```
App
├── TitleBar.vue          窗口标题栏 + 搜索框
├── TabNavigation.vue     标签页导航 (设备|历史|传输|收藏)
├── DevicePanel.vue       设备面板
│   ├── DeviceCard.vue    单个设备卡片 (连接/断开/重命名)
│   └── PendingRow.vue    待配对请求
├── HistoryPanel.vue      历史面板
│   ├── FilterBar.vue     类型筛选 (全部|文本|图片|文件|链接)
│   └── HistoryItem.vue   单条历史 (预览/复制/收藏/删除/置顶)
├── TransferPanel.vue     传输面板
│   ├── ActiveTransfer.vue 传输中任务卡片
│   └── TransferHistory.vue 传输历史
├── FavoritesPanel.vue    收藏面板
│   ├── GroupSidebar.vue  分组侧边栏
│   └── FavoriteItem.vue  收藏项卡片
└── StatusBar.vue         底部状态栏 (设备数/同步状态)
```

---

## 五、QuickClipboard 功能对标详细分析

### 5.1 剪贴板捕获与监控

#### 多轮重试捕获 ★★★★
QuickClipboard 对剪贴板内容进行 7 轮递增延迟重试（0/40/80/140/220/360/560ms），解决 Office/Photoshop 等复杂应用写剪贴板时分批写入导致格式丢失的问题。

**ClipSync 现状**：⚡ 单次捕获。

**建议**：在 `clipboard_monitor` 中增加重试逻辑，~30 行代码。优先级：P0。

#### 全格式捕获 ★★★★★
QuickClipboard 保存**所有**剪贴板格式（CF_UNICODETEXT、CF_TEXT、HTML Format、Rich Text Format、CF_HDROP、CF_DIB 等），写入时完整还原。

**ClipSync 现状**：⚡ 仅捕获 TEXT/RTF/HTML/IMAGE/FILES 五种。

**建议**：扩展 `ClipboardEntry` 增加 `raw_formats: list[tuple[str, bytes]]` 字段。需改存储格式，~200 行代码。优先级：P1。

#### SHA256 内容去重 ★★★
QuickClipboard 对所有写入剪贴板的内容计算 SHA256，缓存最近一次哈希去重。

**ClipSync 现状**：⚡ 用 `changeCount` (macOS) 或简单 `hash(text)` 去重。

**建议**：统一用 SHA256 去重，~50 行代码。优先级：P0。

#### 监控暂停/抑制 ★★★★
QuickClipboard 在写入剪贴板时自动暂停监控（`pause_clipboard_monitor_for(duration_ms)`），防止自循环。

**ClipSync 现状**：✅ 已有 `_suppress_event`，但实现较简单。

**建议**：增加时间窗口抑制，~20 行代码。优先级：P0。

#### 来源应用追踪 ★★★★
QuickClipboard 在 Windows 上通过 `AddClipboardFormatListener` + 前台窗口检测记录复制来源（进程名 + 窗口标题 + 路径），并提取应用图标。

**ClipSync 现状**：❌ 无。

**建议**：
- Windows：`GetForegroundWindow` + `GetWindowThreadProcessId`
- macOS：`NSWorkspace.shared.frontmostApplication`
- Linux：跳过（难以可靠获取）
- `ClipboardEntry` 增加 `source_app: str` 字段
- ~200 行代码（三平台）。优先级：P1。

#### 应用过滤（黑名单/白名单）★★★
QuickClipboard 支持按进程名、窗口标题、路径配置过滤列表，防止敏感应用内容被记录。

**ClipSync 现状**：✅ 已有敏感内容过滤（基于内容而非应用）。

**建议**：增加 `app_filter_enabled` + `app_filter_list` 配置，~150 行代码。优先级：P2。

### 5.2 粘贴系统

#### 多格式粘贴选项 ★★★★★
支持纯文本 / HTML / RTF / 全部格式 / 图片 / 文件六种粘贴动作，用户可右键选择。

**ClipSync 现状**：⚡ 仅单一格式（有 HTML 就用 HTML，否则纯文本）。

**建议**：保存条目原始格式（依赖全格式捕获），粘贴时还原。~400 行代码。优先级：P1。

#### 快捷粘贴窗口 ★★★★★（QuickClipboard 标志性功能）
按下 `Ctrl+\`` 弹出悬浮窗口 → 显示最近 N 条 → 滚轮选择 → 松开即粘贴。

**ClipSync 现状**：❌ 无。

**建议**：用 Vue 3 实现 `quickpaste.html`，监听快捷键。~300 行代码。优先级：P0。

#### 一次性粘贴模式 ★★★
粘贴后不触发放置顶行为，适合快速连续粘贴同一内容。

**ClipSync 现状**：❌ 无。

**建议**：设置中添加"一次性粘贴"开关，~30 行代码。优先级：P1。

#### 粘贴后置顶（Paste-to-Top）★★
粘贴一条历史记录后，该记录自动移到列表最前面。

**ClipSync 现状**：❌ 无。

**建议**：设置可关闭，~20 行代码。优先级：P1。

#### 合并复制/粘贴 ★★★
多选模式下将多条内容合并成一条（用分隔符拼接）。

**ClipSync 现状**：❌ 无多选。

**建议**：在多选实现后，~50 行代码。优先级：P2。

#### 数字快捷键 ★★★★
`Ctrl+1` ~ `Ctrl+9` 直接粘贴对应位置的历史记录。

**ClipSync 现状**：❌ 无。

**建议**：用 `pynput` 或系统热键 API 注册。~150 行代码。优先级：P0。

#### 纯文本粘贴快捷键 ★★★
独立的全局快捷键，无论历史记录是什么格式，一律以纯文本粘贴。

**ClipSync 现状**：❌ 无。

**建议**：注册单独的"纯文本粘贴"快捷键。~50 行代码。优先级：P0。

#### 粘贴模拟 ★★★
写入剪贴板后自动模拟 `Ctrl+V` 粘贴到前台应用。

**ClipSync 现状**：❌ 无（只写到本机剪贴板）。

**建议**：用系统 API 模拟 `Ctrl+V`，注意处理修饰键冲突。~150 行代码（三平台）。优先级：P1。

### 5.3 内容管理

#### 收藏系统 ★★★★★
独立的收藏表 + 分组管理，永久保存，自定义标题，从历史添加。

**ClipSync 现状**：❌ 无。

**建议**：
```python
@dataclass
class FavoriteItem:
    id: str             # uuid
    title: str
    content: str
    html_content: str = ""
    content_type: str = "text"
    group: str = ""
    created_at: float
    paste_count: int = 0
```
~500 行代码（含 UI）。优先级：P0。

#### 分组管理 ★★★★
收藏项支持分组，每组有名称、图标（emoji/Tabler icon）、颜色。

**ClipSync 现状**：❌ 无。

**建议**：在收藏系统基础上增加分组，~300 行代码。优先级：P1。

#### 置顶（Pin Items）★★★
将重要历史项固定在列表顶部，不受清理影响。

**ClipSync 现状**：❌ 无。

**建议**：`ClipboardEntry` 增加 `pinned: bool = False`，~80 行代码。优先级：P1。

#### 拖拽排序 ★★
QuickClipboard 使用 `@dnd-kit` 支持拖拽重新排列列表项。

**ClipSync 现状**：❌ 无（CTk 不支持原生拖拽，WebView 可实现）。

**建议**：在 WebView 迁移后实现，用原生 HTML5 drag API 或轻量库。优先级：P2。

#### 多选批量操作 ★★★★
Ctrl+Click / Shift+Click 多选 + 底部操作栏（合并复制、批量删除、添加到收藏）。

**ClipSync 现状**：❌ 无。

**建议**：~250 行代码。优先级：P0。

#### 搜索 ★★★★★
实时搜索剪贴板历史，200ms 防抖，自动高亮匹配文字。

**ClipSync 现状**：❌ 无。

**建议**：~80 行代码（Vue computed + 防抖）。优先级：P0。

#### 类型筛选 ★★★★
芯片组筛选：全部 / 文本 / 图片 / 文件 / 链接。

**ClipSync 现状**：❌ 无。

**建议**：~60 行代码。优先级：P0。

#### 内容预览 ★★★★
鼠标悬停 120ms 弹出预览窗口（文本/HTML/图片/文件）。

**ClipSync 现状**：❌ 无。

**建议**：~200 行代码。优先级：P1。

#### 来源应用展示 + 粘贴次数 ★★★
条目上显示来源应用图标 + 粘贴计数徽章。

**ClipSync 现状**：❌ 无。

**建议**：~70 行代码（依赖来源追踪）。优先级：P1。

### 5.4 数据管理

#### 数据导入/导出 ★★★
将剪贴板历史 + 收藏 + 分组导出为 ZIP 文件。

**ClipSync 现状**：❌ 无。

**建议**：~150 行代码。优先级：P2。

#### 备份管理 ★★
自动备份 `history.json.bak`，可恢复。

**ClipSync 现状**：❌ 无。

**建议**：~50 行代码。优先级：P2。

### 5.5 快捷键系统

| 快捷键 | 功能 | QuickClipboard | ClipSync |
|---|---|---|---|
| `Shift+Space` | 显示/隐藏主窗口 | ✅ | ⚡ 仅有托盘 |
| `Ctrl+\`` | 快捷粘贴窗口 | ✅ | ❌ |
| `Ctrl+1~9` | 粘贴第 N 条历史 | ✅ | ❌ |
| `Ctrl+Shift+V` | 纯文本粘贴 | ✅ | ❌ |
| `Enter` | 粘贴选中项 | ✅ | ❌ |
| `Escape` | 隐藏窗口 | ✅ | ✅ |
| `Tab` | 聚焦搜索框 | ✅ | ❌ |
| `Ctrl+P` | 置顶/取消置顶 | ✅ | ❌ |
| 自定义监听键 | 开/关剪贴板监控 | ✅ | ❌ |

**建议**：用 `internal/system/hotkey.py`（新建）统一管理，Windows 用 `RegisterHotKey`，macOS 用 `CGEvent`，Linux 用 `pynput`。~200 行代码。

### 5.6 其他功能

#### 低占用模式 ★★★
关闭动画、增大轮询间隔、降低内存。~60 行代码。优先级：P2。

#### 音效反馈 ★★
复制/粘贴/滚动音效。~50 行代码。优先级：P2。

#### 存储格式升级
从 `pickle` 迁移到 SQLite：
```sql
CREATE TABLE clipboard_history (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    html_content TEXT,
    content_type TEXT NOT NULL DEFAULT 'text',
    image_ref TEXT,
    source_app TEXT,
    source_icon TEXT,
    source_device TEXT,
    pinned INTEGER DEFAULT 0,
    paste_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE clipboard_formats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clipboard_id TEXT NOT NULL,
    format_name TEXT NOT NULL,
    raw_data BLOB,
    is_primary INTEGER DEFAULT 0,
    FOREIGN KEY (clipboard_id) REFERENCES clipboard_history(id)
);

CREATE TABLE favorites (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    html_content TEXT,
    content_type TEXT NOT NULL,
    group_name TEXT DEFAULT '',
    paste_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE groups (
    name TEXT PRIMARY KEY,
    icon TEXT DEFAULT '',
    color TEXT DEFAULT '#3b82f6',
    sort_order INTEGER DEFAULT 0
);
```

---

## 六、多 Agent 并行工作路线图

### 依赖关系图

```
Wave 1 (互不依赖，可同时启动)
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Agent A        │  │  Agent B        │  │  Agent C        │
│  后端 API 扩展   │  │  前端基础工程    │  │  剪贴板增强+配置 │
│  (Python)       │  │  (HTML/CSS/JS)  │  │  (Python)       │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │
         │  API 协议 +        │  设计令牌 +        │  新配置字段 +
         │  WebSocket 就绪    │  Vue 脚手架就绪     │  捕获增强就绪
         │                    │                    │
         ▼                    ▼                    ▼
Wave 2 (依赖 Wave 1 完成，互相独立，可并行)
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Agent D        │  │  Agent E        │  │  Agent F        │  │  Agent G        │
│  核心 Vue 组件   │  │  搜索+筛选+预览  │  │  快捷键系统     │  │  收藏系统       │
│  (JS/HTML)      │  │  (JS/CSS/HTML)  │  │  (Python)       │  │  (Python+JS)    │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │                    │
         ▼                    ▼                    ▼                    ▼
Wave 3 (依赖 Wave 2 完成，互相独立，可并行)
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Agent H        │  │  Agent I        │  │  Agent J        │
│  快捷粘贴窗口    │  │  多选+置顶+统计  │  │  来源追踪+其他   │
│  (HTML/JS/PY)   │  │  (JS/Python)    │  │  (Python)       │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │
         └────────────────────┼────────────────────┘
                              ▼
Wave 4 (最终集成，依赖 Wave 3 全部完成)
┌─────────────────────────────────────────────────┐
│  Agent K: WebView 包装 + 集成测试                │
│  Agent L: i18n + 导入导出 + 清理                 │
└─────────────────────────────────────────────────┘
```

---

### Wave 1：基础设施（同时启动，互不依赖）

#### Agent A — 后端 API 扩展
**前置**：无 | **产物**：API 协议 + WebSocket 可用 | **文件**：13 个

```
任务 A1: internal/web/server.py 重写
  - HTTP 请求路由分发
  - WebSocket 升级握手

任务 A2: internal/web/api/*.py (5 个端点模块)
  - history.py:   GET /api/history (分页/搜索/类型筛选)
                  POST /api/paste (粘贴到本机)
                  DELETE /api/history/<id> (删除)
  - devices.py:   GET /api/devices (设备列表+状态)
                  POST /api/devices/<id>/connect
                  POST /api/devices/<id>/disconnect
  - transfer.py:  GET /api/transfers (活跃传输)
                  GET /api/transfers/history
                  POST /api/transfers/<id>/cancel
  - favorites.py: GET/POST/DELETE 收藏 CRUD
  - settings.py:  GET/POST 设置读写

任务 A3: internal/web/ws.py
  - WebSocket 连接管理 + 心跳
  - 消息类型: history_update, devices_update, transfer_progress, notification
  - 广播 vs 单播路由

任务 A4: internal/web/routes.py
  - URL 路由表 + Token 认证中间件
  - 静态文件 serve (static/ 目录)

任务 A5: internal/config/config.py 新增字段
  - ui_backend: str = "ctk"
  - web_port: int = 19992
  - web_history_limit: int = 50
  - favorites_path: str = ""
```

#### Agent B — 前端基础工程
**前置**：无 | **产物**：设计令牌 + Vue 脚手架可用 | **文件**：6 个

```
任务 B1: internal/web/static/css/theme.css
  - CSS 自定义属性设计令牌 (--clipsync-*)
  - 亮色/暗色两套变量
  - Tailwind theme.extend.colors 映射

任务 B2: internal/web/static/css/base.css
  - 全局 reset + 排版预设
  - 自定义滚动条 (6px 圆角)
  - body.dark 切换逻辑

任务 B3: internal/web/static/css/animations.css
  - slideUp / slideDown / fadeIn / fadeOut
  - 面板切换 (cubic-bezier 弹性)
  - 列表项 staggered fadeIn
  - 按钮 hover:scale-105 active:scale-95
  - 通知 slideInRight + auto-dismiss

任务 B4: internal/web/static/js/store.js
  - Vue 3 reactive 全局状态
  - devices, history, transfers, favorites, settings
  - selectedIds, multiSelectMode, activeTab

任务 B5: internal/web/static/js/ws.js
  - WebSocket 连接/重连/心跳
  - 消息分发 → store 更新
  - 连接状态指示

任务 B6: internal/web/static/js/api.js
  - HTTP API 封装 (fetch wrapper)
  - getHistory, pasteItem, deleteItem, getDevices, connectDevice, etc.
```

#### Agent C — 剪贴板增强 + 配置
**前置**：无 | **产物**：捕获质量提升 | **文件**：2 个编辑

```
任务 C1: internal/clipboard/clipboard_monitor.py
  - 多轮重试捕获 (7 轮: 0/40/80/140/220/360/560ms)
  - SHA256 内容哈希去重
  - 时间窗口监控抑制 (替代布尔标志位)

任务 C2: internal/config/config.py
  - paste_to_top: bool = True
  - low_memory_mode: bool = False
  - app_filter_enabled: bool = False
  - app_filter_list: list[str] = []
  - ui_animation_enabled: bool = True
  - sound_enabled: bool = False
```

---

### Wave 2：核心功能（并行，依赖 Wave 1）

#### Agent D — 核心 Vue 组件
**前置**：Agent B | **产物**：主界面完整可用 | **文件**：11 个

```
任务 D1: internal/web/static/index.html
  - Vue 3 CDN + Tailwind CDN 引入
  - 根布局 (侧边栏/主区域/状态栏)
  - app.mount('#app')

任务 D2: internal/web/static/js/app.js
  - createApp + 全局组件注册
  - WebSocket 初始化 + store 连接
  - 快捷键事件绑定 (Escape, Tab, Enter, 上下箭头)

任务 D3-D8: 6 个核心组件
  - components/title-bar.js     (标题栏 + 搜索框)
  - components/tab-navigation.js (设备|历史|传输|收藏 标签)
  - components/device-panel.js   (设备列表容器)
  - components/device-card.js    (单设备卡片：连接/断开/重命名)
  - components/history-panel.js  (历史列表容器)
  - components/history-item.js   (单条历史：预览/复制/收藏/删除)

任务 D9-D11: 3 个辅助组件
  - components/transfer-panel.js (传输进度+历史)
  - components/status-bar.js     (底部状态：设备数/同步状态/IP)
  - components/toast.js          (通知提示)
```

#### Agent E — 搜索 + 筛选 + 预览 + 右键
**前置**：Agent B + Agent D | **产物**：交互增强 | **文件**：3 个

```
任务 E1: 搜索 + 防抖 + 高亮
  - components/filter-bar.js (类型芯片: 全部|文本|图片|文件|链接)
  - title-bar.js 搜索框: 200ms 防抖 → computed filteredHistory
  - 匹配文字 <mark> 高亮

任务 E2: 内容预览
  - 鼠标悬停 120ms → 弹出预览卡片
  - 文本: 多行 (max 20 行)
  - 图片: 缩略图 (max 300x200)
  - 文件: 文件列表 + 大小
  - 鼠标离开或滚动 → 关闭

任务 E3: 右键上下文菜单
  - 历史项右键: 复制 / 粘贴到本机 / 添加到收藏 / 置顶 / 删除
  - 设备项右键: 连接/断开 / 重命名 / 忘记设备
  - 自定义菜单样式 (Tailwind)
```

#### Agent F — 快捷键系统
**前置**：Agent A | **产物**：全局快捷键可用 | **文件**：1 个新建

```
任务 F1: internal/system/hotkey.py (新建)
  - Windows: RegisterHotKey + ctypes + GetMessage
  - macOS: CGEvent via pyobjc / Carbon
  - Linux: pynput global hotkey listener
  - HotkeyManager.register(id, shortcut_str, callback)
  - HotkeyManager.unregister(id)
  - HotkeyManager.reload_from_settings()

任务 F2: 注册核心快捷键
  - Ctrl+\` → 快捷粘贴窗口 (toggle)
  - Ctrl+1~9 → 粘贴第 1-9 条历史
  - Ctrl+Shift+V → 纯文本粘贴
  - 数字快捷键首次写入剪贴板，重复按只 Ctrl+V

任务 F3: src/main.py 接入
  - Application 初始化时启动 HotkeyManager
  - shutdown 时注销所有快捷键
  - 根据设置动态启用/禁用
```

#### Agent G — 收藏系统
**前置**：Agent A + Agent D | **产物**：收藏功能完整 | **文件**：4 个

```
任务 G1: 后端 — favorites CRUD API
  - internal/web/api/favorites.py
  - 存储: {data_dir}/favorites.json
  - GET /api/favorites?group=&search=&type=
  - POST /api/favorites (新增/更新)
  - DELETE /api/favorites/<id>
  - POST /api/favorites/<id>/move (移动到分组)

任务 G2: 后端 — 分组管理 API
  - GET/POST/DELETE /api/groups
  - GroupInfo: name, icon, color, order

任务 G3: 前端 — 收藏面板
  - components/favorites-panel.js
  - components/favorite-item.js
  - 侧边栏分组筛选
  - 从历史"添加到收藏"交互

任务 G4: 前端 — 右键菜单集成
  - 历史项右键 → "添加到收藏"选项
  - 选择目标分组 (弹窗或子菜单)
```

---

### Wave 3：高级功能（并行，依赖 Wave 2）

#### Agent H — 快捷粘贴窗口 (Quick Paste)
**前置**：Agent D + Agent F | **产物**：快捷粘贴浮窗 | **文件**：2 个

```
任务 H1: internal/web/static/quickpaste.html
  - Vue 3 独立应用 (非主 app 子组件)
  - 半透明背景 + 毛玻璃效果
  - 显示最近 9 条历史 (自动更新)
  - 滚轮切换高亮项
  - 当前项放大 + 蓝色渐变背景
  - 序号 + 类型标签 + 内容预览

任务 H2: 粘贴模拟 (Python 侧)
  - internal/clipboard/paste_simulator.py (新建)
  - Windows: SendInput Ctrl+V + Alt 冲突处理
  - macOS: CGEventPost Ctrl+Cmd+V
  - Linux: enigo Ctrl+V
  - 先释放冲突修饰键 → 写剪贴板 → Ctrl+V → 恢复修饰键

任务 H3: 快捷粘贴交互逻辑
  - F 键按下 → 显示窗口 + 选中第 0 项
  - 滚轮/上下键 → 切换选中项
  - 松开 F 键或 Enter → 粘贴当前项 → 关闭
  - Escape → 关闭不粘贴
  - 鼠标离开窗口区域 → 半透明褪色
```

#### Agent I — 多选 + 置顶 + 粘贴统计
**前置**：Agent D | **产物**：内容管理增强 | **文件**：4 个编辑

```
任务 I1: 多选批量操作
  - Ctrl+Click: 切换单个选择
  - Shift+Click: 范围选择 (选中 anchor → 当前)
  - 底部 MultiSelectActionBar: 选中数量 / 合并复制 / 批量删除 / 退出
  - 长按进入多选模式 (移动端)

任务 I2: 置顶 (Pin)
  - ClipboardEntry.pinned: bool = False
  - 置顶项在列表顶部渲染 (separator 分隔)
  - 📌 图标 + 浅底色
  - 右键菜单 "置顶/取消置顶"
  - Ctrl+P 快捷键切换

任务 I3: 粘贴统计
  - ClipboardEntry.paste_count: int = 0
  - 每次粘贴 API 调用时 +1
  - HistoryItem 右上角显示小徽章 (数字)
  - paste_count > 0 的项视觉区分

任务 I4: 粘贴后置顶
  - paste_to_top 设置项控制
  - 粘贴后该项移到列表最前 (如有置顶项则在其之后)
```

#### Agent J — 来源追踪 + 应用过滤 + 音效
**前置**：Agent C | **产物**：来源感知 + 隐私控制 | **文件**：3 个

```
任务 J1: internal/clipboard/source_tracker.py (新建)
  - Windows: GetForegroundWindow → GetWindowThreadProcessId
             → QueryFullProcessImageName → 进程名+路径
             → ExtractAssociatedIcon → PNG → SHA256 文件名
  - macOS: NSWorkspace.shared.frontmostApplication
           → bundleIdentifier + localizedName + icon
  - Linux: 跳过 (不支持的平台)
  - ClipboardEntry.source_app: str
  - ClipboardEntry.source_icon: str (hash)

任务 J2: 前端来源展示
  - HistoryItem 左侧小图标 (<img> 或 fallback)
  - 来源名 tooltip (hover 显示完整路径)
  - 远程设备条目显示设备名

任务 J3: 应用过滤
  - blacklist 模式: 前台应用匹配 → 暂停监控
  - whitelist 模式: 仅匹配时启用监控
  - 支持通配符 (*, ?)
  - 设置面板管理过滤列表

任务 J4: 音效反馈
  - internal/sound.py (新建)
  - 默认音效嵌入资源
  - winsound (Win) / afplay (Mac) / paplay (Linux)
  - 可替换自定义音效文件
  - sound_enabled 开关

任务 J5: 拖拽排序 (P2, WebView 依赖)
  - 依赖: 主界面 WebView 模式可用
  - 实现: HTML5 drag API (dragstart / dragover / drop)
  - 排序结果持久化到 history.json
  - 收藏分组内也支持拖拽排序
```

---

### Wave 4：集成与发布（依赖 Wave 3）

#### Agent K — WebView 包装 + 最终集成
**前置**：Wave 2 + Wave 3 全部 | **文件**：2 个

```
任务 K1: internal/ui/webview_window.py (新建)
  - 平台判断: Windows → Edge WebView2, macOS → WKWebView, Linux → WebKitGTK
  - 备选方案: 不引入 pywebview 依赖，用 webbrowser.open + 控制浏览器窗口
  - 加载 URL: http://127.0.0.1:{web_port}?token={token}
  - 窗口标题 "ClipSync" + 图标
  - 窗口大小记忆 (同现有 CTk 逻辑)
  - 关闭窗口 → 隐藏到托盘 (不退出)

任务 K2: 过渡方案
  - 配置项 ui_backend: "ctk" | "webview"
  - CTk 代码完整保留 (zero breaking change)
  - 设置面板增加 UI 切换选项
  - 启动时根据 ui_backend 决定打开 CTk 还是 WebView
  - Stable 后默认切换为 "webview"

任务 K3: 三平台验证
  - Win: Edge WebView2 运行时检测 + 自动安装提示
  - Mac: WKWebView 性能 + 内存测试
  - Linux: WebKitGTK 兼容性测试
  - 内存 < 80MB, CPU < 0.5%, 启动 < 500ms

任务 K4: 全格式捕获 (P2, 依赖 Wave 4 稳定)
  - ClipboardEntry 增加 raw_formats: dict[str, bytes] 字段
  - 捕获时保存所有剪贴板格式 (CF_UNICODETEXT/CF_TEXT/HTML Format/RTF/CF_HDROP/CF_DIB...)
  - macOS: 保存所有 UTI 类型 (public.utf8-plain-text/public.html/public.rtf/public.png...)
  - 存储格式从 pickle 改为逐格式文件存储或 BLOB
  - ~200 行代码

任务 K5: 多格式粘贴 (P2, 依赖 K4 全格式捕获)
  - 右键菜单: 粘贴(纯文本)/粘贴(HTML)/粘贴(RTF)/粘贴(全部格式)/粘贴(图片)/粘贴(文件)
  - 默认粘贴格式由 paste_with_format 设置控制
  - 粘贴时完整还原原始剪贴板格式
  - ~400 行代码
```

#### Agent L — i18n + 数据管理 + 清理
**前置**：Wave 2 | **文件**：3 个

```
任务 L1: 50+ i18n key 批量添加
  - internal/i18n/__init__.py
  - 中英文翻译: 菜单/标签/按钮/提示/错误消息
  - 收藏/分组/筛选/搜索/快捷粘贴相关

任务 L2: 数据导入/导出
  - 导出: history.json + favorites.json + groups.json → ZIP
  - 导入: ZIP → 解压 → "覆盖" / "追加"模式
  - 设置面板管理按钮

任务 L3: 备份管理
  - 启动时自动创建 .bak 备份文件
  - 保留最近 5 个备份
  - 设置面板显示备份时间 + "恢复"按钮

任务 L4: 代码清理
  - ruff check . 通过
  - 移除 dead code
  - 文档更新

任务 L5: 贴边隐藏 (P2, 低优先级)
  - 窗口贴附屏幕边缘时自动隐藏
  - 鼠标触碰边缘 → 滑出显示
  - 鼠标移开 → 延迟滑入隐藏
  - 可配置启用/禁用 + 贴边位置
  - ~120 行代码
```

---

### Wave 5：长期项目（P2，低优先级，按需启动）

核心功能稳定后再推进。

#### Agent M — 存储迁移 SQLite
**前置**：Wave 4 全部完成 + 搜索性能成为瓶颈时 | **文件**：3 个

```
任务 M1: 数据库 Schema + 迁移脚本
  - 从 history.json + favorites.json 迁移到 SQLite
  - 表结构: clipboard_history, clipboard_formats, favorites, groups
  - 迁移脚本: json → sqlite (一次性，自动检测)

任务 M2: 后端 DAO 层
  - internal/storage/database.py (新建)
  - ClipboardDAO: add/get/delete/pin/search (SQL WHERE LIKE)
  - FavoritesDAO: add/get/update/delete/move
  - GroupsDAO: CRUD + 排序

任务 M3: 前端适配
  - API 响应格式不变，前端无需改动
  - 搜索走 SQL 而非前端 computed filter
  - 分页查询替代全量加载
```

#### Agent N — AI 翻译
**前置**：Wave 4 全部完成 + 配置好 API Key | **文件**：2 个

```
任务 N1: 翻译后端
  - internal/ai/translator.py (新建)
  - 支持 OpenAI / DeepL / 百度翻译 API
  - 设置面板管理 API Key + 目标语言
  - 缓存翻译结果避免重复请求

任务 N2: 翻译前端
  - 历史项右键 → "翻译"
  - 翻译结果弹窗或内联显示
  - 支持复制翻译结果
```

#### P2 待办池（不分配 Agent，按需拾取）

```
□ 贴边隐藏 — 已在 Agent L 任务 L5，低优先级
□ AI 翻译 — 独立 Agent N，依赖外部 API
□ SQLite 迁移 — 独立 Agent M，等搜索性能瓶颈时启动
□ 拖拽排序 — 已在 Agent J 任务 J5，依赖 WebView
□ 全格式捕获 + 多格式粘贴 — 已在 Agent K 任务 K4/K5，依赖 Wave 4
```

---

### 并行度总结

| Wave | 并行 Agent 数 | 预计耗时 | 可同时推进 |
|---|---|---|---|
| Wave 1 | 3 (A, B, C) | 2-3 天 | ✅ 同时启动 |
| Wave 2 | 4 (D, E, F, G) | 3-5 天 | ✅ A/B/C 完成后同时启动 |
| Wave 3 | 3 (H, I, J) | 2-4 天 | ✅ D/E/F/G 完成后同时启动 |
| Wave 4 | 2 (K, L) | 2-3 天 | ✅ 串行收尾 |
| Wave 5 | 2 (M, N) | 按需 | P2 低优先级，核心稳定后启动 |
| **总计** | **14 个 Agent** | **9-15 天 + 按需** | 比顺序执行 (7 周) 快 3.5x |

---

## 七、文件变更汇总

### 新建文件

| 文件 | 目的 |
|---|---|
| `internal/web/routes.py` | API 路由分发 |
| `internal/web/api/history.py` | 历史 API |
| `internal/web/api/devices.py` | 设备 API |
| `internal/web/api/transfer.py` | 传输 API |
| `internal/web/api/favorites.py` | 收藏 API |
| `internal/web/api/settings.py` | 设置 API |
| `internal/web/ws.py` | WebSocket 实时推送 |
| `internal/web/static/index.html` | 主界面 |
| `internal/web/static/settings.html` | 设置页面 |
| `internal/web/static/quickpaste.html` | 快捷粘贴浮窗 |
| `internal/web/static/css/theme.css` | 设计令牌 + 暗色主题 |
| `internal/web/static/css/animations.css` | 动画系统 |
| `internal/web/static/css/base.css` | 基础样式 |
| `internal/web/static/js/app.js` | Vue 3 主入口 |
| `internal/web/static/js/store.js` | 全局状态管理 |
| `internal/web/static/js/ws.js` | WebSocket 客户端 |
| `internal/web/static/js/api.js` | HTTP API 封装 |
| `internal/web/static/components/title-bar.js` | 标题栏组件 |
| `internal/web/static/components/tab-navigation.js` | 标签导航组件 |
| `internal/web/static/components/device-panel.js` | 设备面板组件 |
| `internal/web/static/components/device-card.js` | 设备卡片组件 |
| `internal/web/static/components/history-panel.js` | 历史面板组件 |
| `internal/web/static/components/history-item.js` | 历史条目组件 |
| `internal/web/static/components/filter-bar.js` | 筛选栏组件 |
| `internal/web/static/components/transfer-panel.js` | 传输面板组件 |
| `internal/web/static/components/favorites-panel.js` | 收藏面板组件 |
| `internal/web/static/components/status-bar.js` | 状态栏组件 |
| `internal/clipboard/source_tracker.py` | 来源应用追踪 |
| `internal/system/hotkey.py` | 快捷键管理器 |
| `internal/ui/webview_window.py` | WebView 窗口管理 |
| `internal/storage/database.py` | SQLite DAO 层 (Wave 5) |
| `internal/ai/translator.py` | AI 翻译后端 (Wave 5) |

### 编辑文件

| 文件 | 变更 |
|---|---|
| `internal/web/server.py` | 重写为多路由 HTTP + WebSocket 服务器 |
| `internal/config/config.py` | 新增 `ui_backend`, `favorites_path`, `paste_to_top`, `app_filter_*` 等 |
| `internal/clipboard/clipboard_monitor.py` | 多轮重试 + SHA256 去重 + 监控暂停增强 |
| `src/main.py` | Web 服务器生命周期 + 收藏/快捷键接入 |
| `internal/ui/systray.py` | 托盘菜单新增入口 |
| `internal/i18n/__init__.py` | 新增 50+ 翻译 key |

---

## 八、验证清单

### 第一阶段

- [ ] 浏览器打开 `localhost:19992` 看到 Vue 界面
- [ ] 暗色/亮色主题切换正常
- [ ] 设备列表 WebSocket 实时更新
- [ ] 历史列表分页加载
- [ ] 搜索过滤正常（200ms 防抖 + 高亮）
- [ ] 类型筛选（全部/文本/图片/文件/链接）
- [ ] 面板切换动画流畅
- [ ] Tailwind CSS 设计令牌一致
- [ ] 复制 Office 内容不丢格式（7 轮重试）
- [ ] 相同内容不会重复记录（SHA256 去重）
- [ ] 写入剪贴板不触发自捕获

### 第二阶段

- [ ] `Ctrl+\`` 弹出快捷粘贴窗口
- [ ] 滚轮选择 + 松开粘贴正常
- [ ] 收藏添加/编辑/删除/分组正常
- [ ] 多选 (Ctrl/Shift+Click) + 批量删除
- [ ] 悬停 120ms 弹出预览
- [ ] 右键菜单功能完整
- [ ] 置顶项始终在顶部

### 第三阶段

- [ ] `Ctrl+1~9` 粘贴位置正确
- [ ] `Ctrl+Shift+V` 纯文本粘贴正常
- [ ] 来源应用图标和名称正确显示
- [ ] 粘贴计数递增
- [ ] 粘贴后置顶（可关闭）

### 第四阶段

- [ ] WebView 窗口启动速度 < 500ms
- [ ] 内存占用 < 80MB
- [ ] CPU 空闲 < 0.5%
- [ ] CTk 和 WebView 模式可切换
- [ ] 三平台 (Win/Mac/Linux) 均正常
- [ ] 全格式捕获 + 多格式粘贴正常
- [ ] 所有现有功能不受影响
- [ ] `ruff check .` 通过

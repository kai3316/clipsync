<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp;
  <a href="README_zh.md">中文</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/kai3316/clipsync/master/assets/icon.svg" alt="ClipSync" width="96" height="96">
</p>

<h1 align="center">ClipSync</h1>

<p align="center">
  <strong>一台设备复制，另一台即刻粘贴。</strong>
  <br>
  全平台 &middot; 局域网 &middot; TLS 1.3 + AES-256-GCM &middot; 零配置
</p>

<p align="center">
  <a href="https://github.com/kai3316/clipsync/releases"><img src="https://img.shields.io/github/v/release/kai3316/clipsync?color=3498DB" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platforms">
</p>

---

## 为什么选择 ClipSync？

你在台式机上复制了一段内容，想粘贴到笔记本上。或者你在手机上复制了文字，想传到电脑上。市面上的方案要么经过云端（隐私风险、依赖互联网），要么只支持纯文本（丢失格式和图片）。

ClipSync 通过**局域网直连**同步剪贴板 — 无需账号、无需云端、无需互联网。支持所有剪贴板格式（文本、HTML、RTF、图片），加密点对点传输文件，甚至能生成二维码让手机浏览器直接接入，无需安装任何 App。

---

## 快速开始

1. [下载](https://github.com/kai3316/clipsync/releases/latest) 对应平台的应用，直接运行，无需安装
2. 在**同一局域网**的另一台设备上也运行 ClipSync
3. 确认两端显示的 8 位配对码一致
4. 一台复制，另一台粘贴。就这么简单。

> **macOS：** 如遇 Gatekeeper 拦截，运行 `xattr -cr clipsync.app`，然后右键 → 打开。

---

## 工作原理

```
  +------------+                             +------------+
  |   设备 A   |  --- mDNS 发现 -----------> |   设备 B   |
  |            |  <-- TLS 1.3 握手 --------  |            |
  |   剪贴板   |  --- 剪贴板内容 ----------> |   剪贴板   |
  |            |  <-- AES-256-GCM 帧 ------  |            |
  +------------+                             +------------+
                                                   |
                                              扫码连接
                                                   |
                                             +------------+
                                             |   手机     |
                                             |   (PWA)    |
                                             +------------+
```

1. **发现设备** — 通过 mDNS/Zeroconf 自动发现局域网内的其他设备，无需手动配置 IP
2. **配对验证** — 首次连接用 8 位验证码确认身份，之后 Ed25519 证书锁定自动信任
3. **同步内容** — 剪贴板变化通过 TLS 1.3 广播。每帧数据独立 AES-256-GCM 加密。去重环防回声循环
4. **手机接入** — 开启 Web 伴侣后生成二维码，手机扫码即得 PWA，可查看历史、推送文字、传输文件

---

## 功能特性

### 剪贴板同步

| 格式 | 支持 |
|--------|-----------|
| 纯文本 (UTF-8, CF_TEXT) | ✅ |
| Unicode 文本 (CF_UNICODETEXT) | ✅ |
| HTML (CF_HTML / `text/html`) | ✅ |
| RTF 富文本 (CF_RTF / `text/rtf`) | ✅ |
| 图片 (PNG, BMP, TIFF, DIB) | ✅ |
| EMF (Windows 图元文件) | ✅ |

按内容哈希去重而非时间戳。设备间快速交替复制不会产生回声循环。

### 文件传输

- **点对点直传** — 文件在设备间直传，不经过中继服务器
- **分块传输** — 大文件拆分为 1 MB 分块，支持 ACK 确认重传
- **文件夹支持** — 拖入文件夹自动打包为 zip 发送
- **暂停/续传** — 中途暂停后可从断点续传
- **进度追踪** — 逐文件进度条，显示实时速率 (Mbps)
- **速度测试** — 测试已配对设备间的局域网原始吞吐量

### Web 伴侣

- 内置 HTTP 服务器，局域网内任意设备可访问
- 二维码扫码连接 — 手机扫一扫即可，无需安装 App
- **PWA 支持** — iOS/Android 上"添加到主屏幕"获得原生体验
- 查看剪贴板历史，推送文字到电脑剪贴板
- 手机与电脑间上传下载文件
- Token 认证（自动生成或自定义）

### 安全机制

- **TLS 1.3** — 所有传输层加密，每设备独立 Ed25519 证书
- **AES-256-GCM** — 应用层逐帧加密
- **TOFU 配对** — 首次信任验证，之后证书锁定防止中间人攻击
- **落盘加密** — 私钥和剪贴板历史加密存储 (AES-256-GCM + PBKDF2)
- **可选预共享密码** — PBKDF2（60 万次迭代）为密钥增加额外熵值
- **证书变更检测** — 已配对设备身份变化时告警（防中间人攻击）

### 内容过滤

基于正则的敏感内容过滤，发送前警告或阻止：
- 信用卡号
- 身份证号 / 社保号
- API 密钥和 Token
- 邮箱地址
- 手机号码
- 自定义正则

### 系统托盘

后台静默运行，右键菜单提供：
- 同步开关
- 已连接设备状态（逐设备显示）
- 快速打开主面板和设置
- Web 伴侣二维码弹窗
- 配对请求和传输完成通知

---

## 下载

| 平台 | 文件 | 备注 |
|----------|------|-------|
| Windows 10/11 | `clipsync.exe` | 便携版，无需管理员权限 |
| macOS 12+ | `clipsync.app` (zip) | 通用二进制 (Intel + Apple Silicon) |
| Linux (X11/Wayland) | `clipsync` (tar.gz x86_64) | 需要 `xclip` 或 `wl-clipboard` |
| Linux (ARM64) | `clipsync` (tar.gz arm64) | 树莓派 4/5 等 |

[最新版本](https://github.com/kai3316/clipsync/releases/latest) &nbsp;|&nbsp; [更新日志](CHANGELOG.md)

---

## 从源码运行

**环境要求：** Python 3.12+

```bash
git clone https://github.com/kai3316/clipsync.git
cd clipsync
python -m venv .venv
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows
pip install -r requirements.txt
python src/main.py
```

**Linux 用户先安装剪贴板工具：**

```bash
sudo apt install xclip          # X11
sudo apt install wl-clipboard   # Wayland
```

**Windows** — 剪贴板 I/O 使用原生 Win32 API，无需额外依赖。

---

## 构建

```bash
pip install pyinstaller
pyinstaller clipsync.spec
```

构建结果在 `dist/`：`clipsync.exe`（Windows）、`clipsync.app`（macOS）、`clipsync`（Linux）。

---

## 常见问题

| 问题 | 可能原因 | 解决方法 |
|---------|-------------|-----|
| 设备互相发现不了 | 不同子网或 AP 客户端隔离 | 确保所有设备在同一网段。检查路由器是否启用了"AP 隔离"或"客户端隔离"。 |
| 设备互相发现不了 | 防火墙阻止 mDNS | 防火墙放行 UDP 5353 和 TCP 19990（默认端口）。 |
| 同步不生效 | 对方未连接 | 检查设备面板 — 对方应显示"已连接"。若仅显示"已配对"，检查双方防火墙。 |
| 同步不生效 | 同步开关关闭 | 点击系统托盘同步图标或在主面板打开同步开关。 |
| 证书变更警告 | 对方重装或重置了身份 | 如果最近确实重置过对方设备，属正常。否则应移除并重新配对。 |
| VPN 导致 IP 错误 | VPN 网卡被优先选取 | v1.0.1 已修复 — 局域网 IP (192.168.x.x) 现在优先于 VPN 网卡。 |
| 端口冲突 | 其他程序占用 19990 | 在 设置 → 网络 中修改 TCP 端口。 |

---

## 技术栈

| 层级 | 技术 |
|-------|------------|
| 界面 | CustomTkinter (跨平台桌面) |
| 传输 | Python `asyncio` + `ssl` (TLS 1.3) |
| 发现 | python-zeroconf (mDNS/DNS-SD) |
| 加密 | `cryptography` (Ed25519, AES-256-GCM, PBKDF2) |
| 剪贴板 | Win32 API / `pbpaste`+`pbcopy` / `xclip`+`wl-paste` |
| 二维码 | `qrcode` + Pillow |
| Web 服务 | Python `http.server` (ThreadingHTTPServer) |
| 构建 | PyInstaller (单文件可执行) |
| CI/CD | GitHub Actions (多平台构建 + 发布) |

---

## 架构

```
src/main.py                   # 入口：托盘、锁文件、生命周期管理
internal/
  clipboard/                  # 平台原生剪贴板 I/O
    clipboard.py              #   抽象基类 + 工厂
    clipboard_windows.py      #   Win32 剪贴板 API (CF_* 格式)
    clipboard_darwin.py       #   macOS pbpaste/pbcopy + osascript
    clipboard_linux.py        #   Linux xclip / wl-clipboard
    format.py                 #   ClipboardContent 数据类 + ContentType 枚举
    history.py                #   加密剪贴板历史存储
    filter.py                 #   基于正则的内容过滤
  config/
    config.py                 #   JSON 配置 + 加密 + 原子写入
  i18n/
    __init__.py               #   中英文翻译表
  platform/
    autostart.py              #   各平台开机自启注册
    notify.py                 #   桌面通知（原生或 tkinter）
  protocol/
    codec.py                  #   二进制帧编码 (魔数 + 版本 + JSON + zlib)
  security/
    encryption.py             #   AES-256-GCM 落盘加密 + PBKDF2
    pairing.py                #   Ed25519 身份, TOFU 配对, 指纹验证
  sync/
    manager.py                #   SyncManager: 剪贴板变更 → 编码 → 广播
    file_transfer.py          #   分块文件传输 + ACK 重传
  transport/
    connection.py             #   TransportManager + PeerConnection (TLS 1.3)
    discovery.py              #   mDNS 服务宣告 + 浏览
  ui/
    dashboard.py              #   主窗口：概览、设备、历史、传输
    settings_window.py        #   设置：网络、外观、Web 伴侣、过滤、安全、高级、日志、关于
    dialogs.py                #   通用对话框 (输入、确认、信息、错误)
    systray.py                #   跨平台系统托盘图标 + 菜单
  web/
    server.py                 #   HTTP 服务器：二维码 API、历史 API、文件上传下载、PWA manifest
tests/                        #   218 个测试覆盖剪贴板、编解码、配置、配对、同步、文件传输、跨平台
```

### 数据流

```
剪贴板变更 (OS)
    → 平台剪贴板读取器 (原生格式)
    → ClipboardContent (规范化数据模型)
    → SyncManager (去重检查、编码)
    → TransportManager (广播至所有已连接设备)
    → PeerConnection (TLS 1.3 socket 写入)
    → 网络 (局域网)
    → PeerConnection (TLS 1.3 socket 读取)
    → TransportManager (解码帧)
    → SyncManager (去重检查、写入本地剪贴板)
    → 平台剪贴板写入器 (原生格式)
```

### 安全模型

每个设备首次启动时生成 Ed25519 密钥对。公钥即设备身份。首次与另一设备建立连接时，双端显示 8 位配对验证码（由 TLS 1.3 会话派生）。用户在两端确认后，对方证书指纹被存储（"锁定"）。后续连接验证指纹 — 若发生变化则发出告警（可能的中间人攻击）。

线缆上的数据双重加密：TLS 1.3 提供传输层安全，每个帧体独立 AES-256-GCM 加密。落盘数据（私钥、剪贴板历史）使用 AES-256-GCM，密钥由设备专属种子经 PBKDF2（60 万次迭代）派生。

---

## 参与贡献

详见 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发环境搭建、项目结构和贡献指南。

欢迎提交 PR。提交前请运行 `python -m pytest tests/ -v` 确保测试通过。

---

## 许可证

MIT — 详见 [LICENSE](LICENSE)

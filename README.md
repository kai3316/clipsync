<p align="center">
  <a href="README_en.md">English</a> &nbsp;|&nbsp;
  <a href="README.md">中文</a>
</p>

<p align="center">
  <img src="assets/icon.svg" alt="ClipSync" width="96" height="96">
</p>

<h1 align="center">ClipSync</h1>

<p align="center">
  一台设备复制，另一台粘贴。<br>
  局域网直连 · 端到端加密 · 不用账号
</p>

<p align="center">
  <a href="https://github.com/kai3316/clipsync/releases"><img src="https://img.shields.io/github/v/release/kai3316/clipsync?color=087c6b" alt="Release"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platforms">
</p>

<p align="center">
  <img src="docs/assets/zh-overview.png" alt="ClipSync 概览页" width="820">
</p>

---

## 这是什么

在两台电脑之间同步剪贴板：这边复制，那边直接粘贴。文字、图片、链接、文件都可以。

装好之后，同一个 Wi-Fi 下的电脑会自动找到对方。你在其中一台上复制，另一台就能直接粘贴，不需要登录账号，内容也不会先被传到网上。两台电脑不在同一个网络时，打开「互联网同步」也能继续用；手机不用装 App，用浏览器扫一下二维码就行。

免费、开源，Windows、macOS、Linux 都有安装包。

适合这些场景：自己的几台电脑之间搬内容；临时把文件从一台送到另一台；在公司与家里之间继续同步；在手机上取用电脑里的东西。换一台新设备时需要重新配对一次，但不需要注册账号。

第一次让两台设备互相信任叫"配对"：两边会显示同一个 8 位数字，核对一致后确认，之后它们会自动连接，不用再配。

## 安装

到 [Releases](https://github.com/kai3316/clipsync/releases/latest) 下载对应平台的安装包：

| 系统 | 安装包 |
|---|---|
| Windows 10 / 11（64 位） | `ClipSync_<版本>_x64-setup.exe` |
| macOS 12+（Apple 芯片） | `ClipSync_<版本>_aarch64.dmg` |
| Linux（x86_64） | `ClipSync_<版本>_amd64.deb` 或 `.AppImage` |

安装包自带运行环境，不需要另外装 Python。Intel 芯片的 Mac 暂时没有安装包。

**macOS 第一次打开时**，系统可能提示"已损坏，无法打开"（应用没有做 Apple 公证）。打开一次终端，执行：

```bash
xattr -dr com.apple.quarantine /Applications/ClipSync.app
```

如果提示的是"无法验证开发者"，右键点击应用，选"打开"即可。

**Linux** 上的剪贴板读写需要 `xclip`（X11）或 `wl-clipboard`（Wayland）。`.deb` 会自动安装 `xclip`；用 `.AppImage` 的话需要自己装一个，否则剪贴板同步不会工作。应用里会有提示。

## 第一次使用

1. 两台设备连到同一个 Wi-Fi。
2. 各自打开 ClipSync。
3. 在任意一台上开始配对，两边会显示同一个 8 位配对码，核对一致后确认。
4. 一台复制，另一台粘贴。

如果两台电脑不在同一个网络（比如一台在公司、一台在家），在两台都打开「设置 → 互联网同步」，然后照常配对。之后同步的内容会先加密，再经由公共服务转发；转发服务看不到内容本身。

## 能做什么

- 剪贴板同步：文字、图片、链接、文件，复制后另一台电脑就能粘贴。
- 文件与文件夹：拖进窗口选一台电脑发送，大文件中断后可以接着传。
- 不在同一个网络：打开「互联网同步」即可继续同步。
- 附近聊天：同一个 Wi-Fi 下的电脑可以直接聊天，不需要先配对。
- 收藏库：把常用内容单独保存。
- AI 配置：对比并迁移本机与另一台电脑上的 AI 工具配置。
- 手机伴侣：手机扫码用浏览器打开，不用安装 App。
- 诊断：发现不了电脑或连不上时，检查网络、防火墙和权限。

## 隐私与安全

- 同一个网络里，内容直接在电脑之间传输，不经过服务器。
- 不在同一个网络时，内容会先加密再交给转发服务，服务看不到内容。
- 第一次配对时核对 8 位数字；以后对方的设备身份发生变化会提醒你。
- 保存在电脑上的私钥和剪贴板历史是加密的。
- 信用卡号、身份证号、API 密钥、私钥和密码在发送前会被替换为 `[FILTERED]`（可以关闭）。

## 更新

桌面版会自己检查更新：在"设置 → 系统"里保持"自动检查更新"开启即可。发现新版本后会下载、安装并重新启动。Linux 的 `.deb` 安装时会弹一次系统密码框，`.AppImage` 不会。

同一局域网里如果有另一台电脑已经升级，也可以直接从它那里更新，不需要访问 GitHub。

## 常见问题

| 现象 | 可以试试 |
|---|---|
| 两台电脑互相找不到 | 确认连的是同一个 Wi-Fi；路由器如果开了"AP 隔离 / 客户端隔离"，关掉它 |
| 找不到，但网络没问题 | 检查防火墙是否放行了 UDP 5353 和 TCP 19990，或到诊断页点一键修复 |
| 已配对但不同步 | 设备页里对方要显示"已连接"；只显示"已配对"说明连接还没建立 |
| 已配对但不同步 | 看概览页或托盘里的同步开关是不是被暂停了 |
| 提示设备身份变化 | 对方重装或重置过。如果是本人操作，确认即可；否则移除设备后重新配对 |
| 端口被占用 | 设置 → 网络与高级 → TCP 端口，换一个 |
| macOS 提示应用已损坏 | 用上面的 `xattr` 命令清除下载隔离标记 |
| 手机打不开网页 | 确认手机和电脑在同一个网络，并检查"设备 → 手机 Companion"里的地址和端口 |
| 手机提示访问码无效 | 访问码被更换过，重新扫一次电脑上的二维码 |

## 从源码运行

需要 Python 3.12+、Node.js 22.12+、Rust stable 和对应平台的 C 编译工具链。

```bash
git clone https://github.com/kai3316/clipsync.git
cd clipsync
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
```

启动桌面端：

```powershell
.\Start-ClipSync.bat            # Windows
```

```bash
cd desktop && npm ci && npm run tauri -- dev    # macOS / Linux
```

`Start-ClipSync.bat` 是开发用的启动脚本，不是打包好的应用：第一次运行会编译整个 Rust 程序，数据默认写在仓库里的 `.tauri-dev-data`。只想使用的话，请下载安装包。

运行测试：

```bash
python -m pytest tests -q
cd desktop && npm test && npm run test:e2e
```

## 反馈与链接

- [Releases](https://github.com/kai3316/clipsync/releases)：版本改动和安装包
- [Issues](https://github.com/kai3316/clipsync/issues)：问题反馈
- [落地页](https://kai3316.github.io/clipsync/)：功能介绍和界面截图

## 许可

MIT。

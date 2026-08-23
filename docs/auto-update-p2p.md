# ClipSync 自动更新 + 局域网 P2P 分发 — 设计方案

> 状态:待评审(尚未编码)
> 前置约束:当前**无 macOS Developer ID / Windows Authenticode 生产签名证书**,构建仅 adhoc 签名(`build.yml` 中 `codesign --sign -`)。此约束决定了各平台的落地方式(见 §4)。

---

## 1. 目标与非目标

### 目标
1. **中心自动更新**:周期性检查 GitHub Releases → 下载 → 校验 → 按平台落地安装 → 重启。
2. **局域网 P2P 分发**:同平台的已更新设备,把官方二进制就近传给落后设备,避免每台都从外网拉几十 MB。
3. 全程**可校验**(防篡改/防坏包),失败可回退。

### 非目标(明确不做)
- 不做增量/差分更新(bsdiff/courgette)。全量二进制对当前体量(单文件 exe,几十 MB)足够,差分复杂度收益比低。
- 不做"无外网完全离线"的 P2P(首版仍需一次轻量 HTTPS 拉取 manifest,见 §8)。
- 不改动签名/公证发布流程(那是另一个独立议题,单独排期)。

---

## 2. 现状盘点(直接复用,不重造)

| 能力 | 位置 | 复用方式 |
|---|---|---|
| 中心版本检查/下载 | `internal/system/updater.py` | 扩展:加 manifest 拉取 + SHA-256 校验 |
| 平台资产名映射 | `updater.py:_platform_asset_name()` | 扩展:macOS arm64、Linux arm64 带 arch;Windows / Linux x64 保留旧名以兼容 |
| 受信对等传输(mTLS + 证书 pinning) | `internal/security/pairing.py` | P2P 传输直接走它,防 MITM |
| 分块文件传输 | `internal/sync/file_transfer.py` | 复用 chunk/ack/重传,加 `update` 传输类型 |
| 局域网发现 | `internal/transport/discovery.py` | mDNS TXT 加版本/平台字段 |
| 单文件自重启 | `src/main.py:_spawn_restart_process()`(L3044) | 复用于"重启并落地" |
| 周期循环 | `src/main.py:_update_peers_loop()`(L3463) | 挂载周期性版本检查(现为 peer 列表刷新) |
| 消息帧 | `internal/protocol/codec.py` | 无需改,`file_transfer` 已封装 |

---

## 3. 总体架构

```
                        ┌─────────────────────────────┐
                        │   GitHub Releases (真相源)    │
                        │  release 资产 + manifest.json │
                        └──────────────┬──────────────┘
                                       │ HTTPS(轻量)
        ┌──────────────┐               ▼                ┌──────────────┐
        │  设备 B(新版) │◀── mDNS TXT:v/os/arch ──────▶ │  设备 A(旧版) │
        │  已下好二进制 │                               │  检测到落后    │
        └──────┬───────┘                               └──────┬───────┘
               │  A 向 B 请求二进制(P2P,分块,mTLS)              │
               └───────────────────────────────────────────────┘
                               ▼
                     A 收到 blob → 对 SHA-256 → 落地 → 重启
```

核心原则:**版本真相源是 GitHub,manifest(含各资产 SHA-256)走 HTTPS;大二进制体可选走 P2P。** P2P 只做"就近缓存",不承载信任。

---

## 4. 跨平台落地策略(本设计的核心)

统一抽象一个 `UpdateApplier` 接口,三个平台各自实现:

```python
class UpdateApplier(Protocol):
    def verify(self, artifact_path: str, expected_sha256: str) -> bool: ...
    def stage(self, artifact_path: str) -> "StagedUpdate": ...   # 解压/放置 .new,不做破坏性操作
    def apply(self, staged: "StagedUpdate") -> None: ...         # 替换 + 重启(可能 spawn helper 后退出)
```

"下载什么"和"怎么装"分离:传输层只认 `(platform, arch, version, sha256, blob)`。

### 4.1 Windows(onefile `clipsync.exe`)

- **限制**:运行中的 exe 被锁定,不能覆盖自身。
- **流程**:
  1. 下载 zip → 解压 → `verify()` 校验 SHA-256。
  2. `stage()`:把新 exe 写到 `clipsync.exe.new`(与当前 exe 同目录)。
  3. `apply()`:spawn 一个 helper(`cmd /c` 或复用 `_spawn_restart_process` 的隔离逻辑)→ 本进程退出 → helper 等旧进程释放锁后 `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING | MOVEFILE_DELAY_UNTIL_REBOOT)`(失败则用 `cmd /c move /y`)→ 拉起新 exe。
- **回退**:替换前把旧 exe 改名 `.old`;新 exe 启动后写"启动成功"标记,失败则 helper 回滚 `.old`。
- **无 Authenticode 的代价**:替换后的 exe 首次启动仍会触发 SmartScreen(用户装机时已遇到过一次,可接受)。不会比"手动下载重装"更差。

### 4.2 macOS(`clipsync.app`)

- **限制**:`.app` 主可执行文件运行中被锁;**无 Developer ID + 未公证** → 无法做受信静默替换,Gatekeeper 会拦。
- **结论(受签名约束)**:macOS 只做 **"下载 + 校验 + 交给用户"** 的提示式更新:
  1. 下载 zip → `verify()` 校验。
  2. `stage()`:解压到临时 `.app`。
  3. `apply()`:提示用户拖到 /Applications(或 `os.replace` 到 /Applications 但明示需要用户在系统弹窗里确认"打开")。
- **未来若补 Developer ID + notarization**,再升级为 Sparkle 式半静默替换(helper 在退出后 `ditto` 换包)。本设计预留接口,不阻塞。
- **架构(已定)**:macOS 只出 Apple Silicon(arm64,`macos-latest`),Linux 双架构(x64 + arm64),已落进 `build.yml`。

### 4.3 Linux(单二进制 / tar.gz)

- **限制最小**:运行中的二进制可被 `unlink`/`os.replace`(inode 语义)。
- **流程**:下载 tar.gz → 解压 → `verify()` → `os.replace` 覆盖旧二进制 → 提示重启(或直接重启)。
- **回退**:保留旧二进制 `.old`,启动失败回滚。

### 4.4 架构差异(x64 / arm64)

- 平台标识扩展为 `(platform, arch)` 二元组,统一规范:`platform ∈ {win, macos, linux}`,`arch ∈ {x64, arm64}`。
- P2P 分发**严格按二元组匹配**:Windows 设备永远不发给 mac;arm64 不发给 x64。
- `_platform_asset_name()` 从"仅 Linux 带 arch"改为"按平台+架构"(已实现):`clipsync-macos-arm64.zip` / `clipsync-linux.tar.gz`(x64 保留旧名以兼容)/ `clipsync-linux-arm64.tar.gz` / `clipsync-windows.zip`。

---

## 5. 数据模型与接口

```python
# internal/system/updater.py(扩展)

@dataclass(frozen=True)
class PlatformTarget:
    platform: str          # "win" | "macos" | "linux"
    arch: str              # "x64" | "arm64"

@dataclass(frozen=True)
class AssetInfo:
    url: str
    sha256: str
    size: int

@dataclass(frozen=True)
class UpdateManifest:
    version: str                                # "1.0.51"
    assets: dict[PlatformTarget, AssetInfo]

def fetch_manifest(timeout: float = 10.0) -> UpdateManifest | None: ...   # 拉 manifest.json(HTTPS)
def sha256_file(path: str) -> str: ...                                    # 流式计算,不整读进内存
def verify_artifact(path: str, expected: str) -> bool: ...                # sha256_file == expected
def current_target() -> PlatformTarget: ...                               # 当前平台+架构
```

### manifest.json(发布资产之一)

```json
{
  "version": "1.0.51",
  "assets": {
    "win/x64":   {"url": "…/clipsync-windows.zip",  "sha256": "…", "size": 1234567},
    "macos/arm64": {"url": "…/clipsync-macos-arm64.zip", "sha256": "…", "size": 2345678},
    "linux/x64":  {"url": "…/clipsync-linux.tar.gz", "sha256": "…", "size": 3456789},
    "linux/arm64": {"url": "…/clipsync-linux-arm64.tar.gz", "sha256": "…", "size": 3456790}
  }
}
```

- `build.yml` 的 release 步骤额外生成并上传 `manifest.json`(CI 里算好各资产 SHA-256)。
- **兼容旧资产名**:manifest 里可同时保留旧键(如 `win`)→ 指向同一资产,`current_target()` 优先取新键、回退旧键,保证已发布的旧 tag 仍能解析。

---

## 6. 协议与消息(P2P 部分)

### 6.1 发现:mDNS TXT 附加字段

`discovery.py` 注册 `_clipsync._tcp` 时,TXT 增加:

```
v=1.0.51     os=win     arch=x64
```

- `v` 直接用 `internal.version.__version__`。
- 收到 peer 的 TXT 后,比较 `v` 与本地版本,标记"该 peer 是否有可提供的新版"。

### 6.2 请求/传输:复用现有分块传输

不新造轮子,给 `file_transfer.py` 增加一种传输类型(或一组消息):

```python
# 新增消息类型(在 file_transfer.handle_message 的 dispatch 里扩展)
"update/offer"    # B→A: {"version":"1.0.51","platform":"win","arch":"x64","sha256":"…","size":N}
"update/request"  # A→B: {"version":"1.0.51","platform":"win","arch":"x64"}
# 之后走现有 file_request / file_chunk / file_ack / file_complete 流程,transfer_kind="update"
```

流程:
1. A 从 mDNS 看到 B 的 `v > 本地` 且 `(os,arch)` 相同。
2. A 先走 §7 的安全流程,拿到目标资产 `sha256`(HTTPS manifest 或已缓存的 manifest)。
3. A 向 B 发 `update/request`,B 把本地已下载的官方资产 blob 按现有分块发回。
4. A 收完 `verify_artifact()` 对 `sha256`,对不上丢弃并回退 GitHub。

---

## 7. 安全模型

**信任锚是"开发者发布的哈希",不是"谁发来的"。**

- 中心下载:manifest 走 **GitHub HTTPS**(TLS 认证 github.com)+ 资产 SHA-256 校验。双重保障。
- P2P 下载:peer 只是哑缓存;blob 必须对得上 manifest 里的 SHA-256。**即使 peer 恶意,也塞不进坏二进制**(哈希对不上即弃)。
- 传输通道:复用现有 **mTLS + 证书 pinning**(`pairing.py`),保证你在跟真配对设备说话,防中间人篡改。
- **首版不引入 Ed25519 签名 manifest / 内置公钥**(需要 key 管理与轮换)。因此首版 P2P 仍需一次轻量 HTTPS 拉 manifest(拿权威哈希),做不到"完全离线"。若要完全离线 P2P,作为 M3+ 硬化解(内置公钥 + 签名 manifest,见 §9)。

---

## 8. 分阶段里程碑

### M0 — 基础(不改用户可见行为)
- 抽取 `PlatformTarget` / `AssetInfo` / `UpdateManifest` 数据类。
- `updater.py` 加 `sha256_file` / `verify_artifact` / `current_target` / `fetch_manifest`。
- `build.yml` 生成并上传 `manifest.json`(含各资产 SHA-256)。

### M1 — 中心自动更新(单设备,不依赖签名证书)
- `_update_peers_loop` 挂节流检查(每 6h 一次 + 手动触发)。
- 下载 → 校验 → 三个 `UpdateApplier` 落地(Win self-replace helper / mac 提示拖入 / Linux 就地替换)。
- 复用 `_spawn_restart_process` 完成重启;加 `.old` 回退。
- 托盘/Web UI 通知"发现新版本 → 下载 → 重启安装"。

### M2 — 局域网 P2P 分发
- `discovery.py` TXT 加 `v/os/arch`。
- `file_transfer` 加 `update/offer|request`,复用分块传输。
- 接收端校验哈希,失败回退中心。

### M3+ — 硬化(可选,后续)
- Ed25519 签名 manifest + 内置公钥 → 完全离线 P2P + 公钥轮换策略。
- 增量更新(若未来二进制膨胀)。
- macOS 补齐 Developer ID + notarization → 升级为半静默替换。

---

## 9. 风险与回退

| 风险 | 缓解 |
|---|---|
| Win 替换失败(锁未释放) | helper 带重试 + `MOVEFILE_DELAY_UNTIL_REBOOT`;保留 `.old` 回滚 |
| 坏包/篡改 | SHA-256 强校验,失败即弃并回退中心 |
| P2P 中途断开 | 复用 file_transfer 的重传/断点续传;超时回退中心 |
| macOS 静默更新受限 | 明示"提示式更新",UI 不承诺静默 |
| manifest 与资产不一致 | CI 生成 manifest 时对资产实测 SHA-256,写入前校验 |
| 版本回退/降级 | 不做自动降级;仅 `_is_newer` 触发 |

---

## 10. 与现有代码集成点(文件:行)

- `internal/system/updater.py` — manifest/哈希/`UpdateApplier` 协议。
- `internal/transport/discovery.py` — TXT 字段。
- `internal/sync/file_transfer.py` — `update/offer|request` + `transfer_kind="update"`。
- `src/main.py:_update_peers_loop()`(L3463)— 周期检查。
- `src/main.py:_spawn_restart_process()`(L3044)— 重启落地。
- `src/main.py:_check_for_update()`(L3891)/ 下载入口(L5690)— 接线。
- `.github/workflows/build.yml` — 生成 `manifest.json`。

---

## 11. 关键决策摘要(已定)

1. 无签名证书 → macOS 只做提示式更新;Windows/Linux 用 SHA-256 自建信任。
2. 混合架构:GitHub 为真相源,P2P 为局域网加速。
3. 全量二进制,不做差分。
4. 首版 P2P 保留一次 HTTPS manifest 拉取,不做完全离线。

## 12. 待确认问题

1. ~~**macOS 架构覆盖**~~ → **已定**:macOS 只出 Apple Silicon(arm64),Linux 双架构(x64 + arm64),已落进 `build.yml` 与 `updater.py`。
2. **更新触发 UX**:托盘静默通知 vs 弹窗强提醒?自动下载 vs 仅提示下载?
3. **manifest 是否需要签名**(M3+):如果首版就想要完全离线 P2P,需提前决定内置公钥方案。

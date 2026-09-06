# ClipSync 前瞻性重构方案

> 目标：在不打断现有桌面端、Web 管理界面和局域网同步能力的前提下，将 ClipSync 从“集中式应用脚本”逐步演进为可测试、可扩展、可支持新客户端的同步平台。

## 1. 现状与约束

### 已知现状

- 项目是 Python 桌面应用，包含系统剪贴板监听、设备发现、加密配对、局域网/中继传输、文件与聊天同步，以及 Web 管理界面。
- `src/main.py` 集中了应用生命周期、依赖创建、平台分支、UI 回调、网络控制和业务编排，是首要的复杂度热点。
- Web 层同时有 `server.py`、`routes.py`、API 模块、WebSocket 模块和 Vue 静态组件；桌面 UI 与 Web UI 容易形成两套业务流程。
- 现有代码已有较完整的 Python 与 Web 测试，并通过 Ruff 静态检查。重构应以“行为不变、测试先行、每步可回退”为原则。

### 当前工作树约束

审查时工作树已有大量未提交的在制修改。正式启动重构前，应先将这些修改提交到独立分支，或建立独立工作树；不能将架构迁移与未定稿功能混在同一个提交中。

建议的分支约定：

```text
main
└── refactor/application-boundary
    ├── refactor/lifecycle
    ├── refactor/sync-use-cases
    └── refactor/web-adapter
```

## 2. 目标架构

```text
┌────────────────────── 客户端适配层 ──────────────────────┐
│ 桌面 UI（Tk/CTk） │ Web UI（HTTP/WebSocket） │ 未来移动端 │
└──────────────────────────┬───────────────────────────────┘
                           │ 命令、查询、领域事件
┌──────────────────────────▼───────────────────────────────┐
│ 应用层 Application                                           │
│ 生命周期编排 │ Use Cases │ 服务注册 │ 事件分发 │ 权限/策略 │
└───────────────┬──────────────────────┬────────────────────┘
                │                      │
┌───────────────▼────────────┐ ┌───────▼───────────────────┐
│ 同步领域 Domain             │ │ 设备领域 Domain           │
│ 剪贴板、历史、文件、聊天    │ │ 发现、配对、在线状态      │
└───────────────┬────────────┘ └───────┬───────────────────┘
                │                      │
┌───────────────▼──────────────────────▼───────────────────┐
│ 基础设施 Infrastructure                                     │
│ 操作系统剪贴板 │ SQLite │ LAN/Relay │ 加密 │ 通知 │ 文件系统 │
└───────────────────────────────────────────────────────────┘
```

### 边界规则

1. 客户端适配层不得直接读写同步管理器、传输连接或数据库内部状态。
2. 应用层负责调用顺序、事务边界、错误映射和事件广播；不负责协议细节或平台 API 调用。
3. 领域层只表达业务规则和端口（接口）；不导入 Tk、HTTP、WebSocket、`sys.platform` 或具体数据库实现。
4. 基础设施层实现端口，处理网络、文件、加密、数据库和操作系统差异。
5. 所有跨模块状态变化都用不可变事件对象表达，而不是通过共享可变字典隐式传播。

## 3. 推荐目录布局

以下布局为迁移后的目标，不要求一次性移动全部文件：

```text
src/
  main.py                         # 极薄的进程入口
internal/
  application/
    bootstrap.py                  # 构建依赖图
    lifecycle.py                  # start / stop / recover
    events.py                     # 事件总线与事件定义
    services.py                   # 应用服务容器
    use_cases/
      clipboard.py
      devices.py
      transfers.py
      pairing.py
      history.py
  domain/
    clipboard/
    devices/
    transfer/
    security/
  ports/
    clipboard.py                  # ClipboardReader/Writer/Monitor
    peer_registry.py
    transport.py
    history_repository.py
    notifier.py
  infrastructure/
    clipboard/
    transport/
    persistence/
    security/
    platform/
  adapters/
    desktop/
    web/
```

迁移期间允许旧目录继续存在；每迁移一个能力，旧实现改为调用新 use case，直到没有调用点后再删除。不要以“移动文件”为目标，必须以“清晰依赖方向和可验证行为”为目标。

## 4. 分阶段执行计划

## 阶段 0：建立安全网（预计 1–2 天）

### 目的

让后续每一次拆分都有明确的回归判据，并消除由换行符或混合改动带来的噪声。

### 具体步骤

1. 将当前功能修改提交到独立分支，记录对应基线提交 SHA。
2. 确认 `.gitattributes` 已纳入版本控制，并统一 Python、JavaScript、JSON、YAML 的换行规则；避免 LF/CRLF 警告覆盖真实 diff。
3. 在 CI 中固定执行：

   ```text
   python -m ruff check .
   python -m pytest -q
   npm run test:web
   ```

4. 为以下高风险路径补齐“先锁行为”的测试：应用启动/停止、设备配对恢复、剪贴板接收去重、离线队列重试、WebSocket 断线重连。
5. 为每个重构提交设定规则：不改变公开 HTTP API、协议消息格式和持久化格式；若必须改变，先增加兼容读取，再在后续版本移除旧格式。

### 验收条件

- 全量 Python、Web 与静态检查在干净工作树中通过。
- CI 对每个提交执行三类检查。
- 重构分支不包含无关的格式化或功能改动。

## 阶段 1：抽离应用生命周期与依赖装配（预计 3–5 天）

### 目的

把 `src/main.py` 从“所有事情都做”的中心，变成一个只负责启动与退出的薄入口。

### 新增对象

```python
@dataclass
class ApplicationServices:
    config: Config
    transport: TransportPort
    sync: SyncService
    pairing: PairingService
    history: HistoryRepository
    notifier: Notifier
    event_bus: EventBus

class ApplicationLifecycle:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def recover(self) -> None: ...
```

### 具体步骤

1. 只抽取启动顺序：加载配置、创建服务、注册事件、启动传输、启动 UI/Web、启动监控。此步不改变业务调用关系。
2. 为每一个可关闭资源登记显式的 `stop` 回调，并定义逆序关闭顺序：接受新请求 → 同步队列 → 传输 → 持久化 → UI/通知。
3. 将锁文件、单实例检查、崩溃恢复和日志初始化迁入 `ApplicationLifecycle`。
4. 入口收敛为：构建服务 → 创建生命周期对象 → `start()` → 在 `finally` 中 `stop()`。
5. 通过依赖注入替代运行时从模块全局变量取对象；测试中可传入内存仓库、虚假传输器和事件记录器。

### 关键风险与控制

- 风险：关闭顺序改变导致未发送消息丢失。
- 控制：先为离线队列 flush、连接关闭、数据库关闭顺序补测试；第一版仅包装旧函数，第二版才移动实现。

### 验收条件

- `src/main.py` 不再包含业务流程实现，只保留入口和少量兼容代码。
- 进程可连续启动/退出两次，不留下锁、监听端口或后台线程。
- 生命周期测试覆盖正常启动、部分启动失败和重复停止。

## 阶段 2：建立端口与领域服务（预计 1–2 周）

### 目的

隔离平台、网络和持久化细节，使同步逻辑能够独立测试并可替换实现。

### 优先抽取的端口

```python
class ClipboardPort(Protocol):
    def read(self) -> ClipboardContent | None: ...
    def write(self, content: ClipboardContent) -> None: ...
    def subscribe(self, callback: Callable[[ClipboardContent], None]) -> Subscription: ...

class TransportPort(Protocol):
    def send(self, peer_id: str, message: OutboundMessage) -> DeliveryResult: ...
    def is_online(self, peer_id: str) -> bool: ...
    def close(self) -> None: ...

class HistoryRepository(Protocol):
    def append(self, item: ClipboardRecord) -> None: ...
    def list(self, query: HistoryQuery) -> list[ClipboardRecord]: ...
```

### 具体步骤

1. 先为现有 Windows、macOS、Linux 剪贴板实现套上 `ClipboardPort` 适配器，不改变平台代码内部逻辑。
2. 抽取 `ClipboardSyncUseCase`：验证内容、去重、过滤、持久化、投递和结果事件全部在该 use case 内顺序完成。
3. 抽取 `DevicePairingUseCase`：发起、确认、证书变更、撤销、恢复分别成为可单测的命令。
4. 抽取 `FileTransferUseCase` 与 `ChatUseCase`，复用同一 `TransportPort`、在线状态和重试策略。
5. 将 `SyncManager` 逐步降级为兼容门面，内部委托 use cases；等所有调用点完成迁移后再移除。

### 事件示例

```python
@dataclass(frozen=True)
class ClipboardDelivered:
    peer_id: str
    content_id: str
    channel: Literal["lan", "relay"]

@dataclass(frozen=True)
class TransferFailed:
    transfer_id: str
    reason: str
    retryable: bool
```

事件先在进程内同步分发即可；不要在本阶段引入消息队列或微服务。目标是解耦，不是增加部署复杂度。

### 验收条件

- 领域 use case 不导入 GUI、HTTP 或操作系统模块。
- 可用 fake transport 和内存 history repository 覆盖成功、离线、超时、重试与重复投递。
- 平台剪贴板适配器的行为测试在各受支持操作系统上运行。

## 阶段 3：收敛 Web 与桌面 UI（预计 1 周）

### 目的

让 Web API 和桌面 UI 成为同一应用服务的两个输入/输出适配器，消除业务规则分叉。

### 具体步骤

1. 将 `routes.py` 中的业务判断迁到 use case；路由只做输入校验、调用、HTTP 状态码和 DTO 序列化。
2. 将 WebSocket 推送改为订阅应用事件。服务端不直接观察各管理器内部字段。
3. 桌面 UI 的按钮回调同样改为调用 use case，并订阅事件刷新视图。
4. 统一错误模型，例如 `ValidationError`、`NotFoundError`、`ConflictError`、`TransientTransportError`；由 Web/桌面适配器分别映射为 JSON 或对话框。
5. 为 Web API 建立契约测试：同一 use case 在 HTTP 与桌面调用路径下应产生一致状态变化。

### 验收条件

- HTTP/WebSocket 层无需知道数据库对象、TCP 连接或平台剪贴板实现。
- Web 与桌面对“配对、发送、取消、重试”的同一动作复用同一 use case。
- 现有 API 路径和消息格式保持兼容，或提供带版本号的迁移期端点。

## 阶段 4：数据、安全与可观测性加固（预计 1 周）

### 数据层

1. 历史记录、收藏、离线消息、传输任务和设备元数据通过 repository 访问。
2. 为数据写入定义幂等键（内容 ID、传输 ID、消息 ID），避免重试造成重复记录。
3. 数据迁移采用“新字段可选读取 → 双写/回填 → 强制新格式 → 删除旧字段”的四步策略。

### 安全层

1. 加密密钥、配对证书、密码散列只由安全模块创建和读取；业务模块仅持有高层能力。
2. 日志默认脱敏设备名、IP、文件路径、令牌和密钥指纹；诊断导出须明确告知用户。
3. 将证书变化、重复配对、过期会话和降级连接设计为明确的安全事件，允许 UI 提示、审计与拒绝策略。

### 可观测性

1. 每次同步与传输携带 correlation ID，贯穿日志、事件和 WebSocket 通知。
2. 记录结构化指标：设备在线数、投递成功率、重试次数、平均文件传输时长、队列长度。
3. 诊断页只读取聚合状态，不直接访问内部线程对象。

## 5. 提交与发布策略

### 单个提交的标准

- 一个提交只迁移一个边界或一个 use case。
- 每个提交均可启动应用，并通过 Python、Web 和静态检查。
- 不在重构提交中顺手修改文案、样式或无关格式。
- 先增加兼容层和测试，再迁移调用点，最后删除旧代码；删除不得与新增功能混在同一提交。

### 建议的提交序列

```text
test: lock application lifecycle behavior
refactor: introduce application service container
refactor: move startup and shutdown orchestration to lifecycle
refactor: define clipboard and transport ports
refactor: route clipboard sync through use case
refactor: make web routes thin adapters
refactor: retire legacy sync manager entry points
```

### 发布保护

- 先以内部预览版发布，采集启动失败、恢复、网络切换和配对异常的诊断信息。
- 新旧协议和数据格式共存至少一个小版本。
- 为关键重构开关保留配置项，出现严重问题时可切回旧路径；稳定后删除开关，避免长期双实现。

## 6. 不建议做的事

- 不要在第一阶段引入微服务、外部消息队列或全新的前端框架。
- 不要一次性移动所有文件或重写传输协议。
- 不要让事件总线变成新的全局服务定位器；事件用于通知，命令仍应通过明确 use case 调用。
- 不要为了消除文件行数而机械拆分；模块边界必须对应业务能力和依赖方向。

## 7. 首个两周迭代的可交付成果

第 1 周：完成阶段 0 与阶段 1，交付薄入口、生命周期对象、服务容器、启动/停止测试和稳定 CI。

第 2 周：完成剪贴板同步的端口与 use case 迁移，桌面与 Web 保持调用旧接口但由旧接口委托新 use case；交付离线、去重、过滤和重试的完整单测矩阵。

迭代结束时的成功标准不是“目录变漂亮”，而是：新增一个客户端、替换一种传输方式或调整一个 UI 时，不需要修改剪贴板同步的核心业务规则。

## 8. 多系统适配与“桌面感”专项方案

### 8.1 判断：问题不是缺少框架

项目已有 Web UI、Vue 组件、HTTP/WebSocket 服务与桌面 WebView 相关代码；因此它并非“没有使用框架”。目前看起来像网页，主要原因是 Web 页面几乎直接承担了桌面产品全部可见交互，而窗口边框、菜单、系统设置、通知、文件选择、快捷键、拖放与系统状态之间缺少一致的原生适配层。

大型桌面应用也常使用 Web 技术，但会额外投入三部分：稳定的跨平台运行时、完善的 native bridge（原生能力桥）和按平台规范打磨的交互设计。框架解决的是运行时和工具链，不能自动产生“桌面感”。

### 8.2 建立平台能力层，而不是在业务代码中判断系统

禁止在同步、配对、Web 路由等业务代码中散落 `sys.platform`、注册表调用、`osascript` 或 Linux shell 命令。所有系统差异收敛为能力接口。

```text
internal/platform/
  capabilities.py       # 能力探测：是否支持通知、全局快捷键、开机启动等
  window.py             # 窗口、最小化、聚焦、标题栏、置顶
  tray.py               # 托盘和上下文菜单
  clipboard.py          # 平台剪贴板适配器（可继续复用现有实现）
  notifications.py      # 系统通知
  files.py              # 文件选择、打开文件夹、默认应用
  startup.py            # 登录启动
  shortcuts.py          # 全局快捷键
  theme.py              # 跟随系统深浅色、缩放与高对比度
  windows/
  macos/
  linux/
```

统一接口示例：

```python
class PlatformCapabilities(Protocol):
    def supports(self, feature: Feature) -> bool: ...

class WindowPort(Protocol):
    def show_main_window(self) -> None: ...
    def hide_to_tray(self) -> None: ...
    def set_badge(self, count: int | None) -> None: ...

class FileDialogPort(Protocol):
    def choose_files(self, options: FileDialogOptions) -> list[Path]: ...
```

UI 不应根据操作系统名称决定功能是否显示，而应根据能力决定：例如 Linux 桌面环境不支持系统通知时，在 UI 中解释原因并提供应用内提示；某系统不支持全局快捷键时，不显示不可用的设置项。

### 8.3 架构决策：采用 Tauri，弃用旧桌面 UI

本项目确定采用 **Tauri 2 + Vue 前端 + Python sidecar** 的桌面架构。现有 Tk/CustomTkinter 窗口、现有 WebView 窗口、旧仪表盘与旧设置窗口不再继续开发；它们只在迁移期用于功能对照和紧急回退，最终从发布包和代码树中删除。

选择 Tauri 的原因：

- Python 继续承载剪贴板、局域网发现、配对、加密、传输、历史记录等成熟核心能力，避免重写高风险底层功能。
- Tauri 负责窗口、托盘、菜单、通知、文件对话框、自动更新、深浅色和权限边界，形成真正的桌面宿主。
- Vue 继续承担数据密集型的设备、历史、传输、聊天和设置页面，但只作为受限前端，不再直接访问本地 HTTP 管理接口或系统资源。
- Rust 层以能力声明和命令白名单缩小攻击面；Python 后台进程不对局域网外或任意网页暴露管理端口。

### 8.4 新壳与内容的重新分工

采用“ Tauri 原生壳 + Vue 内容 + 受控 sidecar 协议”的混合模式，而非让 Web 页面直接控制系统。

```text
Tauri 原生壳（Rust）
  进程生命周期、单实例、窗口、标题栏、菜单、托盘、通知、文件选择、快捷键
        │
        ├── 受版本控制的 Native Bridge（白名单 API）
        │
Vue 内容
  设备列表、历史、设置表单、传输队列、聊天、诊断数据可视化
        │
应用 Use Cases
  同步、配对、传输、历史、配置
```

Bridge 只能暴露任务级 API，例如 `chooseFiles()`、`showInFolder(path)`、`setWindowMode(mode)`；禁止把任意 Python 对象、任意本地路径读取或 shell 执行能力暴露给页面。所有入参要校验，所有敏感动作应由原生层确认和审计。

通信链路固定为：

```text
Vue 页面
  → Tauri invoke / event
Rust command handler
  → 版本化 JSON-RPC（stdio 或受认证的本地 socket）
Python sidecar
  → application use cases / domain services
```

不得继续让 UI 访问 `localhost` HTTP API 或直接持有 WebSocket 管理连接。若保留 HTTP/WebSocket，仅作为 Python 内部测试接口或受认证的远程控制接口，而不是桌面 UI 的主通道。

### 8.5 已选运行时路线：Tauri 2

Tauri 负责原生交互与进程监管，Python sidecar 负责业务核心。Rust 不承接剪贴板同步业务规则，只负责：启动/关闭 sidecar、健康检查、命令权限验证、窗口与托盘、更新和系统集成。

建议新增结构：

```text
desktop/
  src/                         # Vue 3 + Vite 页面与组件
  src-tauri/
    src/
      main.rs                  # Tauri 启动与插件注册
      bridge.rs                # invoke → sidecar JSON-RPC 映射
      sidecar.rs               # 子进程监管、重启、健康检查
      commands.rs              # 受白名单保护的原生命令
    capabilities/              # 每个窗口/平台的权限清单
    binaries/                  # 各目标平台 Python sidecar
internal/
  application/                 # 由 sidecar 调用的应用服务
  domain/
  infrastructure/
```

`desktop/src-tauri/capabilities` 必须按窗口和平台最小授权。主窗口可读取业务状态、选择文件、展示通知；不能获得任意 shell 执行、任意文件系统访问或未经校验的 sidecar 参数。

### 8.6 Tauri 前端重建与旧 UI 退出计划

#### 新前端的边界

- 从零建立 Vite + Vue 3 应用骨架、设计令牌、状态层、路由和组件测试；不复制旧 HTML 的全局脚本结构。
- 页面只由 Tauri command/query 获取数据，并通过 Tauri event 接收状态变化。
- 每个页面调用面向任务的 API，例如 `listDevices`、`pairDevice`、`sendClipboard`、`selectTransferFiles`，不读取 Python 内部对象结构。
- 所有原生动作放在 Rust command；Vue 页面不得引入 Node API、文件路径直读或 shell 能力。

#### 旧 UI 的分三步退出

1. **冻结**：不再为 `internal/ui/`、`internal/web/static/`、旧本地 Web server 增加任何功能；只修复阻断迁移的严重缺陷。
2. **替换**：新 Tauri 页面覆盖设备、配对、剪贴板历史、文件传输、聊天、设置、诊断等能力。每覆盖一个能力，将新旧操作共用同一 Python application use case，并用端到端测试对比结果。
3. **删除**：当发布版在三平台连续通过完整回归后，删除旧桌面窗口、旧 Web 静态资源、仅为旧 UI 提供的路由/API、旧 WebView 依赖与相关打包资源。删除必须独立提交，并在删除前提供迁移清单。

#### 不迁移的内容

- 旧 HTML/CSS 的视觉细节不作为兼容目标；只保留信息架构、功能和必要文案。
- 旧本地 Web 管理入口默认不随桌面发行版提供。
- 旧 UI 的直接状态读取、全局对象和页面内业务规则不得搬入 Vue；改为从 application use case 重建。

### 8.7 让应用具有桌面感的优先清单

1. 使用每个平台的窗口控制、原生菜单和托盘约定，而不是 HTML 模拟标题栏和右键菜单。
2. 文件选择、打开所在目录、通知、权限请求、快捷键注册都由原生层执行。
3. 对 Windows、macOS、Linux 分别适配启动方式、菜单布局、关闭窗口行为、通知样式和深色模式。
4. Web UI 使用系统字体栈、系统缩放、键盘焦点可见性、高对比度和 reduced-motion；避免固定像素密度与只适合鼠标的操作。
5. 离线、连接中、后台同步、错误重试应通过系统托盘状态、窗口 badge 和通知提供反馈，而不只依赖页面内 Toast。
6. 将“关闭窗口”定义为平台策略：Windows 通常最小化至托盘，macOS 更符合关闭窗口但保留菜单栏进程，Linux 应提供可配置选项。

### 8.8 Tauri 迁移的 12 周实施节奏

| 周期 | 交付内容 | 验收结果 |
| --- | --- | --- |
| 1–2 周 | Tauri/Vite 骨架、Python sidecar 打包、协议草案、健康检查 | 三平台均能启动空壳并显示 sidecar 健康状态 |
| 3–4 周 | Rust bridge、设备列表与配对页面、原生托盘/窗口行为 | Vue 不访问旧 HTTP UI API；可完成发现、配对、退出 |
| 5–6 周 | 剪贴板历史、文件传输、聊天、事件推送 | 主要业务均经 JSON-RPC use case；断线与重启可恢复 |
| 7–8 周 | 设置、诊断、通知、文件对话框、快捷键、平台主题 | 关键系统操作全由 Tauri 插件或 Rust command 提供 |
| 9–10 周 | 移除旧 UI 调用点、跨平台端到端测试、签名与更新 | 新 UI 覆盖全部发布功能，旧 UI 仅保留在回退构建 |
| 11–12 周 | 删除旧 UI、移除旧 Web UI 路由、预览发布与回归修复 | 发布包不再包含 Tk/WebView/旧静态 UI 依赖 |

### 8.9 跨平台质量门槛

- Windows 10/11：WebView/窗口缩放、托盘、开机启动、文件拖放、通知、全局快捷键。
- macOS：菜单栏、Dock 行为、权限、签名/notarization、Retina 缩放、深色模式。
- Linux：至少定义支持的发行版和桌面环境；验证 X11/Wayland、通知守护进程、托盘实现和包格式。
- 所有平台：从干净安装到发现设备、配对、同步文本、发送文件、断网重试、卸载清理的一条端到端路径。

最终目标不是将所有视觉元素“伪装成原生”，而是让系统相关的交互真实地使用系统能力，同时保留 Web UI 在数据密集型页面、快速迭代与跨端复用上的优势。

## 9. Tauri 实施规格（执行时逐项勾选）

本章是实施准则。除非在对应任务中明确说明，否则不得修改协议字段、配置文件格式、数据库表、加密数据格式或网络帧格式。

### 9.1 实施前的固定规则

1. 从当前可运行提交创建 `refactor/tauri-shell` 分支；不要在包含未提交功能的工作树中开始迁移。
2. 每项任务开始前运行并记录基线：`python -m ruff check .`、`python -m pytest -q`、`npm run test:web`。
3. 每项任务完成后重复上述检查；失败时只修复本项任务引入的问题，不顺手重构其他模块。
4. 一个提交只包含一个编号任务。例如 `tauri-02` 只新增壳和 sidecar 监管，不能同时修改配对算法。
5. 任何新 Tauri 命令都必须同时具备：Rust 单元测试、Vue 调用测试、Python 侧协议测试、权限配置和错误映射。
6. Python 业务层仍是唯一真相来源。Vue 永远不缓存设备、历史或传输状态的权威副本；它只保存显示状态，并以 sidecar 快照/事件刷新。

### 9.2 迁移期间的目录与归属

先新增而不删除旧文件。完成全部功能覆盖后，才进入删除任务。

```text
copyboard/
  desktop/                         # 新 Tauri 桌面端；唯一的新 UI 开发位置
    package.json
    vite.config.ts
    src/
      main.ts
      App.vue
      api/bridge.ts                # Vue 唯一的宿主访问入口
      api/types.ts                 # 与 RPC DTO 一一对应
      stores/
      pages/
      components/
      styles/
    src-tauri/
      Cargo.toml
      tauri.conf.json
      capabilities/
      src/
        main.rs
        state.rs
        commands.rs
        bridge.rs
        sidecar.rs
        error.rs
      binaries/
  internal/
    application/                   # 新增：仅业务编排与 RPC dispatcher
      rpc.py
      lifecycle.py
      services.py
    ...                             # 现有 domain/infrastructure 逐步迁移
  scripts/
    build-sidecar.ps1
    build-tauri.ps1
    smoke-test.ps1
```

归属规则：

| 需求 | 唯一实现位置 |
| --- | --- |
| 剪贴板监听、去重、过滤、投递 | Python `internal/` |
| 配对、证书、加密、局域网发现、文件传输 | Python `internal/` |
| 窗口、托盘、原生菜单、原生文件选择、通知、更新 | Rust `desktop/src-tauri/` |
| 页面布局、表单校验、列表渲染、短暂交互状态 | Vue `desktop/src/` |
| Vue 与 Python 间的数据转换、权限校验、子进程监管 | Rust bridge |

不得将网络同步逻辑迁入 Vue/Rust，也不得让 Python 直接操作 Tauri 窗口对象。

### 9.3 tauri-01：创建可编译的最小桌面壳

#### 要新增的文件

- `desktop/package.json`：仅包含 Vue、Vite、Tauri API、TypeScript、Vitest、Vue Test Utils。
- `desktop/src/main.ts`：挂载 Vue 应用；不得包含业务 HTTP 请求。
- `desktop/src/App.vue`：仅显示 `Starting ClipSync…` 和只读健康状态。
- `desktop/src-tauri/src/main.rs`：注册 Tauri 插件、创建主窗口、加载配置。
- `desktop/src-tauri/tauri.conf.json`：定义应用名、标识符、窗口默认尺寸、图标和打包目标；标识符在发布后不可随意改变。
- `desktop/src-tauri/capabilities/main.json`：只授权主窗口所需的 core/window/event 权限；第一步不得授予 shell、文件系统或任意命令执行权限。

#### 必须实现的行为

1. 开发模式下能启动一个单窗口应用。
2. 窗口加载本地打包资源，不加载远程 URL。
3. 前端显示 Tauri 壳版本和“sidecar 未启动”的静态状态。
4. 关闭窗口必须触发显式退出；托盘最小化行为留到 `tauri-05`，避免一开始出现后台残留进程。

#### 验收

```text
在 Windows、macOS、Linux 各执行一次开发启动。
主窗口无 DevTools 错误。
构建产物中不包含旧 Tk/WebView 窗口启动入口。
```

### 9.4 tauri-02：定义 Python sidecar 与 NDJSON-RPC 协议

#### 进程模型

Tauri 启动一个经过 PyInstaller 打包的 Python sidecar。Rust 持有子进程句柄，Vue 不得直接启动、停止或重启 sidecar。进程间使用标准输入/输出，每条消息一行 UTF-8 JSON（NDJSON）；stderr 只用于结构化日志，不能混入协议输出。

```text
Tauri starts sidecar
  → Python sends {"type":"ready","protocol":1,"pid":...}
  → Rust enables UI commands
  → Vue invokes command
  → Rust validates and writes request line
  → Python returns response line and optional event lines
  → Rust maps response/event to Tauri invoke/event
```

#### 请求与响应的固定格式

```json
{"type":"request","id":"uuid","method":"devices.list","params":{}}
{"type":"response","id":"uuid","ok":true,"result":{"devices":[]}}
{"type":"response","id":"uuid","ok":false,"error":{"code":"DEVICE_NOT_FOUND","message":"...","retryable":false}}
{"type":"event","name":"device.changed","data":{"device":{}}}
{"type":"ready","protocol":1,"pid":12345}
```

约束：

- `id` 由 Rust 生成 UUID；Python 不生成、Vue 不传入。
- `method` 只能是下文白名单中的精确字符串；未知方法返回 `METHOD_NOT_FOUND`。
- `params` 必须是对象；缺字段返回 `VALIDATION_ERROR`；多余字段默认拒绝，除非该方法文档明确允许。
- 单行协议上限为 1 MiB；文件内容不得走 RPC，只传已验证的本地路径和元数据。
- 每个请求必须在 30 秒内返回响应；长任务须先返回任务 ID，再通过事件报告进度。
- Rust 发现非 JSON、协议版本不匹配、重复 ID 或 sidecar 意外退出时，立即禁用相关 UI 操作并显示可操作错误。

#### Python 文件改动

新增 `internal/application/rpc.py`，只包含：读取一行、解析 DTO、调用 application service、序列化结果、写出一行。该文件不得导入 Tk、Web server 或 Vue 资源。

新增 `src/sidecar_main.py`：初始化日志、加载现有配置、构建服务、启动生命周期、发出 `ready`、运行 RPC 循环、在 stdin EOF 或 `app.shutdown` 后逆序关闭服务。

将 `clipsync.spec` 拆分为 `clipsync-sidecar.spec`（仅 Python 核心、无旧 UI 静态资源）和后续的 Tauri 打包脚本引用。不要让 PyInstaller 再打包 `internal/web/static/`。

#### Rust 文件改动

- `sidecar.rs`：启动、读取 stdout、写 stdin、等待、终止、退避重启；最多连续重启 3 次，间隔 1 秒、3 秒、10 秒。
- `bridge.rs`：维护 `request_id → oneshot sender` 映射；事件转发到前端；进程退出时使所有未完成请求失败为 `SIDECAR_UNAVAILABLE`。
- `state.rs`：仅存储 bridge、窗口句柄和运行状态，禁止存储业务对象。

#### 验收测试

1. 以假的 Python sidecar 测试：ready、正常响应、错误响应、异常退出、无效 JSON、超时、连续三次崩溃。
2. Python 测试：每个固定 DTO 的反序列化、未知字段拒绝、错误码映射、stdout 无日志污染。
3. Rust 测试：请求 ID 映射清理、进程退出后 pending request 全部 resolve 为错误。

### 9.5 tauri-03：建立命令白名单与权限清单

第一版只实现以下命令，严格按顺序开发；未列出的能力不得通过“通用调用”绕过。

| Tauri command | RPC method | 参数 | 返回值 | 原生权限 |
| --- | --- | --- | --- | --- |
| `get_app_status` | `app.status` | 无 | 版本、sidecar 状态、同步状态 | 无 |
| `list_devices` | `devices.list` | 无 | 设备摘要数组 | 无 |
| `start_pairing` | `pairing.start` | `device_id` | 配对状态 | 无 |
| `confirm_pairing` | `pairing.confirm` | `device_id`, `code` | 配对状态 | 无 |
| `list_history` | `history.list` | 分页、筛选 | 历史分页 | 无 |
| `send_clipboard` | `clipboard.send` | `device_ids`, `content_id` | 投递任务 ID | 无 |
| `choose_transfer_files` | 无 | 文件类型、是否多选 | 已验证路径数组 | dialog、fs read scope |
| `start_transfer` | `transfer.start` | `device_ids`, `paths` | 传输任务 ID | 无 |
| `open_in_folder` | 无 | 已验证的应用管理路径 | 无 | opener、fs scope |
| `set_autostart` | `settings.set_autostart` | `enabled` | 最终状态 | autostart |
| `quit_app` | `app.shutdown` | 无 | 无 | process lifecycle |

实施要求：

1. `choose_transfer_files` 只在 Rust 中调用原生对话框，返回的路径应使用 canonical path 规范化。
2. `start_transfer` 仅接受由本进程最近一次 `choose_transfer_files` 产生、未过期且仍为普通文件的路径 token；前端不能伪造任意路径字符串。
3. `open_in_folder` 仅接受历史记录或应用导出目录中的白名单路径。
4. Rust command 参数先由 TypeScript schema 校验，再由 Rust schema 校验；Python 仍进行第三次业务校验。
5. 每个权限在 `capabilities/main.json` 单独声明。禁止使用含义过宽的 `shell:allow-execute` 或无限文件系统 scope。

### 9.6 tauri-04：重建 Vue 前端，不搬运旧页面结构

#### 页面与数据源

| 新页面 | 对应功能 | 初始数据 | 实时更新事件 | 迁移完成判定 |
| --- | --- | --- | --- | --- |
| `OverviewPage` | 同步总开关、连接概况 | `app.status`、`devices.list` | `app.status.changed`、`device.changed` | 可启动/暂停同步并显示真实设备数量 |
| `DevicesPage` | 发现、配对、撤销、连通性 | `devices.list` | `device.changed`、`pairing.changed` | 旧设备面板无需打开即可完成全流程 |
| `HistoryPage` | 历史、收藏、复制、删除 | `history.list` | `history.changed` | 分页、筛选、空状态和错误状态完整 |
| `TransfersPage` | 选文件、进度、重试、取消 | `transfers.list` | `transfer.progress`、`transfer.changed` | 大文件、失败重试、取消均通过测试 |
| `ChatPage` | 对话、附件、已读/送达 | `chat.list` | `chat.message`、`chat.delivery` | 两设备双向会话完整 |
| `SettingsPage` | 名称、语言、启动、通知、安全 | `settings.get` | `settings.changed` | 设置保存后重启仍持久化 |
| `DiagnosticsPage` | 日志摘要、导出、健康状态 | `diagnostics.get` | `app.health.changed` | 不暴露密钥、完整路径或原始敏感日志 |

#### 前端强制约束

- 只允许 `src/api/bridge.ts` 导入 `@tauri-apps/api`；其他组件不得直接 `invoke` 或 `listen`。
- 每种远端数据只能由一个 store 管理；组件只调用 store action 和读取 computed state。
- 每个异步状态必须至少有 loading、empty、success、retryable-error、non-retryable-error 五种显示状态。
- 所有事件监听在页面卸载或 store dispose 时取消；严禁重复监听导致重复通知。
- CSS 使用设计令牌（色彩、间距、字号、圆角、阴影）；禁止散落硬编码颜色与平台特例。
- 首先支持键盘操作、屏幕阅读器标签、100%/125%/150%/200% 缩放，再优化动画。

#### 视觉与平台行为

- Windows：原生右键菜单、任务栏/托盘语义、系统强调色、窗口最小化而非模拟浏览器标签。
- macOS：菜单栏优先、标准快捷键、关闭窗口不必退出后台服务的可配置策略。
- Linux：明确支持的桌面环境；托盘不可用时提供窗口内替代入口，不假设所有发行版都有相同行为。

### 9.7 tauri-05：迁移系统集成

按以下顺序实现，每完成一项就在三平台执行人工冒烟测试。

1. 单实例：第二次启动将焦点带回既有窗口，并把 deeplink/文件参数转交给首实例。
2. 系统托盘：显示同步状态、最近错误、打开主窗口、暂停/恢复、退出；菜单文案通过现有 i18n 资源生成。
3. 通知：只通过 Tauri 原生通知插件发送；点击通知必须回到对应页面或任务。
4. 文件对话框与拖放：路径进入 Rust 白名单后再给 sidecar；拒绝目录、符号链接逃逸和不存在文件。
5. 开机启动：只使用 Tauri autostart 能力，迁移旧配置开关；升级后不得产生重复启动项。
6. 全局快捷键：先检测可用性；注册失败必须在设置页解释而不是静默失败。
7. 主题与窗口状态：遵循系统深浅色，窗口大小/位置保存时验证显示器边界，避免恢复到不可见屏幕。

### 9.8 tauri-06：构建、签名、更新与平台产物

#### Sidecar 构建

1. `scripts/build-sidecar.ps1` 以当前锁定 Python 环境构建 sidecar，不包含旧 UI 资源。
2. 每个平台和架构生成独立文件名；Tauri 配置中使用其目标三元组对应的 binary 名称。
3. 构建脚本在产物生成后执行 `--self-test`：sidecar 必须在 10 秒内输出有效 `ready` 并可响应 `app.status`。

#### Tauri 构建

1. `scripts/build-tauri.ps1` 依次执行前端类型检查、前端测试、Rust 测试、sidecar 构建、Tauri 打包。
2. 包中必须只存在一个 Python sidecar；不得同时携带旧 `main.py` 桌面入口和 Tk/WebView 依赖。
3. Windows、macOS、Linux 均由对应平台 CI runner 原生构建，不采用未经验证的交叉编译替代签名流程。
4. 发布前检查应用标识符、签名证书、更新公钥、版本号和更新 feed 一致；这些字段变更必须人工复核。

#### 更新回退

- 新版启动 Python sidecar 失败三次后，显示恢复指引和诊断导出入口，不无限重启。
- 升级不迁移或删除现有用户配置，直到 Tauri 版至少稳定一个小版本。
- 若更新需要迁移数据，必须先备份，再写 migration journal，成功后才替换原文件。

### 9.9 tauri-07：旧 UI 的精确删除清单

只有满足 9.10 的全部发布门槛后，才允许删除。删除顺序固定如下：

1. 删除旧桌面启动路径：`internal/ui/dashboard.py`、`internal/ui/settings_window.py`、`internal/ui/onboarding.py`、`internal/ui/systray.py`、`internal/ui/webview_window.py`、`internal/ui/dialogs.py` 及其仅被旧 UI 使用的依赖。
2. 删除旧 Web 界面资源：`internal/web/static/` 下的页面、组件、样式、浏览器脚本与旧 UI 的前端测试。
3. 删除旧本地 Web UI 宿主：`internal/web/server.py`、`internal/web/routes.py`、`internal/web/ws.py`、`internal/web/dialog.py` 以及仅给旧 UI 使用的 `internal/web/api/` 路由。
4. 逐一删除 `src/main.py` 中创建旧窗口、启动旧 Web server、注册旧 UI 回调的代码；保留并迁移业务启动、单实例、配置和核心服务逻辑到 `src/sidecar_main.py`。
5. 更新 `clipsync.spec`、`requirements.txt`、`pyproject.toml`、CI 工作流、README 和打包脚本，移除不再使用的 Tk/WebView/旧前端依赖。
6. 使用 `rg` 验证已删除模块不存在任何 import、字符串路由或打包声明；确认 `git diff --check` 无误后再合并。

不可删除的现有模块包括剪贴板实现、同步管理、传输、加密、配对、历史库和配置逻辑；它们须先通过 application service/RPC 适配后再考虑内部重构。

### 9.10 发布门槛与逐平台验收脚本

不满足任一项，不得删除旧 UI 或将 Tauri 版标记为稳定版。

#### 自动化门槛

```text
python -m ruff check .
python -m pytest -q
cd desktop && npm run typecheck
cd desktop && npm run test
cd desktop/src-tauri && cargo test
scripts/build-sidecar.ps1 --self-test
scripts/build-tauri.ps1 --verify-package
```

#### Windows 冒烟路径

1. 从全新安装启动，确认只启动一个主进程/sidecar 组合。
2. 发现另一设备、完成配对、同步文本、发送文件、取消一次传输、重试一次失败传输。
3. 最小化到托盘、从托盘恢复、重启应用，确认设备和设置仍存在。
4. 断开网络后发送内容，再恢复网络，确认离线队列按原规则投递。
5. 在 100%、125%、150%、200% 缩放和深/浅色下检查关键页面。

#### macOS 冒烟路径

1. 安装已签名产物，确认系统权限、菜单栏图标、窗口关闭/后台行为。
2. 完成与 Windows/Linux 设备的双向文本和文件同步。
3. 验证 Retina、深色模式、通知点击、标准快捷键和应用退出后的 sidecar 清理。

#### Linux 冒烟路径

1. 明确选择并记录受支持发行版和桌面环境；分别验证 X11 与 Wayland 的限制和降级行为。
2. 验证启动、托盘或其替代入口、通知、文件选择、剪贴板同步和卸载。
3. 如果某桌面环境不支持托盘或全局快捷键，必须展示可理解的降级提示，不能假装成功。

#### 性能与可靠性门槛

- 冷启动到首页可交互的目标值由产品负责人在实现前指定，并在三平台持续测量；不可用“感觉更快”作为结论。
- Sidecar 意外终止时，界面在 2 秒内显示不可用状态；自动恢复后必须重新获取全量快照，不依赖可能丢失的事件。
- 连续启动/退出 20 次后不遗留监听端口、锁文件、子进程或临时敏感文件。
- 协议异常、权限拒绝、文件路径非法、网络超时均有用户可理解的错误与对应日志 correlation ID。

### 9.11 合并前检查表

- [ ] 本任务只改动计划允许的文件范围。
- [ ] 新 command、RPC method、DTO、错误码、权限清单和测试一一对应。
- [ ] Vue 未直接调用 Tauri API（除 `api/bridge.ts` 外）。
- [ ] Rust 未暴露任意命令执行或无限文件系统访问。
- [ ] Python stdout 仅输出 NDJSON-RPC。
- [ ] 所有新事件都可在 sidecar 重启后通过快照恢复。
- [ ] 三类既有检查与新增 Tauri 检查全部通过。
- [ ] 更新说明和回退步骤已写入发布说明。
- [ ] 若删除文件，已完成独立删除提交并运行全仓库引用搜索。

## 10. AI Agent 执行编排清单（本章为实施入口）

本章将本方案改写为可由多个 AI Agent 协作执行的任务图。Agent 必须把第 9 章视为技术合同；本章定义执行顺序、并行边界和交付格式。**任何 Agent 都不得以“顺手优化”为理由扩大任务范围。**

### 10.1 总控 Agent 的固定职责

总控 Agent 不直接进行大规模代码编辑。它只负责：

1. 创建或确认 `refactor/tauri-shell` 基线分支，并记录基线 SHA、当前 `git status`、三类既有检查的结果。
2. 创建一个 `work/tauri-migration-ledger.md` 迁移账本，记录每个任务的负责人、输入提交、输出提交、验证命令、状态、阻塞原因和回退点。
3. 为每个会编辑代码的 Agent 创建隔离工作树；绝不允许两个 Agent 同时编辑相同路径或共享当前脏工作树。
4. 按本章的 Gate 顺序合并结果；Gate 未通过，不派发后续依赖任务。
5. 在每次合并后执行全量验证，并将失败任务退回到原 Agent；总控 Agent 不自行“临时修补”他人的变更。

账本记录格式固定如下：

```markdown
| Task | Agent | Input SHA | Output SHA | Files owned | Checks | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A00 | coordinator | abc123 | def456 | none | baseline | done | clean baseline |
```

状态只能使用 `queued`、`running`、`blocked`、`review`、`done`、`reverted`。不得用模糊状态如“差不多”“待观察”。

### 10.2 通用 Agent 工作契约

每个 Agent 接到任务后必须按以下顺序执行：

1. 阅读本方案中引用的章节和自己拥有的文件范围。
2. 执行任务开始时的只读基线检查，确认工作树没有他人变动。
3. 只改动任务清单明确列出的文件；若必须新增文件，放在任务指定目录。
4. 先添加或更新测试，再实现最小变更；不得先做全局格式化。
5. 执行任务要求的最小验证；输出修改摘要、完整命令结果、剩余风险和提交 SHA。
6. 不得自行合并、删除旧 UI、修改发布配置或升级依赖，除非任务明确授权。

每个 Agent 的最终交付必须采用以下结构：

```text
状态：done / blocked
提交：<SHA>
改动文件：<逐项路径>
验证：<命令> → <结果>
未做事项：<明确列出>
风险/交接：<明确列出；没有则写“无”>
```

### 10.3 任务图与并行规则

```text
A00 基线冻结
  └─ G0：基线可复现
      ├─ A01 旧 UI 盘点（只读）
      ├─ A02 Python 核心启动盘点（只读）
      ├─ A03 Tauri 打包/CI 设计（只读）
      └─ A04 前端信息架构与 DTO 清单（只读）
          └─ G1：接口与删除范围确认
              ├─ A05 Tauri/Vite 最小壳（编辑 desktop/）
              ├─ A06 Python sidecar 入口与 RPC schema（编辑 internal/application/, src/）
              └─ A07 协议测试夹具（编辑 tests/，不改生产逻辑）
                  └─ G2：壳、sidecar、协议可互通
                      ├─ A08 Rust sidecar 监管与 bridge（编辑 desktop/src-tauri/）
                      ├─ A09 Vue bridge、types、stores 基座（编辑 desktop/src/）
                      └─ A10 Tauri 权限与安全审计（审查/最小编辑 capabilities）
                          └─ G3：最小端到端闭环
                              ├─ A11 设备与配对迁移
                              ├─ A12 历史与剪贴板迁移
                              ├─ A13 文件传输与聊天迁移
                              ├─ A14 设置、诊断、i18n 迁移
                              └─ A15 原生系统集成迁移
                                  └─ G4：功能覆盖
                                      ├─ A16 多平台打包/签名/更新
                                      ├─ A17 端到端回归与可访问性
                                      └─ A18 旧 UI 删除（最后且串行）
                                          └─ G5：发布候选
```

并行限制：

- A01–A04 可并行，因为只读；它们不得修改代码或锁文件。
- A05–A07 可并行，但必须分别处于隔离工作树；A05 只能写 `desktop/`，A06 只能写 Python sidecar/RPC 文件，A07 只能写测试夹具。
- A08、A09、A10 可并行；A08 不改 Vue，A09 不改 Rust，A10 只能改 capability 文件和审计文档。
- A11–A15 可以按“一个功能域一个 Agent”并行，但每个 Agent 只允许调用已合并的 bridge/DTO，不得私自增加通用 RPC 逃生口。
- A16 与 A17 可并行；A18 必须在 A16、A17 都完成且 G4 通过后单独串行执行。

### 10.4 A00：冻结基线（串行，必须第一个）

**输入**：当前仓库。  
**拥有文件**：仅 `work/tauri-migration-ledger.md`；不得修改项目代码。  
**步骤**：

1. 检查并记录 `git status --short`；若存在未提交改动，停止并要求总控将其提交、暂存到用户指定分支或明确排除。
2. 创建 `refactor/tauri-shell`，记录起点 SHA。
3. 执行 `python -m ruff check .`、`python -m pytest -q`、`npm run test:web`，分别记录退出状态和摘要。
4. 在账本中登记 A01–A04 为 `queued`。

**完成条件**：有唯一、干净、可回退的起点；所有基线失败都已记录而非隐藏。  
**禁止事项**：不解决基线失败、不升级依赖、不创建 Tauri 文件。

### 10.5 G0：基线可复现（总控串行）

仅在以下条件全部满足时打开 A01–A04：

- 基线 SHA 已写入账本；
- 每条基线检查都有结果；
- 后续编辑 Agent 都有独立工作树；
- 没有 Agent 在用户的原始脏工作树写代码。

若基线测试失败，创建单独的“基线修复”任务，不得伪装成 Tauri 迁移任务。

### 10.6 A01–A04：只读发现批次（四个并行 Agent）

#### A01：旧 UI 盘点 Agent

**范围**：只读 `internal/ui/`、`internal/web/`、`internal/web/static/`、`src/main.py`。  
**输出**：`docs/tauri/legacy-ui-inventory.md`，列出每个旧窗口、路由、静态页面、组件、调用入口、依赖的业务服务、测试文件，以及“替换后删除”或“保留为业务核心”的结论。  
**禁止**：不编辑生产代码，不建议直接删除任何文件。

#### A02：Python 核心盘点 Agent

**范围**：只读 `internal/clipboard/`、`internal/sync/`、`internal/transport/`、`internal/security/`、`internal/config/`、`internal/data/`。  
**输出**：`docs/tauri/python-sidecar-boundary.md`，给出应用启动顺序、长期后台线程、可关闭资源、现有配置路径、可直接复用的业务入口、不得暴露到 UI 的敏感对象。

#### A03：构建与发布盘点 Agent

**范围**：只读 `pyproject.toml`、`requirements.txt`、`clipsync.spec`、`build.bat`、`scripts/`、`.github/workflows/`。  
**输出**：`docs/tauri/build-matrix.md`，给出 Windows/macOS/Linux 的构建输入、Python sidecar 产物名、签名/更新前置条件、CI runner 要求和现有打包依赖。

#### A04：前端信息架构 Agent

**范围**：只读旧 Vue/JavaScript 组件、locale、前端测试及 API 路由。  
**输出**：`docs/tauri/ui-contract.md`，按第 9.6 页面表输出页面、DTO、命令、事件、加载/错误/空状态、i18n key、键盘操作与弃用的旧 UI 元素。

所有发现文档必须标明依据文件路径和行号；不能把推测写成事实。

### 10.7 G1：接口和删除范围确认（总控串行）

总控 Agent 汇总 A01–A04，只创建以下三个不可变清单：

1. `docs/tauri/rpc-v1.md`：首版 RPC methods、参数、结果、错误码、事件。
2. `docs/tauri/legacy-removal-map.md`：旧文件 → 新能力 → 删除 Gate 的映射。
3. `docs/tauri/build-targets.md`：支持的平台、架构、sidecar 文件名和构建责任。

若 A01 与 A02 对某段代码的归属结论冲突，停止派发 A05–A07，由总控先裁决。此 Gate 不允许“先做再说”。

### 10.8 A05–A07：最小可用基础批次（三个并行编辑 Agent）

#### A05：Tauri 壳 Agent

**拥有文件**：仅 `desktop/package.json`、`desktop/vite.config.ts`、`desktop/src/**`（不含具体业务页面）、`desktop/src-tauri/tauri.conf.json`、`desktop/src-tauri/src/main.rs`。  
**依赖**：G1。  
**任务**：完成第 9.3 的最小壳；只显示启动状态，不调用旧 HTTP API。  
**验收**：前端类型检查、基础 Vue 测试、Tauri 开发启动成功。  
**禁止**：不得添加 sidecar 逻辑、业务页面、通用文件系统权限。

#### A06：Python Sidecar Agent

**拥有文件**：`src/sidecar_main.py`、`internal/application/rpc.py`、`internal/application/lifecycle.py`、`internal/application/services.py`、sidecar spec 文件及它们的 Python 测试。  
**依赖**：G1。  
**任务**：实现第 9.4 的 NDJSON framing、`ready`、`app.status`、`app.shutdown`；复用现有业务启动，不修改同步协议。  
**验收**：stdin/stdout 契约测试、EOF 关闭测试、stdout 污染测试。  
**禁止**：不得删除旧入口，不得新增 Web UI API。

#### A07：协议夹具 Agent

**拥有文件**：`tests/tauri/**`、`desktop/src-tauri/tests/**`；不得修改生产代码。  
**依赖**：G1。  
**任务**：用假 sidecar 定义 ready、请求、成功、业务错误、无效 JSON、超时、退出的可复用测试夹具。  
**验收**：夹具能在 A06/A08 未合并时独立运行。  
**禁止**：不得为迁就未实现代码而降低断言强度。

### 10.9 G2：基础互通（总控串行集成）

总控按 A07 → A06 → A05 的顺序 cherry-pick/合并。完成后必须人工验证：

1. Tauri 窗口启动 Python sidecar。
2. sidecar `ready` 令界面从“启动中”切换到“可用”。
3. `app.status` 的数据完整显示。
4. 关闭 Tauri 窗口会干净关闭 sidecar。
5. 人为杀死 sidecar 后界面不可继续发请求。

未通过时，只退回对应所有者；不得把模拟 sidecar 直接当成发布实现。

### 10.10 A08–A10：桥接与安全批次

#### A08：Rust Bridge Agent

**拥有文件**：`desktop/src-tauri/src/sidecar.rs`、`bridge.rs`、`commands.rs`、`state.rs`、`error.rs` 及 Rust 测试。  
**任务**：实现第 9.4 的请求映射、30 秒 timeout、三次受限重启、事件转发、pending request 清理。  
**禁止**：不得添加无限 shell 权限；不得把 Python stdout 原样交给 Vue。

#### A09：Vue 数据基座 Agent

**拥有文件**：`desktop/src/api/**`、`desktop/src/stores/**`、`desktop/src/components/app-shell/**` 及其测试。  
**任务**：只实现 `api/bridge.ts`、DTO 类型、应用状态 store、事件订阅清理和通用错误呈现；不得实现业务页面。  
**验收**：所有 `@tauri-apps/api` 导入只出现在 `api/bridge.ts`。

#### A10：权限审计 Agent

**拥有文件**：`desktop/src-tauri/capabilities/**`、`docs/tauri/security-review.md`。  
**任务**：为 A08/A09 暴露的每条命令建立最小 capability；编写“权限 → 命令 → 用例”矩阵。  
**验收**：没有宽泛 shell 执行、无限 fs scope 或未经窗口限定的敏感权限。

### 10.11 G3：最小端到端闭环

打开业务功能 Agent 前，总控必须确认：

- Rust bridge 可通过 A07 假 sidecar 的所有异常测试；
- Vue 可经唯一 bridge 调用 `app.status`；
- 任何 Vue 组件均无法直接取得任意文件路径或执行 sidecar；
- sidecar 挂掉、超时、协议错误在 UI 中有不同且可操作的错误状态；
- A10 的权限审计无 blocker。

### 10.12 A11–A15：功能域并行迁移批次

每个 Agent 都必须先在 `rpc-v1.md` 提交增量，经总控批准后才可新增 RPC method。禁止多个 Agent 竞争编辑同一 RPC 文档；采用“提案 → 总控合并 → 实现”的串行接口门槛。

| Task | 功能所有者 | 可以编辑 | 必须交付 | 不得编辑 |
| --- | --- | --- | --- | --- |
| A11 | 设备与配对 | `devices/pairing` Vue 页面、对应 RPC adapter、专属测试 | 发现、配对、确认、撤销、在线状态 | 传输、历史、全局权限 |
| A12 | 剪贴板与历史 | `history` Vue 页面、对应 adapter、专属测试 | 分页、筛选、收藏、复制、删除、发送 | 配对、原生文件选择 |
| A13 | 文件传输与聊天 | `transfers/chat` 页面、对应 adapter、专属测试 | 选文件 token、进度、取消、重试、聊天投递 | 通用 dialog 权限定义 |
| A14 | 设置/诊断/i18n | `settings/diagnostics` 页面、locale、对应 adapter、专属测试 | 保存、重启后恢复、脱敏诊断导出 | 同步核心算法 |
| A15 | 系统集成 | Rust 原生 command、托盘、通知、启动、快捷键、集成测试 | 第 9.7 七项能力及降级状态 | Vue 业务状态、Python 领域逻辑 |

每个功能域 Agent 的完成条件：

1. 对应旧 UI 功能可在新 Tauri UI 完整完成。
2. 新旧路径调用同一个 Python application use case，结果可用测试比较。
3. 页面具备 loading、empty、success、retryable-error、non-retryable-error。
4. 事件丢失后刷新页面能恢复一致状态。
5. 新增的每个 RPC method 有 DTO、Python 测试、Rust 测试、Vue 测试和 capability 说明。

### 10.13 G4：功能覆盖审核

总控不看“页面数量”，只按 `legacy-removal-map.md` 逐项审查：

- 每个旧 UI 入口是否存在等价的新入口；
- 每个功能是否有自动化测试与三平台人工冒烟记录；
- 是否仍有新 UI 调用旧 localhost API；
- 是否存在业务规则被复制到 Vue/Rust；
- 是否有任何旧 UI 独占的数据迁移、错误恢复或权限流程。

任何一项未完成，旧 UI 不能删除，A18 保持 `blocked`。

### 10.14 A16–A18：收尾批次与强制串行删除

#### A16：打包与更新 Agent

拥有 `scripts/build-sidecar.ps1`、`scripts/build-tauri.ps1`、CI 构建配置、Tauri bundle/sign/update 配置。必须完成第 9.8 的所有 self-test 和每目标产物验证；不得删除旧 UI。

#### A17：回归与无障碍 Agent

只写/修改测试、测试夹具和发布验证记录。必须完成第 9.10 三平台路径、键盘导航、缩放、深浅色、异常恢复与 20 次启动退出测试；不得为了通过测试放宽产品断言。

#### A18：旧 UI 删除 Agent（唯一允许删除者）

**前置条件**：G4 通过、A16 done、A17 done、总控书面确认可以删除。  
**拥有文件**：仅第 9.9 删除清单中的旧 UI 文件、其单元测试、依赖声明、打包声明和文档。  
**执行顺序**：必须严格按照第 9.9 的 1 → 6 顺序；每一步单独提交并运行引用搜索。  
**完成条件**：发布包不包含 Tk/WebView/旧静态 UI，核心 Python 测试及 Tauri 全量验证均通过。  
**禁止**：不得删除业务核心、配置迁移逻辑或任何尚未存在替代路径的 API。

### 10.15 G5：发布候选决策

总控在以下结果均为真时才标记 Release Candidate：

- A00–A18 全部为 `done`，没有 `blocked` 或未经批准的范围外改动；
- 第 9.10 的自动化和三平台检查都有可追溯结果；
- A18 的删除提交可从基线干净重放；
- 安装、升级、回退、卸载均已至少手工验证一次；
- 用户配置、配对数据与历史数据在升级后仍可读取；
- 安全审计确认不存在任意命令执行、任意路径访问或未隔离前端内容。

若任一条件不满足，状态只能是 `blocked` 或继续迭代，不能以“先发布再修”绕过 Gate。

### 10.16 可直接下发给 Agent 的任务模板

总控可按以下形式下发，尖括号内容必须替换为真实值：

```text
任务：A<编号> <任务名>
基线提交：<SHA>
工作树：<绝对路径>
拥有文件：<精确路径列表>
可读取范围：<精确路径列表>
禁止修改：<精确路径或规则>
依据：方案第 <章节号>，以及 <rpc-v1.md/legacy-removal-map.md>
目标：<一句可验证的结果>
执行步骤：
1. <步骤>
2. <步骤>
验证命令：
- <命令>
- <命令>
完成输出必须包含：状态、提交 SHA、改动文件、验证结果、未做事项、风险/交接。
遇到以下情况立即停止并标记 blocked：<接口冲突/基线不干净/需要新增权限等具体条件>。
```

示例：

```text
任务：A06 Python Sidecar Agent
基线提交：<G1 合并后的 SHA>
工作树：<独立 worktree 路径>
拥有文件：src/sidecar_main.py、internal/application/rpc.py、internal/application/lifecycle.py、internal/application/services.py、tests/tauri/test_rpc.py
禁止修改：src/main.py、internal/ui/**、internal/web/**、desktop/**、网络协议模块。
依据：方案 9.4、10.8 及 docs/tauri/rpc-v1.md。
目标：sidecar 只经 NDJSON-RPC 输出 ready、app.status、app.shutdown。
遇到 stdout 已被现有日志写入、或需要改变同步协议时，立即 blocked，不以临时重定向掩盖问题。
```

## 11. 2026–2030 产品工程就绪度轨道

### 11.1 结论与适用边界

第 1–10 章可以完成 Tauri 迁移和旧 UI 退出；但它本身**不能保证**软件在 2030 年前满足所有市场、行业或司法辖区的要求。适用法规取决于销售地区、客户类型、是否处理个人数据、是否进入企业采购或受监管行业，必须由法务/合规负责人作最终认定。

本章的目标是把 ClipSync 置于可持续演进的工程基线：安全开发遵循 [NIST SSDF](https://csrc.nist.gov/pubs/sp/800/218/final) 的实践方向；构建供应链可提供可验证来源；UI 以 [WCAG 2.2 AA](https://www.w3.org/TR/WCAG22/) 为目标；如果进入欧盟等市场，再由合规负责人根据产品范围核对 CRA 等当地义务及其适用日期。

### 11.2 2030 就绪度必须达到的七条基线

| 编号 | 工程结果 | 不可接受的状态 | 可验证证据 |
| --- | --- | --- | --- |
| R1 | 可重复、可追溯且签名的发布构建 | 开发机手工打包且无法说明来源 | 每个安装包关联源码 SHA、构建日志、SBOM、签名和 provenance |
| R2 | 可管理的漏洞与依赖生命周期 | 不知道依赖版本、漏洞或修复状态 | 依赖清单、扫描报告、漏洞工单、修复/缓解记录 |
| R3 | 默认最小数据与最小权限 | UI、日志或更新机制可随意读取本地数据 | 数据流图、权限矩阵、日志脱敏测试、用户控制项 |
| R4 | 可量化的可靠性与可恢复性 | 只有“感觉稳定”，无崩溃、更新或恢复数据 | SLO、健康指标、故障演练、回退记录 |
| R5 | 具备可访问性与国际化基线 | 只能用鼠标、缩放溢出、错误无语义 | WCAG 2.2 AA 测试报告、键盘/屏读记录、语言回归 |
| R6 | 支持矩阵与弃用政策明确 | 所有系统“理论支持”但没有测试范围 | 平台/架构/版本支持表、CI 覆盖、弃用公告 |
| R7 | Agent 协作可审计且可回退 | Agent 可任意改动、无任务边界和质量 Gate | 账本、隔离工作树、任务产物、审批与回滚提交 |

任何“2030 就绪”宣称都必须以 R1–R7 的证据为准，而不是以是否使用 Tauri、Electron 或 AI Agent 为准。

### 11.3 L01：软件供应链与可重复构建轨道

**开始时间**：tauri-01 创建后；**可与 A11–A15 并行**；**必须在首个稳定版前完成**。

#### Agent L01-A：锁定依赖与生成 SBOM

**拥有文件**：`desktop/package-lock.json`/等效锁文件、Python 锁定清单、`docs/security/dependency-inventory.md`、`artifacts/sbom/` 的生成脚本。  
**步骤**：

1. 列出 Python、Node、Rust、Tauri plugin、PyInstaller、操作系统 native dependency 五类依赖。
2. 为每种语言保留机器可解析的锁定文件；没有锁文件的生态不得进入发布构建。
3. 在 CI 生成每个发布工件对应的 CycloneDX 或 SPDX SBOM；SBOM 文件名包含应用版本、平台、架构和构建 SHA。
4. 将 SBOM 与安装包、校验和一起作为 release artifact 发布；不得只保存在 CI 临时目录。
5. 对同一 Git SHA 在同一 runner 镜像执行两次构建，比较产物哈希；若无法字节级一致，记录不可重复字段和原因。

**验收**：任意安装包可在 5 分钟内追溯到源码 SHA、锁文件、依赖清单和构建日志。  
**阻塞条件**：某依赖无法锁定、无法生成许可信息或需要从不受信任地址下载。

#### Agent L01-B：来源证明与签名

**拥有文件**：发布 CI 工作流、`docs/security/release-provenance.md`、签名验证脚本。  
**步骤**：

1. 仅允许受保护分支/tag 的 CI 生成正式安装包；开发机只能生成标记为 `dev` 的产物。
2. CI 输出 provenance：源码 SHA、触发者、runner、构建命令、锁文件哈希、sidecar 哈希、Tauri 包哈希。
3. 对 Windows、macOS、Linux 分别建立签名与验签步骤；私钥只存在受控密钥服务或 CI secret 中，绝不写入仓库、日志或 Agent 上下文。
4. 发布前自动验证：安装包签名、sidecar 哈希、更新清单签名、SBOM 与包版本一致。
5. 以 [SLSA provenance](https://slsa.dev/spec/v1.2/provenance) 为参考，第一目标是有可验证 provenance，下一目标是在受控 hosted CI 上生成签名 provenance。

**验收**：下载者可独立验证发布包未被替换，并能确定其来源构建。  
**禁止**：不允许 Agent 请求、打印或复制签名私钥。

### 11.4 L02：漏洞、威胁建模与安全响应轨道

**开始时间**：G2 后；**依赖**：稳定的 sidecar/Rust/Vue 边界。

#### Agent L02-A：威胁模型

**拥有文件**：`docs/security/threat-model.md`、`docs/security/data-flow.md`。  
**步骤**：

1. 绘制五条数据流：Vue→Rust、Rust→Python、设备间 LAN、设备→relay、更新服务器→客户端。
2. 对每条数据流列出资产、信任边界、认证方式、加密方式、攻击者能力、失败模式、日志内容和缓解项。
3. 必须覆盖：恶意剪贴板内容、伪造发现包、重放/降级配对、路径穿越、恶意文件名、sidecar stdout 注入、更新源劫持、前端 XSS、丢失设备、日志泄露。
4. 每个高风险项必须映射到一个代码控制、一项测试和一条告警/诊断证据；无法缓解时明确列为已接受风险并指定负责人和复审日期。

**验收**：不存在“由框架自动处理”但没有具体设置、测试或责任人的安全结论。

#### Agent L02-B：自动安全门禁

**拥有文件**：CI 安全工作流、扫描配置、`docs/security/vulnerability-policy.md`。  
**步骤**：

1. 在 pull request 执行密钥扫描、依赖/SCA 扫描、Python/Rust/TypeScript 静态分析和许可证检查。
2. 对高危/严重漏洞设置阻断规则；若必须例外，记录 CVE、影响范围、补偿控制、到期日和批准人。
3. 增加 IPC fuzz/property tests：随机或畸形 NDJSON、超大字段、非法 UTF-8、重复 ID、事件洪泛、无效路径 token。
4. 每个已修复安全缺陷新增回归测试，防止复发。
5. 定义响应时钟：发现后 24 小时内完成分级，严重问题先给缓解/下线方案，再确定修复版本；具体 SLA 由产品负责人确认。

**验收**：安全扫描失败不能被普通 Agent 静默忽略；每个例外有到期日。  
**禁止**：不允许通过全面忽略规则或降低扫描阈值让 CI 变绿。

### 11.5 L03：隐私、数据治理与用户控制轨道

**开始时间**：A12/A13 前；**原因**：剪贴板、聊天和文件都可能包含敏感数据。

#### Agent L03-A：数据目录与保留策略

**拥有文件**：`docs/privacy/data-inventory.md`、配置 schema、历史/传输清理服务及测试。  
**步骤**：

1. 对每个数据项记录：名称、来源、是否个人/敏感数据、保存位置、加密状态、用途、默认保留期、删除方式、导出方式、谁能访问。
2. 至少覆盖：剪贴板历史、文件传输元数据、聊天、设备名/IP、配对证书、日志、诊断包、崩溃报告和遥测。
3. 默认关闭非必要遥测；任何遥测需明确 opt-in、用途说明、撤回入口和离线队列清理策略。
4. 提供“清除历史”“撤销配对”“删除本地缓存”“导出我的数据”四个独立命令；每项执行后必须可验证结果且不得误删密钥/配置。
5. 诊断导出默认掩码文件路径、设备标识、IP、令牌、消息正文和剪贴板内容；用户若选择包含敏感信息，需二次确认。

**验收**：任一数据项都能回答“为何保存、保存多久、在哪里、如何删除”。

#### Agent L03-B：数据迁移与兼容性

**拥有文件**：迁移模块、备份/恢复测试、`docs/privacy/migration-policy.md`。  
**步骤**：

1. 所有持久化格式添加 schema version；读取旧数据时只向前兼容，不悄悄覆盖原文件。
2. 每次迁移先创建带版本/时间戳的备份，再写 migration journal，成功后标记完成。
3. 中断恢复测试至少覆盖：迁移前中断、写一半中断、磁盘满、旧版本回退、重复执行。
4. 迁移失败时应用以只读安全模式启动，展示恢复/导出/回退选择；不得直接清空历史。

**验收**：从受支持的前一稳定版本升级后，配置、配对与历史可读；失败可回退。

### 11.6 L04：可靠性、可观测性与支持运维轨道

#### Agent L04-A：SLO 与健康模型

**拥有文件**：`docs/operations/slo.md`、健康状态 DTO、诊断测试。  
**步骤**：

1. 为桌面应用定义最小 SLO：启动成功率、sidecar 可用率、同步投递成功率、更新成功率、崩溃自由会话率。
2. 每个 SLO 定义分母、分子、采样周期、排除条件、数据来源、告警阈值、负责人；禁止只给百分比不定义计算方式。
3. 以 correlation ID 串联 UI command、Rust bridge、Python use case、网络投递和错误日志。
4. 诊断页只显示聚合健康状态；详细日志导出须执行 L03 的脱敏规则。
5. 增加故障演练：sidecar 崩溃、端口占用、配置损坏、网络切换、更新中断、证书变化、磁盘空间不足。

**验收**：每个故障演练能明确说明用户看到什么、应用自动做什么、数据是否丢失、如何恢复。

#### Agent L04-B：发布环与回滚

**拥有文件**：CI 发布配置、`docs/operations/release-runbook.md`、更新客户端测试。  
**步骤**：

1. 定义 `canary`、`beta`、`stable` 三个发布环；默认只向 canary 推送新大版本。
2. 每环记录目标用户、最短观察时间、升级成功率、崩溃/错误阈值和推进/暂停/回滚责任人。
3. 更新器必须能拒绝签名无效、版本不兼容、sidecar 哈希不匹配的包。
4. 回滚仅回退应用二进制，不自动降级用户数据 schema；需要数据降级时使用明确的恢复工具。
5. 建立紧急禁用机制，仅可关闭高风险功能或更新 feed，不可远程执行任意代码。

**验收**：模拟一次不良 beta 后，可在不丢失用户配置的情况下停止推送并恢复稳定版本。

### 11.7 L05：无障碍、国际化与体验质量轨道

目标为 WCAG 2.2 AA；WCAG 2.2 是当前 W3C Recommendation，且其要求可通过自动与人工组合验证。[W3C WCAG 2.2](https://www.w3.org/TR/WCAG22/)

#### Agent L05-A：可访问性基线

**拥有文件**：Vue 组件、a11y 测试、`docs/quality/accessibility.md`。  
**步骤**：

1. 自动检查语义结构、可见焦点、颜色对比、表单 label、按钮名称、ARIA 误用和键盘可达性。
2. 人工检查键盘完整路径：启动→设备→配对→历史→传输→设置→退出；不允许鼠标成为唯一完成路径。
3. 对拖放操作提供键盘等价入口，符合 WCAG 2.2 对拖动操作的要求。
4. 在 100%、125%、150%、200% 缩放及高对比度/深浅色下截图对比，记录溢出、裁剪和焦点遮挡。
5. 使用至少一种目标平台屏幕阅读器完成页面标题、状态变更、错误和进度播报检查。

**验收**：每个发布页面有自动检查和人工结果；任何阻塞级问题阻止 stable 发布。

#### Agent L05-B：国际化与文案

**拥有文件**：`desktop/src/locales/`、i18n tests、`docs/quality/i18n-policy.md`。  
**步骤**：

1. 建立一个主语言 key 清单；CI 检查所有语言缺 key、未使用 key、参数占位符不一致和硬编码 UI 文案。
2. 日期、时间、数字、文件大小、快捷键名称通过 locale formatter 处理；禁止字符串拼接造句。
3. 对中文、英文和至少一种文字更长的伪本地化测试，验证弹性布局。
4. 所有安全/删除/配对错误文案提供“发生了什么、影响是什么、下一步怎么做”三段信息。

### 11.8 L06：平台支持与兼容性治理轨道

#### Agent L06-A：支持矩阵

**拥有文件**：`docs/platform-support.md`、CI matrix 配置、兼容性测试记录。  
**步骤**：

1. 在发布前明确写出支持的 OS 主版本、架构、Linux 发行版/桌面环境、最低 WebView 运行时、Python sidecar 目标三元组。
2. 每个支持组合标记为 `tier-1`（每次发布自动测试）、`tier-2`（每月测试）或 `best-effort`（无承诺）。
3. 对每个已知能力差异写降级行为：托盘、通知、全局快捷键、Wayland 剪贴板、开机启动、文件关联。
4. 发布前 CI 至少覆盖所有 tier-1 组合的构建和核心冒烟测试；不把“能编译”当成兼容性通过。
5. 设置弃用政策：任何 tier-1 平台停止支持前至少提前两个小版本公告，并提供最低支持版本与替代路径。

**验收**：README、安装器、应用内关于页、CI matrix 和实际测试记录对支持范围一致。

### 11.9 L07：AI Agent 治理轨道

#### Agent L07-A：Agent 变更控制

**拥有文件**：`AGENTS.md`、`docs/agents/change-control.md`、CI 检查脚本。  
**步骤**：

1. 将第 10 章的任务契约转为仓库内 `AGENTS.md`：每个目录的拥有边界、测试命令、禁止事项和安全敏感文件。
2. 对 `desktop/src-tauri/capabilities/`、签名/更新配置、加密、配对、发布 workflow、数据迁移目录设置人工审查门槛；Agent 可以提出变更但不能单独批准。
3. CI 校验：变更 RPC method 时必须同步修改 DTO、Python/Rust/Vue 测试和 `rpc-v1.md`；变更权限时必须同步安全矩阵。
4. 每次 Agent 任务必须在 ledger 中留下输入/输出 SHA、测试结果和未完成事项；无记录变更不进入 release 分支。
5. 任何 Agent 遇到隐私、密钥、签名、任意执行、数据删除或兼容性不确定性，必须标记 `blocked` 并交由总控/人工裁决。

**验收**：Agent 的速度不会绕过边界、审批或可追溯性。

### 11.10 2026–2030 时间表与发布 Gate

| 时间 | 必须完成 | Gate | 不达标时的处理 |
| --- | --- | --- | --- |
| 2026 | Tauri 迁移、sidecar 协议、旧 UI 替换、R7 基础 | G5 Release Candidate | 保留旧 UI 回退构建，不删除核心兼容路径 |
| 2027 | R1/R2/R3：SBOM、签名/provenance、威胁模型、数据目录、漏洞流程 | Security GA | 只能维持 beta/canary，不进入稳定企业发布 |
| 2028 | R4/R5：SLO、发布环、故障演练、WCAG 2.2 AA、i18n CI | Reliability GA | 暂停扩大平台支持和新大功能 |
| 2029 | R6：完整平台分级、长期兼容/迁移策略、企业运维材料 | Platform GA | 不承诺新增 OS/架构支持 |
| 2030 | 每年复审法规、依赖、威胁模型、SLO 与支持矩阵；按证据续期 | Annual Readiness Review | 制定整改 backlog 与负责人/日期 |

这不是承诺某个司法辖区自动合规的清单；它是让项目在出现新的要求时拥有可审计资产、稳定边界和持续整改能力的最低工程路径。

### 11.11 长期轨道的 Agent 并行与 Gate

```text
L01 供应链 ─┐
L02 安全   ├─ 可与 Tauri 功能域迁移并行
L03 隐私   ┤
L05 a11y   ┘
      ↓
H1：Security/Privacy Baseline
      ↓
L04 可靠性 + L06 平台支持 + L07 Agent 治理（可并行）
      ↓
H2：Stable Release Readiness
      ↓
年度复审：依赖、法规适用性、威胁模型、平台矩阵、SLO
```

并行规则：L01/L02/L03/L05 可同时工作，但不得并发编辑同一 CI workflow、依赖锁文件、权限清单或同一页面组件。L04/L06/L07 在 H1 后启动，因为它们依赖稳定的构建来源、错误码、数据边界和平台能力定义。

### 11.12 年度复审 Agent 清单

每年由一个只读复审 Agent 生成 `docs/annual-readiness/YYYY.md`，不得直接改代码。报告必须逐项回答：

1. 支持矩阵是否仍与 Tauri、Python、操作系统和 WebView 的实际兼容范围一致？
2. 每个发布工件是否仍有可验证签名、SBOM、provenance 和可用更新/回退路径？
3. 是否有未过期的安全例外、未修复严重漏洞、未关闭的高风险威胁模型项？
4. 用户数据目录、保留期、遥测、诊断脱敏和删除/导出流程是否仍符合当前产品实际？
5. SLO、崩溃率、更新失败率和支持工单是否触发架构/流程整改？
6. 无障碍与国际化测试是否覆盖新增页面、语言和平台？
7. 过去一年 Agent 变更是否都能追溯到任务账本、测试和审批？

报告中的每个问题必须标记 `pass`、`risk` 或 `blocked`，并给出负责人、整改任务、目标版本和复审日期。没有这些字段的“年度评估”不视为完成。

## 12. 可维护性、可扩展性与高内聚低耦合的强制架构规则

### 12.1 最终验收目标

Tauri 迁移完成不等于重构成功。重构成功必须同时满足：

1. 新增一个功能时，改动集中在一个功能域及其明确接口内，而不是修改 `main.py`、全局状态、多个 UI 页面和多个传输模块。
2. 替换 UI、传输实现、数据库或操作系统适配器时，核心业务规则和大多数测试不变。
3. 任何模块的职责都可以用一句话描述；若需要“它负责各种杂项”，该模块必须继续拆分。
4. 依赖方向可由自动检查验证；不能依赖“开发者自觉”。
5. Agent 在不理解全项目的情况下，能根据模块契约安全地完成局部任务并通过回归测试。

### 12.2 强制依赖方向

```text
desktop/src (Vue 页面、组件、store)
            ↓ 仅 DTO / command name
desktop/src-tauri (Rust adapter、权限、sidecar 监管)
            ↓ 仅 NDJSON-RPC contract
internal/adapters (sidecar RPC adapter)
            ↓
internal/application (use cases、事务、事件编排)
            ↓
internal/domain (实体、值对象、业务规则、ports)
            ↑
internal/infrastructure (SQLite、LAN/relay、OS 剪贴板、加密实现)
```

允许的依赖：

| 模块 | 可以依赖 | 绝对禁止依赖 |
| --- | --- | --- |
| `domain` | 标准库、同一 domain、ports | Tauri、Vue、HTTP、SQLite 驱动、Tk、`sys.platform`、具体 transport |
| `application` | domain、ports、DTO、事件接口 | Vue、Rust/Tauri、HTTP handler、具体 OS API |
| `infrastructure` | domain ports、标准库、具体第三方实现 | Vue、Rust/Tauri、application 的私有状态 |
| `adapters` | application 的公开 use case、DTO | domain/infrastructure 私有对象、UI 状态 |
| Rust bridge | RPC DTO、sidecar 生命周期 | Python 内部类、业务算法、任意 shell |
| Vue | TypeScript DTO、bridge facade、页面 store | Rust command 内部、Python API、文件系统/网络管理权限 |

**唯一例外**：应用启动组合根（`src/sidecar_main.py` 与 `internal/application/bootstrap.py`）可以创建 infrastructure 实现并注入 application use case；组合根不得包含业务规则。

### 12.3 模块边界和公开 API

每个功能域必须使用以下布局；没有公开入口的实现文件一律视为私有：

```text
internal/<feature>/
  __init__.py                  # 仅导出该 feature 的公开 API
  domain.py                    # 实体、值对象、领域规则
  ports.py                     # Protocol / abstract interfaces
  service.py                   # use case 或 application service
  events.py                    # feature 对外事件
  infrastructure.py            # 可替换实现；也可放入 infrastructure/<feature>/
  tests/
```

规则：

1. 跨 feature 调用只能导入另一个 feature 的 `__init__.py` 中明确导出的类型、命令或事件；禁止跨目录 import 私有模块。
2. `__init__.py` 必须维护 `__all__`，并有测试验证其导出对象可用；不得通过隐式 import 暴露内部实现。
3. Feature 对外输入/输出只允许 DTO、值对象、事件或 port；不允许暴露数据库连接、线程、socket、全局 dict 或 GUI widget。
4. 一份配置只能由一个 feature 写入；其他 feature 通过 settings use case 请求修改，不得直接写配置文件。
5. 同一条业务规则只允许一个权威实现。例如文件大小限制由 transfer domain 定义，Vue 只做提示性预校验，Rust 只做安全范围校验。

### 12.4 组合根与依赖注入规则

禁止 service locator、模块全局单例和“从任意地方 import 当前 app”。采用构造函数注入：

```python
class StartTransfer:
    def __init__(
        self,
        repository: TransferRepository,
        transport: TransportPort,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        ...
```

实现要求：

1. `internal/application/bootstrap.py` 是 Python 唯一可实例化具体 repository、transport、clipboard adapter、clock、notifier 的位置。
2. `ApplicationServices` 只能包含公开 use case；不能成为任意对象仓库。
3. 测试通过构造函数传入 fake/in-memory 实现，不通过 monkeypatch 模块全局变量实现隔离。
4. 每个后台线程、timer、subscription 和 file handle 都由一个服务拥有，并提供 `start()`/`stop()`；所有权必须在 lifecycle 文档中登记。
5. 无法注入的系统行为（时间、随机数、进程、文件系统、网络）必须包成 port，避免测试依赖真实环境。

### 12.5 同步调用、事件和状态的一致规则

| 场景 | 允许机制 | 禁止机制 |
| --- | --- | --- |
| 用户明确发起并需要结果 | command/use case → response DTO | 直接改跨模块全局状态 |
| 已完成的状态变化通知 | 不可变 domain/application event | UI 轮询内部对象字段 |
| 长任务进度 | task ID + 有序 progress event + 最终结果 | 一个永不返回的 invoke 调用 |
| 跨 feature 低耦合反应 | 已登记的事件订阅 | 互相 import manager 并回调私有方法 |
| 需要强一致的业务流程 | application use case 内显式编排 | 依赖事件监听顺序“碰巧正确” |

事件规则：

1. 事件名称使用 `<feature>.<noun>.<past-tense>`，如 `transfer.file.completed`、`pairing.device.revoked`。
2. 每个事件包含 `event_id`、`occurred_at`、`correlation_id`、`schema_version` 和最小必要数据。
3. 事件必须可重复处理；订阅者不可假设恰好一次投递。
4. 进程重启后 UI 先请求完整快照，再接受增量事件；事件不是唯一状态来源。
5. 一项事件跨出 feature 边界前，必须写入 `docs/architecture/events.md`，包含发布者、订阅者、版本、幂等规则和错误处理。

### 12.6 扩展一个新功能的固定模板

任何新功能（例如“分享链接”“OCR”“新的传输通道”“移动端配对”）必须按以下顺序实施：

1. 创建 `docs/architecture/adr/NNNN-<feature>.md`，写明问题、非目标、所属 feature、依赖的 ports、数据变化、失败模式、隐私/安全影响和回滚方案。
2. 在 domain 创建实体/值对象和 ports；先写纯业务测试，不能导入 UI 或具体基础设施。
3. 在 application 创建一个面向任务的 use case；输入/输出为 DTO，依赖全部从构造函数注入。
4. 在 infrastructure 编写可替换实现，并通过 port contract test 验证。
5. 仅在确认需要 UI 时，向 `rpc-vN.md` 申请新的 command/query/event；总控批准后再实现 adapter、Rust bridge、Vue bridge method。
6. 新页面或组件只能调用 store action，不得直接操作其他 feature store 的私有状态。
7. 增加最少四类测试：domain 单测、application use case 测试、adapter/RPC 契约测试、用户主路径端到端测试。
8. 在功能 flag 下发布 canary；达到预先定义的可用性和错误率门槛后再稳定启用。

如果一个功能在第 5 步之前就需要修改多个既有 feature 的私有代码，Agent 必须停止并先提出 ADR；这说明现有边界需要修正，不能用横向耦合强行落地。

### 12.7 可维护性自动门禁

新增下列自动检查，检查脚本由独立的 Architecture Guard Agent 维护：

| Guard | 检查内容 | 失败处理 |
| --- | --- | --- |
| `dependency-rules` | Python import 只沿 12.2 方向；Vue 仅 `api/bridge.ts` 导入 Tauri API | 阻止合并 |
| `public-api` | 跨 feature import 只能命中 `__all__` 公共符号 | 阻止合并 |
| `rpc-contract` | RPC 方法修改同时更新 DTO、Rust/Python/Vue 测试和文档 | 阻止合并 |
| `capability-diff` | 权限变更必须附威胁模型条目、安全矩阵与人工批准 | 阻止合并 |
| `dead-legacy-ui` | 新代码不允许 import `internal/ui` 或 `internal/web/static` | 阻止合并 |
| `complexity-watch` | 超过团队约定复杂度/文件阈值的新增代码须 ADR 和拆分计划 | 标记 review，不能静默接受 |
| `ownership` | 每个改动文件属于当前 Agent 的任务范围 | 阻止合并 |

建议新增目录：

```text
tests/architecture/
  test_dependency_rules.py
  test_public_api.py
  test_rpc_contract.py
  test_no_legacy_ui_imports.py
desktop/tests/architecture/
  bridge-boundary.test.ts
  store-boundary.test.ts
docs/architecture/
  adr/
  events.md
  dependency-rules.md
```

门禁必须以解析 AST/import graph 实现；不得使用简单字符串搜索作为唯一判断方式。字符串搜索可作为补充检查，用于删除旧模块后的残余引用扫描。

### 12.8 技术债务与代码规模控制

1. 不设置“文件不得超过 N 行”的机械规则；用职责和依赖决定拆分。
2. 但当一个模块同时出现三类以上职责（业务规则、IO、线程管理、UI mapping、配置读写、协议编解码）时，必须创建拆分任务，不能继续扩展。
3. 每个 feature 的 `service.py` 只处理一个业务能力组；若命令之间没有共同状态、不变式或事务边界，拆成多个 use case。
4. `utils.py`、`helpers.py`、`common.py` 不得成为跨 feature 垃圾桶。共享代码必须有明确领域名称，如 `protocol/codec.py`、`security/fingerprint.py`。
5. 每个 release 预留固定容量清偿技术债；账本中记录债务的来源、影响、到期版本和责任 Agent/负责人。
6. 删除代码优先于新增抽象。只有至少两个真实调用点且变化维度一致时，才创建共享抽象。

### 12.9 Agent 并行开发时的防耦合规则

1. 并行 Agent 只能共享已经合并并版本化的 contract；不得共享未合并分支中的内部实现假设。
2. Interface Agent 与 Implementation Agent 分离：前者提交 ADR/RPC/port 草案，后者在 Gate 批准后实现。一个 Agent 不得在同一轮中同时定义模糊接口并让所有人依赖它。
3. 一次只允许一个 Agent 修改 `bootstrap.py`、RPC 方法表、capabilities、锁文件或发布工作流；它们是高扇入文件。
4. 合并冲突不是“由总控手工拼接”就算完成；冲突必须回到各自 feature owner，明确契约归属后重新生成提交。
5. Agent 任务结束时必须说明“我新增了哪些依赖”和“我移除了哪些依赖”；若无法说明，任务进入 architecture review。
6. Agent 不得通过复制粘贴旧业务逻辑到新 UI/Rust sidecar 以缩短迁移；任何重复规则由 `test_no_duplicate_business_rules` 的 review 检查。

### 12.10 维护性发布 Gate

除第 10、11 章 Gate 外，合并到稳定分支前必须全部通过：

- [ ] 依赖图没有违反 12.2 的反向引用。
- [ ] 新功能有 ADR、feature owner、公开 API、错误模型和测试矩阵。
- [ ] 无新全局可变状态、service locator 或跨 feature 私有 import。
- [ ] UI、Rust、Python 没有复制同一业务规则。
- [ ] Sidecar 协议、事件和数据迁移都有版本与向后兼容策略。
- [ ] Agent 任务账本记录了文件所有权、输入/输出 SHA、验证与剩余债务。
- [ ] 删除旧 UI 后，任何新功能均可由独立 Agent 在单一 feature 工作树内完成，无需编辑遗留中心入口。

达到本章门槛后，项目才具备“容易维护、容易扩展、高内聚、低耦合”的可验证基础，而不是仅仅完成了一次技术栈迁移。

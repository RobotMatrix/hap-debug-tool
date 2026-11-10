# Changelog

所有显著变更记录在本文件。格式参考 Keep a Changelog。

## [Unreleased] - 2026-11-10 (2)

### Added - 设备投屏 + 日志抓取 (双平台, 参考 HoKit 功能设计)

**新模块 `device_tools.py`** (纯标准库, macOS 系统工具链 + 内置 hdc):

- 投屏: 截屏轮询方案。鸿蒙 `snapshot_display`+`file recv`+sips (~0.7s/帧, GIF
  兼容 Tk 8.5/8.6); iOS `devicectl capture screenshot` (~2s/帧, 原生 PNG)
- 输入回传 (鸿蒙): `uitest uiInput` 点击/双击/滑动/Home/返回/电源键; iOS 系统
  无输入注入 API, 只读投屏
- 运行日志: 鸿蒙 `hdc shell hilog` 流式 (可按 level 过滤); iOS `idevicesyslog`
  流式 (需 brew libimobiledevice)
- 崩溃日志: 鸿蒙 `hidumper -s HiviewFaultLogger` 列表 + `hdc file recv` 逐个
  拉取 (shell 层 /data/log 权限拒绝, 但 file recv 走 faultloggerd 通道可直拉,
  实测 jscrash/cppcrash 均通过); iOS `copy from --domain-type systemCrashLogs`
  整包拉取 (.ips)
- 子进程走 `posix_spawn` + 超时 `killpg` (规避 Tk 主循环并存时的
  fork-exec 死锁与 hdc 设备端会话撑住管道导致的永久挂起)

**GUI (`hap_gui.py`)**:

- 主界面新增「🖥️ 投屏」「📜 设备日志」按钮 (双平台通用, 按当前平台+设备打开)
- `MirrorWindow`: 投屏窗口, 状态/FPS 实时显示, 点击=触摸、拖拽=滑动 (鸿蒙),
  Home/返回按键栏, 截图保存
- `DeviceLogWindow`: 运行日志流 (关键词过滤/级别过滤/暂停/保存), 崩溃日志
  下拉列表+单条/全部拉取, 产物落 `~/Downloads/hapdbg-crashes/<设备>/`

## [Unreleased] - 2026-11-10

### Added - iOS 签名支持 .xcarchive (Xcode 归档)

- `ios_sign.py` 新增 `find_app_in_xcarchive()`：自动定位
  `Products/Applications/` 下的主应用；归档内含多个 `.app`（extension/watch app）
  时按 Info.plist 主入口键（WKApplication / NSExtension / WKWatchOnly）甄别主包
- `sign`（CLI）与 `sign_app_source` / `get_embedded_provision` /
  `_read_bundle_id_of`（自动匹配 profile） / `verify` / `read_sign_info` 全部接受
  `.xcarchive` 输入，行为与 .ipa/.app 一致
- CLI `sign` 输出 `<xcarchive名>-signed.ipa`（默认当前目录，`-o` 可指定）
- GUI：拖拽/文件框接受 `.xcarchive`，检测到后自动切 iOS 模式；
  「安装包自带」描述文件支持从 xcarchive 提取；预检/重签管线适配

## [Unreleased] - 2026-09-30

### Fixed - iOS 重签名后启动失败（spawn error 88 "Malformed Mach-o file"）

**现象**：重签名后的 ipa 能安装到真机，但点图标启动即死。设备 syslog 显示
`runningboardd: spawn failed, error=88: Malformed Mach-o file`。安装阶段
`codesign --verify --deep --strict` 在 Mac 上全部通过，无法在 Mac 端复现。

**根因**（控制变量法定位，两个变量叠加）：

1. **Hardened Runtime 不兼容**：重签时加了 `--options runtime`，使 CodeDirectory
   flags 变为 `0x10000(runtime)`、version 升至 `0x20500`。经加固壳（如
   BBSCRUtility/scshield）处理过的二进制，其 `__TEXT` 布局无法通过 Hardened
   Runtime 的内核严格页校验，spawn 阶段即被 `EBADMACHO` 拒绝。
2. **entitlements 过度声明**：直接把 profile 的 Entitlements 全量灌给签名。其中
   `com.apple.security.hardened-process.*` 系列属于 Hardened Runtime 变体
   entitlement——app 声明了它（slot -5/-7 有值）而 CD flags 未启用 runtime
   （flags=0x0），内核校验状态自相矛盾，同样 spawn 即死。

> 对照证据：同一二进制，`--preserve-metadata=flags,runtime,entitlements` 保留
> 原厂 CD v=0x20400/flags=0x0 + 3 项 entitlements → 启动成功；`--options runtime`
> + 25 项 entitlements → 必死。三.ipa（加固壳包/无壳原包/标准 Xcode 构建）全部复现
> 该规律。

**修复**（ios_sign.py `sign_app`）：

- 主包签名移除 `--options runtime`，保持与原厂签名一致的 CD v=0x20400/flags=0x0
- entitlements 按白名单精简：仅保留 `application-identifier`、
  `com.apple.developer.team-identifier`、`get-task-allow`、`keychain-access-groups`；
  被剔除项记录在 `sign_app.last_dropped_entitlements`
- 需要额外 entitlement 的场景用 `-e key=value` 显式追加

**回归验证**（iPhone 13, iOS 26, devicectl install + launch）：

- 加固包 `TuSDK_*_sec.ipa`（含 BBSCRUtility.framework + swizzlingdylib.framework）
  → 安装 ✅ 启动 ✅
- 无壳原包（含 13 个 libswift*.dylib + swizzlingdylib.framework）→ 安装 ✅ 启动 ✅
- 标准 Xcode 构建（Asspp）→ 安装 ✅ 启动 ✅

### Fixed - 安装失败 0xe8008015（A valid provisioning profile for this executable was not found）

**现象**：`sign` 不带 `-p` 描述文件时，重签产物能过本地校验但安装被拒。

**根因**：签名 entitlements 只有 `get-task-allow`，缺 `application-identifier`，
installd 无法将其与包内 embedded.mobileprovision 匹配。

**修复**（ios_sign.py `sign_app`）：未显式指定 profile 时自动回退使用包内
`embedded.mobileprovision` 生成完整 entitlements；并修复该路径下源/目标同文件时
`shutil.copy2` 抛 `SameFileError` 的问题。

### Fixed - 安装失败 0xe800801c（No code signature found）

**现象**：安装壳厂加固包被拒，错误指向某个嵌套 framework（如
`BBSCRUtility.framework`）完全没有签名。

**修复**（ios_sign.py）：`install` 命令安装前扫描 `Frameworks/PlugIns/XPCServices`
下未签名的嵌套 bundle；发现即自动用第一张可用证书（或 `-i` 指定）按
inside-out 顺序重签并校验，再安装重签产物。`install` 新增 `-i/--identity` 参数。

### Added - GUI 描述文件管理（hap_gui.py）

- 描述文件下拉新增固定选项，排序为：**📦 安装包自带**（第一，默认选中）、
  **📂 本地选择**（第二，选过文件后出现）、本机扫描列表（其后）
- **📦 安装包自带**：自动提取当前 ipa 内 `embedded.mobileprovision`（缓存于
  `~/Library/Caches/hap_installer/embedded_prov/`）；换包后若仍选中该项自动重新提取
- **ℹ️ 详情**按钮：弹窗展示当前生效描述文件的名称/类型/AppID/TeamID/设备数/
  创建与到期时间/完整路径；支持一键 **📂 打开所在路径**（Finder 定位）与
  **📱 授权设备**（UDID 列表，开发类型 profile 才显示）
- **profile 兼容性预检**：固定 bundle id 的 profile 与包内 bundle id 不同时，
  签名前输出告警（避免设备上已装旧版触发
  `MismatchedApplicationIdentifierEntitlement` 拒装）

### Added - 其他

- `ios_sign.py` 新增 API：`get_embedded_provision()`（提取 ipa/.app 内嵌描述文件，
  稳定缓存路径）、`read_bundle_id()`、`_unsigned_nested_bundles()`（安装前体检）

### 参考文档

- Apple《Creating distribution-signed code for the Mac》— 签名顺序 inside-out
- Apple《Using the latest code signature format》— 嵌套代码逐项签名、DER entitlements
- Apple TN2206《macOS Code Signing In Depth》— 嵌套代码必须先正确签名
- Apple TN3125/TN3126《Inside Code Signing》— profile 校验模型、per-page hash 与
  Hardened Runtime 页校验
- man codesign(1) `-P/--pagesize`、`--options` — CodeDirectory 页粒度与 flags

# HAP 调试助手

鸿蒙 **HAP/HSP 一键签名 + 安装 + 启动** 工具（`小白调试助手` 的开源重实现），含图形界面与命令行。

- 复用内置 `hdc` / `signer`，无需安装 DevEco
- 对接华为云 DevEco Connect：申请调试证书、生成 Provision Profile
- 拖拽 `.app` / `.hap` → 一键签名 → `hdc install` → 启动
- 支持 App Pack(`.app` 目录或 zip)：自动重签名内部所有 `.hap`/`.hsp` 模块并整包安装
- 启动时/执行前自动检查 `oauth2token` 是否过期，过期则清除 UI 证书与 Token 信息
- 支持个人 / 企业(团队) 证书切换，团队账户默认只读保护
- **GUI 支持平台切换：鸿蒙 (HAP/HSP) / iOS (ipa/app)**

## iOS 签名 (ios_sign.py)

GUI 顶部选「iOS」即进入 iOS 模式：拖入 `.ipa` / `.app` → 选钥匙串证书 + 描述文件 →
codesign 重签名 → 校验 → `xcrun devicectl` 安装。纯 macOS 系统工具链（`security`/`codesign`/`devicectl`），零第三方依赖。

```bash
python3 ios_sign.py identities            # 列出钥匙串签名身份
python3 ios_sign.py provision xx.mobileprovision
python3 ios_sign.py sign app.ipa -i "Apple Development: ..." -p xx.mobileprovision -o out.ipa
python3 ios_sign.py verify out.ipa
python3 ios_sign.py install out.ipa       # devicectl 安装到已连接设备
python3 ios_sign.py launch com.foo.bar
```

### 描述文件三种来源（GUI 描述文件下拉框）

1. **📦 安装包自带**（默认第一项）——自动提取 ipa 内 `embedded.mobileprovision`，缓存于
   `~/Library/Caches/hap_installer/embedded_prov/`；换包后自动重新提取
2. **📂 本地选择**（第二项）——点「📂 本地」按钮弹文件框自选 `.mobileprovision`
3. **本机扫描**——Xcode/MobileDevice 目录中的描述文件列表

选中任意描述文件后点 **ℹ️ 详情**：名称/类型/AppID/TeamID/设备数/有效期/路径，
支持 **📂 打开所在路径**（Finder 定位）与 **📱 授权设备**（UDID 列表）。

### 重签名规则（关键，勿改）

依据 Apple 官方文档《Creating distribution-signed code》《Using the latest code signature format》与 TN2206：

- **嵌套代码（Frameworks/PlugIns/XPCServices）逐个先签，主包最后签**（inside-out）；
  不用 `--deep`（对嵌套代码套用同一签名参数，且漏签非标准位置代码）
- **不启用 Hardened Runtime**（不加 `--options runtime`）：加固壳处理过的二进制在
  runtime 严格页校验下会被 iOS 内核 spawn 阶段直接拒绝（`EBADMACHO`，error 88
  "Malformed Mach-o file"），CD version 也会被升到 0x20500
- **entitlements 白名单精简**：只保留 `application-identifier`、`team-identifier`、
  `get-task-allow`、`keychain-access-groups`。profile 允许 ≠ app 可声明——声明
  `com.apple.security.hardened-process.*` 等变体而 CD flags 未启用 runtime，
  内核校验状态自相矛盾，同样 EBADMACHO。需要额外 entitlement 用 `-e key=value` 显式追加
- 未指定 `-p` 描述文件时，自动回退用包内 `embedded.mobileprovision` 生成 entitlements
  （否则缺 `application-identifier` → 安装报 0xe8008015）
- `install` 安装前扫描未签名嵌套组件（如壳厂漏签的 framework），发现即自动重签再装
  （修复 `0xe800801c No code signature found`）

### 兼容性备忘（真机排障记录 2026-09）

| 现象 | 原因 | 处置 |
|---|---|---|
| 安装报 `0xe800801c No code signature found` | 壳厂漏签某个嵌套 framework | `install` 已自动重签 |
| 安装报 `0xe8008015 A valid provisioning profile...not found` | entitlements 缺 `application-identifier` | 已修复：fallback 内嵌 profile |
| 启动即死 `spawn error 88 Malformed Mach-o file` | `--options runtime`（CD flags 0x10000）或冗余 hardened-process entitlements 与壳包二进制不兼容 | 已修复：无 runtime + entitlements 白名单 |
| 安装报 `MismatchedApplicationIdentifierEntitlement` | 固定 bundle id 的 profile 与设备上已装旧版冲突 | GUI 预检告警；卸载旧版或换通配 profile |

验证基线：加固包（BBSCRUtility 壳）/ 无壳原包 / 标准 Xcode 构建三.ipa 重签后
真机（iPhone 13, iOS 26）安装+启动全部通过。

## 目录

| 文件 | 说明 |
|---|---|
| `hap_cli.py` | 核心 CLI（签名/安装/云接口/签名信息解析） |
| `hap_gui.py` | 图形界面（tkinter + tkinterdnd2，拖拽，鸿蒙/iOS 双平台） |
| `ios_sign.py` | iOS 签名 CLI/库（codesign 重签名 + devicectl 安装） |
| `hap_sniff.py` | 本地 MITM 代理，抓取 app 对华为云的真实请求 |
| `run_gui.command` | 双击启动 GUI |
| `run_capture.sh` | 一键抓包（信任 CA + 起代理 + 起 app） |
| `packaging/` | 打包为 `.dmg`（PyInstaller）、签名/公证脚本 |

## 快速开始

```bash
# GUI
./run_gui.command

# CLI
python3 hap_cli.py info app.hap          # 解析元信息
python3 hap_cli.py sig  app.hap          # 查看签名信息
python3 hap_cli.py install app.hap --no-sign
python3 hap_cli.py sign MyApp.app -o out/   # 重签名 App Pack 内所有模块
python3 hap_cli.py install MyApp.app        # 重签名并安装整包
python3 hap_cli.py cloud token-check        # 检查 token 是否过期 (exit 2=过期)
python3 hap_cli.py cloud --help          # 云接口(证书/Profile)
```

## 一键签名安装流程

```
拖入 .app/.hap
  → cloud: device/list → cert/list(申请/复用) → reapply(下载证书)
           → provision/add(生成 Profile, 下载 p7b)
  → signer: sign-app (内置 key.pem + 证书链 + p7b)
  → hdc install -r
  → aa start -a <mainElement> -b <bundle>
```

App Pack(`.app`)：解出内部所有 `.hap`/`.hsp` 逐个签名后，一次性
`hdc install <hap> <hsp...>`（hap 依赖 hsp，必须同批安装），再启动入口 Ability。

Token 过期检查：调用 `user-team-list` 探测，服务端返回 HTTP 401/403 或鉴权类
`ret.code` 即判为过期；GUI 会清空 Token / 证书名输入框并删除本地缓存凭据。

## 环境

- macOS，Python 3.9+（GUI 需 `tkinter`、`tkinterdnd2`）
- 内置工具来自 `小白调试助手.app`（`hdc` / `signer` / `key.pem` 等），
  **不随本仓库分发**；运行时通过 `--tools` / `HAP_TOOLS_DIR` 指向，
  或放在 `tools/`（`hdc`、`signer`、`store/key.pem`、`store/xiaobai.csr`、`ohos/auto_installer.hap`）。

## 打包 dmg

```bash
./packaging/build_dmg.sh          # 需要本机存在 小白调试助手.app 以收集工具
./packaging/sign_notarize.sh      # 需 Developer ID 证书 + notarytool 凭据
```

## CI 自动出 dmg

`.github/workflows/build-dmg.yml`（macOS runner）在 **手动触发** 或 **打 tag（`v*`）** 时构建 dmg。

内置工具（`hdc`/`signer`/`key.pem` 等）不入库，通过 secret 注入：

```bash
# 本地先打包工具（来自 小白调试助手.app 的 assets）
tar -czf hap-tools.tar.gz -C packaging tools
```

然后在仓库 Settings → Secrets and variables → Actions 配置其一：
- `HAP_TOOLS_URL`：`hap-tools.tar.gz` 的可下载 URL（建议放私有 release/对象存储）
- `HAP_TOOLS_B64`：小体积时用 base64（注意 secret 上限 48KB，大文件请用 URL）

未配置时仍会出 dmg，但不含内置工具（运行时需 `HAP_TOOLS_DIR` 指定）。

**默认不签名、不公证**。仅当配置了证书/公证 secrets 且满足以下之一时才执行签名+公证：
- 手动触发并勾选 `notarize`
- 打 tag（`v*`）

相关 secrets（可选）：`MACOS_CERT_P12_B64`、`MACOS_CERT_PASSWORD`、`KEYCHAIN_PASSWORD`、
`NOTARY_APPLE_ID`、`NOTARY_TEAM_ID`、`NOTARY_PASSWORD`。

## 账户安全

- 企业/团队账户默认**只读**：`cert-add` / `cert-delete` / `provision-add` / `flow` 一律拒绝
- 仅对个人账户做证书/Profile 增删

## 免责声明

仅供**已授权的鸿蒙应用调试/测试**使用。请遵守华为开发者协议与相关法律法规。

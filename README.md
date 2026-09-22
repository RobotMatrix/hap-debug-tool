# HAP 调试助手

鸿蒙 **HAP/HSP 一键签名 + 安装 + 启动** 工具（`小白调试助手` 的开源重实现），含图形界面与命令行。

- 复用内置 `hdc` / `signer`，无需安装 DevEco
- 对接华为云 DevEco Connect：申请调试证书、生成 Provision Profile
- 拖拽 `.app` / `.hap` → 一键签名 → `hdc install` → 启动
- 支持 App Pack(`.app` 目录或 zip)：自动重签名内部所有 `.hap`/`.hsp` 模块并整包安装
- 启动时/执行前自动检查 `oauth2token` 是否过期，过期则清除 UI 证书与 Token 信息
- 支持个人 / 企业(团队) 证书切换，团队账户默认只读保护

## 目录

| 文件 | 说明 |
|---|---|
| `hap_cli.py` | 核心 CLI（签名/安装/云接口/签名信息解析） |
| `hap_gui.py` | 图形界面（tkinter + tkinterdnd2，拖拽） |
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

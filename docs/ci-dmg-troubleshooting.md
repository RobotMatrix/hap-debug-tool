# CI 打包 DMG 排障实录（2026-10-13 ~ 10-14）

> 背景：hap-debug-tool（HarmonyOS HAP 签名调试助手）将打包/发布迁移到 GitHub Actions，
> 以 tag（`v*`）触发构建并自动挂 DMG 到 Release。迁移后 CI 包在本机出现一系列
> 「本地打包正常、CI 打包必挂」的差异问题。本文按发现顺序记录全部问题、根因、
> 修复方式与验证手段，供后续类似项目排障参考。

## 问题总览

| # | 现象 | 根因 | 修复 | 版本 |
|---|------|------|------|------|
| 1 | CI dmg 内工具目录多一层 `tools/tools/`，运行时找不到 signer/key.pem | tar 包顶层是 `tools/`，`tar -C packaging/tools` 解出嵌套 | workflow 解压后 flatten 剥层 + 完整性断言 | v0.4.1 |
| 2 | Token 过期 401 被静默吞掉，GUI 报「设备 0 个 / 找不到可用证书」等误导信息 | `http_req` 把 401 吞成 `(None, b"")`，下游 `(None or {}).get(...)` 误判 | 401/403 抛 `CloudAuthError`，GUI 捕获后引导重新登录 | v0.4.2 |
| 3 | TUN/分流代理环境下直连华为云静默失败，GUI 看不到任何真实错误 | `http_req` 失败日志只打 stdout（`--windowed` 下不可见）；无代理配置入口 | GUI 增加「云代理」输入框；失败信息带真实 exception 进 GUI 日志 | v0.4.3 |
| 4 | CI dmg 所有 HTTPS 请求 `CERTIFICATE_VERIFY_FAILED` | **CI runner 构建的 Python 其 OpenSSL 默认 CA 路径指向构建机目录**，冻结后失效 | `_build_opener` 显式加载 certifi / `/etc/ssl/cert.pem`，打包 certifi | v0.4.6 |
| 5 | CI dmg 中 `token-check` 恒报「网络不可用，跳过检查」 | `check_token` 走的 `http_req_status` 是独立实现，未接入 CA 修复 | `http_req_status` 统一走 `_build_opener` | v0.4.7 |
| 6 | v0.4.5~v0.4.7 三个 tag 的 GUI 全部显示 `v0.4.4` | `APP_VERSION` 常量靠手工与 tag 同步，发版时忘改 | 构建时从 tag 自动注入 `version.txt`，运行时读取 | v0.4.8 |
| 7 | 标题栏显示 `vv0.4.8` | workflow 传 `github.ref_name` 带 `v`，脚本剥 `v` 的逻辑没覆盖 env 分支 | 对所有来源统一 `sed 's/^v//'` | v0.4.9 |
| 8 | `check_token`（第一次请求）成功，之后所有请求全部 SSL 失败——直连与代理皆然 | **CA HTTPSHandler 只在首次请求被创建**：`if not _CA_CHECKED` 短路后，后续 `handlers` 为空，`build_opener()` 回落默认（失效）验证 | HTTPSHandler 首次创建后存模块级缓存，每次 opener 复用 | v0.5.0 |
| 9 | 回退候选证书时下载证书报 `self-signed certificate in chain` | `check_cert_usable` 里下载对象存储 URL 用裸 `urlopen`，未走 opener | 全文件裸 `urlopen` 清零，统一 `_build_opener` | v0.5.1 |

其中 #4 → #8 是同一条主线的连环坑：**CI 打包的 Python 环境与开发机不同，
任何「依赖构建机路径/默认环境」的假设在冻结包里都会失效**。

---

## 问题详述

### 1. 工具目录嵌套（v0.4.1）

**现象**：CI dmg 拖入 .app 走云流程，报「设备 0 个」「找不到可用证书」；
本地 dmg 同一文件一切正常。

**根因**：工具收集脚本产出的 `hap-tools.tar.gz` 顶层目录就是 `tools/`，而
workflow 里 `tar -xzf ... -C packaging/tools`，解出 `packaging/tools/tools/`。
PyInstaller `--add-data "$TOOLS:tools"` 打包后运行时按 `tools/store/key.pem`
查找 → 找不到。

**修复**：解压后 `flatten`（把 `tools/tools/*` 上提一层），并加完整性断言：

```bash
test -f packaging/tools/signer && test -f packaging/tools/store/key.pem \
  || { echo "::error title=工具不完整"; exit 1; }
```

**教训**：打包流水线里对「产物完整性」要做显式断言，否则会静默产出坏包，
把问题延迟到用户运行时才爆，且症状与真实原因相距甚远。

### 2. 401 被静默吞掉（v0.4.2）

**现象**：token 过期后，GUI 流程不报「token 过期」，而是报
「设备 0 个」→「申请失败: None」→「找不到可用证书」。

**根因链**：

```
token 过期
→ device/list 401 → http_req 捕获 HTTPError 返回 (None, b"")
→ 调用方 (None or {}).get("list", []) → 空列表 → "设备 0 个"
→ cert/add 401 → (None or {}).get("ret", {}).get("code") = None
                → code in (0, None) 判定"申请成功"   ← 误判!
→ re-list 仍空 → cert=None → "找不到可用证书"        ← 误导
```

**修复**：`http_req` 遇 401/403 直接抛 `CloudAuthError`；GUI `_pipeline`
捕获后显示「Token 已过期，请重新登录」并走凭据清理流程；`cert/add` 成功判定
收紧为 `r is not None and code in (0, None)`。

**教训**：错误传播时**绝不能把鉴权失败降级成空数据**——空数据和请求失败
是两种语义，混在一起会让上游做出完全错误的分支决策。

### 3. 网络失败不可观测（v0.4.3）

**现象**：用户机器开了 Shadowsocks（PAC + 多张 utun），TUN 接管时直连华为云
被劫持断连，GUI 只显示「请求失败: None」；关掉 TUN 又正常，间歇性、无法复现。

**修复**：

- GUI 增加「🌐 云代理」输入框（默认读 `HAP_CLOUD_PROXY` 环境变量），
  请求统一走 `ProxyHandler`；
- `http_req` 失败时把真实 exception（类型+消息）记入模块级 `LAST_NET_ERROR`，
  GUI 失败分支直接展示，并提示可填本机代理重试。

**教训**：GUI 应用（`--windowed`）的 stdout 不可见，任何「只打到控制台」的
错误信息对用户等于不存在。错误必须进入 GUI 的日志面板。

### 4-5. CI 冻结包 SSL 证书验证失败（v0.4.6 / v0.4.7）⭐ 核心问题

**现象**：CI dmg 所有 HTTPS 请求报
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`；
本地 dmg 同一时刻同一环境完全正常。

**根因**：`actions/setup-python` 构建的 Python 3.12，其 OpenSSL 编译期把
CA bundle 路径硬编码为构建机目录（`/Users/runner/hostedtoolcache/...`）。
PyInstaller 冻结分发后该路径在用户机器上不存在 → `ssl` 默认上下文找不到
任何根证书 → 全部验证失败。本地 dmg 用 miniconda Python 3.13 打包，路径
有效，所以「本地正常 CI 必挂」。

**定位手段**（值得记录的对照实验）：PyInstaller app 支持 CLI 透传模式，
直接在用户机器上跑 CI 包的 `--cli cloud devices --token faketoken`，
stdout 打出真实 SSL 错误——一击定位。同样的命令在本地 dmg 上返回 401
（TLS 成功），差异被压缩到「包本身」。

**修复**：

```python
def _build_opener(proxy):
    # certifi 优先, 系统 CA 兜底
    ctx = ssl.create_default_context(cafile=_find_ca_bundle())
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)
```

`build_dmg.sh` 安装并 `--collect-all certifi`，确保 CA bundle 进包。
`check_token` 用的 `http_req_status` 是独立实现，同步接入（v0.4.7）。

**教训**：跨机器分发的冻结 Python 应用，**永远不要依赖 OpenSSL 默认 CA 路径**，
必须显式指定随包分发的 CA bundle（certifi 是标准做法）。

### 6-7. 版本号错位（v0.4.8 / v0.4.9）

**现象**：v0.4.5/0.4.6/0.4.7 三个 tag 的 GUI 全显示 `v0.4.4`；修复后
v0.4.8 又显示 `vv0.4.8`。

**根因**：前者是 `APP_VERSION` 常量手工同步忘改；后者是 workflow 传入的
`github.ref_name`（`v0.4.8`，带 v）绕过了剥前缀的 `sed` 分支。

**修复**：版本号改为构建时自动注入——`build_dmg.sh` 按
`APP_VERSION env → git describe → dev` 三级回退取值（env 与 describe 都剥
`v` 前缀），写入 `version.txt` 随 PyInstaller 打进包；GUI 运行时读
`_MEIPASS/version.txt`，开发态回退源码常量。手工同步步骤彻底删除。

**教训**：任何「发版时记得同步 X」的流程都不可靠，能用构建注入就不要靠人记。

### 8. CA handler 只在首次请求生效（v0.5.0）⭐ 最隐蔽

**现象**：v0.4.9 后 GUI 里 `check_token`（第一次请求）成功，
之后 `device/list` 等全部 SSL 失败，直连与代理皆然；而 CLI 单命令测试
每次都成功。

**根因**：

```python
if not _CA_CHECKED:        # 只有第一次请求进这里
    handlers.append(HTTPSHandler(...))
    _CA_CHECKED = True
return build_opener(*handlers)   # 第二次起 handlers = [] !
```

CA 的 HTTPSHandler 创建被放在「只执行一次」的检测块内，第二次调用返回的
opener **没有任何 CA handler**，回落到失效的默认验证。CLI 每次新进程
（第一次请求）所以永远测不出来——典型的「单发测试掩盖状态 bug」。

**修复**：HTTPSHandler 首次创建后存模块级 `_CA_SSL_HANDLER`，每次构建
opener 时复用。

**教训**：

- 「初始化一次」的全局状态要区分**「检测结果缓存」**（可缓存）与
  **「由结果构造的对象」**（必须复用或重建）；
- 单次请求的 CLI 测试无法覆盖连接复用场景，回归测试要多发几次。

### 9. 裸 urlopen 残留（v0.5.1）

**现象**：device/list 通了（26 台设备），但回退选证书时下载证书又报
`self-signed certificate in chain`。

**根因**：`check_cert_usable` 里 reapply 返回的对象存储下载 URL 用的
裸 `urllib.request.urlopen`，没走统一 opener——CA 修复没有覆盖到。

**修复**：全文件搜索 `urlopen`，全部替换为 `_build_opener(...).open(...)`。

**教训**：修此类问题时以「全局 grep 裸调用」收尾，而不是修一处算一处——
网络入口必须收敛到唯一函数。

---

## 排障方法论沉淀

1. **对照实验压缩变量**：「同机同时刻，本地包成功 CI 包失败」直接把问题域
   收敛到包差异（Python 版本/打包内容），而不是网络环境。
2. **让应用自己说出真实错误**：PyInstaller app 的 CLI 透传模式是神器——
   GUI 看不到的 SSL 错误，`app --cli <cmd>` 一条命令打回原形。
3. **错误信息要能自解释**：早期「设备 0 个/找不到可用证书」浪费了多轮
   排障；把真实 exception 透出到 GUI 日志后，`self-signed certificate`
   一眼定位。报错透明化之后，后续每个问题的定位速度都以分钟计。
4. **全局 grep 收尾**：任何「某一类调用有问题」的修复，最后必须全局搜索
   确认无残留（裸 `urlopen`、未剥前缀的版本号、未走 opener 的下载）。
5. **CI 产物完整性断言**：`test -f signer && test -f key.pem || exit 1`，
   宁可构建失败也不产出坏包。

## 最终架构（v0.5.1）

```
GUI (hap_gui.py)
  └─ _pipeline: check_token → device/list → cert/add → reapply
              → provision/add → 签名(debug 对齐+预检) → hdc install → 启动
云请求统一入口: http_req / http_req_status
  ├─ CloudAuthError (401/403) → GUI 引导重新登录
  ├─ _build_opener: ProxyHandler(云代理输入框) + HTTPSHandler(certifi CA, 模块级复用)
  └─ LAST_NET_ERROR → GUI 失败时展示真实原因
发版: git tag v* → workflow(注入版本号+加密工具包+完整性断言) → DMG 自动挂 Release
```

## 版本与修复对照

| tag | 关键变更 |
|---|---|
| v0.4.1 | 工具目录 flatten + 完整性断言 |
| v0.4.2 | CloudAuthError（401 不再静默） |
| v0.4.3 | 云代理输入框 + 错误透出 GUI |
| v0.4.4 | 版本号展示（标题栏+日志面板） |
| v0.4.6 | CA bundle 显式加载（certifi/系统 CA） |
| v0.4.7 | http_req_status 接入 CA 修复 |
| v0.4.8 | 版本号 tag 自动注入 |
| v0.4.9 | vv 前缀 + 网络错误带真实 exception |
| v0.5.0 | CA HTTPSHandler 模块级复用 |
| v0.5.1 | check_cert_usable 下载走 opener（裸 urlopen 清零） |

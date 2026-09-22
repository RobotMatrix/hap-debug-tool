开发者: 构建与签名 (脚本在源码 packaging/ 目录, 不在本 dmg 内)
===============================================================

构建 dmg:
    ./packaging/build_dmg.sh
    # 可选: APP_SRC=/path/小白调试助手.app PY=/path/python3 ./packaging/build_dmg.sh

签名 + 公证 (需 "Developer ID Application" 证书, Apple Development 证书不能公证):
    # 1) 一次性配置公证凭据
    xcrun notarytool store-credentials hap-notary \
      --apple-id "你的AppleID" --team-id "TEAMID" --password "App专用密码"
    # 2) 签名 + 公证 + 重打 dmg
    ./packaging/sign_notarize.sh --profile hap-notary
    # 也可指定证书: --identity "Developer ID Application: 名字 (TEAMID)"

未签名/未公证时的临时分发:
    对方首次打开被拦 -> 右键点 app -> 打开; 或
    xattr -dr com.apple.quarantine /Applications/HAP调试助手.app

===============================================================

HAP 调试助手
============

鸿蒙 HAP/HSP 一键签名 + 安装 + 启动工具（小白调试助手 的开源重实现）。

安装
----
把 "HAP调试助手.app" 拖到 Applications 文件夹。

首次打开被系统拦截时
--------------------
右键点 app -> 打开；或终端执行：
    xattr -dr com.apple.quarantine /Applications/HAP调试助手.app

使用
----
1. 打开 app，用 USB 连好鸿蒙手机（开启 USB 调试）。
2. 把 .app / .hap 拖到窗口的拖拽区；下方"签名信息"会立即显示是否已签名、
   证书主体/颁发者/有效期、Profile 的 bundle/类型/开发者/授权设备数。
3. 选择证书团队（默认"个人"）。
4. 点"登录获取"完成华为账号登录（自动填入 token），或手动填 oauth2token / uid。
5. 点"一键安装并启动"：自动申请证书/Profile -> 本地签名 -> hdc 安装 -> 启动。

说明
----
- 团队账户为只读，本工具不会删除其证书/Profile；仅对个人账户做增删。
- 证书名默认带日期时间（xiaobai-debug-YYYYMMDDHHMMSS）；个人账户证书配额满时会自动
  复用已有 xiaobai-debug 证书。
- 内置 hdc / signer，无需另装 DevEco。

日志窗口会显示每一步的详细输出，失败时按提示排查。

命令行 (可选)
-------------
app 内已内置命令行组件 hap (同一二进制, 带子命令即走 CLI)。
把它加入 PATH 即可直接使用:

    export PATH="/Applications/HAP调试助手.app/Contents/Resources/bin:$PATH"
    hap info app.hap
    hap sig app.hap        # 查看签名信息 (证书/Profile)
    hap devices
    hap install app.hap --no-sign
    hap cloud flow --bundle-name com.demo.app --cert-name xiaobai-debug \
        --token <oauth2token> --uid <uid> --team-id <个人teamid> -o signed/
    hap cloud --help      # 查看全部云命令
    hap --help            # 查看全部命令

(永久生效可把上面 export 那行加到 ~/.zshrc)

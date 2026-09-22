#!/bin/bash
# 签名 + 公证 HAP调试助手.app, 并重新生成 dmg
#
# 前置:
#   1) 钥匙串里有 "Developer ID Application: <你的名字> (TEAMID)" 证书
#      (Apple Development 证书不能公证分发)
#   2) 配置 notarytool 凭据 (一次性):
#        xcrun notarytool store-credentials hap-notary \
#          --apple-id "你的AppleID" --team-id "TEAMID" --password "App专用密码"
#
# 用法:
#   ./packaging/sign_notarize.sh [--identity "Developer ID Application: xxx (TEAMID)"] [--profile hap-notary]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/dist/HAP调试助手.app"
DMG="$HERE/HAP调试助手.dmg"
ENT="$HERE/entitlements.plist"
PROFILE="hap-notary"
IDENTITY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --identity) IDENTITY="$2"; shift 2 ;;
    --profile)  PROFILE="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

[ -d "$APP" ] || { echo "先运行 build_dmg.sh 生成 $APP"; exit 1; }

if [ -z "$IDENTITY" ]; then
  IDENTITY="$(security find-identity -v -p codesigning | grep -o '"Developer ID Application:[^"]*"' | head -1 | tr -d '"')"
fi
if [ -z "$IDENTITY" ]; then
  echo "!! 未找到 'Developer ID Application' 证书。"
  echo "   当前可用:"; security find-identity -v -p codesigning | sed 's/^/     /'
  echo "   请到 developer.apple.com 创建 Developer ID Application 证书并导入钥匙串。"
  exit 1
fi
echo ">> 签名身份: $IDENTITY"

echo ">> [1/4] 签名内部二进制 (hardened runtime)"
for f in \
  "$APP/Contents/Resources/tools/hdc" \
  "$APP/Contents/Resources/tools/signer" \
  "$APP/Contents/Resources/tools/libusb_shared.dylib" \
  "$APP/Contents/Frameworks/"*.dylib ; do
  [ -e "$f" ] || continue
  codesign --force --options runtime --timestamp --sign "$IDENTITY" "$f"
done

echo ">> [2/4] 签名 .app"
codesign --force --deep --options runtime --timestamp \
  --entitlements "$ENT" --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo ">> [3/4] 重新生成 dmg 并提交公证"
STAGE="$HERE/build/dmg_stage"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp -f "$HERE/README.txt" "$STAGE/使用说明.txt"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
if command -v create-dmg >/dev/null; then
  create-dmg --volname "HAP调试助手" --window-size 640 420 \
    --icon "HAP调试助手.app" 160 200 --icon "使用说明.txt" 320 200 \
    --app-drop-link 480 200 "$DMG" "$STAGE" >/dev/null 2>&1 \
    || hdiutil create -volname "HAP调试助手" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
else
  hdiutil create -volname "HAP调试助手" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
fi
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait

echo ">> [4/4] 装订 (staple)"
xcrun stapler staple "$DMG" || true
xcrun stapler staple "$APP" || true
spctl -a -vvv -t install "$APP" || true
echo ">> 完成: $DMG"

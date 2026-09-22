#!/bin/bash
# 打包 HAP 调试助手为 .dmg
# 用法: ./packaging/build_dmg.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${PY:-/Users/lasysloth/miniconda3/bin/python3}"
APP_SRC="${APP_SRC:-/Applications/小白调试助手.app}"
ASSETS="$APP_SRC/Contents/Frameworks/App.framework/Versions/A/Resources/flutter_assets/assets"
TOOLS="$HERE/tools"

if [ -x "$TOOLS/hdc" ] && [ -f "$TOOLS/signer" ]; then
  echo ">> [1/4] 已存在自带工具, 跳过收集 ($TOOLS)"
else
  echo ">> [1/4] 从 $APP_SRC 收集依赖工具到 $TOOLS"
  mkdir -p "$TOOLS/store" "$TOOLS/ohos"
  for f in hdc signer libusb_shared.dylib; do
    cp -f "$ASSETS/macos/$f" "$TOOLS/" 2>/dev/null || echo "   (缺 $f)"
  done
  cp -f "$ASSETS/store/key.pem"    "$TOOLS/store/" 2>/dev/null || true
  cp -f "$ASSETS/store/xiaobai.csr" "$TOOLS/store/" 2>/dev/null || true
  cp -f "$ASSETS/ohos/auto_installer.hap" "$TOOLS/ohos/" 2>/dev/null || true
  chmod +x "$TOOLS/hdc" "$TOOLS/signer" 2>/dev/null || true
fi

echo ">> [2/4] 安装 PyInstaller"
"$PY" -m pip install -q pyinstaller 2>&1 | tail -1

echo ">> [3/4] PyInstaller 构建 .app"
cd "$HERE"
"$PY" "$HERE/make_icon.py" >/dev/null 2>&1 || echo "   (图标生成跳过)"
ICON_ARG=""
[ -f "$HERE/build/AppIcon.icns" ] && ICON_ARG="--icon=$HERE/build/AppIcon.icns"
rm -rf dist "HAP调试助手.spec"
"$PY" -m PyInstaller --noconfirm --clean --windowed \
  --name "HAP调试助手" \
  --osx-bundle-identifier com.xiaobai.hapinstaller \
  --add-data "$TOOLS:tools" \
  --collect-all tkinterdnd2 \
  $ICON_ARG \
  --paths "$ROOT" \
  "$ROOT/hap_gui.py" 2>&1 | tail -5

APP="dist/HAP调试助手.app"
[ -d "$APP" ] || { echo "构建失败"; exit 1; }
chmod +x "$APP/Contents/MacOS/"* 2>/dev/null || true
mkdir -p "$APP/Contents/Resources/bin"
ln -sf "../../MacOS/HAP调试助手" "$APP/Contents/Resources/bin/hap"

echo ">> [4/4] 生成 dmg"
DMG="$HERE/HAP调试助手.dmg"
STAGE="$HERE/build/dmg_stage"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp -f "$HERE/README.txt" "$STAGE/使用说明.txt"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
if command -v create-dmg >/dev/null; then
  create-dmg --volname "HAP调试助手" --window-size 640 420 \
    --icon "HAP调试助手.app" 160 200 --icon "使用说明.txt" 320 200 \
    --app-drop-link 480 200 \
    "$DMG" "$STAGE" 2>&1 | tail -3 || hdiutil create -volname "HAP调试助手" \
    -srcfolder "$STAGE" -ov -format UDZO "$DMG"
else
  hdiutil create -volname "HAP调试助手" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
fi
echo ">> 完成: $DMG"
ls -la "$DMG"

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

# 投屏视频流 uitest agent (按设备 uitest 版本选配): 仓库 tools/agent/ → 打包
mkdir -p "$TOOLS/agent"
cp -f "$ROOT/tools/agent/"*.so "$TOOLS/agent/" 2>/dev/null \
  || echo "   (缺视频流 agent .so, 投屏回退 snapshot 轮询)"

# 投屏 scrcpy 引擎 server (原生分辨率 H.264): 仓库 tools/scrcpy/ → 打包
mkdir -p "$TOOLS/scrcpy"
cp -f "$ROOT/tools/scrcpy/"*.so "$TOOLS/scrcpy/" 2>/dev/null \
  || echo "   (缺 scrcpy server .so, 超清档回退 JPEG 引擎)"

echo ">> [2/4] 安装 PyInstaller"
"$PY" -m pip install -q pyinstaller certifi av grpcio 2>&1 | tail -1
# 探针: CI 上 collect_all('av') 是否解析到 PyAV (曾出现 CI 产物静默缺 av/grpc)
"$PY" - <<'EOF' 2>&1 | tail -4
import av
from PyInstaller.utils.hooks import collect_all
d, b, h = collect_all("av")
print(f"[probe] av {av.__version__} at {av.__file__}")
print(f"[probe] collect_all('av') -> datas={len(d)} binaries={len(b)} hooks={len(h)}")
d2, b2, h2 = collect_all("grpc")
print(f"[probe] collect_all('grpc') -> datas={len(d2)} binaries={len(b2)} hooks={len(h2)}")
EOF

# 版本号注入: APP_VERSION env (workflow 传 tag) > git describe > 回退
if [ -z "${APP_VERSION:-}" ]; then
  APP_VERSION="$(git -C "$ROOT" describe --tags --match 'v[0-9]*' 2>/dev/null | sed 's/^v//' | cut -d- -f1 || true)"
fi
APP_VERSION="${APP_VERSION#v}"
APP_VERSION="${APP_VERSION:-dev}"
echo "$APP_VERSION" > "$HERE/version.txt"
echo ">> 版本号: $APP_VERSION"

echo ">> [3/4] PyInstaller 构建 .app"
cd "$HERE"
"$PY" "$HERE/make_icon.py" >/dev/null 2>&1 || echo "   (图标生成跳过)"
ICON_ARG=""
[ -f "$HERE/build/AppIcon.icns" ] && ICON_ARG="--icon=$HERE/build/AppIcon.icns"
rm -rf dist "HAP调试助手.spec"
mkdir -p "$HERE/build"
"$PY" -m PyInstaller --noconfirm --clean --windowed \
  --name "HAP调试助手" \
  --osx-bundle-identifier com.xiaobai.hapinstaller \
  --add-data "$TOOLS:tools" \
  --add-data "$HERE/assets:assets" \
  --add-data "$HERE/version.txt:." \
  --collect-all tkinterdnd2 \
  --collect-all certifi \
  --collect-all av \
  --collect-all grpc \
  $ICON_ARG \
  --paths "$ROOT" \
  "$ROOT/hap_gui.py" > "$HERE/build/pyinstaller_full.log" 2>&1
RC=$?
tail -40 "$HERE/build/pyinstaller_full.log"
[ "$RC" -eq 0 ] || { echo "!! PyInstaller 失败 (rc=$RC), 见 pyinstaller_full.log"; exit 1; }
# 产物体检: 关键组件缺一不可 (scrcpy 投屏依赖)
FW="dist/HAP调试助手.app/Contents/Frameworks"
for c in av grpc PIL tkinterdnd2 tools/agent tools/scrcpy tools/hdc; do
  [ -e "$FW/$c" ] || { echo "!! 打包缺组件: $c"; exit 1; }
done
echo ">> 产物体检通过 (av/grpc/agent/scrcpy/hdc 齐全)"

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

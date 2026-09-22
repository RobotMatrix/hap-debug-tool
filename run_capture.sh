#!/usr/bin/env bash
#
# run_capture.sh -- 一键抓取 "小白调试助手" 对华为云的真实请求
#
#   ./run_capture.sh           启动(信任CA -> 起代理 -> 启动app), Ctrl-C 结束
#   ./run_capture.sh --clean   移除 CA 信任
#   ./run_capture.sh --port N  指定代理端口 (默认 8080)
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SNIFF="$HERE/hap_sniff.py"
CA_DIR="$HOME/.hap_sniff"
PORT=8080
CLEAN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --clean) CLEAN=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }
[ -f "$SNIFF" ] || { echo "找不到 $SNIFF"; exit 1; }

if [ "$CLEAN" = "1" ]; then
  echo ">> 移除 CA 信任..."
  sudo HAP_CA_DIR="$CA_DIR" python3 "$SNIFF" --uninstall-ca --system
  echo ">> 完成。"
  exit 0
fi

PROXY_PID=""
cleanup() {
  echo ""
  echo ">> 停止代理..."
  [ -n "$PROXY_PID" ] && kill "$PROXY_PID" 2>/dev/null
  echo ">> 抓包文件在: $CA_DIR/"
  echo ">> 移除 CA 信任: $0 --clean"
}
trap cleanup EXIT INT TERM

pkill -f hap_sniff.py 2>/dev/null
sleep 1

echo ">> [1/3] 生成 CA 并信任到系统钥匙串 (需要管理员密码)..."
python3 "$SNIFF" --install-ca >/dev/null 2>&1
sudo HAP_CA_DIR="$CA_DIR" python3 "$SNIFF" --install-ca --system || { echo "CA 信任失败"; exit 1; }

echo ">> [2/3] 启动代理 :$PORT ..."
python3 "$SNIFF" --port "$PORT" --all >"$CA_DIR/proxy.log" 2>&1 &
PROXY_PID=$!
sleep 2
if ! kill -0 "$PROXY_PID" 2>/dev/null; then
  echo "代理启动失败，日志:"; cat "$CA_DIR/proxy.log"; exit 1
fi

echo ">> [3/3] 用代理启动 app ..."
python3 "$SNIFF" --launch

echo ""
echo "==================================================================="
echo " 现在在 app 里操作: 登录 / 打开'证书'页 / '设备'页"
echo " 请求实时写入: $CA_DIR/capture-*.jsonl"
echo " 按 Ctrl-C 结束抓包"
echo "==================================================================="
echo ""

tail -f "$CA_DIR/proxy.log"

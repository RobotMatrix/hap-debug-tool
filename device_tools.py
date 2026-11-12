#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device_tools.py -- 设备投屏/日志抓取 (鸿蒙 hdc + iOS devicectl/idevicesyslog, 纯标准库)

能力矩阵 (全部基于 macOS 系统工具链 + 本仓库内置 hdc, 零第三方 Python 依赖):
  投屏:     鸿蒙 snapshot_display + file recv 轮询 (~0.4s/帧, JPEG, sips 转 PNG);
            iOS devicectl capture screenshot 轮询 (~2s/帧, PNG 原生)。
            鸿蒙支持 uitest uiInput 点击/滑动/按键回传; iOS 系统无输入注入, 只读。
  运行日志: 鸿蒙 hdc shell hilog 流式; iOS idevicesyslog 流式 (brew libimobiledevice)。
  崩溃日志: 鸿蒙 hidumper -s HiviewFaultLogger 列表 + hdc file recv 逐个拉取
            (/data/log shell 层权限拒绝, 但 file recv 走 faultloggerd 通道可直拉);
            iOS devicectl copy from --domain-type systemCrashLogs 整包拉取 (.ips)。
"""

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SHOT_REMOTE = "/data/local/tmp/hapdbg_mirror.jpeg"

_BREW_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _find_tool(name):
    """shutil.which + brew 常见目录兜底 (Finder/dmg 启动的 GUI PATH 不含 homebrew)。"""
    p = shutil.which(name)
    if p:
        return p
    for d in _BREW_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _resolve_exe(cmd):
    """posix_spawn 需要 absolute path (不查 PATH)。"""
    c0 = cmd[0]
    if os.path.sep in c0:
        return c0
    return shutil.which(c0) or c0


def _run(cmd, timeout=30):
    """子进程执行 (posix_spawn, 规避 macOS Tk 主循环并存时 fork-exec 死锁)。
    输出不捕获; 需要输出用 _run_capture。超时 killpg。"""
    import signal
    argv = [str(c) for c in cmd]
    try:
        pid = os.posix_spawn(_resolve_exe(argv), argv, os.environ, setsid=True)
    except (FileNotFoundError, ProcessLookupError):
        return 127, "", f"command not found: {argv[0]}"
    except Exception as e:
        return 126, "", f"spawn 失败: {e}"
    return _wait_spawned(pid, argv, timeout)


def _wait_spawned(pid, cmd, timeout):
    """posix_spawn 后用 waitpid 轮询等待 (无管道捕获, 输出丢弃 —— 截屏/输入类命令
    只关心 rc; 需要输出的调用走 _run_capture)。超时 killpg。"""
    import signal
    deadline = time.time() + timeout
    while True:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                rc = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
                    else (os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1)
                return rc, "", ""
        except ChildProcessError:
            return -1, "", "进程已退出 (waitpid 失败)"
        if time.time() > deadline:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
            try:
                os.waitpid(pid, 0)
            except Exception:
                pass
            return 124, "", "timeout"
        time.sleep(0.02)


def _run_capture(cmd, timeout=30):
    """需要 stdout/stderr 的子进程执行。posix_spawn + 非阻塞管道读 + 超时 killpg
    (不依赖 communicate, 规避 Tk/fork 死锁路径)。"""
    import signal
    import select as _select
    argv = [str(c) for c in cmd]
    pr, pw = os.pipe()
    pe, ew = os.pipe()
    try:
        pid = os.posix_spawn(
            _resolve_exe(argv), argv, os.environ,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, pw, 1),
                (os.POSIX_SPAWN_DUP2, ew, 2),
            ])
    except (FileNotFoundError, ProcessLookupError):
        os.close(pr); os.close(pw); os.close(pe); os.close(ew)
        return 127, "", f"command not found: {cmd[0]}"
    os.close(pw); os.close(ew)
    deadline = time.time() + timeout
    bufs = {pr: bytearray(), pe: bytearray()}
    open_fds = {pr, pe}
    rc = None
    while True:
        if open_fds:
            r, _, _ = _select.select(list(open_fds), [], [], 0.05)
            for fd in r:
                chunk = os.read(fd, 65536)
                if not chunk:
                    open_fds.discard(fd)
                    os.close(fd)
                else:
                    bufs[fd] += chunk
        else:
            time.sleep(0.02)
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                rc = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
                    else (os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1)
                if not open_fds:
                    break
        except ChildProcessError:
            rc = -1
            break
        if time.time() > deadline:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
            rc = 124
            break
    for fd in list(open_fds):
        try:
            os.close(fd)
        except Exception:
            pass
    return (rc if rc is not None else -1), bufs[pr].decode(errors="replace"), \
        bufs[pe].decode(errors="replace")


# ---------------------------------------------------------------- harmony (hdc)

class HarmonyScreen:
    """鸿蒙截屏轮询: snapshot_display -> file recv -> sips 转 PNG (tkinter 只认 PNG/GIF)。"""

    def __init__(self, hdc_bin, connect_key=None):
        self.bin = hdc_bin
        self.key = connect_key
        self._tmp = Path(tempfile.mkdtemp(prefix="hapdbg_mirror_"))

    def _base(self):
        return [self.bin] + (["-t", self.key] if self.key else [])

    def grab(self, png_path):
        """抓一帧 -> png_path (PNG)。返回 (ok, 耗时ms, 错误信息)。"""
        t0 = time.time()
        jpeg = self._tmp / "frame.jpeg"
        _run(self._base() + ["shell", "snapshot_display", "-f", SHOT_REMOTE], timeout=8)
        r2, _, e2 = _run_capture(self._base() + ["file", "recv", SHOT_REMOTE, str(jpeg)], timeout=8)
        if r2 != 0 or not jpeg.exists() or jpeg.stat().st_size == 0:
            return False, 0, f"recv 失败: {e2.strip() or '空文件'}"
        r3, _, e3 = _run(["sips", "-s", "format", "png", str(jpeg), "--out", str(png_path)], timeout=8)
        if r3 != 0:
            return False, 0, f"sips 转换失败: {e3.strip()}"
        ms = int((time.time() - t0) * 1000)
        return True, ms, ""

    def cleanup(self):
        shutil.rmtree(self._tmp, ignore_errors=True)
        _run(self._base() + ["shell", "rm", "-f", SHOT_REMOTE], timeout=10)


def _run_out(rc, err):
    return err or ("" if rc == 0 else "fail")


def _ver_cmp(a, b):
    """数值化版本比较 "5.0.1.2" vs "6.0.2.1" → -1/0/1 (按点分段比)。"""
    pa = [int(x) for x in a.split(".")]
    pb = [int(x) for x in b.split(".")]
    for x, y in zip(pa, pb):
        if x != y:
            return -1 if x < y else 1
    return 0


def _kill_uitest_daemons(hdc_bin, connect_key=None):
    """杀掉设备上全部 uitest daemon。注意: 设备上 pgrep 匹配不到 uitest 进程、
    killall 报 Permission denied、无 awk — 必须解析 ps 输出按 PID 逐个 kill。"""
    base = [hdc_bin] + (["-t", connect_key] if connect_key else [])
    rc, out, _ = _run_capture(base + ["shell", "ps -ef | grep uitest | grep -v grep"],
                              timeout=15)
    for line in (out or "").strip().splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit():
            _run(base + ["shell", f"kill -9 {parts[1]}"], timeout=8)


def _pb_rd_varint(b, i):
    v, s = 0, 0
    while True:
        x = b[i]; i += 1
        v |= (x & 0x7F) << s
        if not x & 0x80:
            return v, i
        s += 7


def _pb_parse_stream_msg(b):
    """解析 Scrcpy$ReplyMessage {data=1 bytes, reply_type=2 int,
    payload=3 map<string,ParamValue>}; 只取 payload 里的 data/flags/len/pts。"""
    i, out = 0, {}
    while i < len(b):
        tag, i = _pb_rd_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = _pb_rd_varint(b, i)
            chunk = b[i:i + ln]; i += ln
            if fn == 3:                      # map entry {key=1 string, value=2}
                j, k, v = 0, None, None
                while j < len(chunk):
                    t2, j = _pb_rd_varint(chunk, j)
                    f2, w2 = t2 >> 3, t2 & 7
                    if w2 == 2:
                        l2, j = _pb_rd_varint(chunk, j)
                        c2 = chunk[j:j + l2]; j += l2
                        if f2 == 1:
                            k = c2.decode(errors="replace")
                        elif f2 == 2 and k:   # ParamValue oneof
                            t3, j3 = _pb_rd_varint(c2, 0)
                            f3, w3 = t3 >> 3, t3 & 7
                            if w3 == 2:
                                l3, j3 = _pb_rd_varint(c2, j3)
                                if f3 == 5:               # val_bytes
                                    v = c2[j3:j3 + l3]
                            elif w3 == 0:
                                v3, j3 = _pb_rd_varint(c2, j3)
                                if f3 == 1:               # val_int
                                    v = v3
                            out[k] = v
                    elif w2 == 0:
                        _, j = _pb_rd_varint(chunk, j)
        elif wt == 0:
            _, i = _pb_rd_varint(b, i)
    return out


class HarmonyScreenStream:
    """鸿蒙准视频流 (并发双循环): 两个独立 hdc shell 会话各自循环 snapshot 到
    独立文件 (SurfaceFlinger 快照可并行), host 高频轮询 recv 两缓冲 + md5 去重。
    实测 Mate 60 动态画面 5.1 fps (旧串行 0.2 fps 的 ~25 倍, 单循环流式 1.7 的 3 倍);
    输入回传 (uitest) 独立会话, 点击时画面不冻结。
    三循环实测 4.9fps 收益递减且加重设备负载, 双循环即 shell 通道天花板。
    snapshot_display ~300ms 是设备端 SurfaceFlinger 快照固定成本, 真 H.264
    视频流需设备端 agent (如 HoKit 推服务 hap)。"""

    REMOTE_FMT = "/data/local/tmp/hapdbg_stream_{i}.jpeg"
    N_STREAMS = 2

    def __init__(self, hdc_bin, connect_key=None):
        self.bin = hdc_bin
        self.key = connect_key
        self.procs = []           # 设备端并发抓帧 shell 进程
        self._last_md5 = None
        self._new_frames = 0
        self._tmp = Path(tempfile.mkdtemp(prefix="hapdbg_stream_"))
        self.jpeg_path = self._tmp / "frame.jpeg"
        self.png_path = self._tmp / "frame.png"

    def _base(self):
        return [self.bin] + (["-t", self.key] if self.key else [])

    def start(self):
        """启动 N_STREAMS 个并发设备端抓帧循环 (各自独立 hdc shell 会话)。"""
        if self.procs and any(p.poll() is None for p in self.procs):
            return
        for i in range(self.N_STREAMS):
            remote = self.REMOTE_FMT.format(i=i)
            script = (f"while true; do snapshot_display -f {remote} >/dev/null 2>&1; done")
            p = subprocess.Popen(self._base() + ["shell", script],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            self.procs.append(p)

    def _recv_buf(self, i):
        """recv 单个缓冲 (线程池并行用)。返回 (local, md5) 或 (None, "")。"""
        local = self._tmp / f"buf_{i}.jpeg"
        rc, _, _e = _run_capture(self._base() + ["file", "recv",
                                  self.REMOTE_FMT.format(i=i), str(local)], timeout=8)
        if rc != 0 or not local.exists() or local.stat().st_size == 0:
            # snapshot 正在覆写的瞬间 recv 会拿到空/半文件, 属正常竞态, 静默跳过
            return None, ""
        return local, hashlib.md5(local.read_bytes()).hexdigest()

    def grab(self, out_png=None):
        """并行 recv 两缓冲 (各一线程, 44ms 一轮而非串行 90ms), md5 去重后转 PNG。
        返回 (new_frame, ms, err)。"""
        t0 = time.time()
        is_new = False
        new_local = None
        results = [None] * self.N_STREAMS
        threads = []
        for i in range(self.N_STREAMS):
            th = threading.Thread(target=lambda idx=i: results.__setitem__(
                idx, self._recv_buf(idx)), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        for local, md5 in results:
            if not local:
                continue
            if md5 != self._last_md5:
                is_new = True
                new_local = local
                self._last_md5 = md5
        if is_new:
            self._new_frames += 1
            if out_png and new_local:
                shutil.copy2(new_local, self.jpeg_path)
                rc2, _, e2 = _run_capture(["sips", "-s", "format", "png",
                                           str(self.jpeg_path), "--out", str(out_png)],
                                          timeout=10)
                if rc2 != 0:
                    return False, 0, f"sips 失败: {(e2 or '').strip()}"
        return is_new, int((time.time() - t0) * 1000), ""

    def stop(self):
        """停止并发循环 + 清理远端文件。"""
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    p.kill()
        self.procs = []
        remotes = " ".join(self.REMOTE_FMT.format(i=i) for i in range(self.N_STREAMS))
        _run(self._base() + ["shell", "rm", "-f", remotes], timeout=10)

    def cleanup(self):
        self.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)


class HarmonyVideoStream:
    """鸿蒙真视频流 (Hypium agent 协议, DevEco screen-mirroring 同款):
      1. 查设备 uitest 版本 → 选配套 uitest_agent*.so → push 到
         /data/local/tmp/agent.so (映射反编译自 hosScrcpy 插件:
         ≤5.1.1.2→1.1.3, ==5.1.1.3→1.1.5, ≤6.0.2.1→1.1.12, 更新→1.2.3;
         不匹配会被 daemon EXTENSION_API_CHECK 拒载, RPC 直接被 reset)
         agent 优先从本机 DevEco 插件 jar 提取, 回退仓库 tools/agent/ 内置
      2. shell: uitest start-daemon singleness --extension-name agent.so 加载
         (agent 以 shell uid 调 DisplayManager::GetScreenshot → JPEG, 无需
          CAPTURE_SCREEN 系统权限; 注意 killall 对它无效, 须 pgrep+kill -9)
      3. hdc fport tcp:<local> tcp:8012 (agent RPC 端口)
      4. RPC: HEAD + 4B sessionId + 4B bodyLen + JSON body + TAIL
         {module: hypiumApiHelper, method: Captures,
          params: {api: startCaptureScreen, args: {options: {scale}}}}
      5. 服务端先回 {"result":true} ack, 之后画面变化时推 JPEG 帧 (socket
         直读 SOI/EOI 切帧); 静止时静默 (recv 超时≠故障, 查 daemon 存活定性)。
      输入注入走同一 socket 的 Gestures RPC (touchDown/Move/Up) — uitest CLI
      会话会把录屏会话顶掉, 故仅按键走 CLI 且完成后同 socket 重发
      startCaptureScreen 自愈。
    实测 Mate 60 (uitest 5.0.1.2): 通知栏动画 7.9fps, 静止 4.9fps (状态栏
    跳变帧), 断线重连 2.8s; scale 0.5 时帧 ~22KB。"""

    HEAD = b"_uitestkit_rpc_message_head_"
    TAIL = b"_uitestkit_rpc_message_tail_"
    DEVICE_AGENT = "/data/local/tmp/agent.so"
    DEVICE_PORT = 8012
    MODULE = "com.ohos.devicetest.hypiumApiHelper"
    JPEG_SOI = b"\xff\xd8\xff"
    JPEG_EOI = b"\xff\xd9"
    POLL_HINT_MS = 40          # GUI 轮询间隔 (帧持续到达, 无需像 snapshot 那样等)
    RESTART_COOLDOWN = 5       # 重连冷却 (秒), 防息屏期间连环重启轰炸设备
    # 清晰度档位 (实测宽度按设备端枚举吸附: 0.5→608 / 0.75→928 / 0.9→1120;
    # scale=1.0 反而回落 1056 且不稳定, 不设该档)
    SCALE_OPTIONS = (("超清", 0.9), ("高清", 0.75), ("流畅", 0.5))
    # (uitest 版本上限, agent 文件名); 顺序敏感, 依次匹配; None = 兜底最新
    AGENT_RULES = (("5.1.1.2", "uitest_agent_1.1.3.so"),
                   ("5.1.1.3", "uitest_agent_1.1.5.so"),
                   ("6.0.2.1", "uitest_agent_1.1.12.so"),
                   (None, "uitest_agent_1.2.3.so"))

    def __init__(self, hdc_bin, connect_key=None, scale=0.9, screen_wh=(1216, 2688)):
        self.bin = hdc_bin
        self.connect_key = connect_key   # 不叫 self.key: 会让 key() 输入方法失效
        self.scale = scale
        self.w, self.h = screen_wh
        self.sock = None
        self.fport_port = None
        self._session = 1
        self._buf = b""
        self._proc = None
        self._proc_log = None
        self._tmp = Path(tempfile.mkdtemp(prefix="hapdbg_video_"))
        self._last_md5 = None
        self._last_ok = 0.0
        self._restart_at = 0.0
        self._restart_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._last_wh = None      # 最近一帧 (宽, 高), 清晰度切换时用于跳旧档帧
        self._skip_wh = None      # 非 None = 正在等新清晰度首帧, 丢弃此尺寸旧帧

    def _base(self):
        return [self.bin] + (["-t", self.connect_key] if self.connect_key else [])

    # ---- agent 版本匹配 + 定位 ----
    @classmethod
    def _pick_agent_name(cls, uitest_ver):
        """按设备 uitest 版本选 agent (DevEco hosScrcpy 同款映射)。"""
        if not uitest_ver:
            return cls.AGENT_RULES[-2][1]     # 版本未知 → 中间档 (适配面最宽)
        for cap, name in cls.AGENT_RULES:
            if cap is None or _ver_cmp(uitest_ver, cap) <= 0:
                return name
        return cls.AGENT_RULES[-1][1]

    def _uitest_version(self):
        rc, out, _ = _run_capture(self._base() + ["shell",
                                                  "/system/bin/uitest --version"],
                                  timeout=15)
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", (out or "").strip().splitlines()[-1]
                      if (out or "").strip() else "")
        return m.group(0) if m else None

    @staticmethod
    def _find_agent_so(name):
        """按名字找 agent: DevEco 插件 jar 提取 → 仓库 tools/agent/。"""
        import zipfile
        jars = []
        for pat in ("DevEco-Studio*/Contents/plugins/screen-mirroring/lib/"
                    "hosScrcpy-*.jar",
                    "DevEco Studio*/Contents/plugins/*/lib/hosScrcpy-*.jar"):
            jars += list(Path("/Applications").glob(pat))
        cache = Path.home() / "Library/Caches/hap_installer/agent"
        for jar in sorted(set(jars), reverse=True):
            try:
                with zipfile.ZipFile(jar) as z:
                    if name in z.namelist():
                        cache.mkdir(parents=True, exist_ok=True)
                        z.extract(name, cache)
                        return cache / name
            except Exception:
                continue
        # 仓库内置 (含 PyInstaller 打包 _MEIPASS)
        for base in (Path(getattr(sys, "_MEIPASS", "") or ".") / "tools" / "agent",
                     Path(__file__).resolve().parent / "tools" / "agent"):
            p = base / name
            if p.exists():
                return p
        return None

    def start(self):
        """推送 agent → 起 daemon → fport → 建 RPC socket。
        daemon 用分离进程拉起 (前台驻留型, 若走 _run_capture 超时 killpg 会误杀);
        旧实例清理用 pgrep+kill -9 (killall 对 uitest 会 Permission denied);
        bind 失败 (垂死实例占 socket) 时轮询等待释放。"""
        ver = self._uitest_version()
        name = self._pick_agent_name(ver)
        agent = self._find_agent_so(name)
        if not agent:
            raise RuntimeError(
                f"设备 uitest {ver or '?'} 需 {name}, 本机 DevEco 与仓库 tools/agent/"
                " 均未找到 (装 DevEco Studio 或把该 .so 放入 tools/agent/)")
        rc, _, err = _run_capture(self._base() + ["file", "send", str(agent),
                                                  self.DEVICE_AGENT], timeout=60)
        if rc != 0:
            raise RuntimeError(f"推送 agent 失败: {(err or '').strip()}")
        _kill_uitest_daemons(self.bin, self.connect_key)
        time.sleep(1)
        log_path = self._tmp / "daemon.log"
        self._proc_log = open(log_path, "wb")
        self._proc = subprocess.Popen(
            self._base() + ["shell", "/system/bin/uitest start-daemon singleness "
                            "--extension-name agent.so"],
            stdout=self._proc_log, stderr=subprocess.STDOUT,
            start_new_session=True)
        for _ in range(12):          # 等 daemon bind (垂死实例占 socket 时秒级释放)
            if self._daemon_alive():
                break
            time.sleep(0.5)
        else:
            tail = log_path.read_text(errors="replace").strip()[-300:] \
                if log_path.exists() else ""
            raise RuntimeError(f"uitest daemon 未起来: {tail or '无输出'}")
        self._alloc_fport()
        self._connect()

    def _connect(self):
        """建 RPC 会话并发起录屏。daemon 是先 bind 8012 再 dlopen agent, 连早了
        会被 RST — 发起后等 {"result":true} ack, 没等到就重试。"""
        last_err = None
        for _ in range(4):
            try:
                self.sock = socket.create_connection(
                    ("127.0.0.1", self.fport_port), timeout=6)
                self.sock.settimeout(4)
                self._buf = b""
                self._send({"module": self.MODULE, "method": "Captures",
                            "params": {"api": "startCaptureScreen",
                                       "args": {"options": {"scale": self.scale}}}})
                # 等 ack (json + 换行, 之后帧流开始); RST/超时 = daemon 还没就绪
                ack = b""
                while b"\n" not in ack:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("agent 关闭连接 (未就绪或版本不符)")
                    ack += chunk
                if b'"result":true' not in ack:
                    # 重试竞态下第二次发起会撞 "Capture already running" — 捕获
                    # 已在跑正是目标状态, 视为成功
                    if b'already running' not in ack:
                        raise ConnectionError(f"ack 异常: {ack[:120]!r}")
                self._buf = ack[ack.index(b"\n") + 1:]
                self.sock.settimeout(10)
                self._last_md5 = None
                self._skip_wh = None
                return
            except (OSError, ConnectionError) as e:
                last_err = e
                try:
                    if self.sock:
                        self.sock.close()
                except Exception:
                    pass
                self.sock = None
                time.sleep(1)
        raise RuntimeError(f"RPC 会话建立失败: {last_err}")

    def _send(self, req):
        """封帧发送 (grab 线程并发 recv 不冲突, 发送侧加锁串行)。"""
        body = json.dumps(req).encode()
        with self._send_lock:
            self.sock.sendall(self.HEAD + struct.pack(">ii", self._session, len(body))
                              + body + self.TAIL)
            self._session += 1

    # 输入注入已统一切 uinput (HarmonyInput, 内核级), 不再用 Gestures RPC —
    # uitest CLI/Gestures 与录屏会话互相挤占, uinput 完全独立无此问题。

    def set_scale(self, scale):
        """清晰度切换: 同 socket 先 stop 再按新 scale 重发 (无需动 daemon)。
        stop 生效有 ~1s 延迟, 间隔不足时新 start 撞 already running 被丢弃;
        切换瞬间内核缓冲里排队的旧档帧由 grab 按 JPEG 尺寸跳过。"""
        if not self.sock or scale == self.scale:
            return
        try:
            self._send({"module": self.MODULE, "method": "Captures",
                        "params": {"api": "stopCaptureScreen", "args": {}}})
            time.sleep(1.2)
            self._skip_wh = self._last_wh   # 丢弃这个尺寸的旧档排队帧
            self.scale = scale
            self._send({"module": self.MODULE, "method": "Captures",
                        "params": {"api": "startCaptureScreen",
                                   "args": {"options": {"scale": scale}}}})
            self._last_md5 = None
        except OSError:
            pass

    @staticmethod
    def _jpeg_wh(frame):
        """解析 JPEG SOF 段取 (宽, 高) — 清晰度切换时识别旧档残留帧。"""
        i = 2
        while i < len(frame) - 9:
            if frame[i] != 0xFF:
                return None
            m = frame[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h = int.from_bytes(frame[i + 5:i + 7], "big")
                w = int.from_bytes(frame[i + 7:i + 9], "big")
                return w, h
            i += 2 + int.from_bytes(frame[i + 2:i + 4], "big")
        return None

    def grab(self, out_path=None):
        """收一帧 JPEG (socket 直读, SOI/EOI 切帧)。out_path=JPG 保存路径。
        返回 (new_frame, ms, err)。画面变化才推帧 (状态栏秒级跳变也会推);
        静止/静默不算错误, 返回 (False, ms, "") 与轮询通道"无新帧"语义一致。"""
        t0 = time.time()
        if self.sock is None:
            return False, 0, "未连接"
        deadline = time.time() + 6
        dups = 0
        while time.time() < deadline:
            i = self._buf.find(self.JPEG_SOI)
            j = self._buf.find(self.JPEG_EOI, i + 2) if i >= 0 else -1
            if i >= 0 and j >= 0:
                frame = self._buf[i:j + 2]
                self._buf = self._buf[j + 2:]
                wh = self._jpeg_wh(frame)
                if self._skip_wh and wh and wh == self._skip_wh:
                    continue          # 切档前排队在内核缓冲里的旧分辨率帧
                self._skip_wh = None
                self._last_wh = wh
                md5 = hashlib.md5(frame).hexdigest()
                if md5 == self._last_md5:
                    dups += 1
                    continue  # 重复帧, 继续收
                self._last_md5 = md5
                self._last_ok = time.time()
                if out_path:
                    Path(out_path).write_bytes(frame)
                return True, int((time.time() - t0) * 1000), ""
            try:
                chunk = self.sock.recv(1048576)
            except socket.timeout:
                # 静止画面 agent 静默不推帧: daemon 还活着就不算故障, 不触发重连
                if self._daemon_alive():
                    return False, int((time.time() - t0) * 1000), ""
                return False, 0, "recv 超时且 daemon 已退出"
            except (ConnectionResetError, BrokenPipeError, OSError):
                return False, 0, "连接断开"
            if not chunk:
                return False, 0, "连接关闭"
            self._buf += chunk
        if dups:
            return False, int((time.time() - t0) * 1000), ""
        # 完全静止时 agent 静默 (无重复帧也无新帧): daemon 活着就不算故障
        if self._daemon_alive():
            return False, int((time.time() - t0) * 1000), ""
        return False, 0, "6s 无完整帧且 daemon 已退出"

    def _daemon_alive(self):
        rc, out, _ = _run_capture(self._base() + ["shell",
            "ps -ef | grep 'singleness' | grep -v grep"], timeout=15)
        return bool((out or "").strip())

    def restart_session(self):
        """断线重连: agent 会话随连接断开而终止 (Auto stop copyScreen since
        session dead), 且 daemon 空闲 3 分钟自杀。整链重启 (daemon 活着时
        杀掉重来最可靠), fport 规则一并重建。冷却 + 近期有成功帧即跳过,
        防止息屏期间连环重启轰炸设备。"""
        with self._restart_lock:
            wait = self._restart_at - time.time()
            if wait > 0:
                time.sleep(wait)
            if self.sock is not None and time.time() - self._last_ok < 15:
                return  # 另一次重连已成功, 无需再来
            self._restart_at = time.time() + self.RESTART_COOLDOWN
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            self.sock = None
            if self.fport_port:
                _run(self._base() + ["fport", "rm", f"tcp:{self.fport_port}",
                                 f"tcp:{self.DEVICE_PORT}"], timeout=10)
                self.fport_port = None
            self.start()

    def _alloc_fport(self):
        local = 18212
        for attempt in range(6):
            for port in range(local, local + 30):
                rc, out, _ = _run_capture(self._base() + ["fport", f"tcp:{port}",
                                                          f"tcp:{self.DEVICE_PORT}"], timeout=15)
                if rc == 0 and "OK" in (out or ""):
                    self.fport_port = port
                    return
            # 全部端口占用: 可能 fport 规则残留, 清掉本工具端口段重试
            for port in range(local, local + 30):
                _run(self._base() + ["fport", "rm", f"tcp:{port}",
                                      f"tcp:{self.DEVICE_PORT}"], timeout=10)
            time.sleep(1.5)
        raise RuntimeError("fport 建立失败 (设备 8012 转发不可用)")

    def stop(self):
        try:
            if self.sock:
                stop_req = {"module": self.MODULE, "method": "Captures",
                            "params": {"api": "stopCaptureScreen", "args": {}}}
                body = json.dumps(stop_req).encode()
                self.sock.sendall(self.HEAD + struct.pack(">ii", self._session, len(body))
                                  + body + self.TAIL)
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except Exception:
                self._proc.kill()
        self._proc = None
        if self._proc_log:
            try:
                self._proc_log.close()
            except Exception:
                pass
            self._proc_log = None

    def cleanup(self):
        self.stop()
        if self.fport_port:
            _run(self._base() + ["fport", "rm", f"tcp:{self.fport_port}",
                                 f"tcp:{self.DEVICE_PORT}"], timeout=10)
            self.fport_port = None
        # daemon 交给空闲自杀 (3 分钟); killall 会误伤 DevEco 等其他 uitest 会话
        _run(self._base() + ["shell", "rm", "-f", self.DEVICE_AGENT], timeout=10)
        shutil.rmtree(self._tmp, ignore_errors=True)


class HarmonyScrcpyStream:
    """鸿蒙投屏 scrcpy 引擎 (DevEco screen-mirroring 同款): uitest daemon 加载
    libscrcpy server 扩展 (硬编 H.264) → fport → gRPC ScrcpyService/onStart
    server-streaming 拉流 → PyAV 解码 → 按画布缩放 → PPM (Tk 直读, 不经 sips)。
    实测 (uitest 5.0.1): 原生 1216×2688 (JPEG 通道上限 1120); 动画 ~2.4fps —
    老世代 server 逐帧截图+编码的硬上限 (6.x 设备的 unix server 才有真虚拟
    显示器 30fps); 单帧端到端延迟 ~100ms, 远低于 JPEG 通道的 300ms+。
    输入走 uinput (HarmonyInput), 与本通道无会话冲突。"""

    DEVICE_SO = "libscrcpy_server1.z.so"   # uitest < 6.0.2.1 (server0 实测起不来)
    GRPC_PORT = 5000
    POLL_HINT_MS = 30
    START_ARGS = "-scale 1 -frameRate 30 -bitRate 10 -iFrameInterval 30"

    def __init__(self, hdc_bin, connect_key=None):
        self.bin = hdc_bin
        self.connect_key = connect_key
        self.fport_port = None
        self.fit_wh = None            # (w, h) 画布适配目标, 渲染线程每帧读取
        self._q = queue.Queue()       # 解码出的 PPM 帧 (只保留最新)
        self._proc = None
        self._ch = None
        self._call = None
        self._thread = None
        self._dec = None
        self._err = None
        self._last_ok = 0.0
        self._restart_at = 0.0
        self._restart_lock = threading.Lock()
        self._running = False

    def _base(self):
        return [self.bin] + (["-t", self.connect_key] if self.connect_key else [])

    # ---- server 定位 ----
    @staticmethod
    def _find_server_so():
        import zipfile
        jars = []
        for pat in ("DevEco-Studio*/Contents/plugins/*/lib/hosScrcpy-*.jar",
                    "DevEco Studio*/Contents/plugins/*/lib/hosScrcpy-*.jar"):
            jars += list(Path("/Applications").glob(pat))
        cache = Path.home() / "Library/Caches/hap_installer/scrcpy"
        for jar in sorted(set(jars), reverse=True):
            try:
                with zipfile.ZipFile(jar) as z:
                    name = f"libscrcpy/{HarmonyScrcpyStream.DEVICE_SO}"
                    if name in z.namelist():
                        cache.mkdir(parents=True, exist_ok=True)
                        z.extract(name, cache)
                        return cache / name
            except Exception:
                continue
        for base in (Path(getattr(sys, "_MEIPASS", "") or ".") / "tools" / "scrcpy",
                     Path(__file__).resolve().parent / "tools" / "scrcpy"):
            p = base / HarmonyScrcpyStream.DEVICE_SO
            if p.exists():
                return p
        return None

    def _alive(self):
        rc, out, _ = _run_capture(self._base() +
                                  ["shell", "ps -ef | grep uitest | grep -v grep"],
                                  timeout=15)
        return bool((out or "").strip())

    def start(self):
        try:
            import av      # noqa: F401  (解码)
            import grpc    # noqa: F401  (拉流)
        except ImportError as e:
            raise RuntimeError(f"scrcpy 引擎缺依赖 ({e}; pip install av grpcio)")
        so = self._find_server_so()
        if not so:
            raise RuntimeError("找不到 libscrcpy server (需装 DevEco Studio, "
                               f"或把 {self.DEVICE_SO} 放入 tools/scrcpy/)")
        _kill_uitest_daemons(self.bin, self.connect_key)
        time.sleep(1)
        rc, _, err = _run_capture(self._base() + ["file", "send", str(so),
                                                  f"/data/local/tmp/{self.DEVICE_SO}"],
                                  timeout=120)
        if rc != 0:
            raise RuntimeError(f"推送 server 失败: {(err or '').strip()}")
        _run(self._base() + ["shell", f"chmod +x /data/local/tmp/{self.DEVICE_SO}"],
             timeout=8)
        self._proc = subprocess.Popen(
            self._base() + ["shell",
                            f"/system/bin/uitest start-daemon singleness "
                            f"--extension-name {self.DEVICE_SO} "
                            f"-p {self.GRPC_PORT} {self.START_ARGS}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        # 等 daemon bind 5000 (垂死实例占 socket 时秒级释放)
        for _ in range(16):
            if self._alive():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("scrcpy server daemon 未起来")
        self.fport_port = self._alloc_fport()
        self._connect_stream()

    def _alloc_fport(self):
        lo = 18300
        for _ in range(4):
            for port in range(lo, lo + 20):
                rc, out, _e = _run_capture(
                    self._base() + ["fport", f"tcp:{port}", f"tcp:{self.GRPC_PORT}"],
                    timeout=15)
                if rc == 0 and "OK" in (out or ""):
                    return port
            for port in range(lo, lo + 20):   # 清残留规则重试
                _run(self._base() + ["fport", "rm", f"tcp:{port}",
                                     f"tcp:{self.GRPC_PORT}"], timeout=8)
            time.sleep(1)
        raise RuntimeError("fport 建立失败 (scrcpy 5000 转发不可用)")

    def _connect_stream(self):
        """建 gRPC 通道并启动拉流/解码线程。"""
        import grpc
        self._ch = grpc.insecure_channel(f"127.0.0.1:{self.fport_port}")
        grpc.channel_ready_future(self._ch).result(timeout=6)
        self._err = None
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        self._request_idr()   # 立刻要关键帧: 有 IDR 基准才能解后续 P 帧

    def _request_idr(self):
        try:
            self._ch.unary_unary("/ScrcpyService/onRequestIDRFrame",
                                 request_serializer=lambda m: b"",
                                 response_deserializer=lambda b: b"")(b"")
        except Exception:
            pass

    def _reader(self):
        """拉流线程: gRPC 消息 (payload.data = Annex-B H.264) → PyAV 解码 →
        按画布缩放 → PPM 入队 (只保留最新, 渲染永远拿最新帧, 旧帧即延迟)。"""
        import av
        try:
            self._dec = av.CodecContext.create("h264", "r")
            self._call = self._ch.stream_stream(
                "/ScrcpyService/onStart",
                request_serializer=lambda m: b"",
                response_deserializer=_pb_parse_stream_msg)(iter([b""]))
            for msg in self._call:
                if not self._running:
                    break
                data = msg.get("data")
                if not isinstance(data, (bytes, bytearray)):
                    continue
                try:
                    packet = av.Packet(bytes(data))
                    for frame in self._dec.decode(packet):
                        self._emit(frame)
                    self._last_ok = time.time()
                except Exception as de:
                    if "Invalid data" in str(de):
                        # SPS/PPS 单独成包会抛此错但解码器已正常吸收参数集;
                        # 无 IDR 基准的 P 帧也走这 — 忽略, 等 IDR 到来即恢复。
                        # 切勿重建解码器 (会丢参数集, 之后全崩)。
                        continue
                    raise
        except Exception as e:
            if self._running:
                self._err = str(e)[:120]

    def _emit(self, frame):
        """解码帧 → 画布适配缩放 → PPM, 丢弃积压只留最新。"""
        import av
        w, h = self.fit_wh or (frame.width, frame.height)
        scale = min(w / frame.width, h / frame.height, 1.0)
        tw = max(int(frame.width * scale) & ~1, 2)
        th = max(int(frame.height * scale) & ~1, 2)
        if (tw, th) != (frame.width, frame.height):
            frame = frame.reformat(width=tw, height=th, format="rgb24")
        else:
            frame = frame.reformat(format="rgb24")
        p = frame.planes[0]
        raw = bytes(p)
        stride, row = p.line_size, frame.width * 3
        body = raw if stride == row else \
            b"".join(raw[y * stride:y * stride + row] for y in range(frame.height))
        ppm = f"P6\n{frame.width} {frame.height}\n255\n".encode() + body
        with self._q.mutex:
            self._q.queue.clear()
        self._q.put(ppm)

    def grab(self, out_path=None, timeout_s=0.25):
        """取最新解码帧 (PPM)。返回 (new_frame, ms, err); 无新帧且 daemon
        存活 = 画面静止, 不算错误。"""
        t0 = time.time()
        try:
            ppm = self._q.get(timeout=timeout_s)
        except queue.Empty:
            if self._err:
                return False, 0, f"scrcpy 流断开: {self._err}"
            if self._alive():
                return False, int((time.time() - t0) * 1000), ""
            return False, 0, "scrcpy daemon 已退出"
        if out_path:
            Path(out_path).write_bytes(ppm)
        return True, int((time.time() - t0) * 1000), ""

    def restart_session(self):
        """断线重连: daemon 杀掉整链重启 (带冷却, 防连环轰炸)。"""
        with self._restart_lock:
            wait = self._restart_at - time.time()
            if wait > 0:
                time.sleep(wait)
            if self._last_ok and time.time() - self._last_ok < 15:
                return  # 流其实是好的
            self._restart_at = time.time() + 5
            self.stop_stream()
            self.start()

    def stop_stream(self):
        self._running = False
        for closer in (lambda: self._call.cancel(), lambda: self._ch.close()):
            try:
                closer()
            except Exception:
                pass
        self._call = self._ch = None
        with self._q.mutex:
            self._q.queue.clear()
        self._err = None

    def stop(self):
        self.stop_stream()
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except Exception:
                self._proc.kill()
        self._proc = None

    def cleanup(self):
        self.stop()
        if getattr(self, "fport_port", None):
            _run(self._base() + ["fport", "rm", f"tcp:{self.fport_port}",
                                 f"tcp:{self.GRPC_PORT}"], timeout=8)
            self.fport_port = None
        _kill_uitest_daemons(self.bin, self.connect_key)
        _run(self._base() + ["shell", f"rm -f /data/local/tmp/{self.DEVICE_SO}"],
             timeout=8)


class HarmonyInput:
    """鸿蒙输入回传: 触摸走 uinput -T (内核级注入, 与任何投屏会话无冲突);
    按键走 uitest uiInput keyEvent (uinput -K 键码在该设备不触发系统键;
    uitest 短会话实测不挤 scrcpy 扩展流; JPEG agent 流会被顶掉, GUI 端
    触发 restart_session 自愈)。"""

    def __init__(self, hdc_bin, connect_key=None, screen_wh=(1216, 2688)):
        self.bin = hdc_bin
        self.connect_key = connect_key
        self.w, self.h = screen_wh

    def _base(self):
        return [self.bin] + (["-t", self.connect_key] if self.connect_key else []) \
            + ["shell"]

    def click(self, x_ratio, y_ratio):
        x, y = int(x_ratio * self.w), int(y_ratio * self.h)
        _run(self._base() + [f"uinput -T -c {x} {y}"], timeout=8)

    def double_click(self, x_ratio, y_ratio):
        x, y = int(x_ratio * self.w), int(y_ratio * self.h)
        _run(self._base() + [f"uinput -T -c {x} {y}"], timeout=8)
        time.sleep(0.12)
        _run(self._base() + [f"uinput -T -c {x} {y}"], timeout=8)

    def swipe(self, x0r, y0r, x1r, y1r):
        """uinput -T -m 平滑拖拽 (内核侧插值, 单命令完成)。"""
        x0, y0 = int(x0r * self.w), int(y0r * self.h)
        x1, y1 = int(x1r * self.w), int(y1r * self.h)
        _run(self._base() + [f"uinput -T -m {x0} {y0} {x1} {y1}"], timeout=8)

    def key(self, name):
        mapping = {"back": "Back", "home": "Home", "power": "Power"}
        _run(self._base() + [f"uitest uiInput keyEvent {mapping.get(name, name)}"],
             timeout=10)


class HarmonyLogs:
    """鸿蒙日志: hilog 流式 + faultlogger 崩溃列表/拉取。"""

    def __init__(self, hdc_bin, connect_key=None):
        self.bin = hdc_bin
        self.key = connect_key

    def _base(self):
        return [self.bin] + (["-t", self.key] if self.key else [])

    def hilog_args(self, level=None, domain=None, app_only=True):
        a = list(self._base()) + ["shell", "hilog"]
        if app_only:
            a += ["-t", "app"]
        if level:
            a += ["-L", level]
        if domain:
            a += ["-D", domain]
        return a

    def crash_list(self):
        """faultlogger 崩溃列表 [(name, time_str)]。shell ls 被拒, 走 hidumper 通道。"""
        rc, out, err = _run_capture(self._base() + ["shell", "hidumper", "-s", "HiviewFaultLogger"], timeout=20)
        names = []
        if rc == 0:
            for line in (out or "").splitlines():
                line = line.strip()
                m = re.match(
                    r"^((?:jscrash|cppcrash|appfreeze|syscrash|syntax|thermal|leak|ACE_)"
                    r"[-\w.]+?-\d+-\d+)$", line)
                if m:
                    names.append(m.group(1))
                elif re.match(r"^(jscrash|cppcrash|appfreeze|syscrash)", line) and not line.startswith("*"):
                    names.append(line)
        seen, uniq = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                uniq.append(n)
        return uniq

    def crash_pull(self, name, dest_dir):
        """拉取单个崩溃日志到 dest_dir。shell 层 /data/log 权限拒绝,
        但 hdc file recv 走 faultloggerd 转发通道可直拉 (实测 Mate60/MatePad Pro 通过)。"""
        remote = f"/data/log/faultlog/faultlogger/{name}"
        dest = Path(dest_dir) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = _run_capture(self._base() + ["file", "recv", remote, str(dest)], timeout=30)
        if rc != 0 or not dest.exists() or dest.stat().st_size == 0:
            return None, (err or "").strip() or "recv 失败 (设备可能限制 faultlogger 通道)"
        return dest, ""


# ---------------------------------------------------------------- iOS (devicectl)

class IOSScreen:
    """iOS 截屏轮询, 双通道:
      - iOS<17 老设备 (40 位 hex UDID): idevicescreenshot (screenshotr, ~0.6s/帧)
      - iOS 17+ (CoreDevice UDID): devicectl capture screenshot (~2s/帧, 原生 PNG)
    两者输出均为 PNG; 输出统一 8-bit (idevicescreenshot 可能产 16-bit, tkinter
    PhotoImage 不认), 经 sips 归一。"""

    def __init__(self, udid):
        self.udid = udid
        self.legacy = bool(re.fullmatch(r"[0-9a-fA-F]{40}", udid))
        self._tmp = Path(tempfile.mkdtemp(prefix="hapdbg_mirror_ios_"))

    def grab(self, png_path):
        t0 = time.time()
        if self.legacy:
            idevice_shot = _find_tool("idevicescreenshot")
            if not idevice_shot:
                return False, 0, ("老设备投屏需要 libimobiledevice: brew install libimobiledevice")
            rc, out, err = _run_capture([idevice_shot, "-u", self.udid,
                                         str(png_path)], timeout=15)
            if rc != 0 or not Path(png_path).exists():
                return False, 0, (err.strip() or out.strip() or "截图失败")
            _normalize_png(png_path)
        else:
            rc, out, err = _run_capture(["xcrun", "devicectl", "device", "capture", "screenshot",
                                         "--device", self.udid, "--destination", str(png_path)],
                                        timeout=30)
            if rc != 0 or not Path(png_path).exists():
                return False, 0, (err.strip() or out.strip() or "截图失败")
        return True, int((time.time() - t0) * 1000), ""

    def cleanup(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


def _normalize_png(path):
    """归一为 tkinter PhotoImage 可认的 8-bit PNG:
    idevicescreenshot 产 16-bit PNG, sips 无法直接降位深 — 经 JPEG 中转再转回
    PNG 得到 8-bit。已是 8-bit 则原样返回。"""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
        if head[:4] == b"\x89PNG" and head[24] == 8:
            return
        tmp_j = str(path) + ".mid.jpg"
        tmp_p = str(path) + ".norm.png"
        rc1, _, _ = _run(["sips", "-s", "format", "jpeg", str(path), "--out", tmp_j], timeout=10)
        rc2, _, _ = _run(["sips", "-s", "format", "png", tmp_j, "--out", tmp_p], timeout=10)
        if rc1 == 0 and rc2 == 0 and Path(tmp_p).exists():
            shutil.move(tmp_p, path)
        Path(tmp_j).unlink(missing_ok=True)
    except Exception:
        pass


class IOSLogs:
    """iOS 日志: idevicesyslog 流式 + systemCrashLogs 崩溃拉取。"""

    def __init__(self, udid):
        self.udid = udid

    def syslog_available(self):
        return shutil.which("idevicesyslog") is not None

    def syslog_cmd(self, process=None):
        a = ["idevicesyslog", "-u", self.udid]
        if process:
            a += ["-p", process]
        return a

    def crash_pull_all(self, dest_dir):
        """拉取全部崩溃日志 (.ips) 到 dest_dir/。"""
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        rc, out, err = _run_capture(["xcrun", "devicectl", "device", "copy", "from",
                             "--device", self.udid, "--domain-type", "systemCrashLogs",
                             "--source", "/", "--destination", str(dest)], timeout=120)
        files = sorted(dest.glob("*.ips")) if dest.exists() else []
        if rc != 0 and not files:
            return [], (err.strip() or out.strip() or "拉取失败")
        return files, ""


def list_ios_devices():
    """devicectl 设备列表 [(udid, name, state)]。"""
    rc, out, err = _run_capture(["xcrun", "devicectl", "list", "devices"], timeout=60)
    devs = []
    if rc == 0:
        for line in out.splitlines():
            if not line.strip() or line.lstrip().startswith("Name") or set(line.strip()) <= {"-", " "}:
                continue
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 3:
                continue
            m = re.match(r"([0-9A-Za-z-]+)", parts[1])
            if not m or m.group(1) == "UDID":
                continue
            devs.append((m.group(1), parts[0], parts[2]))
    return devs

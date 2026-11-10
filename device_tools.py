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

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SHOT_REMOTE = "/data/local/tmp/hapdbg_mirror.jpeg"


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


class HarmonyInput:
    """鸿蒙输入回传: uitest uiInput (click/swipe/keyEvent/inputText)。"""

    def __init__(self, hdc_bin, connect_key=None, screen_wh=(1216, 2688)):
        self.bin = hdc_bin
        self.key = connect_key
        self.w, self.h = screen_wh

    def _base(self):
        return [self.bin] + (["-t", self.key] if self.key else [])

    def click(self, x_ratio, y_ratio):
        x, y = int(x_ratio * self.w), int(y_ratio * self.h)
        _run(self._base() + ["shell", "uitest", "uiInput", "click", str(x), str(y)], timeout=10)

    def double_click(self, x_ratio, y_ratio):
        x, y = int(x_ratio * self.w), int(y_ratio * self.h)
        _run(self._base() + ["shell", "uitest", "uiInput", "doubleClick", str(x), str(y)], timeout=10)

    def swipe(self, x0r, y0r, x1r, y1r):
        a = [int(x0r * self.w), int(y0r * self.h), int(x1r * self.w), int(y1r * self.h)]
        _run(self._base() + ["shell", "uitest", "uiInput", "swipe", *map(str, a)], timeout=10)

    def key(self, name):
        mapping = {"back": "Back", "home": "Home", "power": "Power"}
        k = mapping.get(name, name)
        _run(self._base() + ["shell", "uitest", "uiInput", "keyEvent", k], timeout=10)


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
            if not shutil.which("idevicescreenshot"):
                return False, 0, ("老设备投屏需要 libimobiledevice: brew install libimobiledevice")
            rc, out, err = _run_capture(["idevicescreenshot", "-u", self.udid,
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

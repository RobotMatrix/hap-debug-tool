#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hap_gui.py -- 小白调试助手 的图形前端 (重实现)

功能:
  - 拖拽 .app / .hap, 顶部提示行显示签名摘要 (详情可折叠)
  - 个人 / 企业(团队) 证书切换
  - 一键: 云申请证书+Profile -> 本地签名 -> hdc 安装 -> 启动
  - 同一二进制带子命令即走 CLI (见 hap_cli.py)

运行: /Users/lasysloth/miniconda3/bin/python3 hap_gui.py
"""

import os
import plistlib
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
import subprocess

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import hap_cli as H
except Exception as e:
    print("无法导入 hap_cli.py:", e)
    sys.exit(1)

try:
    import ios_sign as IOS
except Exception as e:
    print("无法导入 ios_sign.py:", e)
    IOS = None

try:
    import device_tools as DT
except Exception as e:
    print("无法导入 device_tools.py:", e)
    DT = None

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    TkinterDnD, DND_FILES, HAS_DND = None, None, False

DEFAULT_TEAMS = [
    ("个人 · 周鑫飞", "420086000304296880"),
    ("企业 · 北京梆梆安全科技有限公司 (只读,禁止增删)", "30086000687991714"),
]

def _load_app_version():
    """版本号: 打包时注入的 version.txt 优先, 回退源码常量 (开发态)。"""
    try:
        base = getattr(sys, "_MEIPASS", None)
        if base:
            vf = Path(base) / "version.txt"
            if vf.exists():
                return vf.read_text().strip() or FALLBACK_VERSION
        vf = Path(__file__).resolve().parent / "version.txt"
        if vf.exists():
            return vf.read_text().strip() or FALLBACK_VERSION
    except Exception:
        pass
    return FALLBACK_VERSION


FALLBACK_VERSION = "0.5.2"
APP_VERSION = _load_app_version()

CLI_COMMANDS = {
    "info", "sig", "sign", "verify", "genkeys", "signprofile",
    "devices", "connect", "shell", "udid", "apps",
    "install", "uninstall", "push", "pull", "hilog", "passthrough", "cloud",
}


def is_dark_mode():
    try:
        import subprocess
        out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return out.lower() == "dark"
    except Exception:
        return False


def _cn(subject):
    m = re.search(r"CN=([^,/]+)", subject or "")
    return m.group(1).strip() if m else (subject or "")


def _load_platform_logos():
    """加载平台小 logo (20px); 失败返回 {} 走纯文字"""
    try:
        from PIL import Image, ImageTk
    except Exception:
        return {}
    base = Path(getattr(sys, "_MEIPASS", HERE))
    if base == HERE:
        asset_dir = HERE / "packaging/assets"
    else:
        asset_dir = base / "assets"
    p = asset_dir / "harmonyos.png"
    a = asset_dir / "apple.png"
    if not (p.exists() and a.exists()):
        return {}
    try:
        out = {}
        for key, path in (("harmony", p), ("ios", a)):
            im = Image.open(path).convert("RGBA").resize((20, 20), Image.LANCZOS)
            out[key] = ImageTk.PhotoImage(im)
        return out
    except Exception:
        return {}


def sig_summary(info):
    if not info.get("signed"):
        return "未签名"
    p = info.get("profile", {}) or {}
    c = info.get("cert", {}) or {}
    v = (p.get("validity") or {})
    until = ""
    if v.get("not-after"):
        until = " · 至 " + time.strftime("%Y-%m-%d", time.localtime(v["not-after"]))
    return f"已签名 · {p.get('type', '?')} · {_cn(c.get('subject'))}{until}"


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"HAP 调试助手 v{APP_VERSION}")
        root.geometry("800x660")
        root.minsize(700, 580)
        self.logq = queue.Queue()
        self.hap_path = None
        self.meta = None
        self.teams = list(DEFAULT_TEAMS)
        self.busy = False
        self.sig_expanded = False
        self.platform = tk.StringVar(value="harmony")   # harmony | ios
        self.ios_meta = None
        self.ios_profiles = []
        self.ios_local_prov = None
        self.ios_embedded_prov = None
        self.ios_prov_path = None
        self.mirror_win = None
        self.log_wins = {}
        self._ios_devs = ([], "")
        self._ios_dev_keys = []

        dark = is_dark_mode()
        C = {
            "bg":      "#1e1e1e" if dark else "#ECECEC",
            "drop":    "#2c2c2e" if dark else "#FFFFFF",
            "drop_bd": "#3a3a3c" if dark else "#D9D9DE",
            "text":    "#F2F2F7" if dark else "#1D1D1F",
            "sub":     "#98989D" if dark else "#8E8E93",
            "accent":  "#0A84FF" if dark else "#007AFF",
            "success": "#30D158" if dark else "#34C759",
            "log_bg":  "#1e1e1e" if dark else "#FFFFFF",
        }
        self.C = C
        self.drop_bg, self.drop_fg = C["drop"], C["text"]
        self.box_bg, self.box_fg = C["log_bg"], C["text"]
        root.configure(bg=C["bg"])

        base = tkfont.nametofont("TkDefaultFont")
        mono = tkfont.nametofont("TkFixedFont")
        self.f_title = base.copy(); self.f_title.configure(size=base.cget("size") + 6, weight="bold")
        self.f_sub = base.copy(); self.f_sub.configure(size=base.cget("size") - 1)
        self.f_body = base.copy()
        self.f_bold = base.copy(); self.f_bold.configure(weight="bold")
        self.f_mono = mono.copy()
        self.f_drop = base.copy(); self.f_drop.configure(size=base.cget("size") + 3)

        st = ttk.Style()
        try:
            st.theme_use("aqua")
        except Exception:
            pass
        st.configure("Title.TLabel", font=self.f_title, foreground=C["text"], background=C["bg"])
        st.configure("Hint.TLabel", font=self.f_sub, foreground=C["sub"], background=C["bg"])
        st.configure("Body.TLabel", font=self.f_body, foreground=C["text"], background=C["bg"])
        st.configure("Sub.TLabel", font=self.f_body, foreground=C["sub"], background=C["bg"])
        st.configure("Go.TButton", font=self.f_bold)
        st.configure("Sub.TButton", font=self.f_sub, foreground=C["sub"])
        st.configure("TFrame", background=C["bg"])
        st.configure("TLabel", background=C["bg"])
        st.configure("TLabelframe", background=C["bg"])
        st.configure("TLabelframe.Label", font=self.f_bold, foreground=C["text"], background=C["bg"])

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)

        self.title_lbl = ttk.Label(outer, text="📦  HAP 调试助手", style="Title.TLabel")
        self.title_lbl.pack(anchor="w")
        self.sub_lbl = ttk.Label(outer, text="拖入 .app / .hap 一键签名、安装、启动",
                                 style="Hint.TLabel")
        self.sub_lbl.pack(anchor="w", pady=(2, 6))

        platrow = ttk.Frame(outer)
        platrow.pack(fill="x", pady=(0, 8))
        ttk.Label(platrow, text="🧭  平台:", style="Body.TLabel").pack(side="left", padx=(0, 6))
        self._logos = _load_platform_logos()
        img_h = self._logos.get("harmony")
        img_i = self._logos.get("ios")
        self.plat_h = ttk.Radiobutton(platrow, text="鸿蒙 (HAP/HSP)" if img_h else "🌸 鸿蒙 (HAP/HSP)",
                                      value="harmony", variable=self.platform,
                                      command=self.on_platform_change,
                                      image=img_h, compound="left")
        self.plat_h.image = img_h
        self.plat_h.pack(side="left")
        self.plat_i = ttk.Radiobutton(platrow, text="iOS (ipa/app)" if img_i else "🍎 iOS (ipa/app)",
                                      value="ios", variable=self.platform,
                                      command=self.on_platform_change,
                                      image=img_i, compound="left")
        self.plat_i.image = img_i
        self.plat_i.pack(side="left", padx=(12, 0))

        self.body = ttk.Frame(outer)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        left = ttk.Frame(self.body)
        left.grid(row=0, column=0, sticky="nsew")

        self.drop = tk.Label(
            left, text="⬇️   把 .app / .hap 拖到这里   (或点此选择文件)",
            relief="solid", bd=1, height=4, justify="center",
            bg=self.drop_bg, fg=self.drop_fg, font=self.f_drop,
            highlightthickness=0)
        self.drop.pack(fill="x")
        self.drop.bind("<Button-1>", lambda e: self.pick_file())
        if HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self.on_drop)

        infrow = ttk.Frame(left)
        infrow.pack(fill="x", pady=(8, 2))
        self.sig_toggle = ttk.Button(infrow, text="详情 ▸", width=8,
                                     command=self.toggle_sig, state="disabled")
        self.sig_toggle.pack(side="right")
        self.sig_hint = ttk.Label(infrow, text="签名: —", foreground=C["sub"])
        self.sig_hint.pack(side="right", padx=(12, 8))
        self.sig_dot = ttk.Label(infrow, text="●", foreground=C["sub"])
        self.sig_dot.pack(side="right")
        self.info = ttk.Label(infrow, text="未选择文件", justify="left", anchor="w")
        self.info.pack(side="right", fill="x", expand=True)

        self._detail_text = ""
        self._detail_signed = False
        self.detail_visible = False

        cfg = ttk.LabelFrame(left, text=" ⚙️  签名设置 (鸿蒙) ", padding=12)
        cfg.pack(fill="x", pady=8)
        cfg.columnconfigure(1, weight=1)
        cfg.columnconfigure(3, weight=1)
        self.cfg_harmony = cfg

        ttk.Label(cfg, text="👥  团队:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        self.team_var = tk.StringVar(value=self.teams[0][0])
        self.team_box = ttk.Combobox(cfg, textvariable=self.team_var,
                                     values=[n for n, _ in self.teams], state="readonly")
        self.team_box.grid(row=0, column=1, sticky="we", pady=3)

        ttk.Label(cfg, text="🔑  证书名:").grid(row=0, column=2, sticky="e", padx=(12, 6), pady=4)
        self.cert_name = tk.StringVar(value="auto-debug-" + time.strftime("%Y%m%d%H%M%S"))
        ttk.Entry(cfg, textvariable=self.cert_name).grid(row=0, column=3, sticky="we", pady=3)
        ttk.Button(cfg, text="🕒 重命名",
                   command=lambda: self.cert_name.set("auto-debug-" + time.strftime("%Y%m%d%H%M%S"))
                   ).grid(row=0, column=4, padx=(6, 0), pady=4)

        adv = ttk.Frame(cfg)
        adv.grid(row=1, column=0, columnspan=5, sticky="we", pady=(4, 0))
        adv.columnconfigure(0, weight=1)
        self.adv_open = tk.BooleanVar(value=False)
        self.adv_btn = ttk.Button(adv, text="▸ 高级 (Token / 登录 / 代理)",
                                  command=self.toggle_advanced, style="Sub.TButton")
        self.adv_btn.grid(row=0, column=0, sticky="w")

        self.adv_body = ttk.Frame(adv, padding=(2, 6, 0, 0))
        self.adv_body.columnconfigure(1, weight=1)
        self.adv_body.columnconfigure(3, weight=1)

        ttk.Label(self.adv_body, text="🔐  Token:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        self.token_var = tk.StringVar(value=self._load_token())
        self.token_entry = ttk.Entry(self.adv_body, textvariable=self.token_var, show="*")
        self.token_entry.grid(row=0, column=1, columnspan=3, sticky="we", pady=3)
        btns = ttk.Frame(self.adv_body)
        btns.grid(row=0, column=4, padx=(6, 0), pady=3)
        ttk.Button(btns, text="🔓 登录", command=self.do_login).pack(side="left")
        ttk.Button(btns, text="🔍 检查", command=lambda: self.check_token(notify=True)).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="🔄 团队", command=self.refresh_teams).pack(side="left", padx=(4, 0))

        ttk.Label(self.adv_body, text="👤  UID:").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        uid0 = H.UID_FILE.read_text().strip() if H.UID_FILE.exists() else os.environ.get("HUAWEI_UID", "")
        self.uid_var = tk.StringVar(value=uid0)
        ttk.Entry(self.adv_body, textvariable=self.uid_var).grid(row=1, column=1, columnspan=3, sticky="we", pady=3)

        ttk.Label(self.adv_body, text="🌐  代理:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        self.proxy_var = tk.StringVar(value=os.environ.get("HAP_CLOUD_PROXY", ""))
        ttk.Entry(self.adv_body, textvariable=self.proxy_var).grid(row=2, column=1, columnspan=3, sticky="we", pady=3)
        ttk.Label(self.adv_body, text="(如 http://127.0.0.1:1087; 空=直连)", style="Sub.TLabel").grid(
            row=2, column=4, sticky="w", padx=(6, 0), pady=4)

        ioscfg = ttk.LabelFrame(left, text=" ⚙️  签名设置 (iOS) ", padding=12)
        ioscfg.columnconfigure(1, weight=1)
        self.cfg_ios = ioscfg

        ttk.Label(ioscfg, text="📱  设备:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        self.ios_device_var = tk.StringVar()
        self.ios_device_box = ttk.Combobox(ioscfg, textvariable=self.ios_device_var,
                                           state="readonly", width=30)
        self.ios_device_box.grid(row=0, column=1, sticky="we", pady=3)
        ttk.Button(ioscfg, text="🔄 刷新", command=self.refresh_ios_devices).grid(row=0, column=2, padx=(6, 0), pady=4)
        self.ios_device_lbl = ttk.Label(ioscfg, text="", style="Sub.TLabel")
        self.ios_device_lbl.grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Label(ioscfg, text="🪪  证书:").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        self.ios_identity_var = tk.StringVar()
        self.ios_identity_box = ttk.Combobox(ioscfg, textvariable=self.ios_identity_var, state="readonly")
        self.ios_identity_box.grid(row=1, column=1, sticky="we", pady=3)
        ttk.Button(ioscfg, text="🔄 刷新", command=self.refresh_ios_identities).grid(row=1, column=2, padx=(6, 0), pady=4)

        ttk.Label(ioscfg, text="📄  描述文件:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        self.ios_prov_var = tk.StringVar()
        self.ios_prov_box = ttk.Combobox(ioscfg, textvariable=self.ios_prov_var, state="readonly")
        self.ios_prov_box.grid(row=2, column=1, sticky="we", pady=3)
        self.ios_prov_btns = ttk.Frame(ioscfg)
        self.ios_prov_btns.grid(row=2, column=2, padx=(6, 0), pady=4)
        ttk.Button(self.ios_prov_btns, text="🔄 刷新",
                   command=self.refresh_ios_profiles).pack(side="left")
        ttk.Button(self.ios_prov_btns, text="📂 本地",
                   command=self.pick_ios_provision).pack(side="left", padx=(4, 0))
        ttk.Button(self.ios_prov_btns, text="ℹ️ 详情",
                   command=self.show_ios_provision_detail).pack(side="left", padx=(4, 0))
        self.ios_prov_box.bind("<<ComboboxSelected>>", lambda e: self.on_ios_provision_selected())

        ttk.Label(ioscfg, text="🆔  BundleID:").grid(row=3, column=0, sticky="e", padx=(0, 6), pady=4)
        self.ios_bundle_var = tk.StringVar()
        ttk.Entry(ioscfg, textvariable=self.ios_bundle_var).grid(row=3, column=1, sticky="we", pady=3)
        ttk.Label(ioscfg, text="留空 = 保持原样", style="Sub.TLabel").grid(row=3, column=2, sticky="w", padx=(6, 0))

        ttk.Label(ioscfg, text="📉  最低系统:").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=4)
        self.ios_min_os_var = tk.StringVar(value="13.0")
        ttk.Entry(ioscfg, textvariable=self.ios_min_os_var, width=8).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(ioscfg, text="降包的 MinimumOSVersion (老设备装高版本标高的包); 留空 = 不降",
                  style="Sub.TLabel").grid(row=4, column=2, sticky="w", padx=(6, 0))

        devrow = ttk.Frame(left)
        self.devrow = devrow
        devrow.pack(fill="x", pady=(0, 2))
        ttk.Label(devrow, text="📱  设备:").pack(side="left", padx=(0, 6))
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(devrow, textvariable=self.device_var,
                                       state="readonly", width=32)
        self.device_box.pack(side="left")
        ttk.Button(devrow, text="🔄 刷新设备", command=self.refresh_devices).pack(side="left", padx=(6, 0))
        self.device_lbl = ttk.Label(devrow, text="", style="Sub.TLabel")
        self.device_lbl.pack(side="left", padx=(10, 0))

        act = ttk.Frame(left)
        self.act = act
        act.pack(fill="x", pady=(2, 8))
        self.go = ttk.Button(act, text="🚀  一键安装并启动", style="Go.TButton", command=self.one_click)
        self.go.pack(side="left")
        self.progress = ttk.Progressbar(act, mode="indeterminate", length=120)
        ttk.Button(act, text="✍️ 仅签名", command=lambda: self.one_click(install=False)).pack(side="left", padx=6)
        ttk.Button(act, text="📲 仅安装", command=self.install_only).pack(side="left", padx=6)
        ttk.Button(act, text="🖥️ 投屏", command=self.open_mirror).pack(side="left", padx=6)
        ttk.Button(act, text="📜 设备日志", command=self.open_device_log).pack(side="left", padx=6)
        ttk.Button(act, text="🗑️ 清空日志", command=self.clear_log).pack(side="right")

        outrow = ttk.Frame(left)
        outrow.pack(fill="x", pady=(0, 8))
        ttk.Label(outrow, text="📦  产物:").pack(side="left")
        self.out_var = tk.StringVar(value="—")
        self.out_label = ttk.Label(outrow, textvariable=self.out_var, foreground=C["success"])
        self.out_label.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(outrow, text="📂 打开目录", command=self.reveal_out).pack(side="right")
        ttk.Button(outrow, text="📋 复制路径", command=self.copy_out).pack(side="right", padx=4)

        logf = ttk.LabelFrame(outer, text=f" 📜  日志  ·  v{APP_VERSION} ", padding=4)
        logf.pack(fill="both", expand=True, pady=(8, 0))
        self.logbox = tk.Text(logf, height=10, bg=self.box_bg, fg=self.box_fg,
                              insertbackground=self.box_fg, font=self.f_mono,
                              highlightthickness=1, highlightbackground=C["drop_bd"],
                              wrap="word", state="disabled", cursor="arrow")
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        self.logbox.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(fill="both", expand=True)
        for tag, color in (("+", C["success"]), ("-", "#FF453A" if dark else "#FF3B30"),
                           ("!", "#FFD60A" if dark else "#B25000"), ("*", C["accent"])):
            self.logbox.tag_configure(tag, foreground=color)
        self.logbox.tag_configure("dim", foreground=C["sub"])
        self.log_autoscroll = tk.BooleanVar(value=True)
        logbar = ttk.Frame(logf)
        logbar.pack(fill="x", side="bottom")
        ttk.Checkbutton(logbar, text="自动滚动", variable=self.log_autoscroll).pack(side="right")
        self.log_ts = tk.BooleanVar(value=False)
        ttk.Checkbutton(logbar, text="时间戳", variable=self.log_ts,
                        command=self._rebuild_log_ts).pack(side="right", padx=(0, 10))
        self._raw_log = []
        self.logbox.bind("<Button-3>", self._log_context_menu)
        self.logbox.bind("<Button-2>", self._log_context_menu)

        self.root.after(100, self._drain)
        self.root.after(800, self.check_token)
        self.root.after(300, self.refresh_ios_profiles)
        self.root.after(200, self.refresh_devices)

    def log(self, msg):
        self.logq.put(str(msg))

    def _ui(self, fn, *args):
        """工作线程调用 UI 操作的统一入口: 主线程直接执行, 工作线程经 logq 队列
        投递到主线程 (_drain 已在消费)。Tk 8.5 下工作线程直接 root.after 会
        'main thread is not in main loop'。"""
        import threading as _th
        if _th.current_thread() is _th.main_thread():
            try:
                fn(*args)
            except Exception:
                pass
            return
        self.logq.put(("__UI__", fn, args))

    def clear_log(self):
        self._raw_log.clear()
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.config(state="disabled")

    def _log_tag_for(self, msg):
        s = msg.lstrip()
        for prefix, tag in (("[+]", "+"), ("[-]", "-"), ("[!]", "!"), ("[*]", "*")):
            if s.startswith(prefix):
                return tag
        if s.startswith("=== ") or s.startswith("Traceback"):
            return "!"
        return None

    def _append_log(self, msg):
        self._raw_log.append(msg)
        self.logbox.config(state="normal")
        if self.log_ts.get():
            ts = time.strftime("%H:%M:%S ")
            self.logbox.insert("end", ts, "dim")
        tag = self._log_tag_for(msg)
        self.logbox.insert("end", msg + "\n", tag or ())
        self.logbox.config(state="disabled")

    def _rebuild_log_ts(self):
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        for msg in self._raw_log:
            if self.log_ts.get():
                self.logbox.insert("end", time.strftime("%H:%M:%S "), "dim")
            self.logbox.insert("end", msg + "\n", self._log_tag_for(msg) or ())
        self.logbox.config(state="disabled")
        self.logbox.see("end")

    def _log_context_menu(self, event):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="复制", command=self._log_copy)
        m.add_command(label="全选", command=lambda: self.logbox.tag_add("sel", "1.0", "end"))
        m.tk_popup(event.x_root, event.y_root)

    def _log_copy(self):
        text = self.logbox.get("sel.first", "sel.last") or self.logbox.get("1.0", "end").strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("[*] 日志已复制到剪贴板")

    def _set_out(self, path):
        self.out_var.set(path or "—")

    def copy_out(self):
        p = self.out_var.get()
        if p and p != "—":
            self.root.clipboard_clear()
            self.root.clipboard_append(p)
            self.log(f"[*] 已复制路径: {p}")

    def reveal_out(self):
        p = self.out_var.get()
        if p and p != "—":
            import subprocess
            subprocess.run(["open", "-R", p])

    def _drain(self):
        dirty = False
        try:
            while True:
                item = self.logq.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__UI__":
                    _, fn, args = item
                    try:
                        fn(*args)
                    except Exception:
                        pass
                    dirty = True
                    continue
                self._append_log(item)
                dirty = True
        except queue.Empty:
            pass
        if dirty and self.log_autoscroll.get():
            self.logbox.see("end")
        if self.detail_visible and self.detail_win is not None:
            try:
                want_x = self.root.winfo_rootx() + self.root.winfo_width() + 8
                want_y = self.root.winfo_rooty()
                if abs(self.detail_win.winfo_x() - want_x) > 2 or abs(self.detail_win.winfo_y() - want_y) > 2:
                    self._move_detail()
            except Exception:
                pass
        self.root.after(100, self._drain)

    def toggle_sig(self):
        if self.detail_visible:
            self.detail_win.destroy()
            self.detail_win = None
            self.detail_visible = False
            self.sig_toggle.config(text="详情 ▸")
        else:
            self._show_detail()

    def _detail_geo(self):
        x = self.root.winfo_rootx() + self.root.winfo_width() + 8
        y = self.root.winfo_rooty()
        return f"380x{max(self.root.winfo_height() - 40, 300)}+{x}+{y}"

    def _show_detail(self):
        w = tk.Toplevel(self.root)
        w.title("签名详情")
        w.geometry(self._detail_geo())
        w.resizable(True, True)
        try:
            w.transient(self.root)
        except Exception:
            pass
        w.protocol("WM_DELETE_WINDOW", self.toggle_sig)
        self.detail_head = ttk.Label(w, text="—", style="Sub.TLabel")
        self.detail_head.pack(fill="x", padx=10, pady=(8, 4))
        self.detail_body = tk.Text(w, wrap="char", relief="flat",
                                   bg=self.box_bg, fg=self.box_fg, font=self.f_mono,
                                   highlightthickness=1, highlightbackground=self.C["drop_bd"])
        sb = ttk.Scrollbar(w, command=self.detail_body.yview)
        self.detail_body.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 6), pady=(0, 8))
        self.detail_body.pack(fill="both", expand=True, padx=(10, 0), pady=(0, 8))
        self._render_detail()
        self.detail_win = w
        self.detail_visible = True
        self.sig_toggle.config(text="收起 ▸")

    def _move_detail(self):
        if self.detail_win is not None:
            try:
                self.detail_win.geometry("+" + str(self.root.winfo_rootx() + self.root.winfo_width() + 8)
                                         + "+" + str(self.root.winfo_rooty()))
            except Exception:
                pass

    def _render_detail(self):
        self.detail_head.config(
            text=("● 已签名" if self._detail_signed else "● 未签名"),
            foreground=(self.C["success"] if self._detail_signed else self.C["sub"]))
        self.detail_body.config(state="normal")
        self.detail_body.delete("1.0", "end")
        self.detail_body.insert("1.0", self._detail_text or "(无)")
        self.detail_body.config(state="disabled")

    def _ellipsize(self, text, maxlen=52):
        text = str(text).replace("\n", " ")
        return text if len(text) <= maxlen else text[:maxlen - 1] + "…"

    def _set_sig(self, summary, detail, signed):
        self.sig_hint.config(text=f"签名: {self._ellipsize(summary, 30)}",
                             foreground=(self.C["success"] if signed else self.C["sub"]))
        self.sig_dot.config(foreground=(self.C["success"] if signed else self.C["sub"]))
        self._detail_text = detail
        self._detail_signed = bool(signed)
        self.sig_toggle.config(state="normal")
        if self.detail_visible and self.detail_win is not None:
            self._render_detail()

    def toggle_advanced(self):
        open_ = not self.adv_open.get()
        self.adv_open.set(open_)
        self.adv_btn.config(text="▾ 高级 (Token / 登录 / 代理)" if open_
                            else "▸ 高级 (Token / 登录 / 代理)")
        if open_:
            self.adv_body.grid(row=2, column=0, columnspan=5, sticky="we", pady=(4, 0))
        else:
            self.adv_body.grid_forget()

    def _device_key(self):
        v = (self.device_var.get() or "").strip()
        return v or None

    def refresh_devices(self):
        def work():
            try:
                t = H.Tools()
                h = H.Hdc(t, self._device_key())
                keys = h.targets()
                labels = {k: h.device_label(k) for k in keys}
            except SystemExit as e:
                self.root.after(0, lambda: (self.device_lbl.config(text=f"找不到 hdc ({e})"),
                                            self.log(f"[-] 设备刷新失败: 找不到 hdc ({e})")))
                return
            except Exception as e:
                self.root.after(0, lambda: (self.device_lbl.config(text=f"刷新失败: {e}"),
                                            self.log(f"[-] 设备刷新失败: {e}")))
                return
            def done():
                self._device_keys = keys
                self._device_labels = labels
                if keys:
                    disp = [labels[k] for k in keys]
                    self.device_box.config(values=disp)
                    if not self.device_var.get() or self.device_var.get() not in disp:
                        self.device_var.set(disp[0])
                    self.device_lbl.config(text=f"{len(keys)} 台")
                else:
                    self.device_box.config(values=[])
                    self.device_var.set("")
                    self.device_lbl.config(text="未连接 (检查 USB 调试/网络调试)")
            self.root.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _device_key(self):
        v = (self.device_var.get() or "").strip()
        labels = getattr(self, "_device_labels", {})
        if v in labels:
            return v
        for k, lbl in labels.items():
            if lbl == v:
                return k
        keys = getattr(self, "_device_keys", [])
        return keys[0] if keys else None

    def refresh_ios_devices(self):
        """iOS 设备下拉: devicectl + usbmuxd 双通道合并 (老设备 iOS<17 只有后者可见)。"""
        if not IOS:
            return
        def work():
            try:
                return IOS.list_all_devices(), ""
            except Exception as e:
                return [], str(e)
        def done():
            devs, err = self._ios_devs
            if err:
                self.ios_device_lbl.config(text=f"刷新失败: {err}")
                return
            self._ios_dev_keys = devs
            labels = [d[1] + ("" if d[2].lower().startswith("available")
                              or d[2] == "available" or d[3] == "usbmuxd"
                              else f" [{d[2]}]") for d in devs]
            self.ios_device_box.config(values=labels)
            if devs:
                cur = self.ios_device_var.get()
                if cur not in labels:
                    self.ios_device_var.set(labels[0])
                self.ios_device_lbl.config(text=f"{len(devs)} 台")
            else:
                self.ios_device_var.set("")
                self.ios_device_lbl.config(text="未连接 (解锁亮屏重试)")
        def work_done():
            self._ios_devs = work()
            done()
        threading.Thread(target=work_done, daemon=True).start()

    def _ios_device_key(self):
        """下拉框 label -> 设备 UDID (按序对应)。"""
        labels = self.ios_device_box.cget("values") or ()
        cur = self.ios_device_var.get()
        try:
            idx = list(labels).index(cur)
            return self._ios_dev_keys[idx][0] if idx < len(self._ios_dev_keys) else None
        except (ValueError, AttributeError):
            return None

    def _device_tool_key(self):
        """设备工具窗口用的设备标识: iOS 优先下拉选中设备, 鸿蒙=connect key。"""
        if self.platform.get() == "ios":
            udid = self._ios_device_key() if hasattr(self, "_ios_dev_keys") else None
            if not udid:
                udid = IOS.get_first_physical_udid() if IOS else None
            if not udid:
                messagebox.showwarning("提示", "没有已连接的 iOS 设备 (解锁亮屏后点「🔄 刷新」)")
                return None
            return udid
        return self._device_key()

    def open_mirror(self):
        if not DT:
            messagebox.showwarning("提示", "device_tools.py 不可用")
            return
        key = self._device_tool_key()
        if not key:
            return
        if self.mirror_win is not None and self.mirror_win.winfo_exists():
            self.mirror_win.win.lift()
            return
        self.mirror_win = MirrorWindow(self, self.platform.get(), key)
        self.log(f"[*] 投屏窗口已打开 ({self.platform.get()} · {key})")

    def open_device_log(self):
        if not DT:
            messagebox.showwarning("提示", "device_tools.py 不可用")
            return
        key = self._device_tool_key()
        if not key:
            return
        win = self.log_wins.get(self.platform.get())
        if win is not None and win.winfo_exists():
            win.win.lift()
            return
        self.log_wins[self.platform.get()] = DeviceLogWindow(self, self.platform.get(), key)
        self.log(f"[*] 设备日志窗口已打开 ({self.platform.get()} · {key})")

    def on_platform_change(self):
        ios = self.platform.get() == "ios"
        self.title_lbl.config(text="📱  iOS 调试助手" if ios else "📦  HAP 调试助手")
        self.sub_lbl.config(text="拖入 .ipa / .app 一键签名、安装、启动" if ios
                            else "拖入 .app / .hap 一键签名、安装、启动")
        self.drop.config(text="⬇️   把 .ipa / .app 拖到这里   (或点此选择文件)" if ios
                         else "⬇️   把 .app / .hap 拖到这里   (或点此选择文件)")
        if ios:
            self.cfg_harmony.pack_forget()
            self.devrow.pack_forget()
            self.cfg_ios.pack(fill="x", pady=8)
            self.refresh_ios_devices()
            self.refresh_ios_identities()
            self.refresh_ios_profiles()
        else:
            self.cfg_ios.pack_forget()
            self.cfg_harmony.pack(fill="x", pady=8)
            self.devrow.pack(fill="x", pady=(0, 2), before=self.act)
        self.info.config(text="未选择文件")
        self.hap_path = None
        self.meta = None
        self.ios_meta = None
        self._set_sig("—", "(无)", False)
        self.sig_toggle.config(state="disabled")
        self._set_out("—")
        self.log("[*] 平台已切换, 请重新选择文件")

    def refresh_ios_identities(self):
        if not IOS:
            self.log("[-] ios_sign.py 导入失败, iOS 功能不可用")
            return
        def work():
            try:
                ids = IOS.list_identities()
            except Exception as e:
                self.log(f"[-] 读取钥匙串身份失败: {e}")
                return
            def upd():
                names = [i["name"] for i in ids]
                self.ios_identity_box.config(values=names)
                if names:
                    dev = next((n for n in names if "Development" in n or "development" in n), names[0])
                    self.ios_identity_var.set(dev)
                    self.log(f"[+] 钥匙串身份 {len(names)} 个, 默认选: {dev}")
                else:
                    self.ios_identity_var.set("")
                    self.log("[-] 钥匙串无可用代码签名身份")
            self.root.after(0, upd)
        threading.Thread(target=work, daemon=True).start()

    def _ios_provision_display(self, pr):
        tag = "✓" if pr["valid"] else "✗过期"
        return f"{tag} {pr['name']} · {pr['appid']} · {pr['summary']}"

    def _prov_specials(self):
        """下拉列表头部特殊选项 (label, key): 安装包自带第一, 本地选择第二"""
        specials = [("📦 安装包自带", "embedded")]
        if self.ios_local_prov:
            specials.append(("📂 本地选择: " + Path(self.ios_local_prov).name, "local"))
        return specials

    def refresh_ios_profiles(self):
        if not IOS or self.platform.get() != "ios":
            return
        def work():
            try:
                total = IOS.list_profiles(include_expired=True, include_ineligible=True)
                ps = IOS.list_profiles()
            except Exception as e:
                self.log(f"[-] 扫描描述文件失败: {e}")
                return
            self.ios_profiles = ps
            def upd():
                disp = [label for label, _k in self._prov_specials()]
                disp += [self._ios_provision_display(pr) for pr in ps]
                self.ios_prov_box.config(values=disp)
                skipped = len(total) - len(ps)
                self.log(f"[+] 描述文件 {len(ps)} 个可用"
                         + (f" (已过滤 {skipped} 个无效/过期)" if skipped else ""))
                self.ios_prov_box.current(0)
                self.on_ios_provision_selected()
            self.root.after(0, upd)
        threading.Thread(target=work, daemon=True).start()

    def on_ios_provision_selected(self):
        idx = self.ios_prov_box.current()
        if idx < 0:
            return
        specials = self._prov_specials()
        if idx < len(specials):
            key = specials[idx][1]
            if key == "embedded":
                self.use_embedded_provision()
            else:
                self._apply_provision(self.ios_local_prov, source="📂 本地选择")
            return
        list_idx = idx - len(specials)
        if list_idx < len(self.ios_profiles):
            self._apply_provision(self.ios_profiles[list_idx]["path"])

    def use_embedded_provision(self):
        """提取安装包内嵌描述文件并应用"""
        if self.platform.get() != "ios":
            return
        src = self.hap_path
        if not src:
            self.log("[-] 请先拖入 .ipa 安装包再使用「安装包自带」")
            self.ios_prov_var.set("")
            return
        p = Path(src)
        if p.suffix.lower() not in (".ipa", ".xcarchive"):
            self.log(f"[-] 安装包 {p.name} 不是 .ipa / .xcarchive, 无内嵌描述文件")
            self.ios_prov_var.set("")
            return
        try:
            got = IOS.get_embedded_provision(p)
        except Exception as e:
            self.log(f"[-] 提取内嵌描述文件失败: {e}")
            self.ios_prov_var.set("")
            return
        if not got:
            self.log(f"[-] {p.name} 内没有 embedded.mobileprovision")
            self.ios_prov_var.set("")
            return
        self.ios_embedded_prov = str(got)
        self.log(f"[+] 已提取安装包内嵌描述文件: {got}")
        self._apply_provision(str(got), source="📦 安装包自带")

    def _apply_provision(self, path, source=None):
        self.ios_prov_path = path
        try:
            prov = IOS.read_mobileprovision(path)
        except Exception as e:
            self.log(f"[-] 描述文件解析失败: {e}")
            return
        appid = (prov.get("Entitlements") or {}).get("application-identifier", "")
        team = prov.get("TeamIdentifier")
        tag = f"[{source}] " if source else ""
        self.log(f"[+] {tag}描述文件: {IOS.provision_summary(prov)}  AppID={appid}"
                 + (f"  Team={team[0]}" if team else "")
                 + f"  设备数={len(prov.get('ProvisionedDevices') or [])}")
        bid = appid.split(".", 1)[1] if "." in appid and appid != "" else ""
        if bid and not bid.endswith("*") and hasattr(self, "ios_bundle_var"):
            cur = self.ios_bundle_var.get().strip()
            if not cur:
                self.ios_bundle_var.set(bid)
                self.log(f"    BundleID 建议填入: {bid}")

    def current_provision_source(self):
        """当前选中描述文件的 (显示名, 路径); 无选中返回 None"""
        path = self.ios_prov_path
        if not path:
            return None
        idx = self.ios_prov_box.current()
        specials = self._prov_specials()
        if 0 <= idx < len(specials):
            return (specials[idx][0], path)
        list_idx = idx - len(specials)
        if 0 <= list_idx < len(self.ios_profiles):
            return (self.ios_profiles[list_idx]["name"], path)
        return (Path(path).name, path)

    def show_ios_provision_detail(self):
        cur = self.current_provision_source()
        if not cur:
            messagebox.showinfo("描述文件详情", "尚未选择描述文件")
            return
        disp, path = cur
        try:
            prov = IOS.read_mobileprovision(path)
        except Exception as e:
            messagebox.showerror("描述文件详情", f"解析失败: {e}\n路径: {path}")
            return
        ent = prov.get("Entitlements") or {}
        exp = prov.get("ExpirationDate")
        created = prov.get("CreationDate")
        fmt = lambda d: d.strftime("%Y-%m-%d %H:%M") if hasattr(d, "strftime") else "—"
        ptype = "开发 (development)" if ent.get("get-task-allow") else "发布 (distribution)"
        teams = prov.get("TeamIdentifier") or []
        appid = ent.get("application-identifier", "—")
        devs = prov.get("ProvisionedDevices") or []
        aps = ent.get("aps-environment")

        win = tk.Toplevel(self.root)
        win.title("描述文件详情")
        win.transient(self.root)
        win.resizable(True, True)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        rows = [
            ("名称", str(prov.get("Name", "—"))),
            ("类型", ptype),
            ("AppID", appid),
            ("TeamID", ", ".join(teams) if teams else "—"),
            ("设备数", str(len(devs))),
            ("创建时间", fmt(created)),
            ("到期时间", fmt(exp)),
        ]
        if aps:
            rows.append(("推送环境", str(aps)))
        for i, (k, v) in enumerate(rows):
            ttk.Label(frm, text=f"{k}:").grid(row=i, column=0, sticky="ne", padx=(0, 10), pady=2)
            ttk.Label(frm, text=v, wraplength=380, justify="left").grid(row=i, column=1, sticky="w", pady=2)
        r = len(rows)
        ttk.Label(frm, text="路径:").grid(row=r, column=0, sticky="ne", padx=(0, 10), pady=2)
        path_lbl = ttk.Label(frm, text=str(path), wraplength=380, justify="left",
                             style="Sub.TLabel")
        path_lbl.grid(row=r, column=1, sticky="w", pady=2)

        btns = ttk.Frame(frm)
        btns.grid(row=r + 1, column=0, columnspan=2, sticky="we", pady=(12, 0))
        def reveal():
            subprocess.run(["open", "-R", str(path)])
        def show_devices():
            if not devs:
                messagebox.showinfo("授权设备", "该描述文件不包含设备列表 (发布/企业类型)", parent=win)
                return
            top = tk.Toplevel(win)
            top.title(f"授权设备 ({len(devs)})")
            txt = tk.Text(top, width=44, height=min(24, 6 + len(devs)))
            txt.pack(fill="both", expand=True, padx=8, pady=8)
            txt.insert("1.0", "\n".join(devs))
            txt.config(state="disabled")
        ttk.Button(btns, text="📂 打开所在路径", command=reveal).pack(side="left")
        if devs:
            ttk.Button(btns, text="📱 授权设备", command=show_devices).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="right")

    def pick_ios_provision(self):
        p = filedialog.askopenfilename(
            title="选择描述文件",
            filetypes=[("Provision Profile", "*.mobileprovision *.provisionprofile"), ("All", "*.*")])
        if not p:
            return
        try:
            prov = IOS.read_mobileprovision(p)
        except Exception as e:
            self.log(f"[-] 描述文件解析失败: {e}")
            return
        self.ios_local_prov = p
        vals = [label for label, _k in self._prov_specials()]
        vals += [self._ios_provision_display(pr) for pr in self.ios_profiles]
        self.ios_prov_box.config(values=vals)
        self.ios_prov_box.current(1)
        self._apply_provision(p, source="📂 本地选择")

    def pick_file(self):
        ios = self.platform.get() == "ios"
        if ios:
            p = filedialog.askopenfilename(
                title="选择 ipa / app / xcarchive",
                filetypes=[("iOS App", "*.ipa *.app *.xcarchive"), ("All", "*.*")])
        else:
            p = filedialog.askopenfilename(
                title="选择 HAP / App Pack",
                filetypes=[("HAP / App", "*.hap *.app *.hsp"), ("All", "*.*")])
        if p:
            self.set_file(p)

    def on_drop(self, event):
        p = event.data.strip().strip("{}")
        if p:
            self.set_file(p)

    def set_file(self, path):
        p = Path(path)
        suffix = p.suffix.lower()
        if suffix in (".ipa", ".xcarchive") and self.platform.get() != "ios":
            self.log(f"[*] 检测到 {suffix}, 自动切换到 iOS 模式")
            self.platform.set("ios")
            self.on_platform_change()
        elif suffix in (".hap", ".hsp") and self.platform.get() == "ios":
            self.log("[*] 检测到 .hap/.hsp, 自动切换到鸿蒙模式")
            self.platform.set("harmony")
            self.on_platform_change()
        self.hap_path = path
        if self.platform.get() == "ios":
            self.set_file_ios(path)
            return
        try:
            self.meta = H.read_hap_meta(path)
            m = self.meta["modules"][0] if self.meta["modules"] else {}
            size_mb = self.meta.get("size", 0) / 1024 / 1024
            signed = "已签名" if self.meta.get("signed") else "未签名"
            dot = self.C["success"] if self.meta.get("signed") else self.C["sub"]
            self.info.config(
                text=f"{self._ellipsize(os.path.basename(path), 34)}   "
                     f"{m.get('bundleName')}  v{m.get('versionName')}  "
                     f"{size_mb:.1f} MB",
                foreground=self.C["text"])
            self.sig_hint.config(text=signed)
            self.sig_dot.config(foreground=dot)
            self.log(f"[+] 已选择 {path}\n    bundle={m.get('bundleName')} "
                     f"v{m.get('versionName')} ({size_mb:.1f} MB, {signed})")
        except Exception as e:
            self.info.config(text=f"解析失败: {e}")
            self.log(f"[-] 解析失败: {e}")
        try:
            target = H.resolve_hap(path) if path.endswith(".app") else path
            si = H.read_sign_info(target)
            self._set_sig(sig_summary(si), H.format_sign_info(si), si.get("signed"))
        except Exception as e:
            self._set_sig("解析失败", f"签名信息解析失败: {e}", False)

    def set_file_ios(self, path):
        p = Path(path)
        if p.suffix.lower() not in (".ipa", ".app", ".xcarchive"):
            self.info.config(text=f"iOS 模式仅支持 .ipa / .app / .xcarchive: {p.name}")
            self.log(f"[-] iOS 模式仅支持 .ipa / .app / .xcarchive, 收到 {p.name}")
            return
        try:
            info = IOS.read_sign_info(p)
            bundle, ver = "", "?"
            tmp = None
            if p.suffix.lower() == ".xcarchive":
                app_dir = IOS.find_app_in_xcarchive(p)
            elif p.suffix == ".ipa":
                tmp = tempfile.TemporaryDirectory(prefix="iosgui_")
                app_dir = IOS.extract_ipa(p, tmp.name, keep_signature=True)
            else:
                app_dir = p
            with open(Path(app_dir) / "Info.plist", "rb") as f:
                pl = plistlib.load(f)
            bundle = pl.get("CFBundleIdentifier", "")
            ver = pl.get("CFBundleShortVersionString", "")
            disp = pl.get("CFBundleDisplayName") or pl.get("CFBundleName") or ""
            self.ios_meta = {"bundle": bundle, "version": str(ver), "name": str(disp)}
            if tmp:
                tmp.cleanup()
            self.info.config(text=f"{self._ellipsize(p.name, 38)}  "
                                  f"bundle: {bundle}  版本: {ver}")
            self.log(f"[+] 已选择 {path}\n    bundle={bundle}")
            self._set_sig(info["summary"], IOS.format_sign_info(info), info["signed"])
        except Exception as e:
            self.info.config(text=f"解析失败: {e}")
            self.log(f"[-] 解析失败: {e}")
            self.ios_meta = None
            return
        # 换包后「安装包自带」选中态自动重新提取
        vals = list(self.ios_prov_box.cget("values"))
        if vals and self.ios_prov_box.current() == 0:
            self.use_embedded_provision()
        else:
            self._auto_match_provision(bundle)

    def _auto_match_provision(self, bundle):
        """按 bundle-id + 当前身份自动匹配描述文件 (仅建议, 不强制切换)。"""
        ident = self.ios_identity_var.get().strip()
        if not bundle or not ident or not IOS:
            return
        def work():
            try:
                path, detail = IOS.auto_select_provision(bundle, ident)
            except Exception as e:
                self.root.after(0, lambda: self.log(f"[-] profile 自动匹配失败: {e}"))
                return
            def done():
                if path:
                    self.log(f"[*] 建议 profile: {detail}\n    {path}\n"
                             f"    (下拉选择即生效; 安装包自带优先, 手选不受影响)")
                else:
                    self.log(f"[!] {detail}")
            self.root.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _load_token(self):
        if H.TOKEN_FILE.exists():
            return H.TOKEN_FILE.read_text().strip()
        return os.environ.get("HUAWEI_TOKEN", "")

    def _clear_credentials(self):
        self.token_var.set("")
        self.cert_name.set("")
        H.clear_credentials()
        self._set_sig("—", "(Token 已过期，证书/Token 信息已清除)", False)
        self.sig_toggle.config(state="disabled")

    def _on_token_expired(self):
        self._clear_credentials()
        messagebox.showwarning("登录已过期", "Token 已过期/无效，请重新登录。\n已清除证书与 Token 信息。")

    def check_token(self, notify=False):
        tok = self.token_var.get().strip()
        uid = self.uid_var.get().strip()
        if not tok:
            if notify:
                self.log("[-] 未填写 Token")
            return

        def work():
            try:
                ok, why = H.check_token(tok, uid, self.team_id())
            except Exception as e:
                ok, why = True, f"检查异常: {e}"
            if ok:
                if notify:
                    self.log(f"[+] Token 有效: {why}")
                return
            self.log(f"[-] Token 已过期/无效: {why}")
            self.root.after(0, self._on_token_expired)
        threading.Thread(target=work, daemon=True).start()

    def team_id(self):
        name = self.team_var.get()
        for n, i in self.teams:
            if n == name:
                return i
        return self.teams[0][1]

    def refresh_teams(self):
        tok = self.token_var.get().strip()
        uid = self.uid_var.get().strip()
        if not tok:
            self.log("[-] 先填 token 再刷新团队")
            return
        def work():
            try:
                j, _ = H.http_req(H.URL_TEAM_LIST, "GET", H.hw_headers(tok, uid))
                ts = (j or {}).get("teams", [])
                if ts:
                    self.teams = [(f"{t.get('name')} ({'企业' if t.get('userType')==2 else '个人'})",
                                   str(t.get("id"))) for t in ts]
                    names = [n for n, _ in self.teams]
                    def upd():
                        self.team_box.config(values=names)
                        self.team_var.set(names[0])
                    self.root.after(0, upd)
                    self.log(f"[+] 团队已刷新 ({len(ts)}): {[t.get('name') for t in ts]}")
                else:
                    self.log(f"[-] 团队为空: {j}")
            except Exception as e:
                self.log(f"[-] 刷新团队失败: {e}")
        threading.Thread(target=work, daemon=True).start()

    def do_login(self):
        self.log("[*] 打开浏览器登录 (回环回调 127.0.0.1:8888) ...")
        self.log("    提示: 若需切换账号, 请先在浏览器退出华为账号再登录")
        def work():
            class A:
                port = 8888
                appid = "1007"
                code = "20698961dd4f420c8b44f49010c6f0cc"
                version = "0.0.0"
                timeout = 300
            try:
                H._cloud_login(A)
            except SystemExit as e:
                if e.code:
                    self.log(f"[-] 登录未完成 (code={e.code})")
                return
            except Exception as e:
                self.log(f"[-] 登录失败: {e}")
                return
            def fill():
                if H.TOKEN_FILE.exists():
                    self.token_var.set(H.TOKEN_FILE.read_text().strip())
                if H.UID_FILE.exists():
                    self.uid_var.set(H.UID_FILE.read_text().strip())
                self.log(f"[+] 已自动填入 oauth2token 和 uid={self.uid_var.get()}")
                self.refresh_teams()
            self.root.after(0, fill)
        threading.Thread(target=work, daemon=True).start()

    def _set_busy(self, busy):
        self.busy = busy
        self.go.config(state="disabled" if busy else "normal")
        if busy:
            self.go.config(text="⏳  处理中…")
            self.progress.pack(fill="x", before=self.go, pady=(0, 2))
            self.progress.start(12)
        else:
            self.go.config(text="🚀  一键安装并启动")
            self.progress.stop()
            self.progress.pack_forget()

    def one_click(self, install=True):
        if self.busy:
            return
        if not self.hap_path:
            messagebox.showwarning("提示", "请先拖入或选择文件")
            return
        if self.platform.get() == "ios":
            if not IOS:
                messagebox.showwarning("提示", "ios_sign.py 不可用")
                return
            if not self.ios_identity_var.get().strip():
                messagebox.showwarning("提示", "请先刷新并选择签名证书")
                return
            snap = {
                "identity": self.ios_identity_var.get().strip(),
                "prov": self.ios_prov_path,
                "bundle": self.ios_bundle_var.get().strip() or None,
                "min_os": self.ios_min_os_var.get().strip() or None,
                "path": self.hap_path,
                "device": self._ios_device_key(),
            }
            self._set_busy(True)
            threading.Thread(target=self._pipeline_ios, args=(install, snap), daemon=True).start()
            return
        if not self.token_var.get().strip():
            messagebox.showwarning("提示", "请填写 oauth2token (或点登录)")
            return
        self._set_busy(True)
        threading.Thread(target=self._pipeline, args=(install,), daemon=True).start()

    def _pipeline_ios(self, install=True, snapshot=None):
        try:
            if snapshot is None:
                # Tk 变量读操作必须在主线程; 线程入口先快照 (one_click 已在主线程取过则直接用)
                snapshot = {
                    "identity": self.ios_identity_var.get().strip(),
                    "prov": self.ios_prov_path,
                    "bundle": self.ios_bundle_var.get().strip() or None,
                    "min_os": self.ios_min_os_var.get().strip() or None,
                    "path": self.hap_path,
                    "device": self._ios_device_key(),
                }
            src = Path(snapshot["path"])
            identity = snapshot["identity"]
            prov = snapshot["prov"] or None
            bundle_override = snapshot["bundle"]
            min_os = snapshot["min_os"]
            self.log(f"=== iOS 开始: {src.name} | 证书 {identity[:60]} | "
                     f"描述文件 {'有' if prov else '无'} | min-os {min_os or '不降'} ===")

            # profile 兼容性预检: 固定 bundle id 的 profile 与设备上已装的同名应用
            # (通配 entitlement) 会冲突, 报 MismatchedApplicationIdentifierEntitlement
            if prov and src.suffix.lower() in (".ipa", ".xcarchive"):
                try:
                    with tempfile.TemporaryDirectory(prefix="iosgui_pre_") as _td:
                        if src.suffix.lower() == ".xcarchive":
                            _app = IOS.find_app_in_xcarchive(src)
                        else:
                            _app = IOS.extract_ipa(src, _td, keep_signature=True)
                        pkg_bid = IOS.read_bundle_id(_app)
                    prov_appid = (IOS.read_mobileprovision(prov).get("Entitlements") or {}) \
                        .get("application-identifier", "")
                    prof_bid = prov_appid.split(".", 1)[1] if "." in prov_appid else ""
                    if prof_bid and "*" not in prof_bid and pkg_bid and prof_bid != pkg_bid:
                        self.log(f"[!] 注意: 描述文件绑定 bundle id '{prof_bid}', "
                                 f"与包内 '{pkg_bid}' 不同; 签名后包将变为 '{prof_bid}'. "
                                 f"若设备上已装旧版, 建议先卸载或改用通配 profile")
                except Exception:
                    pass

            workdir = Path.home() / "Library/Caches/hap_installer/gui"
            workdir.mkdir(parents=True, exist_ok=True)

            self.log("1) 重签名")
            if src.suffix == ".ipa":
                out = workdir / f"{src.stem}-signed.ipa"
                IOS.sign_ipa(src, out, identity, prov,
                             bundle_id=bundle_override, min_os=min_os)
            else:
                out = IOS.sign_app_source(src, workdir, identity, prov,
                                          bundle_id=bundle_override, min_os=min_os)
            self._ui(self._set_out, str(out))
            self.log(f"   产物: {out}")

            self.log("2) 校验")
            ok, detail = IOS.verify(out)
            self.log("   " + ("[+] 校验通过" if ok else "[-] 校验失败") + "\n   " +
                     detail.splitlines()[0] if detail else "   (无输出)")
            if not ok:
                raise RuntimeError("签名校验失败")

            if not install:
                self.log(f"[+] 完成(仅签名): {out}")
                return

            self.log("3) 安装到设备")
            IOS.install_to_device(out, device_id=snapshot.get("device"))
            self.log("4) 完成 (设备上手动打开应用即可)")
            self.log("[+] iOS 流程完成")
        except Exception as e:
            self.log(f"[-] 失败: {e}")
            self.log(traceback.format_exc())
        finally:
            self._ui(self._set_busy, False)

    def _pipeline(self, install):
        try:
            t = H.Tools()
            bundle = self.meta["modules"][0]["bundleName"]
            ability = self.meta["modules"][0].get("mainElement") or "EntryAbility"
            tok = self.token_var.get().strip()
            uid = self.uid_var.get().strip()
            team = self.team_id()
            if str(team) == H.ENTERPRISE_TEAM:
                raise RuntimeError(f"团队账户 {team} 受保护：禁止增删其证书/Profile。请选个人账户。")
            cert_name = self.cert_name.get().strip() or ("auto-debug-" + time.strftime("%Y%m%d%H%M%S"))
            self.log(f"=== 开始: {bundle} | 团队 {team}(个人) | 证书 {cert_name} ===")

            ok, why = H.check_token(tok, uid, team)
            if not ok:
                self.log(f"[-] Token 已过期/无效: {why}")
                self.root.after(0, self._on_token_expired)
                return

            Hh = H.hw_headers(tok, uid, team)
            os.environ["HAP_CLOUD_PROXY"] = self.proxy_var.get().strip()
            self.log(f"   云代理: {os.environ['HAP_CLOUD_PROXY'] or '直连'}")
            workdir = Path.home() / "Library/Caches/hap_installer/gui"
            workdir.mkdir(parents=True, exist_ok=True)

            self.log("1) device/list")
            dev, _ = H.http_req(H.URL_DEVICE_LIST + "?start=1&pageSize=100&encodeFlag=0", "GET", Hh)
            if dev is None:
                hint = H.LAST_NET_ERROR or "未知错误"
                raise RuntimeError(
                    f"device/list 请求失败 [{hint}]。直连异常时可尝试在「云代理」填 "
                    f"http://127.0.0.1:1087 (本机 privoxy) 后重试")
            dev_ids = [d["id"] for d in (dev or {}).get("list", [])]
            self.log(f"   设备 {len(dev_ids)} 个")

            self.log("2) cert/list")
            cl, _ = H.http_req(H.URL_CERT_LIST, "GET", Hh)
            if cl is None:
                hint = H.LAST_NET_ERROR or "未知错误"
                raise RuntimeError(
                    f"cert/list 请求失败 [{hint}]。直连异常时可尝试在「云代理」填 "
                    f"http://127.0.0.1:1087 (本机 privoxy) 后重试")
            certs = (cl or {}).get("certList", [])
            cert = next((c for c in certs if c.get("certName") == cert_name), None)
            if not cert:
                csr = H.find_csr(t)
                self.log(f"   无 '{cert_name}'，走华为申请流程 cert/add (csr={Path(csr).name})")
                r, _ = H.http_req(H.URL_CERT_ADD, "POST", Hh,
                                  {"certName": cert_name, "certType": 1, "csr": Path(csr).read_text()})
                code = (r or {}).get("ret", {}).get("code")
                if code in (0, None) and r is not None:
                    cl, _ = H.http_req(H.URL_CERT_LIST, "GET", Hh)
                    certs = (cl or {}).get("certList", [])
                    cert = next((c for c in certs if c.get("certName") == cert_name), None)
                else:
                    if r is None:
                        self.log("   (cert/add 请求本身失败: 网络/代理异常或响应非 JSON，"
                                 "检查 云代理 设置；http_req 的详细错误打印在系统控制台)")
                    self.log(f"   申请失败: {(r or {}).get('ret', {}).get('msg')}")
                    keystore = t.store_file("key.pem", required=False)
                    cert = H._pick_fallback_cert(certs, Hh, logger=self.log,
                                                 keystore=keystore, tools=t)
                    if not cert:
                        raise RuntimeError("所有候选证书均不可用 (过期/与内置私钥不配对/下载失败)，"
                                           "请按上方日志中的 cert-delete 命令删除过期或不配对的证书腾出配额后重试")
            if not cert:
                raise RuntimeError("找不到可用证书 (配额已满且无 auto-debug/xiaobai-debug)")
            self.log(f"   用证书 {cert['certName']} id={cert['id']}")

            self.log("3) reapply 下载证书")
            rp, _ = H.http_req(H.URL_OBJECT_REAPPLY, "POST", Hh, {"sourceUrls": cert["certObjectId"]})
            cer = workdir / f"{bundle}_debug.cer"
            H.download(((rp or {}).get("urlsInfo") or [{}])[0].get("newUrl"), str(cer))

            self.log("4) provision/add 生成 Profile")
            acl_perms = H.get_hap_acl_permissions(self.hap_path)
            if acl_perms:
                self.log(f"   检测到受限 ACL 权限 {len(acl_perms)} 项, 将声明进 Profile: "
                         + ", ".join(p.split('.')[-1] for p in acl_perms))
            pa, _ = H.http_req(H.URL_PROVISION_ADD, "POST", Hh, {
                "provisionName": f"auto-debug_{bundle.replace('.', '_')}",
                "aclPermissionList": acl_perms, "deviceList": dev_ids,
                "certList": [cert["id"]], "packageName": bundle})
            purl = (pa or {}).get("provisionFileUrl")
            if not purl:
                raise RuntimeError(f"provision/add 失败: {pa}")
            p7b = workdir / f"{bundle}_debug.p7b"
            H.download(purl, str(p7b))

            self.log("5) 本地签名")
            key = t.store_file("key.pem")
            h = H.Hdc(t, self._device_key())
            if H.is_app_pack(self.hap_path):
                signed = H.sign_app_pack(t, self.hap_path, str(workdir), key, "", "xiaobai",
                                         str(cer), str(p7b))
                self.root.after(0, lambda p=str(signed): self._set_out(p))
                self.log(f"   已重签名 App Pack: {signed}")
                if not install:
                    self.log(f"[+] 完成(仅签名): {signed}")
                    return
                self.log("6) hdc 安装 App Pack (hap + hsp)")
                rc, out = h.install_packages(H.app_pack_module_files(signed), replace=True)
            else:
                src = H.resolve_hap(self.hap_path)
                signed = workdir / f"{bundle}-signed.hap"
                H.do_sign(t, src, str(signed), key, "", "xiaobai", str(cer), str(p7b))
                self.root.after(0, lambda p=str(signed): self._set_out(p))
                self.log(f"   已签名: {signed}")
                if not install:
                    self.log(f"[+] 完成(仅签名): {signed}")
                    return
                self.log("6) hdc 安装")
                rc, out = h.install(str(signed), replace=True)

            self.log("   " + (out or "").strip())
            if rc != 0 or "fail" in (out or "").lower():
                m_perm = re.search(r"PermissionName:\s*(ohos\.permission\.\S+)", out or "")
                if "9568332" in (out or "") or "sign info inconsistent" in (out or ""):
                    self.log(f"[*] 设备已装旧签名版本 (签名信息不一致) — 自动卸载后重装")
                    rc_u, out_u = h.uninstall(bundle)
                    self.log("   卸载: " + (out_u or "").strip().splitlines()[0][:80])
                    if H.is_app_pack(self.hap_path):
                        rc, out = h.install_packages(H.app_pack_module_files(signed), replace=True)
                    else:
                        rc, out = h.install(str(signed), replace=True)
                    self.log("   重装: " + (out or "").strip())
                    if rc != 0 or "fail" in (out or "").lower():
                        raise RuntimeError("卸载重装后仍失败")
                elif m_perm and "grant request permissions failed" in (out or ""):
                    missing = m_perm.group(1)
                    self.log(f"[*] 检测到未授权的受限权限: {missing}")
                    if missing in acl_perms:
                        raise RuntimeError(
                            f"权限 {missing} 已声明进 Profile 仍被拒 — 请在 AGC 手工确认该权限可申请")
                    self.log("[*] 自动重发 Profile (补充 ACL) 并重签重装 ...")
                    acl_perms = sorted(set(acl_perms) | {missing})
                    pa, _ = H.http_req(H.URL_PROVISION_ADD, "POST", Hh, {
                        "provisionName": f"auto-debug_{bundle.replace('.', '_')}",
                        "aclPermissionList": acl_perms, "deviceList": dev_ids,
                        "certList": [cert["id"]], "packageName": bundle})
                    purl = (pa or {}).get("provisionFileUrl")
                    if not purl:
                        raise RuntimeError(f"provision/add 失败: {pa}")
                    H.download(purl, str(p7b))
                    if H.is_app_pack(self.hap_path):
                        signed = H.sign_app_pack(t, self.hap_path, str(workdir), key, "", "xiaobai",
                                                 str(cer), str(p7b))
                        mods = H.app_pack_module_files(signed)
                        rc, out = h.install_packages(mods, replace=True)
                    else:
                        src = H.resolve_hap(self.hap_path)
                        H.do_sign(t, src, str(signed), key, "", "xiaobai", str(cer), str(p7b))
                        rc, out = h.install(str(signed), replace=True)
                    self.log("   重试安装: " + (out or "").strip())
                    if rc != 0 or "fail" in (out or "").lower():
                        raise RuntimeError(f"重试后仍失败 (ACL 已补 {missing})")
                else:
                    raise RuntimeError("安装失败")

            self.log("7) 启动")
            h.shell_out("aa", "start", "-a", ability, "-b", bundle)
            self.log(f"[+] 完成: {bundle} 已安装并启动")
        except H.CloudAuthError as e:
            self.log(f"[-] Token 已过期/无效: {e}")
            self.log("    请重新登录 DevEco 获取新 Token 后重试 (浏览器登录或抓包)")
            self.root.after(0, self._on_token_expired)
        except Exception as e:
            self.log(f"[-] 失败: {e}")
            self.log(traceback.format_exc())
        finally:
            self.root.after(0, lambda: self._set_busy(False))

    def install_only(self):
        if not self.hap_path:
            messagebox.showwarning("提示", "请先选择文件")
            return
        if self.busy:
            return
        if self.platform.get() == "ios":
            self._set_busy(True)
            def ios_work():
                try:
                    self.log(f"安装 {self.hap_path} ...")
                    self.root.after(0, lambda p=self.hap_path: self._set_out(p))
                    IOS.install_to_device(self.hap_path)
                    self.log("[+] 已发起安装 (设备上手动打开应用即可)")
                except Exception as e:
                    self.log(f"[-] {e}")
                finally:
                    self.root.after(0, lambda: self._set_busy(False))
            threading.Thread(target=ios_work, daemon=True).start()
            return
        self._set_busy(True)
        def work():
            try:
                t = H.Tools()
                h = H.Hdc(t, self._device_key())
                bundle = (self.meta or {}).get("modules", [{}])[0].get("bundleName")
                ability = (self.meta or {}).get("modules", [{}])[0].get("mainElement") or "EntryAbility"
                if H.is_app_pack(self.hap_path):
                    mods = H.app_pack_module_files(self.hap_path)
                    self.log(f"安装 App Pack ({len(mods)} 模块) {self.hap_path} ...")
                    self.root.after(0, lambda p=self.hap_path: self._set_out(p))
                    rc, out = h.install_packages(mods, replace=True)
                    self.log("   " + (out or "").strip())
                    if rc == 0 and "fail" not in (out or "").lower() and bundle:
                        h.shell_out("aa", "start", "-a", ability, "-b", bundle)
                        self.log(f"[+] 已安装并启动 {bundle}")
                    return
                self.log(f"安装 {self.hap_path} ...")
                self.root.after(0, lambda p=self.hap_path: self._set_out(p))
                rc, out = h.install(self.hap_path, replace=True)
                self.log("   " + (out or "").strip())
                if rc == 0 and "fail" not in (out or "").lower() and bundle:
                    h.shell_out("aa", "start", "-a", ability, "-b", bundle)
                    self.log(f"[+] 已安装并启动 {bundle}")
            except Exception as e:
                self.log(f"[-] {e}")
            finally:
                self.root.after(0, lambda: self._set_busy(False))
        threading.Thread(target=work, daemon=True).start()


class MirrorWindow:
    """设备投屏窗口: 截屏轮询。鸿蒙支持点击/滑动/按键回传; iOS 系统无输入注入, 只读。"""

    POLL_MS = {"harmony": 300, "ios": 300}

    def __init__(self, app, platform, device_key):
        self.app = app
        self.platform = platform          # harmony | ios
        self.device_key = device_key
        self.capture = None
        self.input = None
        self._running = False
        self._photo = None
        self._fps_cnt = 0
        self._fps_t0 = time.time()
        self._frame_q = queue.Queue()
        self._shot_png = Path(tempfile.mkdtemp(prefix="hapdbg_gui_mirror_")) / "frame.png"

        w = tk.Toplevel(app.root)
        w.title(f"📱 设备投屏 · {device_key}")
        w.geometry("420x760")
        w.configure(bg=app.C["bg"])
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.win = w

        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        self.lbl_status = ttk.Label(bar, text="● 连接中", foreground=app.C["sub"])
        self.lbl_status.pack(side="left")
        self.lbl_fps = ttk.Label(bar, text="—", style="Sub.TLabel")
        self.lbl_fps.pack(side="left", padx=(10, 0))
        if platform == "harmony":
            # 清晰度 (仅视频流模式可见; 档位定义在 DT.HarmonyVideoStream)
            self._quality_var = tk.StringVar(value=DT.HarmonyVideoStream.SCALE_OPTIONS[0][0])
            self._quality_cb = ttk.Combobox(bar, textvariable=self._quality_var,
                                            width=4, state="readonly",
                                            values=[n for n, _ in
                                                    DT.HarmonyVideoStream.SCALE_OPTIONS])
            self._quality_cb.pack(side="left", padx=(10, 0))
            self._quality_cb.bind("<<ComboboxSelected>>",
                                  lambda e: self._on_quality_change())
        ttk.Button(bar, text="⌨️ Home", command=lambda: self.send_key("home")).pack(side="right")
        ttk.Button(bar, text="⬅️ 返回", command=lambda: self.send_key("back")).pack(side="right", padx=(0, 4))

        self.canvas = tk.Label(w, bg="#000", anchor="center")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=4)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<ButtonPress-1>", self._on_press_xy)
        self.canvas.bind("<ButtonRelease-1>", self._on_release_swipe)
        self.bind_scroll()

        foot = ttk.Frame(w)
        foot.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(foot, text="📸 截图保存", command=self.save_screenshot).pack(side="left")
        ttk.Label(foot, text="(iOS 系统无输入注入, 只读投屏)" if platform == "ios"
                  else "点击画面 = 触摸设备, 拖拽 = 滑动",
                  style="Sub.TLabel").pack(side="left", padx=8)

        self.start_capture()
        self._poll()

    def bind_scroll(self):
        pass

    def start_capture(self):
        try:
            if self.platform == "harmony":
                hdc_bin = H.Tools().tool("hdc")
                wh = self._probe_harmony_wh()
                try:
                    # 视频流 (Hypium agent, DevEco 同款): 帧率远超 snapshot 轮询;
                    # 输入走同 socket RPC (uitest CLI 会话会顶掉录屏流)
                    self.capture = DT.HarmonyVideoStream(hdc_bin, self.device_key,
                                                         screen_wh=wh)
                    self.capture.start()
                    self.input = self.capture
                    self.app.log("[+] 投屏: Hypium agent 视频流")
                except Exception as e:
                    # 无 DevEco/agent 推送失败等 → 回退 snapshot 双循环轮询
                    self.app.log(f"[-] 视频流不可用: {e}")
                    self.app.log("[-] 回退 snapshot 轮询模式")
                    try:
                        self.capture.cleanup()  # 清掉可能已推送的 agent/fport
                    except Exception:
                        pass
                    self.capture = DT.HarmonyScreenStream(hdc_bin, self.device_key)
                    self.capture.start()
                    self.input = DT.HarmonyInput(hdc_bin, self.device_key, screen_wh=wh)
                    if hasattr(self, "_quality_cb"):   # 轮询模式无档位可切
                        self._quality_cb.pack_forget()
            else:
                self.capture = DT.IOSScreen(self.device_key)
        except Exception as e:
            self.app.log(f"[-] 投屏初始化失败: {e}")
            self.lbl_status.config(text="● 初始化失败", foreground=self.app.C["error"])
            return
        self._running = True
        self.lbl_status.config(text="● 投屏中", foreground=self.app.C["success"])

    def _probe_harmony_wh(self):
        rc, out, _ = DT._run([H.Tools().tool("hdc")] + (["-t", self.device_key] if self.device_key else [])
                             + ["shell", "snapshot_display", "-f", DT.SHOT_REMOTE], timeout=15)
        m = re.search(r"width\s+(\d+),\s*height\s+(\d+)", out or "")
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return (1216, 2688)

    def _poll(self):
        if not self._running:
            return
        def work():
            ok, ms, err = self.capture.grab(self._shot_png)
            self._frame_q.put((ok, ms, err))
        threading.Thread(target=work, daemon=True).start()
        # 视频流自带 POLL_HINT_MS (帧持续到达, 高频 drain); 其余按平台间隔
        ms = getattr(self.capture, "POLL_HINT_MS", None) \
            or self.POLL_MS.get(self.platform, 800)
        self.win.after(ms, self._drain_frame)

    def _drain_frame(self):
        if not self._running:
            return
        try:
            ok, ms, err = self._frame_q.get_nowait()
        except queue.Empty:
            self.win.after(60, self._drain_frame)
            return
        self._frame_done(ok, ms, err)

    def _frame_done(self, ok, ms, err):
        if not self._running:
            return
        if ok:
            try:
                disp = self._make_display_frame()
                img = tk.PhotoImage(file=str(disp))
                self._photo = img
                self.canvas.config(image=img, text="")
                self._img_wh = self._src_wh or (img.width(), img.height())
                self._fps_cnt += 1
                now = time.time()
                if now - self._fps_t0 >= 2.0:
                    fps = self._fps_cnt / (now - self._fps_t0)
                    self.lbl_fps.config(text=f"{fps:.1f} fps · {ms}ms")
                    self._fps_cnt = 0
                    self._fps_t0 = now
                self.lbl_status.config(text="● 投屏中", foreground=self.app.C["success"])
            except Exception as e:
                self.canvas.config(text=f"渲染失败: {e}")
        elif err:
            self.lbl_status.config(text="● 重连中", foreground=self.app.C["accent"])
            if hasattr(self.capture, "restart_session"):
                # 视频流: 断线整链重连 (带冷却, 静止画面不会走到这)
                threading.Thread(target=self.capture.restart_session, daemon=True).start()
            else:
                self.lbl_status.config(text="● 抓帧失败", foreground=self.app.C["error"])
                self.app.log(f"[!] 投屏抓帧失败: {err}")
        # False 且无 err = 静止画面去重轮次, 状态不变
        if self._running:
            self.win.after(30, self._poll)

    def _make_display_frame(self):
        """显示帧: sips 按画布重采样。Tk>=8.6 用 PNG (24bit 真彩, 清晰);
        Tk 8.5 (PNG 不支持) 回退 GIF (256 色量化, 较模糊)。"""
        cw = max(self.canvas.winfo_width(), 200)
        ch = max(self.canvas.winfo_height(), 300)
        src = str(self._shot_png)
        try:
            out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", src],
                                 capture_output=True, text=True, timeout=10).stdout
            w = int(re.search(r"pixelWidth:\s*(\d+)", out).group(1))
            h = int(re.search(r"pixelHeight:\s*(\d+)", out).group(1))
            self._src_wh = (w, h)
        except Exception:
            self._src_wh = None
            w = h = 0
        scale = min(cw / w, ch / h, 1.0) if w else 1.0
        png_out = self._shot_png.parent / "disp.png"
        gif_out = self._shot_png.parent / "disp.gif"
        args = ["sips", "-s", "format", "png"]
        if self._src_wh and scale < 0.98:
            args += ["-Z", str(max(1, int(max(w, h) * scale)))]
        args += [src, "--out", str(png_out)]
        subprocess.run(args, capture_output=True, timeout=10)
        if png_out.exists() and tk.TkVersion >= 8.6:
            return str(png_out)
        gif_args = ["sips", "-s", "format", "gif"]
        if self._src_wh and scale < 0.98:
            gif_args += ["-Z", str(max(1, int(max(w, h) * scale)))]
        gif_args += [src, "--out", str(gif_out)]
        subprocess.run(gif_args, capture_output=True, timeout=10)
        return str(gif_out) if gif_out.exists() else src

    def _canvas_ratio(self, event):
        wh = getattr(self, "_img_wh", None)
        if not wh:
            return None, None
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        scale = min(cw / wh[0], ch / wh[1])
        draw_w, draw_h = wh[0] * scale, wh[1] * scale
        ox, oy = (cw - draw_w) / 2, (ch - draw_h) / 2
        x = (event.x - ox) / max(draw_w, 1)
        y = (event.y - oy) / max(draw_h, 1)
        if 0 <= x <= 1 and 0 <= y <= 1:
            return x, y
        return None, None

    def _on_press_xy(self, event):
        self._press_pt = self._canvas_ratio(event)

    def _on_release_swipe(self, event):
        if self.platform != "harmony" or not self.input:
            return
        p0 = getattr(self, "_press_pt", (None, None))
        p1 = self._canvas_ratio(event)
        if p0[0] is None or p1[0] is None:
            return
        dx, dy = abs(p1[0] - p0[0]), abs(p1[1] - p0[1])
        if dx < 0.02 and dy < 0.02:
            return
        threading.Thread(target=lambda: self.input.swipe(*p0, *p1), daemon=True).start()

    def _on_click(self, event):
        if self.platform != "harmony" or not self.input:
            return
        x, y = self._canvas_ratio(event)
        if x is None:
            return
        threading.Thread(target=lambda: self.input.click(x, y), daemon=True).start()

    def _on_double(self, event):
        if self.platform != "harmony" or not self.input:
            return
        x, y = self._canvas_ratio(event)
        if x is None:
            return
        threading.Thread(target=lambda: self.input.double_click(x, y), daemon=True).start()

    def send_key(self, name):
        if self.platform != "harmony" or not self.input:
            if self.platform == "ios":
                self.app.log("[!] iOS 投屏只读, 无法发送按键 (系统无输入注入)")
            return
        threading.Thread(target=lambda: self.input.key(name), daemon=True).start()

    def _on_quality_change(self):
        """清晰度切换: 同 socket 重发捕获 (不动 daemon, 不断流)。"""
        c = getattr(self, "capture", None)
        if not c or not hasattr(c, "set_scale"):
            return
        scale = dict(DT.HarmonyVideoStream.SCALE_OPTIONS).get(self._quality_var.get())
        if scale:
            threading.Thread(target=c.set_scale, args=(scale,), daemon=True).start()

    def save_screenshot(self):
        src = self._shot_png
        if not src.exists():
            self.app.log("[-] 尚无截图帧")
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".png", initialfile=f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png",
            filetypes=[("PNG", "*.png")])
        if dest:
            import shutil as _sh
            _sh.copy2(src, dest)
            self.app.log(f"[+] 截图已保存: {dest}")

    def close(self):
        self._running = False
        if self.capture:
            try:
                self.capture.cleanup()
            except Exception:
                pass
        shutil.rmtree(self._shot_png.parent, ignore_errors=True)
        self.win.destroy()
        self.app.mirror_win = None


class DeviceLogWindow:
    """设备日志窗口: 运行日志流 (hilog/syslog) + 崩溃日志列表/拉取。"""

    MAX_LINES = 8000

    def __init__(self, app, platform, device_key):
        self.app = app
        self.platform = platform
        self.device_key = device_key
        self.proc = None
        self._q = queue.Queue()
        self._after_id = None

        w = tk.Toplevel(app.root)
        w.title(f"📜 设备日志 · {device_key}")
        w.geometry("980x620")
        w.configure(bg=app.C["bg"])
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.win = w

        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(bar, text="过滤:", style="Sub.TLabel").pack(side="left")
        self.filter_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.filter_var, width=24)
        e.pack(side="left", padx=(4, 8))
        e.bind("<KeyRelease>", lambda ev: self._apply_filter())
        self.level_var = tk.StringVar(value="全部")
        cb = ttk.Combobox(bar, textvariable=self.level_var, width=6, state="readonly",
                          values=["全部", "ERROR", "FATAL"] if platform == "harmony" else ["全部"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda ev: self._restart_stream())
        self.btn_stream = ttk.Button(bar, text="⏸ 暂停", command=self.toggle_stream)
        self.btn_stream.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="💾 保存日志", command=self.save_log).pack(side="right")
        ttk.Button(bar, text="🧹 清空", command=self._clear).pack(side="right", padx=4)

        self.txt = tk.Text(w, wrap="none", font=app.f_mono,
                           bg=app.C["box_bg"], fg=app.C["box_fg"],
                           insertbackground=app.C["box_fg"],
                           state="disabled", highlightthickness=1,
                           highlightbackground=app.C["drop_bd"])
        ys = ttk.Scrollbar(w, command=self.txt.yview, orient="vertical")
        xs = ttk.Scrollbar(w, command=self.txt.xview, orient="horizontal")
        self.txt.config(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.txt.pack(fill="both", expand=True, padx=(8, 0), pady=(0, 4))
        for tag, color in (("+", app.C["success"]), ("-", "#FF453A"), ("!", "#FFD60A"),
                           ("*", app.C["accent"])):
            self.txt.tag_configure(tag, foreground=color)
        self.txt.tag_configure("err", foreground="#FF453A")
        self.txt.tag_configure("dim", foreground=app.C["sub"])

        crashf = ttk.LabelFrame(w, text=" 💥 崩溃日志 ", padding=4)
        crashf.pack(fill="x", padx=8, pady=(0, 8))
        self.crash_list = ttk.Combobox(crashf, state="readonly")
        self.crash_list.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(crashf, text="🔄 刷新", command=self.refresh_crashes).pack(side="left")
        ttk.Button(crashf, text="⬇️ 拉取选中", command=self.pull_crash).pack(side="left", padx=4)
        ttk.Button(crashf, text="⬇️ 全部拉取", command=self.pull_all_crashes).pack(side="left", padx=(0, 4))
        ttk.Button(crashf, text="📂 打开目录", command=self.open_crash_dir).pack(side="left")
        self.crash_dir = Path.home() / "Downloads" / "hapdbg-crashes" / device_key.replace(":", "_")

        self._start_stream()
        self._drain()
        self.refresh_crashes()

    # ---- 运行日志流 ----
    def _log_cmd(self):
        if self.platform == "harmony":
            hdc_bin = H.Tools().tool("hdc")
            logs = DT.HarmonyLogs(hdc_bin, self.device_key)
            level = {"ERROR": "ERROR", "FATAL": "FATAL"}.get(self.level_var.get())
            return logs.hilog_args(level=level, app_only=True)
        return DT.IOSLogs(self.device_key).syslog_cmd()

    def _start_stream(self):
        try:
            cmd = self._log_cmd()
        except Exception as e:
            self.app.log(f"[-] 日志流启动失败: {e}")
            return
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1)
        except Exception as e:
            self._put(f"[-] 无法启动日志命令: {e}\n", "err")
            return
        self.btn_stream.config(text="⏸ 暂停")
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self._q.put(line)
        except Exception:
            pass

    def _drain(self):
        n = 0
        stopped = False
        while n < 400:
            try:
                line = self._q.get_nowait()
            except queue.Empty:
                break
            self._put(line)
            n += 1
        if self.proc is not None and self.proc.poll() is not None:
            stopped = True
        if self.win.winfo_exists():
            self._after_id = self.win.after(120, self._drain)
            if stopped and self._streaming_wanted():
                self._put("[*] 日志进程退出, 重启流\n", "dim")
                self._start_stream()

    def _streaming_wanted(self):
        return getattr(self, "_want_stream", True)

    def toggle_stream(self):
        if self.proc is not None and self.proc.poll() is None:
            self._want_stream = False
            self.proc.terminate()
            self.btn_stream.config(text="▶ 继续")
            self._put("[*] 已暂停\n", "dim")
        else:
            self._want_stream = True
            self._start_stream()

    def _restart_stream(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            time.sleep(0.2)
        if self._streaming_wanted():
            self._start_stream()

    def _put(self, line, tag=None):
        if self.filter_var.get().strip() and self.filter_var.get().strip().lower() not in line.lower():
            return
        t = tag or ("err" if re.search(r"\b(F|FATAL|ERROR|Error|panic|crash)\b", line) else None)
        self.txt.config(state="normal")
        self.txt.insert("end", line, t or ())
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > self.MAX_LINES:
            self.txt.delete("1.0", f"{lines - self.MAX_LINES}.0")
        self.txt.config(state="disabled")
        self.txt.see("end")

    def _apply_filter(self):
        pass

    def _clear(self):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.config(state="disabled")

    def save_log(self):
        dest = filedialog.asksaveasfilename(
            defaultextension=".log", initialfile=f"{self.platform}-log-{time.strftime('%Y%m%d-%H%M%S')}.log",
            filetypes=[("日志", "*.log *.txt")])
        if not dest:
            return
        with open(dest, "w") as f:
            f.write(self.txt.get("1.0", "end"))
        self.app.log(f"[+] 日志已保存: {dest}")

    # ---- 崩溃日志 ----
    def _crash_helper(self):
        if self.platform == "harmony":
            return DT.HarmonyLogs(H.Tools().tool("hdc"), self.device_key)
        return DT.IOSLogs(self.device_key)

    def refresh_crashes(self):
        def work():
            try:
                if self.platform == "harmony":
                    return self._crash_helper().crash_list(), ""
                return [], ""
            except Exception as e:
                return [], str(e)
        threading.Thread(target=lambda: self._crash_done(*work()), daemon=True).start()

    def _crash_done(self, names, err):
        if not self.win.winfo_exists():
            return
        if err:
            self.app.log(f"[-] 崩溃列表获取失败: {err}")
            return
        if self.platform == "ios":
            self.crash_list.config(values=[], state="disabled")
            self._put("[*] iOS 崩溃日志请在下方「全部拉取」后于本地目录查看 (.ips)\n", "dim")
            return
        self.crash_list.config(values=names, state="readonly" if names else "disabled")
        if names:
            self.crash_list.current(0)
            self._put(f"[*] 发现 {len(names)} 条崩溃记录\n", "dim")

    def pull_crash(self):
        name = self.crash_list.get()
        if not name:
            return
        self._pull_names([name])

    def pull_all_crashes(self):
        if self.platform == "ios":
            def work():
                files, err = self._crash_helper().crash_pull_all(self.crash_dir)
                return files, err
            threading.Thread(target=lambda: self._pull_ios_done(*work()), daemon=True).start()
            self._put("[*] 正在拉取 iOS 崩溃日志...\n", "dim")
            return
        names = list(self.crash_list.cget("values") or [])
        self._pull_names(names)

    def _pull_ios_done(self, files, err):
        if err:
            self.app.log(f"[-] iOS 崩溃拉取失败: {err}")
            self._put(f"[-] iOS 崩溃拉取失败: {err}\n", "err")
            return
        self._put(f"[+] 已拉取 {len(files)} 个 .ips -> {self.crash_dir}\n", "dim")
        self.app.log(f"[+] iOS 崩溃日志 {len(files)} 个 -> {self.crash_dir}")

    def _pull_names(self, names):
        def work():
            out = []
            errs = []
            for n in names:
                p, e = self._crash_helper().crash_pull(n, self.crash_dir)
                if p:
                    out.append(p)
                else:
                    errs.append(f"{n}: {e}")
            return out, errs
        threading.Thread(target=lambda: self._pull_done(*work()), daemon=True).start()
        self._put(f"[*] 正在拉取 {len(names)} 个崩溃日志...\n", "dim")

    def _pull_done(self, files, errs):
        if files:
            self._put(f"[+] 已拉取 {len(files)} 个 -> {self.crash_dir}\n", "dim")
            self.app.log(f"[+] 崩溃日志 {len(files)} 个 -> {self.crash_dir}")
        for e in errs:
            self._put(f"[-] {e}\n", "err")
            self.app.log(f"[-] 崩溃拉取失败: {e}")

    def open_crash_dir(self):
        self.crash_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(self.crash_dir)])

    def close(self):
        self._want_stream = False
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        if self._after_id:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                pass
        self.win.destroy()
        self.app.log_wins.pop(self.platform, None)


def main():
    if len(sys.argv) > 1 and (sys.argv[1] in CLI_COMMANDS or sys.argv[1] == "--cli"
                              or sys.argv[1] in ("-h", "--help")):
        if sys.argv[1] == "--cli":
            del sys.argv[1]
        H.main()
        return
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

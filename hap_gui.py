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
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    TkinterDnD, DND_FILES, HAS_DND = None, None, False

DEFAULT_TEAMS = [
    ("个人 · 周鑫飞", "420086000304296880"),
    ("企业 · 北京梆梆安全科技有限公司 (只读,禁止增删)", "30086000687991714"),
]

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
        root.title("HAP 调试助手")
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
        self.cert_name = tk.StringVar(value="xiaobai-debug-" + time.strftime("%Y%m%d%H%M%S"))
        ttk.Entry(cfg, textvariable=self.cert_name).grid(row=0, column=3, sticky="we", pady=3)
        ttk.Button(cfg, text="🕒 重命名",
                   command=lambda: self.cert_name.set("xiaobai-debug-" + time.strftime("%Y%m%d%H%M%S"))
                   ).grid(row=0, column=4, padx=(6, 0), pady=4)

        ttk.Label(cfg, text="🔐  Token:").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        self.token_var = tk.StringVar(value=self._load_token())
        ttk.Entry(cfg, textvariable=self.token_var, show="*").grid(row=1, column=1, columnspan=3, sticky="we", pady=3)
        btns = ttk.Frame(cfg)
        btns.grid(row=1, column=4, padx=(6, 0), pady=3)
        ttk.Button(btns, text="🔓 登录", command=self.do_login).pack(side="left")
        ttk.Button(btns, text="🔍 检查", command=lambda: self.check_token(notify=True)).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="🔄 团队", command=self.refresh_teams).pack(side="left", padx=(4, 0))

        ttk.Label(cfg, text="👤  UID:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        uid0 = H.UID_FILE.read_text().strip() if H.UID_FILE.exists() else os.environ.get("HUAWEI_UID", "")
        self.uid_var = tk.StringVar(value=uid0)
        ttk.Entry(cfg, textvariable=self.uid_var).grid(row=2, column=1, columnspan=3, sticky="we", pady=3)

        ioscfg = ttk.LabelFrame(left, text=" ⚙️  签名设置 (iOS) ", padding=12)
        ioscfg.columnconfigure(1, weight=1)
        self.cfg_ios = ioscfg

        ttk.Label(ioscfg, text="🪪  证书:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        self.ios_identity_var = tk.StringVar()
        self.ios_identity_box = ttk.Combobox(ioscfg, textvariable=self.ios_identity_var, state="readonly")
        self.ios_identity_box.grid(row=0, column=1, sticky="we", pady=3)
        ttk.Button(ioscfg, text="🔄 刷新", command=self.refresh_ios_identities).grid(row=0, column=2, padx=(6, 0), pady=4)

        ttk.Label(ioscfg, text="📄  描述文件:").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        self.ios_prov_var = tk.StringVar()
        self.ios_prov_box = ttk.Combobox(ioscfg, textvariable=self.ios_prov_var, state="readonly")
        self.ios_prov_box.grid(row=1, column=1, sticky="we", pady=3)
        self.ios_prov_btns = ttk.Frame(ioscfg)
        self.ios_prov_btns.grid(row=1, column=2, padx=(6, 0), pady=4)
        ttk.Button(self.ios_prov_btns, text="🔄 刷新",
                   command=self.refresh_ios_profiles).pack(side="left")
        ttk.Button(self.ios_prov_btns, text="📂 本地",
                   command=self.pick_ios_provision).pack(side="left", padx=(4, 0))
        ttk.Button(self.ios_prov_btns, text="ℹ️ 详情",
                   command=self.show_ios_provision_detail).pack(side="left", padx=(4, 0))
        self.ios_prov_box.bind("<<ComboboxSelected>>", lambda e: self.on_ios_provision_selected())

        ttk.Label(ioscfg, text="🆔  BundleID:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        self.ios_bundle_var = tk.StringVar()
        ttk.Entry(ioscfg, textvariable=self.ios_bundle_var).grid(row=2, column=1, sticky="we", pady=3)
        ttk.Label(ioscfg, text="留空 = 保持原样", style="Sub.TLabel").grid(row=2, column=2, sticky="w", padx=(6, 0))

        act = ttk.Frame(left)
        act.pack(fill="x", pady=(2, 8))
        self.go = ttk.Button(act, text="🚀  一键安装并启动", style="Go.TButton", command=self.one_click)
        self.go.pack(side="left")
        ttk.Button(act, text="✍️ 仅签名", command=lambda: self.one_click(install=False)).pack(side="left", padx=6)
        ttk.Button(act, text="📲 仅安装", command=self.install_only).pack(side="left", padx=6)
        ttk.Button(act, text="🗑️ 清空日志", command=self.clear_log).pack(side="right")

        outrow = ttk.Frame(left)
        outrow.pack(fill="x", pady=(0, 8))
        ttk.Label(outrow, text="📦  产物:").pack(side="left")
        self.out_var = tk.StringVar(value="—")
        self.out_label = ttk.Label(outrow, textvariable=self.out_var, foreground=C["success"])
        self.out_label.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(outrow, text="📂 打开目录", command=self.reveal_out).pack(side="right")
        ttk.Button(outrow, text="📋 复制路径", command=self.copy_out).pack(side="right", padx=4)

        logf = ttk.LabelFrame(outer, text=" 📜  日志 ", padding=4)
        logf.pack(fill="both", expand=True, pady=(8, 0))
        self.logbox = tk.Text(logf, height=10, bg=self.box_bg, fg=self.box_fg,
                              insertbackground=self.box_fg, font=self.f_mono,
                              highlightthickness=1, highlightbackground=C["drop_bd"])
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        self.logbox.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(fill="both", expand=True)

        self.root.after(100, self._drain)
        self.root.after(800, self.check_token)
        self.root.after(300, self.refresh_ios_profiles)

    def log(self, msg):
        self.logq.put(str(msg))

    def clear_log(self):
        self.logbox.delete("1.0", "end")

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
        try:
            while True:
                self.logbox.insert("end", self.logq.get_nowait() + "\n")
                self.logbox.see("end")
        except queue.Empty:
            pass
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

    def on_platform_change(self):
        ios = self.platform.get() == "ios"
        self.title_lbl.config(text="📱  iOS 调试助手" if ios else "📦  HAP 调试助手")
        self.sub_lbl.config(text="拖入 .ipa / .app 一键签名、安装、启动" if ios
                            else "拖入 .app / .hap 一键签名、安装、启动")
        self.drop.config(text="⬇️   把 .ipa / .app 拖到这里   (或点此选择文件)" if ios
                         else "⬇️   把 .app / .hap 拖到这里   (或点此选择文件)")
        if ios:
            self.cfg_harmony.pack_forget()
            self.cfg_ios.pack(fill="x", pady=8)
            self.refresh_ios_identities()
            self.refresh_ios_profiles()
        else:
            self.cfg_ios.pack_forget()
            self.cfg_harmony.pack(fill="x", pady=8)
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
        if not IOS:
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
        src = self.hap_path
        if not src:
            self.log("[-] 请先拖入 ipa 再使用「安装包自带」")
            self.ios_prov_var.set("")
            return
        p = Path(src)
        if p.suffix.lower() != ".ipa":
            self.log(f"[-] 安装包 {p.name} 不是 .ipa, 无内嵌描述文件")
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
                title="选择 ipa / app",
                filetypes=[("iOS App", "*.ipa *.app"), ("All", "*.*")])
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
        if suffix == ".ipa" and self.platform.get() != "ios":
            self.log("[*] 检测到 .ipa, 自动切换到 iOS 模式")
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
            self.info.config(text=f"{self._ellipsize(os.path.basename(path), 38)}  "
                                  f"bundle: {m.get('bundleName')}  "
                                  f"版本: {m.get('versionName')}")
            self.log(f"[+] 已选择 {path}\n    bundle={m.get('bundleName')}")
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
        if p.suffix not in (".ipa", ".app"):
            self.info.config(text=f"iOS 模式仅支持 .ipa / .app: {p.name}")
            self.log(f"[-] iOS 模式仅支持 .ipa / .app, 收到 {p.name}")
            return
        try:
            info = IOS.read_sign_info(p)
            bundle, ver = "", "?"
            tmp = None
            if p.suffix == ".ipa":
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
            self.busy = True
            self.go.config(state="disabled")
            threading.Thread(target=self._pipeline_ios, args=(install,), daemon=True).start()
            return
        if not self.token_var.get().strip():
            messagebox.showwarning("提示", "请填写 oauth2token (或点登录)")
            return
        self.busy = True
        self.go.config(state="disabled")
        threading.Thread(target=self._pipeline, args=(install,), daemon=True).start()

    def _pipeline_ios(self, install=True):
        try:
            src = Path(self.hap_path)
            identity = self.ios_identity_var.get().strip()
            prov = self.ios_prov_path or None
            bundle_override = self.ios_bundle_var.get().strip() or None
            self.log(f"=== iOS 开始: {src.name} | 证书 {identity[:60]} | "
                     f"描述文件 {'有' if prov else '无'} ===")

            # profile 兼容性预检: 固定 bundle id 的 profile 与设备上已装的同名应用
            # (通配 entitlement) 会冲突, 报 MismatchedApplicationIdentifierEntitlement
            if prov and src.suffix == ".ipa":
                try:
                    with tempfile.TemporaryDirectory(prefix="iosgui_pre_") as _td:
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
                             bundle_id=bundle_override)
            else:
                out = IOS.sign_app_source(src, workdir, identity, prov,
                                          bundle_id=bundle_override)
            self.root.after(0, lambda p=str(out): self._set_out(p))
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
            IOS.install_to_device(out)
            self.log("4) 完成 (设备上手动打开应用即可)")
            self.log("[+] iOS 流程完成")
        except Exception as e:
            self.log(f"[-] 失败: {e}")
            self.log(traceback.format_exc())
        finally:
            self.busy = False
            self.root.after(0, lambda: self.go.config(state="normal"))

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
            cert_name = self.cert_name.get().strip() or ("xiaobai-debug-" + time.strftime("%Y%m%d%H%M%S"))
            self.log(f"=== 开始: {bundle} | 团队 {team}(个人) | 证书 {cert_name} ===")

            ok, why = H.check_token(tok, uid, team)
            if not ok:
                self.log(f"[-] Token 已过期/无效: {why}")
                self.root.after(0, self._on_token_expired)
                return

            Hh = H.hw_headers(tok, uid, team)
            workdir = Path.home() / "Library/Caches/hap_installer/gui"
            workdir.mkdir(parents=True, exist_ok=True)

            self.log("1) device/list")
            dev, _ = H.http_req(H.URL_DEVICE_LIST + "?start=1&pageSize=100&encodeFlag=0", "GET", Hh)
            dev_ids = [d["id"] for d in (dev or {}).get("list", [])]
            self.log(f"   设备 {len(dev_ids)} 个")

            self.log("2) cert/list")
            cl, _ = H.http_req(H.URL_CERT_LIST, "GET", Hh)
            certs = (cl or {}).get("certList", [])
            cert = next((c for c in certs if c.get("certName") == cert_name), None)
            if not cert:
                self.log(f"   无 '{cert_name}'，走华为申请流程 cert/add (csr=xiaobai.csr)")
                csr = t.store_file("xiaobai.csr")
                r, _ = H.http_req(H.URL_CERT_ADD, "POST", Hh,
                                  {"certName": cert_name, "certType": 1, "csr": Path(csr).read_text()})
                code = (r or {}).get("ret", {}).get("code")
                if code in (0, None):
                    cl, _ = H.http_req(H.URL_CERT_LIST, "GET", Hh)
                    certs = (cl or {}).get("certList", [])
                    cert = next((c for c in certs if c.get("certName") == cert_name), None)
                else:
                    self.log(f"   申请失败: {(r or {}).get('ret', {}).get('msg')}")
                    cert = next((c for c in certs if c.get("certName") == "xiaobai-debug"), None)
                    if cert:
                        self.log("   配额已满 -> 回退复用已有证书 'xiaobai-debug'")
            if not cert:
                raise RuntimeError("找不到可用证书 (配额已满且无 xiaobai-debug)")
            self.log(f"   用证书 {cert['certName']} id={cert['id']}")

            self.log("3) reapply 下载证书")
            rp, _ = H.http_req(H.URL_OBJECT_REAPPLY, "POST", Hh, {"sourceUrls": cert["certObjectId"]})
            cer = workdir / f"{bundle}_debug.cer"
            H.download(((rp or {}).get("urlsInfo") or [{}])[0].get("newUrl"), str(cer))

            self.log("4) provision/add 生成 Profile")
            pa, _ = H.http_req(H.URL_PROVISION_ADD, "POST", Hh, {
                "provisionName": f"xiaobai-debug_{bundle.replace('.', '_')}",
                "aclPermissionList": [], "deviceList": dev_ids,
                "certList": [cert["id"]], "packageName": bundle})
            purl = (pa or {}).get("provisionFileUrl")
            if not purl:
                raise RuntimeError(f"provision/add 失败: {pa}")
            p7b = workdir / f"{bundle}_debug.p7b"
            H.download(purl, str(p7b))

            self.log("5) 本地签名")
            key = t.store_file("key.pem")
            h = H.Hdc(t)
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
                raise RuntimeError("安装失败")

            self.log("7) 启动")
            h.shell_out("aa", "start", "-a", ability, "-b", bundle)
            self.log(f"[+] 完成: {bundle} 已安装并启动")
        except Exception as e:
            self.log(f"[-] 失败: {e}")
            self.log(traceback.format_exc())
        finally:
            self.busy = False
            self.root.after(0, lambda: self.go.config(state="normal"))

    def install_only(self):
        if not self.hap_path:
            messagebox.showwarning("提示", "请先选择文件")
            return
        if self.busy:
            return
        if self.platform.get() == "ios":
            self.busy = True
            self.go.config(state="disabled")
            def ios_work():
                try:
                    self.log(f"安装 {self.hap_path} ...")
                    self.root.after(0, lambda p=self.hap_path: self._set_out(p))
                    IOS.install_to_device(self.hap_path)
                    self.log("[+] 已发起安装 (设备上手动打开应用即可)")
                except Exception as e:
                    self.log(f"[-] {e}")
                finally:
                    self.busy = False
                    self.root.after(0, lambda: self.go.config(state="normal"))
            threading.Thread(target=ios_work, daemon=True).start()
            return
        self.busy = True
        self.go.config(state="disabled")
        def work():
            try:
                t = H.Tools()
                h = H.Hdc(t)
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
                self.busy = False
                self.root.after(0, lambda: self.go.config(state="normal"))
        threading.Thread(target=work, daemon=True).start()


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

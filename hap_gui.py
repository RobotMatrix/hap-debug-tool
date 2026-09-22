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
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import hap_cli as H
except Exception as e:
    print("无法导入 hap_cli.py:", e)
    sys.exit(1)

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

        ttk.Label(outer, text="📦  HAP 调试助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="拖入 .app / .hap 一键签名、安装、启动",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 10))

        self.drop = tk.Label(
            outer, text="⬇️   把 .app / .hap 拖到这里   (或点此选择文件)",
            relief="solid", bd=1, height=4, justify="center",
            bg=self.drop_bg, fg=self.drop_fg, font=self.f_drop,
            highlightthickness=0)
        self.drop.pack(fill="x")
        self.drop.bind("<Button-1>", lambda e: self.pick_file())
        if HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self.on_drop)

        self.info = ttk.Label(outer, text="未选择文件", justify="left")
        self.info.pack(fill="x", pady=(8, 2))

        sigrow = ttk.Frame(outer)
        sigrow.pack(fill="x", pady=(0, 4))
        self.sig_dot = ttk.Label(sigrow, text="●", foreground=C["sub"])
        self.sig_dot.pack(side="left")
        self.sig_hint = ttk.Label(sigrow, text="签名: —", foreground=C["sub"])
        self.sig_hint.pack(side="left", padx=(4, 0))
        self.sig_toggle = ttk.Button(sigrow, text="详情 ▸", width=8,
                                     command=self.toggle_sig, state="disabled")
        self.sig_toggle.pack(side="right")

        self.sig_frame = ttk.Frame(outer)
        self.sigbox = tk.Text(self.sig_frame, height=8, wrap="none", relief="flat",
                              bg=self.box_bg, fg=self.box_fg, font=self.f_mono,
                              highlightthickness=1, highlightbackground=C["drop_bd"])
        self.sigbox.pack(fill="x")
        self.sigbox.insert("end", "(无)")
        self.sigbox.config(state="disabled")

        cfg = ttk.LabelFrame(outer, text=" ⚙️  签名设置 ", padding=12)
        cfg.pack(fill="x", pady=8)
        cfg.columnconfigure(1, weight=1)
        cfg.columnconfigure(3, weight=1)

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
        ttk.Button(btns, text="🔄 团队", command=self.refresh_teams).pack(side="left", padx=(4, 0))

        ttk.Label(cfg, text="👤  UID:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        uid0 = H.UID_FILE.read_text().strip() if H.UID_FILE.exists() else os.environ.get("HUAWEI_UID", "")
        self.uid_var = tk.StringVar(value=uid0)
        ttk.Entry(cfg, textvariable=self.uid_var).grid(row=2, column=1, columnspan=3, sticky="we", pady=3)

        act = ttk.Frame(outer)
        act.pack(fill="x", pady=(2, 8))
        self.go = ttk.Button(act, text="🚀  一键安装并启动", style="Go.TButton", command=self.one_click)
        self.go.pack(side="left")
        ttk.Button(act, text="✍️ 仅签名", command=lambda: self.one_click(install=False)).pack(side="left", padx=6)
        ttk.Button(act, text="📲 仅安装", command=self.install_only).pack(side="left", padx=6)
        ttk.Button(act, text="🗑️ 清空日志", command=self.clear_log).pack(side="right")

        outrow = ttk.Frame(outer)
        outrow.pack(fill="x", pady=(0, 8))
        ttk.Label(outrow, text="📦  产物:").pack(side="left")
        self.out_var = tk.StringVar(value="—")
        self.out_label = ttk.Label(outrow, textvariable=self.out_var, foreground=C["success"])
        self.out_label.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(outrow, text="📂 打开目录", command=self.reveal_out).pack(side="right")
        ttk.Button(outrow, text="📋 复制路径", command=self.copy_out).pack(side="right", padx=4)

        logf = ttk.LabelFrame(outer, text=" 📜  日志 ", padding=4)
        logf.pack(fill="both", expand=True)
        self.logbox = tk.Text(logf, height=10, bg=self.box_bg, fg=self.box_fg,
                              insertbackground=self.box_fg, font=self.f_mono,
                              highlightthickness=1, highlightbackground=C["drop_bd"])
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        self.logbox.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(fill="both", expand=True)

        self.root.after(100, self._drain)

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
        self.root.after(100, self._drain)

    def toggle_sig(self):
        if self.sig_expanded:
            self.sig_frame.pack_forget()
            self.sig_toggle.config(text="详情 ▸")
        else:
            self.sig_frame.pack(fill="x", pady=(0, 6))
            self.sig_toggle.config(text="收起 ▾")
        self.sig_expanded = not self.sig_expanded

    def _set_sig(self, summary, detail, signed):
        self.sig_hint.config(text=f"签名: {summary}",
                             foreground=(self.C["success"] if signed else self.C["sub"]))
        self.sig_dot.config(foreground=(self.C["success"] if signed else self.C["sub"]))
        self.sigbox.config(state="normal")
        self.sigbox.delete("1.0", "end")
        self.sigbox.insert("end", detail)
        self.sigbox.config(state="disabled")
        self.sig_toggle.config(state="normal")

    def pick_file(self):
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
        self.hap_path = path
        try:
            self.meta = H.read_hap_meta(path)
            m = self.meta["modules"][0] if self.meta["modules"] else {}
            self.info.config(text=f"{os.path.basename(path)}    bundle: {m.get('bundleName')}    "
                                  f"版本: {m.get('versionName')} ({m.get('versionCode')})")
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

    def _load_token(self):
        if H.TOKEN_FILE.exists():
            return H.TOKEN_FILE.read_text().strip()
        return os.environ.get("HUAWEI_TOKEN", "")

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
            messagebox.showwarning("提示", "请先拖入或选择 .app/.hap")
            return
        if not self.token_var.get().strip():
            messagebox.showwarning("提示", "请填写 oauth2token (或点登录)")
            return
        self.busy = True
        self.go.config(state="disabled")
        threading.Thread(target=self._pipeline, args=(install,), daemon=True).start()

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
            src = H.resolve_hap(self.hap_path)
            signed = workdir / f"{bundle}-signed.hap"
            H.do_sign(t, src, str(signed), key, "", "xiaobai", str(cer), str(p7b))
            self.root.after(0, lambda p=str(signed): self._set_out(p))
            self.log(f"   已签名: {signed}")

            if not install:
                self.log(f"[+] 完成(仅签名): {signed}")
                return

            self.log("6) hdc 安装")
            h = H.Hdc(t)
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
            messagebox.showwarning("提示", "请先选择已签名的 .hap")
            return
        if self.busy:
            return
        self.busy = True
        self.go.config(state="disabled")
        def work():
            try:
                t = H.Tools()
                h = H.Hdc(t)
                bundle = (self.meta or {}).get("modules", [{}])[0].get("bundleName")
                ability = (self.meta or {}).get("modules", [{}])[0].get("mainElement") or "EntryAbility"
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

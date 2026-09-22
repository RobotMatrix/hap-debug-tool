#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hap_cli.py -- HarmonyOS HAP/HSP 签名 + 安装调试助手 (Python 重实现)

复刻 "小白调试助手" (hap_installer-Mac) 的逻辑，复用其内置命令行工具：
    hdc       HarmonyOS Device Connector  (连设备 / 推文件 / shell / install)
    signer    华为 hap-sign-tool          (本地签名 sign-app / 验签 verify-app)
    packing_tool / hnpcli                 打包与 HNP 工具 (原包缺 libcjson，可能不可用)

纯离线可用；华为云证书/Profile 相关命令为 best-effort 实现。

无第三方依赖，仅标准库。
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
APP_NAME = "小白调试助手.app"
ASSETS_REL = "Contents/Frameworks/App.framework/Versions/A/Resources/flutter_assets/assets"
TOOLS_REL = ASSETS_REL + "/macos"
STORE_REL = ASSETS_REL + "/store"
OHOS_REL = ASSETS_REL + "/ohos"
JAR_REL = ASSETS_REL + "/jar"

# 内置调试证书 (来自原 app，注意：证书已于 2026 年过期，且此 signer 构建要求 PEM 私钥)
DEFAULT_KEY_ALIAS = "xiaobai"
DEFAULT_KEYSTORE_PWD = "xiaobai123"
DEFAULT_KEYSTORE = "key.pem"          # 该 signer 需要 PEM 私钥, 不接受 p12
DEFAULT_APP_CERT = "xiaobai-debug.cer"
DEFAULT_PROFILE = "xiaobai-debug.p7b"

# 设备端自动安装器 (实测: 必须签名后才能 hdc install)
AUTO_INSTALLER_BUNDLE = "com.xiaobai.autoinstaller"
AUTO_INSTALLER_HAP = "auto_installer.hap"
DEVICE_TMP = "/data/local/tmp"
DEBUG_APP_LIST = DEVICE_TMP + "/debug_app_list.json"

# 华为云 (DevEco Connect)
DEVECO_AUTH = "https://cn.devecostudio.huawei.com"
CONNECT_API = "https://connect-api.cloud.huawei.com"
URL_LOGIN = DEVECO_AUTH + "/console/DevEcoIDE/apply"
URL_TEMPTOKEN_CHECK = DEVECO_AUTH + "/authrouter/auth/api/temptoken/check"
URL_JWTOKEN_CHECK = DEVECO_AUTH + "/authrouter/auth/api/jwToken/check"
URL_DEVICE_LIST = CONNECT_API + "/api/cps/device-manage/v1/device/list"
URL_DEVICE_ADD = CONNECT_API + "/api/cps/device-manage/v1/device/add"
URL_CERT_LIST = CONNECT_API + "/api/cps/harmony-cert-manage/v1/cert/list"
URL_CERT_ADD = CONNECT_API + "/api/cps/harmony-cert-manage/v1/cert/add"
URL_CERT_DELETE = CONNECT_API + "/api/cps/harmony-cert-manage/v1/cert/delete"
URL_PROVISION_ADD = CONNECT_API + "/api/cps/provision-manage/v1/ide/test/provision/add"
URL_TEAM_LIST = CONNECT_API + "/api/ups/user-permission-service/v1/user-team-list"
URL_OBJECT_REAPPLY = CONNECT_API + "/api/amis/app-manage/v1/objects/url/reapply"

# 受保护的团队(企业)账户: 禁止对其证书/Profile 做任何增删 (只读)
ENTERPRISE_TEAM = "30086000687991714"
PERSONAL_TEAM = "420086000304296880"
TOKEN_FILE = Path.home() / ".hap_cli_token"
UID_FILE = Path.home() / ".hap_cli_uid"

# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
class Log:
    @staticmethod
    def _p(tag, color, msg):
        if sys.stdout.isatty():
            return f"\033[{color}m{tag}\033[0m {msg}"
        return f"{tag} {msg}"

    @staticmethod
    def info(msg):
        print(Log._p("[*]", "36", msg))

    @staticmethod
    def ok(msg):
        print(Log._p("[+]", "32", msg))

    @staticmethod
    def warn(msg):
        print(Log._p("[!]", "33", msg))

    @staticmethod
    def err(msg):
        print(Log._p("[-]", "31", msg), file=sys.stderr)


def run(cmd, check=True, capture=False, cwd=None, env=None):
    """执行外部命令。"""
    if not capture:
        Log.info("$ " + " ".join(str(c) for c in cmd))
    try:
        r = subprocess.run(
            cmd,
            check=False,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
        )
    except FileNotFoundError as e:
        if check:
            raise SystemExit(f"命令不存在: {e}")
        return 127, str(e)
    if capture:
        out = r.stdout or ""
        if check and r.returncode != 0:
            raise SystemExit(f"命令失败({r.returncode}): {' '.join(cmd)}\n{out}")
        return r.returncode, out
    if check and r.returncode != 0:
        raise SystemExit(f"命令失败({r.returncode}): {' '.join(cmd)}")
    return r.returncode, ""


# --------------------------------------------------------------------------- #
# 工具定位
# --------------------------------------------------------------------------- #
def find_app(explicit=None):
    """定位 .app 包。"""
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("HAP_APP"):
        cands.append(os.environ["HAP_APP"])
    here = Path(__file__).resolve().parent
    cands += [
        here / APP_NAME,
        Path("/Applications") / APP_NAME,
    ]
    home = Path.home()
    cands += [Path(p) for p in glob.glob(str(home / "Downloads" / "*" / APP_NAME))]
    cands += [Path(p) for p in glob.glob(str(home / "Downloads" / APP_NAME))]
    for c in cands:
        if c and Path(c).is_dir() and (Path(c) / "Contents").is_dir():
            return Path(c).resolve()
    return None


class Tools:
    def __init__(self, app=None, tools_dir=None):
        self.app = find_app(app)
        self.assets = None
        self.macos = None
        self.store = None
        self.ohos = None
        self.jar = None
        base = tools_dir or os.environ.get("HAP_TOOLS_DIR") or self._bundled_base()
        if base:
            base = Path(base)
            self.macos = base
            self.store = base / "store" if (base / "store").is_dir() else base
            self.ohos = base / "ohos" if (base / "ohos").is_dir() else base
            self.jar = base / "jar"
            return
        if self.app:
            self.assets = self.app / ASSETS_REL
            self.macos = self.app / TOOLS_REL
            self.store = self.app / STORE_REL
            self.ohos = self.app / OHOS_REL
            self.jar = self.app / JAR_REL

    @staticmethod
    def _bundled_base():
        cands = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "tools")
        cands.append(Path(__file__).resolve().parent / "tools")
        for c in cands:
            if (c / "hdc").exists() or (c / "signer").exists():
                return c
        return None

    def tool(self, name, required=True):
        """返回工具路径：优先内置，其次 PATH。"""
        if self.macos:
            p = self.macos / name
            if p.exists():
                return str(p)
        w = shutil.which(name)
        if w:
            return w
        if required:
            raise SystemExit(
                f"找不到工具 '{name}'。用 --app 指定 .app 路径，或 --tools 指定工具目录，"
                f"或设置 HAP_APP / HAP_TOOLS_DIR。"
            )
        return None

    def store_file(self, name, required=True):
        if self.store and (self.store / name).exists():
            return str(self.store / name)
        if required:
            raise SystemExit(f"找不到内置证书文件 '{name}'，请用 --keystore/--app-cert/--profile 指定。")
        return None

    def ohos_file(self, name, required=True):
        if self.ohos and (self.ohos / name).exists():
            return str(self.ohos / name)
        if required:
            raise SystemExit(f"找不到内置 '{name}'。")
        return None


# --------------------------------------------------------------------------- #
# HAP 信息解析
# --------------------------------------------------------------------------- #
def _parse_module_json(entry, raw):
    data = json.loads(raw.decode("utf-8"))
    app = data.get("app", {})
    mod = data.get("module", {})
    return {
        "entry": entry,
        "bundleName": app.get("bundleName"),
        "versionName": app.get("versionName"),
        "versionCode": app.get("versionCode"),
        "apiVersion": app.get("minAPIVersion"),
        "targetAPIVersion": app.get("targetAPIVersion"),
        "debug": app.get("debug"),
        "vendor": app.get("vendor"),
        "moduleName": mod.get("name"),
        "moduleType": mod.get("type"),
        "deviceTypes": mod.get("deviceTypes"),
        "mainElement": mod.get("mainElement"),
        "abilities": [a.get("name") for a in mod.get("abilities", [])],
        "permissions": [p.get("name") for p in mod.get("requestPermissions", [])],
    }


def is_signed(path):
    p = Path(path)
    if p.is_dir():
        return _app_pack_signed(p)
    with open(path, "rb") as f:
        return b"<hap sign block>" in f.read()


def _path_size(path):
    p = Path(path)
    if p.is_file():
        return os.path.getsize(path)
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def is_app_pack(path):
    """判断是否为 App Pack(.app)：目录形式 (含 .hap/.hsp) 或 zip 形式。"""
    p = Path(path)
    if not str(path).endswith(".app"):
        return False
    if p.is_dir():
        return any(f.is_file() and f.suffix in (".hap", ".hsp") for f in p.iterdir())
    if p.is_file():
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                return any(n.endswith((".hap", ".hsp")) for n in names) or "pack.info" in names
        except Exception:
            return False
    return False


def _read_modules_from_zip(z, prefix=""):
    out = []
    for m in z.namelist():
        if m.endswith("module.json"):
            try:
                out.append(_parse_module_json(prefix + m, z.read(m)))
            except Exception:
                continue
    return out


def _app_pack_signed(path):
    """App Pack 是否已签名 (以入口 hap 为准)。"""
    p = Path(path)
    if p.is_dir():
        for f in sorted(p.iterdir()):
            if f.is_file() and f.suffix == ".hap":
                try:
                    with open(f, "rb") as fh:
                        return b"<hap sign block>" in fh.read()
                except Exception:
                    return False
        return False
    try:
        with open(path, "rb") as fh:
            return b"<hap sign block>" in fh.read()
    except Exception:
        return False


def read_hap_meta(hap):
    """从 HAP/HSP/App Pack(.app, 目录或 zip) 中读取 module.json / pack.info。"""
    p = Path(hap)
    meta = {"path": str(hap), "size": _path_size(hap), "modules": [],
            "signed": None, "isAppPack": is_app_pack(hap)}

    # 目录形式的 App Pack
    if p.is_dir():
        meta["files"] = sum(1 for f in p.rglob("*") if f.is_file())
        for f in sorted(p.iterdir()):
            if f.is_file() and f.suffix in (".hap", ".hsp"):
                try:
                    with zipfile.ZipFile(f) as z:
                        meta["modules"] += _read_modules_from_zip(z, f.name + "!")
                except Exception:
                    continue
        pi = p / "pack.info"
        if pi.exists():
            try:
                meta["pack"] = json.loads(pi.read_text(encoding="utf-8"))
            except Exception:
                pass
        meta["signed"] = _app_pack_signed(p)
        return meta

    with zipfile.ZipFile(hap) as z:
        names = z.namelist()
        mods = [n for n in names if n.endswith("module.json")]
        if not mods and "module.json" in names:
            mods = ["module.json"]
        for m in mods:
            try:
                meta["modules"].append(_parse_module_json(m, z.read(m)))
            except Exception:
                continue
        if not meta["modules"]:
            import io
            for n in names:
                if n.endswith((".hap", ".hsp")):
                    try:
                        raw = z.read(n)
                        if meta["signed"] is None:
                            meta["signed"] = b"<hap sign block>" in raw
                        with zipfile.ZipFile(io.BytesIO(raw)) as inner:
                            for m in inner.namelist():
                                if m.endswith("module.json"):
                                    meta["modules"].append(_parse_module_json(n + "!" + m, inner.read(m)))
                    except Exception:
                        continue
        if meta["signed"] is None:
            meta["signed"] = is_signed(hap)
        if "pack.info" in names:
            try:
                meta["pack"] = json.loads(z.read("pack.info").decode("utf-8"))
            except Exception:
                pass
        meta["files"] = len(names)
    return meta


def _extract_json_obj(data, start):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(data)):
        c = data[i:i + 1]
        if in_str:
            if esc:
                esc = False
            elif c == b"\\":
                esc = True
            elif c == b'"':
                in_str = False
        else:
            if c == b'"':
                in_str = True
            elif c == b"{":
                depth += 1
            elif c == b"}":
                depth -= 1
                if depth == 0:
                    return data[start:i + 1]
    return None


def _unescape_dn(s):
    return re.sub(r"(?:\\x[0-9A-Fa-f]{2})+",
                  lambda m: bytes.fromhex(m.group(0).replace("\\x", "")).decode("utf-8", "replace"),
                  s or "")


def _openssl_cert(pem):
    try:
        r = subprocess.run(["openssl", "x509", "-noout", "-subject", "-issuer", "-dates"],
                           input=pem, capture_output=True, text=True)
        out = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = _unescape_dn(v.strip())
        return out
    except Exception:
        return {}


def read_sign_info(path):
    """解析 HAP/App Pack 签名块: 内嵌 profile + 开发者证书。"""
    p = Path(path)
    if p.is_dir() or str(path).endswith(".app"):
        try:
            path = resolve_hap(path)
        except SystemExit:
            return {"signed": False}
    with open(path, "rb") as f:
        data = f.read()
    info = {"signed": b"<hap sign block>" in data}
    if not info["signed"]:
        return info
    idx = data.find(b'{"version-name"')
    if idx < 0:
        return info
    obj = _extract_json_obj(data, idx)
    if not obj:
        return info
    try:
        prof = json.loads(obj.decode("utf-8", "replace"))
    except Exception:
        return info
    bi = prof.get("bundle-info", {})
    di = prof.get("debug-info", {})
    info["profile"] = {
        "bundle-name": bi.get("bundle-name"),
        "type": prof.get("type"),
        "developer-id": bi.get("developer-id"),
        "app-identifier": bi.get("app-identifier"),
        "apl": bi.get("apl"),
        "app-feature": bi.get("app-feature"),
        "issuer": prof.get("issuer"),
        "validity": prof.get("validity"),
        "device-count": len(di.get("device-ids", [])),
        "permissions": prof.get("permissions"),
    }
    cert_pem = bi.get("development-certificate") or bi.get("distribution-certificate")
    if cert_pem:
        info["cert"] = _openssl_cert(cert_pem)
        info["cert_pem"] = cert_pem
    return info


def _pick_entry_hap(paths):
    for p in paths:
        try:
            meta = read_hap_meta(p)
            m = meta["modules"][0] if meta["modules"] else {}
            if m.get("moduleType") == "entry":
                return p
        except Exception:
            continue
    return paths[0]


def resolve_hap(path):
    """返回可签名的 .hap 路径；App Pack(.app 目录/zip) 则返回入口 hap。"""
    p = Path(path)
    if p.is_dir():
        if not is_app_pack(path):
            raise SystemExit(f"{path} 不是有效的 App Pack")
        haps = [str(f) for f in sorted(p.iterdir()) if f.is_file() and f.suffix == ".hap"]
        if not haps:
            raise SystemExit(f"{path} 中未找到 .hap")
        return _pick_entry_hap(haps)
    if not path.endswith(".app"):
        return path
    out_dir = tempfile.mkdtemp(prefix="happack_")
    with zipfile.ZipFile(path) as z:
        haps = [n for n in z.namelist() if n.endswith(".hap")]
        if not haps:
            raise SystemExit(f"{path} 中未找到 .hap")
        z.extract(haps[0], out_dir)
    return os.path.join(out_dir, haps[0])


def app_pack_module_files(app_path):
    """返回 App Pack 内的模块文件列表 (hap 在前, hsp 在后)。"""
    p = Path(app_path)
    mods = [str(f) for f in p.rglob("*") if f.is_file() and f.suffix in (".hap", ".hsp")]
    mods.sort(key=lambda x: (0 if x.endswith(".hap") else 1, x))
    return mods


def sign_app_pack(t, app_path, out_dir, keystore, keystore_pwd, key_alias,
                  app_cert, profile, sign_alg="SHA256withECDSA"):
    """重签名 App Pack(.app 目录或 zip) 内所有 .hap/.hsp 模块，输出 {name}-signed.app。"""
    src = Path(app_path)
    if not is_app_pack(app_path):
        raise SystemExit(f"不是有效的 App Pack: {app_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / (src.stem + "-signed.app")
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        dest.mkdir()
        with zipfile.ZipFile(src) as z:
            z.extractall(dest)
    modules = [f for f in sorted(dest.rglob("*")) if f.is_file() and f.suffix in (".hap", ".hsp")]
    if not modules:
        raise SystemExit(f"{app_path} 中未找到 .hap/.hsp 模块")
    Log.info(f"App Pack 共 {len(modules)} 个模块，逐个重签名 ...")
    for f in modules:
        tmp = f.with_name(f.stem + ".signed" + f.suffix)
        do_sign(t, str(f), str(tmp), keystore, keystore_pwd, key_alias,
                app_cert, profile, sign_alg)
        os.replace(str(tmp), str(f))
    Log.ok(f"App Pack 已重签名 -> {dest}")
    return str(dest)


def cmd_info(args):
    t = Tools(args.app, args.tools)
    meta = read_hap_meta(args.hap)
    if args.json:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return
    print(f"文件   : {meta['path']}  ({meta['size']} bytes, {meta['files']} entries)")
    print(f"已签名 : {'是' if meta['signed'] else '否'}")
    for m in meta["modules"]:
        print("-" * 60)
        print(f"  bundleName   : {m['bundleName']}")
        print(f"  version      : {m['versionName']} ({m['versionCode']})")
        print(f"  apiVersion   : min={m['apiVersion']} target={m['targetAPIVersion']}")
        print(f"  module       : {m['moduleName']} type={m['moduleType']}")
        print(f"  debug        : {m['debug']}  vendor={m['vendor']}")
        print(f"  deviceTypes  : {', '.join(m['deviceTypes'] or [])}")
        print(f"  mainElement  : {m['mainElement']}")
        print(f"  abilities    : {', '.join(m['abilities'] or [])}")
        if m["permissions"]:
            print(f"  permissions  : {', '.join(m['permissions'])}")


# --------------------------------------------------------------------------- #
# 签名
# --------------------------------------------------------------------------- #
def do_sign(t, hap, out, keystore, keystore_pwd, key_alias, app_cert, profile,
            sign_alg="SHA256withECDSA", extra=None):
    signer = t.tool("signer")
    keystore_pwd = keystore_pwd or DEFAULT_KEYSTORE_PWD
    cmd = [
        signer, "sign-app",
        "-mode", "localSign",
        "-keyAlias", key_alias,
        "-appCertFile", app_cert,
        "-profileFile", profile,
        "-inFile", hap,
        "-outFile", out,
        "-signAlg", sign_alg,
        "-keystoreFile", keystore,
        "-keystorePwd", keystore_pwd,
    ]
    if extra:
        cmd += extra
    rc, outp = run(cmd, check=False, capture=True)
    low = (outp or "").lower()
    if rc != 0 or "sign-app success" not in low or not os.path.exists(out):
        Log.err("签名失败：")
        print((outp or "").rstrip())
        raise SystemExit(rc or 1)
    print(outp.rstrip())
    Log.ok(f"已签名 -> {out}")
    return out


def resolve_sign_args(t, args):
    keystore = args.keystore or (t.store_file(DEFAULT_KEYSTORE, required=False))
    app_cert = args.app_cert or (t.store_file(DEFAULT_APP_CERT, required=False))
    profile = args.profile or (t.store_file(DEFAULT_PROFILE, required=False))
    if not keystore:
        raise SystemExit("缺少 keystore (PEM 私钥，此 signer 不接受 p12)，用 --keystore 指定。")
    if not app_cert:
        raise SystemExit("缺少 app 证书 (.cer)，用 --app-cert 指定。")
    if not profile:
        raise SystemExit("缺少 profile (.p7b)，用 --profile 指定。")
    return keystore, (args.keystore_pwd or DEFAULT_KEYSTORE_PWD), (args.key_alias or DEFAULT_KEY_ALIAS), app_cert, profile


def format_sign_info(info):
    if not info.get("signed"):
        return "已签名: 否"
    p = info.get("profile", {})
    lines = ["已签名: 是"]
    lines.append(f"  bundle-name : {p.get('bundle-name')}")
    lines.append(f"  type        : {p.get('type')}   issuer: {p.get('issuer')}")
    lines.append(f"  developer-id: {p.get('developer-id')}   app-identifier: {p.get('app-identifier')}")
    lines.append(f"  apl/feature : {p.get('apl')} / {p.get('app-feature')}")
    v = p.get("validity") or {}
    nb, na = v.get("not-before"), v.get("not-after")
    if nb and na:
        lines.append(f"  profile有效期: {time.strftime('%Y-%m-%d', time.localtime(nb))} ~ "
                     f"{time.strftime('%Y-%m-%d', time.localtime(na))}")
    lines.append(f"  授权设备数  : {p.get('device-count')}")
    c = info.get("cert") or {}
    if c:
        lines.append(f"  证书主体    : {c.get('subject')}")
        lines.append(f"  证书颁发者  : {c.get('issuer')}")
        lines.append(f"  证书有效期  : {c.get('notBefore')} ~ {c.get('notAfter')}")
    return "\n".join(lines)


def cmd_sig(args):
    info = read_sign_info(resolve_hap(args.hap))
    if args.json:
        info.pop("cert_pem", None)
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(format_sign_info(info))


def cmd_sign(args):
    t = Tools(args.app, args.tools)
    src = args.hap
    if not os.path.exists(src):
        raise SystemExit(f"文件不存在: {src}")
    keystore, pwd, alias, cert, profile = resolve_sign_args(t, args)
    if is_app_pack(src):
        out_dir = args.out or os.path.dirname(os.path.abspath(src))
        sign_app_pack(t, src, out_dir, keystore, pwd, alias, cert, profile, args.sign_alg)
        return
    hap = resolve_hap(src)
    out = args.out or os.path.splitext(src)[0] + "-signed.hap"
    do_sign(t, hap, out, keystore, pwd, alias, cert, profile, args.sign_alg)


def cmd_verify(args):
    t = Tools(args.app, args.tools)
    signer = t.tool("signer")
    tmp = tempfile.mkdtemp(prefix="hapverify_")
    cert_chain = os.path.join(tmp, "outCertChain.cer")
    profile = os.path.join(tmp, "outProfile.p7b")
    cmd = [signer, "verify-app", "-inFile", args.hap,
           "-outCertChain", cert_chain, "-outProfile", profile]
    rc, outp = run(cmd, check=False, capture=True)
    print(outp.rstrip())
    if os.path.exists(cert_chain):
        Log.ok(f"证书链: {cert_chain}")
    if os.path.exists(profile):
        Log.ok(f"Profile: {profile}")
    raise SystemExit(rc)


def cmd_genkeys(args):
    """用 openssl 生成自签名 CA + app 证书 + PEM 私钥 (signer 的 generate-* 为空操作)。"""
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    key = out / "key.pem"
    key_sec1 = out / ".key-sec1.pem"
    csr = out / "cert.csr"
    root_ca = out / "root-ca.pem"
    app_cert = out / "app-cert.pem"
    subj = f"/C=CN/O={args.org}/OU=Dev/CN={args.key_alias}"
    days = str(args.validity)

    run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key_sec1)])
    run(["openssl", "pkcs8", "-topk8", "-nocrypt", "-in", str(key_sec1), "-out", str(key)])
    os.remove(key_sec1)
    run(["openssl", "req", "-new", "-x509", "-key", str(key), "-out", str(root_ca),
         "-days", days, "-subj", f"/C=CN/O={args.org}/OU=Dev/CN={args.org} Root CA"])
    run(["openssl", "req", "-new", "-key", str(key), "-out", str(csr), "-subj", subj])
    run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(root_ca), "-CAkey", str(key),
         "-CAcreateserial", "-out", str(app_cert), "-days", days, "-sha256"])
    Log.ok(f"已生成于 {out}")
    print(f"  PEM 私钥  : {key}    (用作 --keystore)")
    print(f"  app 证书  : {app_cert}   (用作 --app-cert)")
    print(f"  Root CA   : {root_ca}")
    print("注意: 设备只信任其预置/云下发的证书链。此自签名证书仅适用于信任该 CA 的")
    print("      开发设备/模拟器。真机调试请用 DevEco 或华为云签发匹配的 cert+profile。")


def cmd_signprofile(args):
    """用 openssl CMS 把 Provision Profile JSON 签成 p7b (实验性: 设备端格式校验可能拒绝)。"""
    if args.profile_json:
        pj = json.loads(Path(args.profile_json).read_text())
    else:
        now = int(time.time() * 1000)
        pj = {
            "version-name": "2.0.0",
            "version-code": 2,
            "app-distribution-type": "os_integration",
            "uuid": args.uuid,
            "validity": {"not-before": now, "not-after": now + args.validity * 86400 * 1000},
            "type": args.profile_type,
            "bundle-info": {
                "developer-id": args.developer_id,
                "bundle-name": args.bundle_name,
                "distribution-certificate": Path(args.app_cert).read_text(),
                "apl": "normal",
                "app-feature": "hos_system_app",
            },
            "permissions": [],
            "debug-info": {"device-id-type": "udid", "device-ids": args.udids or []},
        }
    tmp_json = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(pj, tmp_json, ensure_ascii=False, indent=2)
    tmp_json.close()
    out = args.out or "profile.p7b"
    cmd = ["openssl", "cms", "-sign", "-binary", "-in", tmp_json.name,
           "-signer", args.profile_cert, "-inkey", args.keystore,
           "-outform", "DER", "-nodetach", "-out", out]
    run(cmd, capture=True)
    Log.ok(f"Profile 已签发 -> {out}")
    Log.warn("此为 openssl CMS 格式；HarmonyOS 设备可能要求专用 profile 格式，"
             "真机请使用 DevEco/华为云签发的 p7b。")


# --------------------------------------------------------------------------- #
# hdc 封装
# --------------------------------------------------------------------------- #
class Hdc:
    def __init__(self, t, connect_key=None):
        self.bin = t.tool("hdc")
        self.key = connect_key

    def _base(self):
        return [self.bin] + (["-t", self.key] if self.key else [])

    def raw(self, args, check=True, capture=False):
        return run(self._base() + list(args), check=check, capture=capture)

    def targets(self):
        _, out = self.raw(["list", "targets"], capture=True)
        return [l.strip() for l in out.splitlines() if l.strip() and "Empty" not in l]

    def tconn(self, addr):
        return self.raw(["tconn", addr], check=False, capture=True)

    def wait(self):
        return self.raw(["wait"], check=False, capture=True)

    def shell(self, *cmd):
        return self.raw(["shell", *cmd])

    def shell_out(self, *cmd):
        return self.raw(["shell", *cmd], check=False, capture=True)

    def send(self, local, remote):
        return self.raw(["file", "send", local, remote])

    def recv(self, remote, local):
        return self.raw(["file", "recv", remote, local])

    def install(self, hap, shared=False, replace=True):
        a = ["install"]
        if replace:
            a.append("-r")
        if shared:
            a.append("-s")
        a.append(hap)
        return self.raw(a, check=False, capture=True)

    def install_packages(self, paths, replace=True, shared=False):
        """一次安装多个包 (App Pack 的 hap + hsp 需同时安装才能解析依赖)。"""
        a = ["install"]
        if replace:
            a.append("-r")
        if shared:
            a.append("-s")
        a += [str(p) for p in paths]
        return self.raw(a, check=False, capture=True)

    def uninstall(self, bundle, keep=False, shared=False):
        a = ["uninstall"]
        if keep:
            a.append("-k")
        if shared:
            a.append("-s")
        a.append(bundle)
        return self.raw(a, check=False, capture=True)


def cmd_devices(args):
    t = Tools(args.app, args.tools)
    h = Hdc(t, args.connect_key)
    _, out = h.raw(["list", "targets", "-v"], capture=True)
    print(out.rstrip())


def cmd_connect(args):
    t = Tools(args.app, args.tools)
    h = Hdc(t, args.connect_key)
    if args.kill:
        print(h.raw(["kill"], check=False, capture=True)[1])
        return
    if args.start:
        print(h.raw(["start"], check=False, capture=True)[1])
        return
    rc, out = h.tconn(args.address)
    print(out.rstrip())
    if rc != 0:
        Log.err("连接失败。确认设备已开启 USB 调试 / 网络调试，端口正确。")
        raise SystemExit(rc)
    Log.ok("已连接")


def cmd_shell(args):
    t = Tools(args.app, args.tools)
    h = Hdc(t, args.connect_key)
    if args.cmd:
        rc, out = h.shell_out(*args.cmd)
        print(out.rstrip())
        raise SystemExit(rc)
    # 交互式
    os.execv(h.bin, h._base() + ["shell"])


def cmd_udid(args):
    t = Tools(args.app, args.tools)
    h = Hdc(t, args.connect_key)
    for cmdline in (["bm", "get", "--udid"], ["bm", "get", "--udid", "-u", "0"]):
        _, out = h.shell_out(*cmdline)
        if "udid" in out.lower():
            print(out.rstrip())
            return
    print(out.rstrip())


def cmd_apps(args):
    t = Tools(args.app, args.tools)
    h = Hdc(t, args.connect_key)
    if args.debug_list:
        tmp = tempfile.mktemp(suffix=".json")
        h.recv(DEBUG_APP_LIST, tmp)
        if os.path.exists(tmp):
            print(Path(tmp).read_text(errors="replace"))
        return
    if args.bundle:
        _, out = h.shell_out("bm", "dump", "-n", args.bundle)
    else:
        _, out = h.shell_out("bm", "dump", "-g")
    print(out.rstrip())


def cmd_install(args):
    t = Tools(args.app, args.tools)
    h = Hdc(t, args.connect_key)
    src = args.hap
    if not os.path.exists(src):
        raise SystemExit(f"文件不存在: {src}")

    if is_app_pack(src):
        _install_app_pack(t, h, src, args)
        return

    hap = resolve_hap(src)

    # 1. 可选签名
    sign_one = None
    if args.no_sign:
        signed = hap
    else:
        signed = args.out or os.path.splitext(src)[0] + "-signed.hap"
        keystore, pwd, alias, cert, profile = resolve_sign_args(t, args)
        do_sign(t, hap, signed, keystore, pwd, alias, cert, profile, args.sign_alg)

        def sign_one(src, outname):
            dst = os.path.join(tempfile.gettempdir(), outname)
            do_sign(t, src, dst, keystore, pwd, alias, cert, profile, args.sign_alg)
            return dst

    # 2. 安装
    if args.device_side:
        _install_via_auto_installer(t, h, signed, sign_one)
    else:
        rc, out = h.install(signed, shared=args.shared)
        print(out.rstrip())
        if rc != 0:
            Log.err("安装失败。可尝试 --device-side 走设备端 auto_installer 流程。")
            raise SystemExit(rc)
        Log.ok("安装完成")


def _install_app_pack(t, h, src, args):
    """重签名并安装 App Pack(.app)：hap + hsp 需同时安装。"""
    if args.no_sign:
        modules = app_pack_module_files(src)
    else:
        keystore, pwd, alias, cert, profile = resolve_sign_args(t, args)
        out_dir = os.path.dirname(os.path.abspath(args.out)) if args.out else tempfile.mkdtemp(prefix="happack_sign_")
        signed = sign_app_pack(t, src, out_dir, keystore, pwd, alias, cert, profile, args.sign_alg)
        modules = app_pack_module_files(signed)
    print("安装模块: " + ", ".join(os.path.basename(m) for m in modules))
    rc, out = h.install_packages(modules, replace=True)
    print(out.rstrip())
    if rc != 0 or "fail" in out.lower():
        Log.err("App Pack 安装失败。")
        raise SystemExit(rc or 1)
    Log.ok("App Pack 安装完成")


def pkg_dir(bundle):
    return f"{DEVICE_TMP}/{bundle.replace('.', '_')}"


def build_debug_app_list(hap, bundle, version, label, icons, install_time=None):
    return {
        "appList": [{
            "packageName": bundle,
            "appInfo": {
                "packageName": bundle,
                "pathList": [f"{pkg_dir(bundle)}/{os.path.basename(hap)}"],
                "version": version,
                "icon": icons,
                "label": label,
                "deviceType": ["phone", "tablet", "2in1"],
                "acl": [],
            },
            "installTime": install_time,
            "certEndTime": None,
            "canReInstall": True,
            "isGame": False,
        }]
    }


def extract_icons(hap, dest_dir):
    """从 hap 中解出 resources/base/media/* 供设备端列表显示。"""
    out = []
    with zipfile.ZipFile(hap) as z:
        for n in z.namelist():
            if n.startswith("resources/base/media/") and not n.endswith("/"):
                p = os.path.join(dest_dir, n)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(z.read(n))
                out.append(n)
    return out


def _install_via_auto_installer(t, h, hap, sign_args=None):
    """复刻 app 的设备端安装流程 (实测布局: /data/local/tmp/<pkg>/ + debug_app_list.json)。"""
    meta = read_hap_meta(hap)
    mod = meta["modules"][0]
    bundle = mod["bundleName"]
    remote_dir = pkg_dir(bundle)

    Log.info("步骤 1/6: 准备并安装设备端自动安装器 (需签名)")
    installer = t.ohos_file(AUTO_INSTALLER_HAP)
    if sign_args:
        installer = sign_args(installer, "auto_installer-signed.hap")
    rc, out = h.install(installer, replace=True)
    print(out.rstrip())
    if "no signature" in out.lower() or "fail" in out.lower():
        Log.warn("helper 未签名或安装失败，设备端流程可能无法继续。")

    Log.info(f"步骤 2/6: 创建 {remote_dir}")
    h.shell_out("mkdir", "-p", remote_dir)

    Log.info("步骤 3/6: 推送已签名 HAP 与图标资源")
    remote_hap = f"{remote_dir}/{os.path.basename(hap)}"
    h.send(hap, remote_hap)
    tmp_icons = tempfile.mkdtemp(prefix="hap_icons_")
    icons = extract_icons(hap, tmp_icons)
    for rel in icons:
        rdir = f"{remote_dir}/{os.path.dirname(rel)}"
        h.shell_out("mkdir", "-p", rdir)
        h.send(os.path.join(tmp_icons, rel), f"{remote_dir}/{rel}")

    Log.info("步骤 4/6: 生成并推送 debug_app_list.json")
    dbg = build_debug_app_list(hap, bundle, mod["versionName"], bundle, icons)
    tmp_json = tempfile.mktemp(suffix=".json")
    Path(tmp_json).write_text(json.dumps(dbg, ensure_ascii=False))
    h.send(tmp_json, DEBUG_APP_LIST)

    Log.info("步骤 5/6: 启动设备端安装器")
    h.shell_out("aa", "start", "-a", "EntryAbility", "-b", AUTO_INSTALLER_BUNDLE)

    Log.info("步骤 6/6: 轮询安装状态")
    for _ in range(30):
        time.sleep(1)
        _, out = h.shell_out("bm", "dump", "-n", bundle)
        if "applicationInfo" in out or "appId" in out:
            Log.ok(f"设备已安装 {bundle}")
            return
    Log.warn("未在超时内确认安装，请用 'hap_cli.py apps --bundle " + bundle + "' 查看。")


def cmd_uninstall(args):
    t = Tools(args.app, args.tools)
    h = Hdc(t, args.connect_key)
    rc, out = h.uninstall(args.bundle, keep=args.keep)
    print(out.rstrip())
    raise SystemExit(rc)


def cmd_push(args):
    t = Tools(args.app, args.tools)
    Hdc(t, args.connect_key).send(args.local, args.remote)


def cmd_pull(args):
    t = Tools(args.app, args.tools)
    Hdc(t, args.connect_key).recv(args.remote, args.local)


def cmd_hilog(args):
    t = Tools(args.app, args.tools)
    h = Hdc(t, args.connect_key)
    if args.cmd:
        rc, out = h.shell_out("hilog", *args.cmd)
        print(out.rstrip())
        raise SystemExit(rc)
    os.execv(h.bin, h._base() + ["hilog"])


def cmd_passthrough(args):
    """packing_tool / hnpcli 透传。"""
    t = Tools(args.app, args.tools)
    tool = t.tool(args.tool_name, required=False)
    if not tool:
        Log.err(f"找不到 {args.tool_name}。")
        raise SystemExit(1)
    os.execv(tool, [tool] + args.args)


# --------------------------------------------------------------------------- #
# 华为云 (best-effort)
# --------------------------------------------------------------------------- #
def hw_headers(token=None, uid=None, team=None, jwt=None, refresh=None):
    h = {"user-agent": "Dart/3.6 (dart:io)",
         "content-type": "application/json; charset=utf-8",
         "accept-encoding": "gzip"}
    if token:
        h["oauth2token"] = token
    if uid:
        h["uid"] = uid
    if team:
        h["teamid"] = team
    if jwt:
        h["jwttoken"] = jwt
    if refresh is not None:
        h["refresh"] = "true" if refresh else "false"
    return h


def http_req(url, method="GET", headers=None, body=None):
    import gzip as _gzip
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if "gzip" in (resp.headers.get("Content-Encoding") or "").lower():
                raw = _gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        Log.err(f"HTTP {e.code} {url}")
        return None, raw
    except Exception as e:
        Log.err(f"请求失败: {e}")
        return None, b""
    try:
        return json.loads(raw.decode("utf-8", "replace")), raw
    except Exception:
        return None, raw


def http_json(url, method="GET", token=None, client_id=None, body=None, extra_headers=None):
    headers = {}
    if token:
        headers["oauth2token"] = token
    if client_id:
        headers["client_id"] = client_id
    if extra_headers:
        headers.update(extra_headers)
    j, _ = http_req(url, method, headers, body)
    return j


def http_req_status(url, method="GET", headers=None, body=None, timeout=60):
    """同 http_req，但返回 (status, json, raw)。网络异常时 status=0。"""
    import gzip as _gzip
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            if "gzip" in (resp.headers.get("Content-Encoding") or "").lower():
                raw = _gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception:
        return 0, None, b""
    try:
        return status, json.loads(raw.decode("utf-8", "replace")), raw
    except Exception:
        return status, None, raw


AUTH_FAIL_CODES = {401, 403, 1101}


def check_token(token, uid=None, team=None, timeout=30):
    """检查 oauth2token 是否有效。返回 (ok, reason)。

    仅当服务端明确返回鉴权失败 (HTTP 401/403 或鉴权类 ret.code) 时 ok=False；
    网络异常/未知错误返回 ok=True，避免误清空凭据。
    """
    if not token:
        return False, "token 为空"
    status, j, _ = http_req_status(URL_TEAM_LIST, "GET", hw_headers(token, uid, team), timeout=timeout)
    if status in (401, 403):
        return False, f"HTTP {status} (token 过期/无效)"
    if status == 0:
        return True, "网络不可用，跳过检查"
    if isinstance(j, dict):
        ret = j.get("ret") or {}
        code = ret.get("code")
        if code in (0, None):
            return True, "有效"
        if code in AUTH_FAIL_CODES or str(code).startswith("40"):
            return False, f"ret.code={code} {ret.get('msg', '')}".strip()
        return True, f"ret.code={code} (非鉴权错误)"
    return True, "响应无法解析，跳过检查"


def clear_credentials():
    """清除本地保存的 token / uid。"""
    for f in (TOKEN_FILE, UID_FILE):
        try:
            f.unlink()
        except OSError:
            pass


def guard_team(team):
    if str(team) == ENTERPRISE_TEAM:
        Log.err(f"团队账户 {ENTERPRISE_TEAM} 受保护: 禁止对其证书/Profile 增删。"
                f"请改用个人账户 --team-id {PERSONAL_TEAM}")
        raise SystemExit(1)


def _cloud_ctx(args):
    token = args.token or os.environ.get("HUAWEI_TOKEN")
    uid = args.uid or os.environ.get("HUAWEI_UID")
    if not token:
        Log.err("需要 --token (oauth2token)。抓包/登录后从请求头获取。")
        raise SystemExit(1)
    return hw_headers(token, uid, args.team_id)


def download(url, dest):
    _, raw = http_req(url, "GET", {"user-agent": "Dart/3.6 (dart:io)"})
    if raw:
        Path(dest).write_bytes(raw)
        Log.ok(f"已下载 {dest} ({len(raw)} bytes)")
        return True
    return False


def cmd_cloud(args):
    sub = args.cloud_cmd
    if sub == "login":
        _cloud_login(args)
        return
    if sub == "token-check":
        tok = args.token or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
        uid = args.uid or (UID_FILE.read_text().strip() if UID_FILE.exists() else "")
        ok, why = check_token(tok, uid, args.team_id)
        print(("有效: " if ok else "过期/无效: ") + why)
        raise SystemExit(0 if ok else 2)
    H = _cloud_ctx(args)

    if sub == "teams":
        j, _ = http_req(URL_TEAM_LIST, "GET", H)
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif sub == "certs":
        j, _ = http_req(URL_CERT_LIST, "GET", H)
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif sub == "devices":
        j, _ = http_req(URL_DEVICE_LIST + "?start=1&pageSize=100&encodeFlag=0", "GET", H)
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif sub == "device-add":
        j, _ = http_req(URL_DEVICE_ADD, "POST", H,
                        {"deviceName": args.name, "udid": args.udid})
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif sub == "provision-add":
        guard_team(args.team_id)
        j, _ = http_req(URL_PROVISION_ADD, "POST", H, {
            "provisionName": args.name,
            "aclPermissionList": [],
            "deviceList": args.device_ids or [],
            "certList": [],
            "packageName": args.bundle_name,
        })
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif sub == "reapply":
        j, _ = http_req(URL_OBJECT_REAPPLY, "POST", H, {"sourceUrls": args.source_url})
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif sub == "cert-add":
        guard_team(args.team_id)
        csr_path = args.csr or Tools(args.app, args.tools).store_file("xiaobai.csr")
        csr = Path(csr_path).read_text()
        j, _ = http_req(URL_CERT_ADD, "POST", H, {
            "certName": args.name, "certType": int(args.cert_type), "csr": csr})
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif sub == "cert-delete":
        guard_team(args.team_id)
        j, _ = http_req(URL_CERT_DELETE, "POST", H, {"certIds": args.cert_ids})
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif sub == "flow":
        _cloud_flow(args, H)
    else:
        raise SystemExit(f"未知 cloud 子命令: {sub}")


def _cloud_flow(args, H):
    """复刻 app 的云流程: device/list -> cert/list -> reapply(下载cer) -> provision/add(下载p7b)。"""
    guard_team(args.team_id)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bundle = args.bundle_name

    Log.info("1) device/list")
    dev, _ = http_req(URL_DEVICE_LIST + "?start=1&pageSize=100&encodeFlag=0", "GET", H)
    devices = (dev or {}).get("list", [])
    dev_ids = [d["id"] for d in devices]
    Log.ok(f"   设备 {len(dev_ids)} 个")

    Log.info("2) cert/list")
    cl, _ = http_req(URL_CERT_LIST, "GET", H)
    certs = (cl or {}).get("certList", [])
    cert = next((c for c in certs if c.get("certName") == args.cert_name), None)
    if not cert and args.create_cert:
        guard_team(args.team_id)
        csr_path = args.csr or Tools(args.app, args.tools).store_file("xiaobai.csr")
        Log.info(f"   证书 '{args.cert_name}' 不存在, 申请中 (csr={csr_path})")
        r, _ = http_req(URL_CERT_ADD, "POST", H, {
            "certName": args.cert_name, "certType": 1, "csr": Path(csr_path).read_text()})
        if (r or {}).get("ret", {}).get("code") in (0, None):
            cl, _ = http_req(URL_CERT_LIST, "GET", H)
            certs = (cl or {}).get("certList", [])
            cert = next((c for c in certs if c.get("certName") == args.cert_name), None)
        else:
            Log.warn(f"   申请失败: {(r or {}).get('ret', {}).get('msg')}")
            cert = next((c for c in certs if c.get("certName") == "xiaobai-debug"), None)
            if cert:
                Log.warn("   配额已满 -> 回退复用 'xiaobai-debug'")
    if not cert:
        Log.err(f"   找不到证书 '{args.cert_name}' (可加 --create-cert 申请)。现有: " +
                ", ".join(c.get("certName", "?") for c in certs))
        raise SystemExit(1)
    Log.ok(f"   使用证书 {cert['certName']} id={cert['id']}")

    Log.info("3) reapply 下载证书")
    rp, _ = http_req(URL_OBJECT_REAPPLY, "POST", H, {"sourceUrls": cert["certObjectId"]})
    info = (rp or {}).get("urlsInfo", [{}])[0]
    cer_path = out / f"{bundle}_debug.cer"
    if info.get("newUrl"):
        download(info["newUrl"], cer_path)

    Log.info("4) provision/add")
    prov_name = f"xiaobai-debug_{bundle.replace('.', '_')}"
    pa, _ = http_req(URL_PROVISION_ADD, "POST", H, {
        "provisionName": prov_name,
        "aclPermissionList": [],
        "deviceList": dev_ids,
        "certList": [cert["id"]],
        "packageName": bundle,
    })
    url = (pa or {}).get("provisionFileUrl")
    if not url:
        Log.err(f"   provision/add 未返回 provisionFileUrl: {pa}")
        raise SystemExit(1)
    p7b_path = out / f"{bundle}_debug.p7b"
    download(url, p7b_path)
    print(f"\n证书: {cer_path}\nProfile: {p7b_path}")
    print("私钥需与证书匹配（app 本地生成/保存）；签名: hap_cli.py sign <hap> --keystore <key.pem> --app-cert "
          f"{cer_path} --profile {p7b_path}")


def _cloud_login(args):
    """本地回调服务器 + 浏览器登录 DevEco，捕获 code -> tempToken。"""
    import http.server
    import socketserver

    port = args.port
    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _capture(self):
            u = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(u.query)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = {}
            if raw:
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "json" in ctype:
                    try:
                        body = json.loads(raw.decode("utf-8", "replace"))
                    except Exception:
                        body = {"_raw": raw.decode("utf-8", "replace")}
                else:
                    body = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
            Log.info(f"回调 {self.command} {u.path} query={json.dumps(params, ensure_ascii=False)} body={json.dumps(body, ensure_ascii=False)}")
            Log.info("回调请求头: " + json.dumps(dict(self.headers), ensure_ascii=False))
            for k, v in params.items():
                captured[k] = v[0]
            for k, v in body.items():
                if isinstance(v, str):
                    captured[k] = v
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<html><body>登录完成，可关闭此页面。</body></html>".encode("utf-8"))

        def do_GET(self):
            self._capture()

        def do_POST(self):
            self._capture()

        def log_message(self, *a):
            pass

    url = (f"{URL_LOGIN}?port={port}&appid={args.appid}"
           f"&code={args.code}&version={args.version}")
    class ReuseServer(socketserver.TCPServer):
        allow_reuse_address = True

    httpd = ReuseServer(("127.0.0.1", port), Handler)
    httpd.timeout = 5
    Log.info(f"等待回调 (127.0.0.1:{port})，请在浏览器完成登录并点确认 ...")
    Log.info(f"若浏览器未自动打开，手动访问: {url}")
    webbrowser.open(url)
    deadline = time.time() + args.timeout
    try:
        while time.time() < deadline and not (captured.get("tempToken") or captured.get("code")):
            httpd.handle_request()
    finally:
        httpd.server_close()
    if not captured:
        Log.err("未收到回调。")
        raise SystemExit(1)
    Log.ok(f"收到回调参数: {json.dumps(captured, ensure_ascii=False)}")
    temp = captured.get("tempToken") or captured.get("code")
    if not temp:
        raise SystemExit("回调未含 tempToken/code。")
    Path("/tmp/last_temptoken").write_text(temp)

    tt_url = (f"{URL_TEMPTOKEN_CHECK}?site=CN&tempToken={urllib.parse.quote(temp)}"
              f"&appid={args.appid}&version={args.version}")
    Log.info(f"temptoken/check -> jwToken")
    _, raw = http_req(tt_url, "GET")
    jwt = (raw or b"").decode("utf-8", "replace").strip()
    if jwt.startswith("{"):
        try:
            jwt = (json.loads(jwt).get("jwToken") or json.loads(jwt).get("token")
                   or json.loads(jwt).get("data", {}).get("jwToken") or jwt)
        except Exception:
            pass
    if not jwt or len(jwt) < 40:
        Log.err(f"temptoken/check 未返回 jwToken: {jwt[:200]}")
        raise SystemExit(1)
    Log.ok(f"jwToken 获取成功 ({len(jwt)} 字符)")

    Log.info("jwToken/check -> accessToken(oauth2token)")
    j2, _ = http_req(URL_JWTOKEN_CHECK, "GET", hw_headers(jwt=jwt, refresh=False))
    ui = (j2 or {}).get("userInfo") or {}
    tok = ui.get("accessToken")
    uid = ui.get("userId")
    if not tok:
        Log.err(f"jwToken/check 未返回 accessToken: {j2}")
        raise SystemExit(1)
    TOKEN_FILE.write_text(tok)
    (Path.home() / ".hap_cli_uid").write_text(uid or "")
    Log.ok(f"oauth2token 已保存: {TOKEN_FILE}")
    Log.ok(f"uid: {uid}  昵称: {ui.get('name')}  实名: {ui.get('realName')}")
    print(f"\n已自动填入 GUI 输入框 (oauth2token + uid)。也可命令行使用:\n"
          f"  --token \"{tok}\" --uid {uid}")


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="hap_cli.py",
        description="HarmonyOS HAP/HSP 签名 + 安装调试助手 (小白调试助手 Python 重实现)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  hap_cli.py info app.hap
  hap_cli.py sign app.hap -o app-signed.hap
  hap_cli.py connect 192.168.1.10:5555
  hap_cli.py devices
  hap_cli.py install app.hap --no-sign
  hap_cli.py install app.hap --device-side
  hap_cli.py shell bm dump -g
  hap_cli.py uninstall com.example.app
  hap_cli.py cloud login
""")
    p.add_argument("--app", help=".app 路径 (默认自动查找)")
    p.add_argument("--tools", help="内置工具目录 (覆盖)")
    p.add_argument("--connect-key", "-t", help="hdc -t 指定设备 connect key")
    sub = p.add_subparsers(dest="command", required=True)

    def add_sign_opts(sp):
        sp.add_argument("--keystore", help="keystore (.p12/.jks)")
        sp.add_argument("--keystore-pwd", help="keystore 密码 (默认 xiaobai123)")
        sp.add_argument("--key-alias", help="key alias (默认 xiaobai)")
        sp.add_argument("--app-cert", help="app 证书 (.cer)")
        sp.add_argument("--profile", help="profile (.p7b)")
        sp.add_argument("--sign-alg", default="SHA256withECDSA")

    sp = sub.add_parser("info", help="解析 HAP/HSP 元信息")
    sp.add_argument("hap")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("sig", help="显示 HAP 签名信息 (证书/Profile)")
    sp.add_argument("hap")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sig)

    sp = sub.add_parser("sign", help="本地签名 HAP")
    sp.add_argument("hap")
    sp.add_argument("-o", "--out")
    add_sign_opts(sp)
    sp.set_defaults(func=cmd_sign)

    sp = sub.add_parser("verify", help="验签 HAP")
    sp.add_argument("hap")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("genkeys", help="生成自签名 CA + app/profile 证书")
    sp.add_argument("-o", "--out-dir", default="hap_keys")
    sp.add_argument("--key-alias", default="xiaobai")
    sp.add_argument("--org", default="XiaoBai")
    sp.add_argument("--validity", type=int, default=3650)
    sp.set_defaults(func=cmd_genkeys)

    sp = sub.add_parser("signprofile", help="签发 Provision Profile (json -> p7b)")
    sp.add_argument("--profile-json")
    sp.add_argument("--bundle-name", default="com.example.app")
    sp.add_argument("--developer-id", default="xiaoabidev")
    sp.add_argument("--uuid", default="00000000-0000-0000-0000-000000000000")
    sp.add_argument("--profile-type", default="debug")
    sp.add_argument("--validity", type=int, default=365)
    sp.add_argument("--udids", nargs="*")
    sp.add_argument("--keystore", required=True)
    sp.add_argument("--keystore-pwd", default="xiaobai123")
    sp.add_argument("--key-pwd")
    sp.add_argument("--key-alias", default="xiaobai")
    sp.add_argument("--profile-cert", required=True)
    sp.add_argument("--app-cert", required=True)
    sp.add_argument("-o", "--out")
    sp.set_defaults(func=cmd_signprofile)

    sp = sub.add_parser("devices", help="列出设备")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("connect", help="连接设备 (tconn / start / kill)")
    sp.add_argument("address", nargs="?")
    sp.add_argument("--start", action="store_true")
    sp.add_argument("--kill", action="store_true")
    sp.set_defaults(func=cmd_connect)

    sp = sub.add_parser("shell", help="执行 shell 或进入交互")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_shell)

    sp = sub.add_parser("udid", help="读取设备 UDID")
    sp.set_defaults(func=cmd_udid)

    sp = sub.add_parser("apps", help="列出已安装应用 / 可调试应用")
    sp.add_argument("--bundle", help="指定 bundle 详情")
    sp.add_argument("--debug-list", action="store_true", help="读取设备端 debug_app_list.json")
    sp.set_defaults(func=cmd_apps)

    sp = sub.add_parser("install", help="签名并安装 HAP")
    sp.add_argument("hap")
    sp.add_argument("-o", "--out")
    sp.add_argument("--no-sign", action="store_true", help="跳过签名")
    sp.add_argument("--device-side", action="store_true", help="走设备端 auto_installer 流程")
    sp.add_argument("--shared", action="store_true")
    add_sign_opts(sp)
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="卸载应用")
    sp.add_argument("bundle")
    sp.add_argument("-k", "--keep", action="store_true")
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("push", help="推送文件到设备")
    sp.add_argument("local")
    sp.add_argument("remote")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("pull", help="从设备拉取文件")
    sp.add_argument("remote")
    sp.add_argument("local")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("hilog", help="设备日志")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_hilog)

    sp = sub.add_parser("passthrough", help="透传 packing_tool / hnpcli")
    sp.add_argument("tool_name", choices=["packing_tool", "hnpcli"])
    sp.add_argument("args", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_passthrough)

    sp = sub.add_parser("cloud", help="华为云证书/Profile (best-effort)")
    csub = sp.add_subparsers(dest="cloud_cmd", required=True)
    cl = csub.add_parser("login", help="浏览器登录 DevEco (回环回调)")
    cl.add_argument("--port", type=int, default=8888)
    cl.add_argument("--appid", default="1007")
    cl.add_argument("--code", default="20698961dd4f420c8b44f49010c6f0cc")
    cl.add_argument("--version", default="0.0.0")
    cl.add_argument("--timeout", type=int, default=300)
    def add_auth(c):
        c.add_argument("--token", help="oauth2token (从抓包/登录获取)")
        c.add_argument("--uid")
        c.add_argument("--team-id")

    for name, help_ in [("teams", "团队列表"), ("certs", "证书列表"),
                        ("devices", "设备列表")]:
        add_auth(csub.add_parser(name, help=help_))
    add_auth(csub.add_parser("token-check", help="检查 oauth2token 是否过期"))
    c = csub.add_parser("device-add", help="注册调试设备")
    add_auth(c)
    c.add_argument("--name", required=True); c.add_argument("--udid", required=True)
    c = csub.add_parser("provision-add", help="创建调试 Profile")
    add_auth(c)
    c.add_argument("--bundle-name", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--device-ids", nargs="*")
    c = csub.add_parser("reapply", help="重新签发对象 URL")
    add_auth(c)
    c.add_argument("--source-url", required=True)
    c = csub.add_parser("cert-add", help="申请证书 (csr -> cert)")
    add_auth(c)
    c.add_argument("--name", required=True, help="证书名 (可含日期时间)")
    c.add_argument("--cert-type", default="1", help="1=调试 2=发布")
    c.add_argument("--csr", help="CSR 文件 (默认内置 xiaobai.csr)")
    c = csub.add_parser("cert-delete", help="删除证书 (格式待定)")
    add_auth(c)
    c.add_argument("--cert-ids", nargs="+", required=True)
    c = csub.add_parser("flow", help="一键: 设备/证书/Profile 全流程 (实测格式)")
    add_auth(c)
    c.add_argument("--bundle-name", required=True)
    c.add_argument("--cert-name", default="xiaobai-debug")
    c.add_argument("--create-cert", action="store_true", help="证书不存在时用 csr 申请")
    c.add_argument("--csr", help="CSR 文件 (默认内置 xiaobai.csr)")
    c.add_argument("-o", "--out-dir", default=".")
    sp.set_defaults(func=cmd_cloud)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        Log.warn("已取消")


if __name__ == "__main__":
    main()

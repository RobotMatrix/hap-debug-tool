#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ios_sign.py -- iOS 签名/安装助手 (纯标准库, 复用 macOS 系统工具链)

功能:
  - 列出钥匙串里的代码签名身份 (security find-identity)
  - 解析 .mobileprovision 描述文件 (CMS 解包 + plist, 含 entitlements/有效期/设备)
  - .ipa / .app 重签名: 解包 Payload -> 替换描述文件 -> codesign --force --sign
  - 安装到已连接设备 (xcrun devicectl, iOS 17+; cfgutil 兜底)
  - 校验签名 (codesign --verify --deep --strict)

macOS 自带: security / codesign / plutil / unzip / xcrun devicectl
运行: /Users/lasysloth/miniconda3/bin/python3 ios_sign.py --help
"""

import glob
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path


# ---------------------------------------------------------------- subprocess

def _run(cmd, input_text=None, timeout=120):
    """执行命令, 返回 (rc, stdout, stderr)"""
    try:
        p = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout: {' '.join(cmd[:3])}..."


# ---------------------------------------------------------------- identity

def list_identities():
    """列出钥匙串代码签名身份 -> [{hash, name}]"""
    rc, out, err = _run(["security", "find-identity", "-v", "-p", "codesigning"])
    if rc != 0:
        raise RuntimeError(f"security find-identity 失败: {err.strip() or out.strip()}")
    ids = []
    for line in out.splitlines():
        m = re.match(r'\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"(.+)"\s*$', line)
        if m:
            ids.append({"hash": m.group(1).upper(), "name": m.group(2)})
    return ids


# ---------------------------------------------------------------- provision

def read_mobileprovision(path):
    """解析 .mobileprovision -> dict(plist 字段 + path), 失败抛异常"""
    data = Path(path).read_bytes()
    # mobileprovision = CMS 签名的 plist; 找 plist 边界 "<?xml ... </plist>"
    start = data.find(b"<?xml")
    if start < 0:
        raise ValueError("不是有效的 mobileprovision (找不到 XML plist)")
    end = data.rfind(b"</plist>")
    if end < 0:
        raise ValueError("不是有效的 mobileprovision (plist 未闭合)")
    p = plistlib.loads(data[start:end + len(b"</plist>")])
    d = dict(p)
    d["_path"] = str(path)
    return d


def provision_summary(p):
    """mobileprovision dict -> 单行摘要"""
    exp = p.get("ExpirationDate")
    until = exp.strftime("%Y-%m-%d") if hasattr(exp, "strftime") else "?"
    kind = "开发" if p.get("GetTaskAllow") else ("分发" if "distribution" in str(p.get("Name", "")).lower() or "Distribution" in str(p.get("Name", "")) else "开发")
    return f"{kind} · {p.get('Name', '?')} · 至 {until}"


def entitlements_of(p):
    return (p.get("Entitlements") or {})


PROVISION_DIRS = [
    Path.home() / "Library/MobileDevice/Provisioning Profiles",
    Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles",
]

_EXTRA_PROVISION_GLOBS = [
    Path.home() / "Desktop/*.mobileprovision",
    Path.home() / "Downloads/*.mobileprovision",
    Path.home() / "Documents/*.mobileprovision",
    Path.home() / "Library/Developer/Xcode/Archives/*/*.xcarchive/Products/Applications/*.app/embedded.mobileprovision",
]


def list_profiles(include_expired=False, include_ineligible=False):
    """
    扫描 Mac 上的描述文件, 默认只返回 有效 且 可用于签名的:
      - 过滤已过期 (ExpirationDate < now)
      - 过滤 Apple 内置类型 (ProvisionsAllDevices / Xcode iOS 等不可用于重签)
      - 过滤平台不匹配 (iOS 之外: watchOS/tvOS 单独 profile 保留, macOS profile 剔除)
    排序: 有效优先 + 到期时间倒序
    """
    out = []
    seen = set()
    cand = []
    for d in PROVISION_DIRS:
        if d.is_dir():
            cand += sorted(d.glob("*.mobileprovision"))
            cand += sorted(d.glob("*.provisionprofile"))
    for g in _EXTRA_PROVISION_GLOBS:
        cand += sorted(glob.glob(str(g)))
    now = time.time()
    for f in cand:
        rp = Path(f).resolve()
        if rp in seen:
            continue
        seen.add(rp)
        try:
            p = read_mobileprovision(rp)
        except Exception:
            continue
        exp = p.get("ExpirationDate")
        t = exp.timestamp() if hasattr(exp, "timestamp") else 0
        valid = t > now
        if not include_expired and not valid:
            continue
        ent = p.get("Entitlements") or {}
        appid = ent.get("application-identifier", "")
        name = str(p.get("Name", ""))
        # enterprise 内置 / 不能装设备的 profile
        if p.get("ProvisionsAllDevices") and not include_ineligible:
            continue
        # macOS / 其他平台 profile 对 iOS 重签无用
        if (ent.get("com.apple.application-identifier")
                or "macOS" in str(p.get("Platform") or [])
                or (p.get("Platform") and "iOS" not in str(p.get("Platform"))
                    and "OSX" not in str(p.get("Platform")))):
            if not include_ineligible:
                continue
        # Xcode Cloud / watchOS-only 等杂项按名字粗滤
        if any(k in name.lower() for k in ("xcode support", "watchos", "tvos Provis")):
            if not include_ineligible:
                continue
        out.append({
            "path": str(rp),
            "name": name or "?",
            "summary": provision_summary(p),
            "appid": appid,
            "expiration": exp,
            "valid": valid,
            "team": p.get("TeamIdentifier"),
            "devices": len(p.get("ProvisionedDevices") or []),
        })
    def key(pr):
        t = pr["expiration"].timestamp() if hasattr(pr["expiration"], "timestamp") else 0
        return (0 if pr["valid"] else 1, -t)
    out.sort(key=key)
    return out


# ---------------------------------------------------------------- app discovery

def extract_ipa(ipa_path, dest, keep_signature=False):
    """解包 .ipa, 返回 Payload/<App>.app 目录; keep_signature=False 时剥离旧签名"""
    ipa = Path(ipa_path)
    with zipfile.ZipFile(ipa) as z:
        if keep_signature:
            z.extractall(dest)
        else:
            names = [n for n in z.namelist()
                     if "/_CodeSignature/" not in n and n != "_CodeSignature/CodeResources"]
            z.extractall(dest, members=names)
    payload = Path(dest) / "Payload"
    if not payload.is_dir():
        raise ValueError("ipa 缺少 Payload/ 目录")
    apps = [d for d in payload.iterdir() if d.is_dir() and d.name.endswith(".app")]
    if len(apps) != 1:
        raise ValueError(f"Payload 下应有 1 个 .app, 实际 {len(apps)}")
    return apps[0]


def find_embedded_provision(app_dir):
    """在 .app 里找 embedded.mobileprovision"""
    for name in ("embedded.mobileprovision", "embedded.provisionprofile"):
        p = Path(app_dir) / name
        if p.exists():
            return p
    return None


def get_embedded_provision(ipa_or_app, cache_dir=None):
    """
    从 .ipa/.app 提取包内 embedded.mobileprovision, 返回稳定路径 (None=没有).
    .ipa 解包到 cache_dir (<ipaname>_embedded.mobileprovision), 复用避免每次重解.
    """
    p = Path(ipa_or_app)
    if cache_dir is None:
        cache_dir = Path.home() / "Library/Caches/hap_installer/embedded_prov"
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    app_dir = p
    if p.suffix.lower() == ".ipa":
        out = cache_dir / f"{p.stem}_embedded.mobileprovision"
        with tempfile.TemporaryDirectory(prefix="iossign_emb_") as td:
            app_dir = extract_ipa(p, td, keep_signature=True)
            embedded = find_embedded_provision(app_dir)
            if not embedded:
                return None
            shutil.copy2(embedded, out)
        return out
    embedded = find_embedded_provision(app_dir)
    return embedded


# ---------------------------------------------------------------- signing

def sign_app(app_dir, identity, provision_path=None, entitlements_plist=None,
             entitlements_overrides=None, deep=True, keychain=None):
    """
    对 .app 目录做重签名:
      1. 可选替换 embedded.mobileprovision
      2. 由描述文件生成 entitlements (可叠加 overrides)
      3. 删旧签名 -> (可选) 逐个重签 PlugIns/Frameworks/XPCServices -> codesign 主包
    返回签名时使用的 entitlements dict
    """
    app_dir = Path(app_dir)
    if not app_dir.name.endswith(".app"):
        raise ValueError(f"不是 .app: {app_dir}")

    # 未显式指定 profile 时, 优先用包内 embedded.mobileprovision 生成 entitlements:
    # 只给 get-task-allow 会让 installd 报 0xe8008015 "A valid provisioning profile
    # for this executable was not found" (entitlements 缺 application-identifier)
    if not provision_path and not entitlements_plist:
        embedded = find_embedded_provision(app_dir)
        if embedded:
            try:
                read_mobileprovision(embedded)
                provision_path = embedded
            except Exception:
                pass

    ent = {}
    if provision_path:
        provision_path = Path(provision_path)
        if not provision_path.exists():
            raise FileNotFoundError(f"描述文件不存在: {provision_path}")
        prov = read_mobileprovision(provision_path)
        ent = dict(entitlements_of(prov))
        embedded_dst = app_dir / "embedded.mobileprovision"
        if provision_path.resolve() != embedded_dst.resolve():
            shutil.copy2(provision_path, embedded_dst)
        # 统一 application-identifier 前缀与证书一致, 避免 "bundle id 不匹配" 报错
        if entitlements_overrides:
            ent.update(entitlements_overrides)
        # application-identifier 保留描述文件的 TeamID 前缀; bundle id 允许通配
    else:
        if entitlements_plist:
            with open(entitlements_plist, "rb") as f:
                ent = plistlib.load(f)

    if entitlements_overrides and not provision_path:
        ent.update(entitlements_overrides)

    # entitlements 精简: profile 允许 ≠ app 可声明. hardened-process 等变体声明
    # 会与 CD flags 不一致, iOS 内核 spawn 时直接判 EBADMACHO (error 88,
    # "Malformed Mach-o file"). 只保留功能性核心项, 由 overrides 显式追加其余.
    _ENT_KEEP = (
        "application-identifier",
        "com.apple.developer.team-identifier",
        "get-task-allow",
        "keychain-access-groups",
    )
    dropped = sorted(set(ent) - set(_ENT_KEEP))
    ent = {k: v for k, v in ent.items() if k in _ENT_KEEP}
    sign_app.last_dropped_entitlements = dropped

    # 不启用 Hardened Runtime (--options runtime / CD flags 0x10000):
    # 壳处理过的二进制 (加固包) 在 runtime 严格页校验下 spawn 即被内核拒绝
    # (error 88), 且 CD version 会被升到 0x20500. 原厂签名均为 v=0x20400/flags=0,
    # 保持一致兼容性最好.
    if not ent:
        ent = {"get-task-allow": True}

    ent_file = Path(tempfile.mkdtemp(prefix="iossign_")) / "entitlements.plist"
    with open(ent_file, "wb") as f:
        plistlib.dump(ent, f)

    # 删除旧签名, 防止 --force 之后残留引发校验失败
    cs_dir = app_dir / "_CodeSignature"
    if cs_dir.exists():
        shutil.rmtree(cs_dir)

    # 先签嵌套 bundle (Frameworks/PlugIns/XPCServices/Watch), 再签主包;
    # --deep 对重签场景不可靠, 官方亦不推荐
    nested = []
    for sub in ("Frameworks", "PlugIns", "XPCServices", "Watch"):
        d = app_dir / sub
        if d.is_dir():
            nested += [x for x in sorted(d.iterdir())
                       if x.name.endswith((".framework", ".dylib", ".appex", ".xpc"))]
    for x in nested:
        rc, out, err = _run(["codesign", "--force", "--sign", identity,
                             "--timestamp=none", str(x)], timeout=300)
        if rc != 0:
            raise RuntimeError(f"codesign 嵌套失败 {x.name} (rc={rc}):\n{err.strip() or out.strip()}")

    args = ["codesign", "--force", "--sign", identity,
            "--entitlements", str(ent_file),
            "--timestamp=none"]
    args.append(str(app_dir))

    rc, out, err = _run(args, timeout=300)
    if rc != 0:
        raise RuntimeError(f"codesign 失败 (rc={rc}):\n{err.strip() or out.strip()}")
    return ent


def sign_ipa(ipa_path, out_path, identity, provision_path=None,
             entitlements_overrides=None, bundle_id=None):
    """
    重签名 .ipa:
      解包 -> 重签 .app -> 压回新 ipa
    返回产物路径
    """
    ipa_path = Path(ipa_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="iossign_") as td:
        app = extract_ipa(ipa_path, td)
        if bundle_id:
            set_bundle_id(app, bundle_id)
        sign_app(app, identity, provision_path, entitlements_overrides=entitlements_overrides)
        # 压回: 必须以 Payload/ 为根, 否则设备不认
        if out_path.exists():
            out_path.unlink()
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            payload = Path(td) / "Payload"
            for root, _dirs, files in os.walk(payload):
                for fn in files:
                    full = Path(root) / fn
                    z.write(full, full.relative_to(Path(td)))
    return out_path


def sign_app_source(src, out_dir, identity, provision_path=None,
                    entitlements_overrides=None, bundle_id=None):
    """
    对 .app 目录(或包着 .app 的任意目录)重签名, 输出为 zip (Payload/xxx.app)
    返回产物路径
    """
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    app = src
    if not app.name.endswith(".app"):
        subs = [d for d in app.iterdir() if d.is_dir() and d.name.endswith(".app")]
        if len(subs) == 1:
            app = subs[0]
        elif not subs:
            raise ValueError(f"找不到 .app: {src}")
        else:
            raise ValueError(f"多个 .app: {[s.name for s in subs]}")

    if bundle_id:
        set_bundle_id(app, bundle_id)

    # 拷贝一份再签, 不污染原目录
    with tempfile.TemporaryDirectory(prefix="iossign_") as td:
        work_app = Path(td) / "Payload" / app.name
        shutil.copytree(app, work_app, symlinks=True)
        sign_app(work_app, identity, provision_path, entitlements_overrides=entitlements_overrides)
        out = out_dir / f"{work_app.stem}-signed.ipa"
        if out.exists():
            out.unlink()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, files in os.walk(Path(td) / "Payload"):
                for fn in files:
                    full = Path(root) / fn
                    z.write(full, full.relative_to(td))
    return out


def read_bundle_id(app_dir):
    """读 Info.plist 的 CFBundleIdentifier"""
    info = Path(app_dir) / "Info.plist"
    with open(info, "rb") as f:
        return plistlib.load(f).get("CFBundleIdentifier", "")


def set_bundle_id(app_dir, bundle_id):
    """改 Info.plist 的 CFBundleIdentifier"""
    info = Path(app_dir) / "Info.plist"
    with open(info, "rb") as f:
        pl = plistlib.load(f)
    pl["CFBundleIdentifier"] = bundle_id
    with open(info, "wb") as f:
        plistlib.dump(pl, f)


def verify(app_or_ipa):
    """codesign --verify --deep --strict; ipa 先解包. 返回 (ok, 输出)"""
    p = Path(app_or_ipa)
    if p.suffix == ".ipa":
        with tempfile.TemporaryDirectory(prefix="iossign_") as td:
            app = extract_ipa(p, td, keep_signature=True)
            rc, out, err = _run(["codesign", "--verify", "--deep", "--strict", str(app)])
            detail = (err.strip() or out.strip())
            rc2, out2, _ = _run(["codesign", "-dv", "--verbose=4", str(app)])
            return rc == 0, (detail + "\n" + out2.strip()).strip()
    rc, out, err = _run(["codesign", "--verify", "--deep", "--strict", str(p)])
    detail = (err.strip() or out.strip())
    rc2, out2, _ = _run(["codesign", "-dv", "--verbose=4", str(p)])
    return rc == 0, (detail + "\n" + out2.strip()).strip()


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return "?"


def read_sign_info(app_or_ipa):
    """
    汇总 .ipa/.app 的签名信息 (对齐 hap_cli.read_sign_info 的角色):
      signed / summary / detail / team / provision{...} / cert{...}
    """
    p = Path(app_or_ipa)
    app_dir = p
    tmp = None
    if p.suffix == ".ipa":
        tmp = tempfile.TemporaryDirectory(prefix="iossign_info_")
        app_dir = extract_ipa(p, tmp.name, keep_signature=True)
    app_dir = Path(app_dir)

    rc, out, err = _run(["codesign", "--verify", "--deep", "--strict", str(app_dir)])
    signed = rc == 0
    info = {
        "signed": signed,
        "path": str(app_dir),
        "summary": "未签名" if not signed else "",
        "detail": "" if signed else (err.strip() or "codesign 校验失败"),
        "provision": {},
        "cert": {},
        "entitlements": {},
    }

    embedded = find_embedded_provision(app_dir)
    if embedded:
        try:
            prov = read_mobileprovision(embedded)
            exp = prov.get("ExpirationDate")
            created = prov.get("CreationDate")
            ent = dict(entitlements_of(prov))
            appid = ent.get("application-identifier", "")
            info["provision"] = {
                "name": prov.get("Name", "?"),
                "type": "development" if ent.get("get-task-allow") else "distribution",
                "app-identifier": appid,
                "team": prov.get("TeamIdentifier") or [],
                "created": created.timestamp() if hasattr(created, "timestamp") else None,
                "not-before": created.timestamp() if hasattr(created, "timestamp") else None,
                "not-after": exp.timestamp() if hasattr(exp, "timestamp") else None,
                "device-count": len(prov.get("ProvisionedDevices") or []),
                "aps-environment": ent.get("aps-environment"),
            }
            info["entitlements"] = ent
        except Exception as e:
            info["detail"] += f"\n(描述文件解析失败: {e})"

    rc, out, err = _run(["codesign", "-dv", "--verbose=4", str(app_dir)])
    cs_out = (err or out)
    cert = {}
    for line in cs_out.splitlines():
        if line.startswith("Authority="):
            cert.setdefault("chain", []).append(line.split("=", 1)[1])
        elif line.startswith("TeamIdentifier="):
            cert["team"] = line.split("=", 1)[1]
        elif line.startswith("Identifier="):
            cert["identifier"] = line.split("=", 1)[1]
        elif line.startswith("CDHash="):
            cert["cdhash"] = line.split("=", 1)[1]
        elif line.startswith("Signed Time="):
            cert["signed-time"] = line.split("=", 1)[1]
    if cert:
        info["cert"] = cert

    if tmp:
        tmp.cleanup()

    if signed:
        prov = info.get("provision") or {}
        _until = _fmt_ts(prov["not-after"]) if prov.get("not-after") else "?"
        info["summary"] = (f"已签名 · {prov.get('type', '?')} · "
                           f"{(cert.get('chain') or ['?'])[0]} · 至 {_until}")
    return info


def format_sign_info(info):
    """签名信息 dict -> 多行文本 (对齐 hap_cli.format_sign_info 风格)"""
    if not info.get("signed"):
        return "已签名: 否\n" + (info.get("detail") or "")
    prov = info.get("provision") or {}
    cert = info.get("cert") or {}
    ent = info.get("entitlements") or {}
    lines = ["已签名: 是"]
    lines.append(f"  bundle-id    : {cert.get('identifier', '?')}")
    lines.append(f"  签名类型     : {prov.get('type', '?')}   Team: {cert.get('team', '?')}")
    chain = cert.get("chain") or []
    if chain:
        lines.append(f"  证书链       : {chain[0]}")
        for c in chain[1:]:
            lines.append(f"                 └ {c}")
    if prov:
        lines.append(f"  描述文件     : {prov.get('name')}  ({prov.get('app-identifier')})")
        if prov.get("not-before") and prov.get("not-after"):
            lines.append(f"  profile有效期: {_fmt_ts(prov['not-before'])} ~ {_fmt_ts(prov['not-after'])}")
        lines.append(f"  授权设备数   : {prov.get('device-count', 0)}")
        if prov.get("aps-environment"):
            lines.append(f"  推送环境     : {prov['aps-environment']}")
    if cert.get("cdhash"):
        lines.append(f"  CDHash       : {cert['cdhash']}")
    keys = [k for k in ent.keys() if k != "application-identifier"]
    if keys:
        lines.append(f"  entitlements : {', '.join(sorted(keys))}")
    return "\n".join(lines)


# ---------------------------------------------------------------- devices / install

def list_devices():
    """列出 iOS 设备 (devicectl, 兜底 cfgutil). 返回 [(id, 名称, 状态)], 含模拟器和不可用设备"""
    rc, out, err = _run(["xcrun", "devicectl", "list", "devices"], timeout=60)
    if rc == 0:
        devs = []
        for line in out.splitlines():
            if not line.strip() or line.lstrip().startswith("Name") or set(line.strip()) <= {"-", " "}:
                continue
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 4:
                continue
            name, ident, state, model = parts[0], parts[1], parts[2], " ".join(parts[3:])
            m = re.match(r"([0-9A-Za-z-]+)", ident)
            if not m or m.group(1) == "UDID":
                continue
            devs.append((m.group(1), name, state, "physical" in model))
        return devs
    rc, out, err = _run(["cfgutil", "list-devices"], timeout=60)
    if rc == 0:
        devs = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] not in ("UDID", ""):
                devs.append((parts[0], parts[1].strip(), "Connected", True))
        return devs
    raise RuntimeError(f"设备枚举失败: devicectl/cfgutil 均不可用\n{err.strip()}")


def _unsigned_nested_bundles(ipa_path):
    """返回 ipa 内未签名的嵌套 bundle 相对路径列表 (installd 会因它们拒装整个包)"""
    try:
        with tempfile.TemporaryDirectory(prefix="iossign_chk_") as td:
            app = extract_ipa(ipa_path, td, keep_signature=True)
            bad = []
            for sub in ("Frameworks", "PlugIns", "XPCServices"):
                d = Path(app) / sub
                if not d.is_dir():
                    continue
                for x in sorted(d.iterdir()):
                    if not x.name.endswith((".framework", ".dylib", ".appex", ".xpc")):
                        continue
                    rc, _out, _err = _run(["codesign", "--verify", str(x)])
                    if rc != 0:
                        bad.append(f"{sub}/{x.name}")
            return bad
    except Exception:
        return []


def install_to_device(ipa_path, device_id=None, timeout=300, identity=None):
    """安装 ipa 到设备. device_id 为空时选第一台可用的物理设备; 无可用时给出具体原因.
    identity 给出时, 检测到未签名嵌套 bundle 会自动重签后安装"""
    ipa_path = str(ipa_path)
    devs = list_devices()
    physical = [d for d in devs if d[3]]
    if device_id:
        target = next((d for d in physical if d[0].startswith(device_id)), None)
        if not target:
            raise RuntimeError(f"未找到设备 {device_id}")
    else:
        available = [d for d in physical if d[2] in ("Available", "connected", "Connected")]
        if available:
            target = available[0]
        elif physical:
            name, _id, state, _ = physical[0]
            raise RuntimeError(
                f"发现物理设备 '{name}' 但状态为 {state}, 暂不可安装。\n"
                "  常见原因: 1) iPhone 已锁屏 -> 解锁后重试\n"
                "           2) 未信任此电脑 -> iPhone 上点『信任』并输入密码\n"
                "           3) 未开启开发者模式 -> 设置>隐私与安全性>开发者模式\n"
                "           4) iOS 17+ 需要 Xcode/CoreDevice 服务, 打开一次 Xcode 或重插数据线")
        else:
            raise RuntimeError(f"没有物理 iOS 设备 (共 {len(devs)} 台, 均为模拟器/不可用)")
        device_id = target[0]
        print(f"[*] 自动选择设备: {target[1]} ({device_id})")
    if identity is None:
        ids = list_identities()
        identity = ids[0]["name"] if ids else None
    bad = _unsigned_nested_bundles(ipa_path)
    if bad:
        if not identity:
            print(f"[-] 包内未签名组件: {', '.join(bad)}; 未找到可用证书, 无法自动重签, 直接尝试安装")
        else:
            print(f"[*] 检测到未签名组件: {', '.join(bad)} -> 自动重签 (证书: {identity})")
            src = Path(ipa_path)
            fixed = src.parent / f"{src.stem}-autosign.ipa"
            sign_ipa(src, fixed, identity)
            ok, detail = verify(fixed)
            if not ok:
                raise RuntimeError(f"自动重签后校验失败:\n{detail}")
            ipa_path = str(fixed)
            print(f"[+] 自动重签产物: {fixed}")
    rc, out, err = _run(["xcrun", "devicectl", "device", "install", "app",
                         "--device", device_id, ipa_path], timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"devicectl 安装失败 (rc={rc}):\n{err.strip() or out.strip()}")
    return (out or err).strip()


def launch_on_device(bundle_id, device_id=None):
    """启动设备上的应用"""
    if not device_id:
        physical = [d for d in list_devices() if d[3] and d[2] == "Available"]
        if not physical:
            raise RuntimeError("没有可用的物理 iOS 设备 (解锁并信任后重试)")
        device_id = physical[0][0]
    rc, out, err = _run(["xcrun", "devicectl", "device", "process", "launch",
                         "--device", device_id, bundle_id], timeout=120)
    if rc != 0:
        raise RuntimeError(f"启动失败 (rc={rc}):\n{err.strip() or out.strip()}")
    return (out or err).strip()


# ---------------------------------------------------------------- CLI

def main():
    import argparse
    ap = argparse.ArgumentParser(description="iOS 签名助手 (security/codesign/devicectl)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("identities", help="列出钥匙串签名身份")
    sub.add_parser("devices", help="列出已连接 iOS 设备")
    sub.add_parser("profiles", help="列出 Mac 上的描述文件 (Xcode/MobileDevice)")

    s = sub.add_parser("provision", help="解析描述文件")
    s.add_argument("path")

    s = sub.add_parser("sign", help="签名 .ipa / .app")
    s.add_argument("path")
    s.add_argument("-i", "--identity", required=True, help="证书 common name 或 SHA-1")
    s.add_argument("-p", "--provision", help=".mobileprovision 路径")
    s.add_argument("-o", "--out", help="输出路径 (默认 ./<名字>-signed.ipa)")
    s.add_argument("-b", "--bundle-id", help="覆盖 CFBundleIdentifier")
    s.add_argument("-e", "--ent", action="append", default=[],
                   help="追加 entitlements 覆盖, key=value, 可多次")

    s = sub.add_parser("verify", help="校验签名")
    s.add_argument("path")

    s = sub.add_parser("install", help="安装 ipa 到设备")
    s.add_argument("path")
    s.add_argument("-d", "--device", help="设备 UDID (默认第一台可用)")
    s.add_argument("-i", "--identity", help="自动重签用的证书 (检测到未签名组件时启用; 默认取第一张可用证书)")

    s = sub.add_parser("launch", help="启动设备上的应用")
    s.add_argument("bundle_id")
    s.add_argument("-d", "--device", help="设备 UDID")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return

    if args.cmd == "identities":
        for i in list_identities():
            print(f"{i['hash']}  {i['name']}")
    elif args.cmd == "profiles":
        ps = list_profiles()
        if not ps:
            print("(未找到描述文件; Xcode 签名过一次后才有)")
        for pr in ps:
            mark = "✓" if pr["valid"] else "✗"
            print(f"{mark} {pr['name']}  [{pr['appid']}]  {pr['summary']}\n  {pr['path']}")
    elif args.cmd == "devices":
        for d in list_devices():
            print(f"{d[0]}  {d[1]}  {d[2]}")
    elif args.cmd == "provision":
        p = read_mobileprovision(args.path)
        print(provision_summary(p))
        print("TeamID:", p.get("TeamIdentifier"))
        print("AppID:", p.get("Entitlements", {}).get("application-identifier"))
        print("设备数:", len(p.get("ProvisionedDevices") or []))
    elif args.cmd == "sign":
        overrides = {}
        for kv in args.ent:
            k, _, v = kv.partition("=")
            overrides[k] = v == "true" if v in ("true", "false") else v
        src = Path(args.path)
        out = Path(args.out) if args.out else Path.cwd() / f"{src.stem}-signed.ipa"
        if src.suffix == ".ipa":
            got = sign_ipa(src, out, args.identity, args.provision, overrides, args.bundle_id)
        else:
            got = sign_app_source(src, out.parent, args.identity, args.provision, overrides, args.bundle_id)
        print(f"[+] 已签名: {got}")
        ok, detail = verify(got)
        print(("[+] 校验通过" if ok else "[-] 校验失败") + "\n" + detail)
    elif args.cmd == "verify":
        ok, detail = verify(args.path)
        print(("[+] 校验通过" if ok else "[-] 校验失败") + "\n" + detail)
        raise SystemExit(0 if ok else 1)
    elif args.cmd == "install":
        print(install_to_device(args.path, args.device, identity=args.identity))
    elif args.cmd == "launch":
        print(launch_on_device(args.bundle_id, args.device))


if __name__ == "__main__":
    main()

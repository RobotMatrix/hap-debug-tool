#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hap_sniff.py -- 本地 MITM 代理，抓取 "小白调试助手" 对华为云的真实 HTTP(S) 请求。

原理:
  Dart/Flutter 的 dart:io HttpClient 默认使用 findProxyFromEnvironment，
  读取 http_proxy / https_proxy 环境变量。用代理方式启动 app 即可拦截。
  实测 app 走系统信任库且不放行自签证书，必须把 CA 信任到系统钥匙串:
      sudo python3 hap_sniff.py --install-ca --system

用法:
  # 1) 启动代理 (生成 ~/.hap_sniff/ca.crt 自签名 CA)
  python3 hap_sniff.py --port 8080

  # 2) 另开终端，用代理启动 app
  python3 hap_sniff.py --launch
     # 或手动:
     # env https_proxy=http://127.0.0.1:8080 http_proxy=http://127.0.0.1:8080 \
     #   "/Applications/小白调试助手.app/Contents/MacOS/小白调试助手"

  # 3) 在 app 里点 "登录/证书/设备" 等，请求会实时打印并写入
  #    ~/.hap_sniff/capture-<时间>.jsonl

无第三方依赖，仅标准库 + openssl CLI。
"""

import argparse
import glob
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

CA_DIR = Path(os.environ.get("HAP_CA_DIR") or os.path.expanduser("~/.hap_sniff"))
HUAWEI_HINT = ("huawei.com", "devecostudio", "connect-api", "hwcloud")
MAX_BODY_LOG = 200000


def log(tag, color, msg):
    if sys.stdout.isatty():
        print(f"\033[{color}m{tag}\033[0m {msg}", flush=True)
    else:
        print(f"{tag} {msg}", flush=True)


def openssl(*args):
    r = subprocess.run(["openssl", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"openssl {' '.join(args)} 失败:\n{r.stderr}")
    return r.stdout


def ensure_ca():
    CA_DIR.mkdir(parents=True, exist_ok=True)
    key = CA_DIR / "ca.key"
    crt = CA_DIR / "ca.crt"
    if key.exists() and crt.exists():
        return key, crt
    sec1 = CA_DIR / "ca.sec1.key"
    openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(sec1))
    openssl("pkcs8", "-topk8", "-nocrypt", "-in", str(sec1), "-out", str(key))
    os.remove(sec1)
    cnf = CA_DIR / "ca.cnf"
    cnf.write_text(
        "[req]\n"
        "distinguished_name=dn\n"
        "x509_extensions=v3_ca\n"
        "prompt=no\n"
        "[dn]\n"
        "CN=hap-sniff Root CA\n"
        "[v3_ca]\n"
        "basicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
    )
    csr = CA_DIR / "ca.csr"
    openssl("req", "-new", "-key", str(key), "-out", str(csr), "-subj", "/CN=hap-sniff Root CA")
    openssl("x509", "-req", "-in", str(csr), "-signkey", str(key), "-out", str(crt),
            "-days", "3650", "-sha256", "-extfile", str(cnf), "-extensions", "v3_ca")
    os.remove(csr)
    log("[+]", "32", f"已生成 CA: {crt}")
    return key, crt


class CertCache:
    def __init__(self, ca_key, ca_crt):
        self.ca_key = ca_key
        self.ca_crt = ca_crt
        self.dir = CA_DIR / "certs"
        self.dir.mkdir(exist_ok=True)
        self.lock = threading.Lock()

    def get(self, host):
        host = host.split(":")[0]
        crt = self.dir / f"{host}.crt"
        key = self.dir / f"{host}.key"
        with self.lock:
            if crt.exists() and key.exists():
                return crt, key
            sec1 = self.dir / f"{host}.sec1.key"
            csr = self.dir / f"{host}.csr"
            ext = self.dir / f"{host}.ext"
            openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(sec1))
            openssl("pkcs8", "-topk8", "-nocrypt", "-in", str(sec1), "-out", str(key))
            openssl("req", "-new", "-key", str(key), "-out", str(csr), "-subj", f"/CN={host}")
            ext.write_text(
                f"subjectAltName=DNS:{host}\n"
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n"
            )
            openssl("x509", "-req", "-in", str(csr), "-CA", str(self.ca_crt), "-CAkey", str(self.ca_key),
                    "-CAcreateserial", "-out", str(crt), "-days", "3650", "-sha256", "-extfile", str(ext))
            for f in (sec1, csr, ext):
                try:
                    os.remove(f)
                except OSError:
                    pass
            return crt, key


class Buf:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def readline(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                line, self.buf = self.buf, b""
                return line
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line + b"\r\n"

    def read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def read_some(self):
        if self.buf:
            d, self.buf = self.buf, b""
            return d
        return self.sock.recv(65536)


def read_http_request(reader, host_hint):
    line = reader.readline()
    if not line:
        return None
    try:
        head = line.decode("latin1").rstrip("\r\n")
    except Exception:
        return None
    if not head:
        return None
    parts = head.split(" ")
    if len(parts) < 3:
        return None
    method, target, version = parts[0], parts[1], parts[2]

    headers = {}
    while True:
        hl = reader.readline()
        if not hl or hl == b"\r\n":
            break
        try:
            k, v = hl.decode("latin1").rstrip("\r\n").split(":", 1)
            headers[k.strip()] = v.strip()
        except ValueError:
            continue

    body = b""
    if headers.get("Transfer-Encoding", "").lower() == "chunked":
        while True:
            size_line = reader.readline()
            try:
                size = int(size_line.strip().split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                reader.readline()
                break
            body += reader.read(size)
            reader.readline()
    elif "Content-Length" in headers:
        try:
            body = reader.read(int(headers["Content-Length"]))
        except ValueError:
            body = b""
    return method, target, version, headers, body


def read_http_response(reader):
    status_line = reader.readline()
    if not status_line:
        return None
    try:
        head = status_line.decode("latin1").rstrip("\r\n")
    except Exception:
        return None
    headers = {}
    while True:
        hl = reader.readline()
        if not hl or hl == b"\r\n":
            break
        try:
            k, v = hl.decode("latin1").rstrip("\r\n").split(":", 1)
            headers[k.strip()] = v.strip()
        except ValueError:
            continue
    body = b""
    if headers.get("Transfer-Encoding", "").lower() == "chunked":
        while True:
            size_line = reader.readline()
            try:
                size = int(size_line.strip().split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                reader.readline()
                break
            body += reader.read(size)
            reader.readline()
    elif "Content-Length" in headers:
        try:
            body = reader.read(int(headers["Content-Length"]))
        except ValueError:
            body = b""
    else:
        while True:
            d = reader.read_some()
            if not d:
                break
            body += d
    return status_line, headers, body


class Proxy:
    def __init__(self, certs, capture_path, only_huawei=True):
        self.certs = certs
        self.capture_path = capture_path
        self.only_huawei = only_huawei
        self.n = 0
        self.lock = threading.Lock()

    def record(self, host, method, url, req_headers, req_body, status, resp_headers, resp_body):
        relevant = (not self.only_huawei) or any(h in host for h in HUAWEI_HINT)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "host": host, "method": method, "url": url,
            "status": status,
            "request_headers": req_headers,
            "request_body": req_body.decode("utf-8", "replace")[:MAX_BODY_LOG],
            "response_headers": resp_headers,
            "response_body": resp_body.decode("utf-8", "replace")[:MAX_BODY_LOG],
        }
        with self.lock:
            with open(self.capture_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.n += 1
        if relevant:
            log(f"[{status}]", "36", f"{method} {url}")
            if req_body:
                try:
                    log("  req", "33", json.dumps(json.loads(req_body), ensure_ascii=False)[:1500])
                except Exception:
                    log("  req", "33", req_body.decode("utf-8", "replace")[:1500])
            if resp_body:
                try:
                    log("  resp", "32", json.dumps(json.loads(resp_body), ensure_ascii=False)[:1500])
                except Exception:
                    log("  resp", "32", resp_body.decode("utf-8", "replace")[:800])

    def handle(self, conn, addr):
        try:
            reader = Buf(conn)
            line = reader.readline()
            if not line:
                return
            head = line.decode("latin1").rstrip("\r\n")
            if not head.upper().startswith("CONNECT"):
                self.handle_plain(conn, reader, head)
                return
            hostport = head.split(" ")[1]
            host, _, port = hostport.partition(":")
            port = int(port or 443)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            crt, key = self.certs.get(host)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(crt), str(key))
            tls = ctx.wrap_socket(conn, server_side=True)
            self.handle_tls(tls, host, port)
        except Exception as e:
            log("[!]", "31", f"连接 {addr} 出错: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def handle_tls(self, tls, host, port):
        reader = Buf(tls)
        while True:
            req = read_http_request(reader, host)
            if not req:
                break
            method, target, version, headers, body = req
            if target.startswith("http://") or target.startswith("https://"):
                url = target
                _, _, rest = target.partition("://")
                up_host = rest.split("/")[0]
            else:
                url = f"https://{host}{target}"
                up_host = host
            status, resp_headers, resp_body = self.forward(up_host, port, method, target, headers, body)
            if status is None:
                break
            self.record(host, method, url, headers, body, status, resp_headers, resp_body)
            out = [status]
            for k, v in resp_headers.items():
                out.append(f"{k}: {v}")
            head = ("\r\n".join(out) + "\r\n\r\n").encode("latin1")
            tls.sendall(head + resp_body)
            if resp_headers.get("Connection", "").lower() == "close":
                break

    def handle_plain(self, conn, reader, head):
        parts = head.split(" ")
        if len(parts) < 3:
            return
        method, target = parts[0], parts[1]
        headers = {}
        while True:
            hl = reader.readline()
            if not hl or hl == b"\r\n":
                break
            try:
                k, v = hl.decode("latin1").rstrip("\r\n").split(":", 1)
                headers[k.strip()] = v.strip()
            except ValueError:
                continue
        body = b""
        if "Content-Length" in headers:
            body = reader.read(int(headers["Content-Length"]))
        _, _, rest = target.partition("://")
        host = rest.split("/")[0] if rest else headers.get("Host", "")
        status, resp_headers, resp_body = self.forward(host, 80, method, target, headers, body)
        if status:
            self.record(host, method, target, headers, body, status, resp_headers, resp_body)
            out = [status] + [f"{k}: {v}" for k, v in resp_headers.items()]
            conn.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin1") + resp_body)

    def forward(self, host, port, method, target, headers, body):
        hostname = host.split(":")[0]
        try:
            raw = socket.create_connection((hostname, port), timeout=30)
            if port == 443:
                cctx = ssl.create_default_context()
                cctx.check_hostname = False
                cctx.verify_mode = ssl.CERT_NONE
                raw = cctx.wrap_socket(raw, server_hostname=hostname)
            path = target
            if target.startswith("http"):
                from urllib.parse import urlsplit
                sp = urlsplit(target)
                path = sp.path + (("?" + sp.query) if sp.query else "")
            hdr = dict(headers)
            hdr["Host"] = hostname
            hdr.setdefault("Connection", "close")
            req = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in hdr.items()]
            raw.sendall(("\r\n".join(req) + "\r\n\r\n").encode("latin1") + body)
            rreader = Buf(raw)
            resp = read_http_response(rreader)
            raw.close()
            if not resp:
                return None, {}, b""
            return resp[0].decode("latin1").rstrip("\r\n"), resp[1], resp[2]
        except Exception as e:
            log("[!]", "31", f"转发 {host} 失败: {e}")
            return None, {}, b""


def keychain(system):
    if system:
        return "/Library/Keychains/System.keychain"
    return str(Path.home() / "Library/Keychains/login.keychain-db")


def install_ca(ca_crt, system=False):
    cmd = ["security", "add-trusted-cert", "-r", "trustRoot", "-k", keychain(system), str(ca_crt)]
    if system:
        cmd = ["sudo"] + cmd
    log("[*]", "36", "$ " + " ".join(cmd))
    r = subprocess.run(cmd)
    log("[+]" if r.returncode == 0 else "[-]", "32" if r.returncode == 0 else "31",
        f"rc={r.returncode}")


def uninstall_ca(ca_crt, system=False):
    cmd = ["security", "remove-trusted-cert", "-d", str(ca_crt)]
    if system:
        cmd = ["sudo"] + cmd
    log("[*]", "36", "$ " + " ".join(cmd))
    subprocess.run(cmd)


def launch_app(app):
    from hap_cli import find_app
    p = find_app(app)
    if not p:
        log("[-]", "31", "找不到 .app，请用 --app 指定。")
        sys.exit(1)
    exe = p / "Contents" / "MacOS" / "小白调试助手"
    if not exe.exists():
        cands = list((p / "Contents" / "MacOS").glob("*"))
        exe = cands[0] if cands else exe
    env = dict(os.environ)
    env["https_proxy"] = f"http://127.0.0.1:{ARGS.port}"
    env["http_proxy"] = f"http://127.0.0.1:{ARGS.port}"
    env["HTTPS_PROXY"] = env["https_proxy"]
    env["HTTP_PROXY"] = env["http_proxy"]
    log("[+]", "32", f"启动 app (proxy={env['https_proxy']}): {exe}")
    subprocess.Popen([str(exe)], env=env)


def main():
    global ARGS
    ap = argparse.ArgumentParser(description="抓取小白调试助手 -> 华为云请求的本地 MITM 代理")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--all", action="store_true", help="记录所有域名 (默认仅华为相关)")
    ap.add_argument("--launch", action="store_true", help="用代理环境变量启动 app")
    ap.add_argument("--app", help=".app 路径")
    ap.add_argument("--install-ca", action="store_true", help="信任 CA 后退出")
    ap.add_argument("--uninstall-ca", action="store_true", help="移除 CA 信任后退出")
    ap.add_argument("--system", action="store_true", help="配合 --install-ca 用系统钥匙串(需 sudo)")
    ARGS = ap.parse_args()

    ca_key, ca_crt = ensure_ca()
    if ARGS.install_ca:
        install_ca(ca_crt, ARGS.system)
        return
    if ARGS.uninstall_ca:
        uninstall_ca(ca_crt, ARGS.system)
        return
    certs = CertCache(ca_key, ca_crt)
    capture_path = CA_DIR / f"capture-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    proxy = Proxy(certs, capture_path, only_huawei=not ARGS.all)

    if ARGS.launch:
        launch_app(ARGS.app)
        time.sleep(1)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((ARGS.host, ARGS.port))
    except OSError as e:
        if ARGS.launch:
            log("[i]", "36", f"端口 {ARGS.port} 已占用(已有代理在跑)，仅启动 app。")
            return
        raise SystemExit(f"端口 {ARGS.port} 被占用: {e}")
    srv.listen(128)
    log("[+]", "32", f"代理监听 {ARGS.host}:{ARGS.port}")
    log("[+]", "32", f"抓包文件: {capture_path}")
    log("[i]", "36", "需先把 CA 信任到系统钥匙串: sudo python3 hap_sniff.py --install-ca --system")
    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=proxy.handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        log("[*]", "33", f"共捕获 {proxy.n} 条请求。")
    finally:
        srv.close()


if __name__ == "__main__":
    main()

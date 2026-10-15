"""Serve the panel and subscriptions over a validated HTTPS endpoint."""

import asyncio
import os
import secrets
import shutil
from pathlib import Path

import uvicorn

from . import brand, db, singbox
from .system_ops import panel_port, run_cmd

# Sample traffic once per minute; online status uses a three-minute window.
STATS_INTERVAL = 60

PANEL_CERT = db.DATA_ROOT / 'web' / 'panel-cert-current' / 'fullchain.pem'
PANEL_KEY = db.DATA_ROOT / 'web' / 'panel-cert-current' / 'key.pem'


def ensure_panel_cert():
    """Require a panel certificate matching this server.

    Normally this must be a publicly issued certificate: the login form and
    subscription URLs carry credentials, so silently accepting a self-signed
    certificate would invite man-in-the-middle attacks.

    NAT / port-restricted hosts cannot pass an ACME HTTP-01 challenge at all
    (port 80 belongs to the host machine), so they may opt in to a self-signed
    certificate via settings. The opt-in is explicit and stays visible in the
    panel, and the browser warning is expected in that mode.
    """
    self_signed_allowed = db.get_setting('panel_allow_self_signed') == '1'
    if not (PANEL_CERT.is_file() and PANEL_KEY.is_file()):
        if not self_signed_allowed:
            raise RuntimeError('缺少可信面板证书；请运行安装器或证书续期服务')
        # NAT 模式：证书还没生成过，就地签发一张自签证书。
        _issue_self_signed_panel_cert()
    elif self_signed_allowed:
        # 自签模式下证书在 root 拥有的库中，面板用户需要「穿过」DATA_DIR 才能读到。
        # 安装器已设置好权限；这里兜底修复目录被改回 0700 的情况（例如升级或恢复备份后）。
        _ensure_cert_traversable()
    host = db.get_setting('public_host') or ''
    checks = [
        ['openssl', 'x509', '-in', str(PANEL_CERT), '-checkend', '86400', '-noout'],
    ]
    # 自签证书里 CN/SAN 写的是内网地址，而 public_host 可能是宿主机的映射入口，
    # 两者对不上是正常的，因此 NAT 模式下跳过身份校验，只查有效期与密钥匹配。
    if not self_signed_allowed:
        checks.append(
            ['openssl', 'x509', '-in', str(PANEL_CERT),
             '-checkip' if host.replace('.', '').isdigit() or ':' in host else '-checkhost',
             host, '-noout'])
    for command in checks:
        code, out, err = run_cmd(command, 15)
        if code != 0:
            raise RuntimeError(err or out or '面板证书身份或有效期检查失败')
    _, subject, _ = run_cmd(['openssl', 'x509', '-in', str(PANEL_CERT),
                              '-noout', '-subject', '-nameopt', 'RFC2253'], 10)
    _, issuer, _ = run_cmd(['openssl', 'x509', '-in', str(PANEL_CERT),
                             '-noout', '-issuer', '-nameopt', 'RFC2253'], 10)
    is_self_signed = subject.partition('=')[2] == issuer.partition('=')[2]
    if is_self_signed and not self_signed_allowed:
        raise RuntimeError('面板证书是自签证书，拒绝开放密码登录')
    cert_code, cert_public, cert_error = run_cmd(
        ['openssl', 'x509', '-in', str(PANEL_CERT), '-pubkey', '-noout'], 10)
    key_code, key_public, key_error = run_cmd(
        ['openssl', 'pkey', '-in', str(PANEL_KEY), '-pubout'], 10)
    if cert_code or key_code or cert_public != key_public:
        raise RuntimeError(cert_error or key_error or '面板证书和私钥不匹配')
    # 收紧私钥权限。证书可能属于 root（NAT 模式下的自签证书在 root 拥有的证书库里），
    # 此时面板用户无权 chmod，属正常情况——能读到即可，权限由安装器保证。
    try:
        PANEL_KEY.chmod(0o600)
    except PermissionError:
        pass


def _issue_self_signed_panel_cert():
    """为 NAT 模式签发自签面板证书。

    面板进程以非特权用户运行，写不了 /etc/freedom-vpn/web/（该目录属于面板用户，
    但证书要指向 root 拥有的证书库）。因此这里以 root 身份调用：安装器在 NAT 模式下
    会提前调用本函数；若在面板进程里被调用且没有权限，会给出明确错误而不是崩溃。

    证书落在 root 拥有的 PANEL_CERT_STORE，再用符号链接暴露到 web/panel-cert-current
    —— 与安装器为 Let's Encrypt 证书建立的布局完全一致。
    """
    import ipaddress
    store = Path(os.environ.get('IVPN_PANEL_CERT_STORE') or brand.CERT_STORE)
    version = store / 'self-signed'
    version.mkdir(parents=True, exist_ok=True)
    cert_path = version / 'fullchain.pem'
    key_path = version / 'key.pem'

    host = (db.get_setting('public_host') or '').strip()
    names = [n for n in {host, 'localhost', '127.0.0.1'} if n]
    # 证书里同时写上内网地址，方便从容器内直接访问时也能通过主机名校验。
    try:
        if host:
            names.append(str(ipaddress.ip_address(host)))
    except ValueError:
        pass
    san = ','.join(
        f'IP:{n}' if _looks_like_ip(n) else f'DNS:{n}' for n in dict.fromkeys(names))

    code, out, err = run_cmd([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-days', '3650', '-keyout', str(key_path), '-out', str(cert_path),
        '-subj', f'/CN={host or "renrenvpn"}', '-addext', f'subjectAltName={san}',
    ], 90)
    if code != 0 or not cert_path.is_file():
        raise RuntimeError(f'生成自签面板证书失败：{err or out or "openssl 执行失败"}')

    # 权限：面板以非特权用户（brand.WEB_USER）运行，必须能读到证书与私钥。
    # 目录 0750 + 同组、文件 0640 同组可读——沿用安装器对 Let's Encrypt
    # 证书的处理方式（见 fix_data_perms）。私钥不给 other 读。
    for target in (store, version):
        try:
            shutil.chown(target, group=brand.WEB_USER)
        except (LookupError, PermissionError, OSError):
            pass
        target.chmod(0o750)
    for target in (cert_path, key_path):
        try:
            shutil.chown(target, group=brand.WEB_USER)
        except (LookupError, PermissionError, OSError):
            pass
        target.chmod(0o640)

    # 建立 web/panel-cert-current → store/self-signed 的符号链接（与安装器一致）。
    # PANEL_CERT 是 .../panel-cert-current/fullchain.pem，所以链接本身是它的父目录。
    link = PANEL_CERT.parent
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.is_dir():
        # 已有一个真实目录（例如安装器为 Let's Encrypt 建的版本目录）——替换掉。
        shutil.rmtree(link)
    tmp = link.parent / f'.panel-cert-current.{secrets.token_hex(4)}'
    tmp.symlink_to(version)
    os.replace(tmp, link)


def _looks_like_ip(value):
    import ipaddress
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


class _Server(uvicorn.Server):
    # Avoid competing signal handlers; systemd terminates the process with SIGTERM.
    def install_signal_handlers(self):
        pass


def _ensure_cert_traversable():
    """确保面板用户能穿过 DATA_DIR 读到证书。

    证书库（brand.CERT_STORE）属于 root、组为面板服务用户，但它的路径要经过
    数据根目录。该目录默认 0700 root:root，非特权面板进程无法进入，
    表现为「证书存在却读不到」。这里只放开组进入位，不改变属主。
    """
    try:
        root = db.DATA_ROOT
        import grp
        gid = grp.getgrnam(brand.WEB_USER).gr_gid
        if root.stat().st_gid != gid:
            os.chown(root, -1, gid)
        mode = root.stat().st_mode & 0o777
        if not mode & 0o050:      # 组缺少 r-x，补上
            root.chmod(0o750)
    except (KeyError, OSError, PermissionError):
        # 非 root 或环境不匹配；安装器会处理，读不到时由下面的校验给出明确错误。
        pass


def main():
    ensure_panel_cert()
    # Share HTTPS between the panel and subscriptions; reserve port 80 for ACME renewal.
    # Disable access logs because subscription URLs contain bearer tokens.
    common = {'host': '0.0.0.0', 'access_log': False, 'server_header': False}
    servers = [
        _Server(uvicorn.Config('app.main:app', port=panel_port(),
                               ssl_certfile=str(PANEL_CERT), ssl_keyfile=str(PANEL_KEY),
                               **common)),
    ]

    async def collect_forever():
        """Persist sing-box traffic counters on a single process-level schedule."""
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            try:
                await asyncio.to_thread(singbox.collect_stats)
            except Exception:
                pass

    async def run_all():
        tasks = [s.serve() for s in servers]
        if os.environ.get('IVPN_PREVIEW') != '1':
            tasks.append(collect_forever())
        await asyncio.gather(*tasks)

    asyncio.run(run_all())


if __name__ == '__main__':
    main()

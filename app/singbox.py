"""Render sing-box configuration from persistent state and apply it safely.

Configuration changes are validated before replacement. Service restart failures
restore the previous configuration.
"""

import functools
import base64
import contextlib
import fcntl
import json
import hashlib
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import concurrent.futures
from pathlib import Path
from typing import NamedTuple

from . import db, stats, system_ops
from .system_ops import ACME_PORT, close_port, open_port, panel_port, run_cmd

SB_BIN = os.environ.get('IVPN_SINGBOX_BIN', '/usr/local/bin/sing-box')
SERVICE = 'sing-box'
BASE = db.DATA_ROOT / 'singbox'
CONFIG_PATH = BASE / 'config.json'
CERT_PATH = BASE / 'cert.pem'
KEY_PATH = BASE / 'key.pem'
APPLY_LOCK_PATH = db.DATA_ROOT / 'web' / 'apply.lock'
_LOCK_STATE = threading.local()
_CERT_CACHE = {}
_CERT_CACHE_LOCK = threading.Lock()


class Protocol(NamedTuple):
    key: str
    simple_name: str     # User-facing protocol label.
    tech_name: str       # Protocol identifier displayed by compatible clients.
    hint: str
    proto: str           # Firewall transport: tcp or udp.
    port: int
    default_on: bool


# Default inbounds share port 443 across independent TCP and UDP transports.
PROTOCOLS = (
    Protocol('vless-reality', '日常连接', 'VLESS + REALITY + Vision',
             '推荐优先使用，速度和稳定性最均衡，也最难被识别。', 'tcp', 443, True),
    Protocol('hysteria2', '弱网连接', 'Hysteria2',
             '手机信号不稳、网络丢包严重时改用这个。', 'udp', 443, True),
    Protocol('anytls', '兼容连接', 'AnyTLS',
             '前两个都导入失败时试它，客户端支持面较广。', 'tcp', 8443, False),
    Protocol('tuic', '低延迟连接', 'TUIC',
             '视频通话、游戏等对延迟敏感的场景。', 'udp', 2053, False),
)
BY_KEY = {p.key: p for p in PROTOCOLS}
DEFAULT_KEYS = [p.key for p in PROTOCOLS if p.default_on]
# Retain historical managed ports for firewall cleanup during upgrades.
REMOVED_PROTOCOL_RULES = {(2096, 'tcp')}

# ---------------------------------------------------------------------------
# 入口端口覆盖（NAT / 端口受限的服务器）
# ---------------------------------------------------------------------------
# NAT 小鸡通常只被映射出去一两个高位端口，内核必须监听那个**内网**端口，
# 而客户端要连的是**宿主机上的映射端口**。两者不是同一个数，所以分开存：
#   listen_port_<key>  内核实际监听的本机端口
#   public_port_<key>  分享链接/订阅里给客户端的端口（宿主机映射端口）
# 常规服务器上两者一致（都是 443），不配置即保持原行为。
LISTEN_PORT_KEYS = tuple(f'listen_port_{p.key}' for p in PROTOCOLS)
PUBLIC_PORT_KEYS = tuple(f'public_port_{p.key}' for p in PROTOCOLS)


def protocol_port(protocol, settings=None):
    """该协议实际监听的端口（内核视角）。"""
    settings = settings if settings is not None else db.all_settings()
    raw = str(settings.get(f'listen_port_{protocol.key}', '') or '').strip()
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
    return protocol.port


def public_protocol_port(protocol, settings=None):
    """分享给客户端的端口（外部视角；NAT 下是宿主机的映射端口）。"""
    settings = settings if settings is not None else db.all_settings()
    raw = str(settings.get(f'public_port_{protocol.key}', '') or '').strip()
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
    return protocol_port(protocol, settings)


def port_override_active(settings=None):
    """是否配置了端口覆盖（NAT 模式）。"""
    settings = settings if settings is not None else db.all_settings()
    return any(str(settings.get(k, '') or '').strip()
               for k in LISTEN_PORT_KEYS + PUBLIC_PORT_KEYS)

# REALITY targets must support TLS 1.3, HTTP/2, and X25519.
SNI_CANDIDATES = (
    'www.leagueoflegends.com', 'www.blizzard.com', 'store.epicgames.com',
    'shop.tiktok.com', 'www.amazon.co.jp', 'account.booking.com',
    'jp.shein.com', 'shopee.sg',
)
# Used only when a stored REALITY target is unavailable.
FALLBACK_SNI = 'www.amazon.co.jp'

IP_PROVIDERS = (
    'https://api4.ipify.org', 'https://ipv4.icanhazip.com',
    'https://v4.api.ipinfo.io/ip', 'https://ipv4.myexternalip.com/raw',
    'https://4.ident.me',
)
IPV4_RE = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')


# The unauthenticated statistics endpoint must remain bound to loopback.
STATS_ADDR = '127.0.0.1:10086'


def _share_with_core(path):
    """Make a file under singbox/ readable by the unprivileged sing-box service.

    sing-box runs as its own user (not root, so a compromised panel cannot use
    config.json to make it write files as root). The installer makes singbox/
    setgid with that user's group, so 0640 is enough; files written by root
    (renrenvpn) are handed back to the directory owner so the panel can still read them.
    """
    os.chmod(path, 0o640)
    if os.geteuid() == 0:
        st = path.parent.stat()
        os.chown(path, st.st_uid, st.st_gid)


class ConfigError(RuntimeError):
    """Configuration validation failed; includes the sing-box diagnostic."""


def _cmd(cmd, timeout=30):
    code, out, err = run_cmd(cmd, timeout)
    if code != 0:
        raise RuntimeError(err or out or f'命令失败：{" ".join(cmd)}')
    return out


# ---------------------------------------------------------------------------
# Public server address
# ---------------------------------------------------------------------------
def detect_public_ip():
    """Query multiple providers and reject responses that are not IP addresses."""
    for url in IP_PROVIDERS:
        code, out, _ = run_cmd(['curl', '-s4', '--max-time', '4', url], 8)
        candidate = out.strip()
        if code == 0 and IPV4_RE.match(candidate):
            return candidate
    code, out, _ = run_cmd(['hostname', '-I'], 5)
    if code == 0 and out.split():
        return out.split()[0]
    return ''


def public_host(settings=None):
    settings = settings if settings is not None else db.all_settings()
    value = settings.get('public_host') or 'SERVER_IP'
    if value == 'SERVER_IP':
        return value
    try:
        return system_ops.canonical_host(value)
    except ValueError:
        return 'SERVER_IP'


# ---------------------------------------------------------------------------
# REALITY
# ---------------------------------------------------------------------------
def probe_sni(host, timeout=4.0):
    """Check TLS 1.3, HTTP/2, and X25519 support; return success and latency."""
    command = [
        'openssl', 's_client', '-4', '-tls1_3', '-curves', 'X25519',
        '-alpn', 'h2', '-verify_return_error', '-verify_hostname', host,
        '-servername', host, '-connect', f'{host}:443',
    ]
    started = time.monotonic()
    try:
        result = subprocess.run(command, input='', capture_output=True,
                                text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, float('inf')
    output = result.stdout + result.stderr
    ok = (result.returncode == 0 and 'New, TLSv1.3,' in output
          and 'ALPN protocol: h2' in output)
    return (ok, time.monotonic() - started if ok else float('inf'))


def sni_usable(host):
    """Return whether the configured REALITY target is reachable."""
    if not host:
        return True
    return probe_sni(host)[0]


def pick_sni(candidates=SNI_CANDIDATES):
    """Select the lowest-latency target that passes the TLS probe."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        results = list(pool.map(lambda host: (host, *probe_sni(host)), candidates))
    usable = sorted((latency, host) for host, ok, latency in results if ok)
    if not usable:
        raise RuntimeError('没有找到通过 TLS1.3、h2 和 X25519 检查的伪装域名')
    return usable[0][1]


def probe_relay(settings, timeout=6.0):
    """探测中转代理的 TCP 端口是否可连，返回 (是否可达, 延迟秒数)。

    只做 TCP 连通性检查，不做协议握手：住宅代理的认证方式五花八门，
    在面板里模拟一次 SOCKS5 协商既复杂又容易因供应商差异误报。
    端口连不上是最常见、也最值得提前发现的一种故障。
    """
    try:
        parsed = system_ops.relay_settings(settings)
    except ValueError:
        return False, float('inf')
    if not parsed:
        return True, 0.0
    _kind, server, port, _user, _password, _udp = parsed
    started = time.monotonic()
    try:
        with socket.create_connection((server, port), timeout=timeout):
            return True, time.monotonic() - started
    except OSError:
        return False, float('inf')


def relay_usable(settings):
    """Return whether the configured relay endpoint accepts a TCP connection."""
    return probe_relay(settings)[0]


def probe_uplink(settings, timeout=6.0):
    """探测落地机的 TCP 端口是否可连，返回 (是否可达, 延迟秒数)。

    与 probe_relay 一样只做 TCP 连通性检查：VLESS 走 TCP，Hysteria2 走 UDP，
    对 UDP 做一次有意义的握手探测在面板里代价过高，因此 hysteria2 的
    端口探测仅供参考（UDP 服务不会响应 TCP 连接，这种情况下回报失败属正常）。
    """
    try:
        parsed = system_ops.uplink_settings(settings)
    except ValueError:
        return False, float('inf')
    if not parsed:
        return True, 0.0
    if parsed['kind'] == 'hysteria2':
        # UDP 端口无法用 TCP 探测，交给内核启动后的日志判断。
        return True, 0.0
    started = time.monotonic()
    try:
        with socket.create_connection((parsed['server'], parsed['port']), timeout=timeout):
            return True, time.monotonic() - started
    except OSError:
        return False, float('inf')


def uplink_usable(settings):
    """Return whether the configured landing machine accepts a TCP connection."""
    return probe_uplink(settings)[0]


def ensure_reality_keys(settings=None):
    """Generate missing REALITY credentials with the installed sing-box binary."""
    settings = settings if settings is not None else db.all_settings()
    private = settings.get('reality_private_key', '')
    public = settings.get('reality_public_key', '')
    short_id = settings.get('reality_short_id', '')
    if private and public and short_id:
        return
    if private and public:
        db.set_setting('reality_short_id', secrets.token_hex(8))
        return
    out = _cmd([SB_BIN, 'generate', 'reality-keypair'], 15)
    private = public = ''
    for line in out.splitlines():
        if 'Private' in line:
            private = line.split()[-1]
        elif 'Public' in line:
            public = line.split()[-1]
    if not private or not public:
        raise RuntimeError('REALITY 密钥生成失败，sing-box 输出无法解析')
    db.set_settings({
        'reality_private_key': private,
        'reality_public_key': public,
        'reality_short_id': secrets.token_hex(8),
    })


# ---------------------------------------------------------------------------
# TLS certificate
# ---------------------------------------------------------------------------
def cert_valid():
    if not (CERT_PATH.exists() and KEY_PATH.exists()):
        return False
    # Regenerate certificates with less than 24 hours of validity remaining.
    code, _, _ = run_cmd(['openssl', 'x509', '-in', str(CERT_PATH), '-checkend', '86400'], 10)
    if code != 0:
        return False
    cert_code, cert_public, _ = run_cmd(
        ['openssl', 'x509', '-in', str(CERT_PATH), '-pubkey', '-noout'], 10)
    key_code, key_public, _ = run_cmd(
        ['openssl', 'pkey', '-in', str(KEY_PATH), '-pubout'], 10)
    return cert_code == 0 and key_code == 0 and cert_public.strip() == key_public.strip()


def ensure_cert(host):
    """Provision a self-signed transport certificate without replacing valid pins."""
    if cert_valid():
        return
    CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    tmp_key = KEY_PATH.parent / f'.key.{token}.tmp'
    tmp_cert = CERT_PATH.parent / f'.cert.{token}.tmp'
    code, output, _ = run_cmd([SB_BIN, 'generate', 'tls-keypair', host, '--months', '120'], 90)
    if code != 0:
        raise RuntimeError('sing-box 生成代理证书失败')
    key_match = re.search(r'-----BEGIN PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]+-----END PRIVATE KEY-----', output)
    cert_match = re.search(r'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----', output)
    if not key_match or not cert_match:
        raise RuntimeError('sing-box 返回的代理证书格式不正确')
    try:
        for path, pem in ((tmp_key, key_match.group()), (tmp_cert, cert_match.group())):
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                stream.write(pem + '\n')
            _share_with_core(path)
        valid_code, _, _ = run_cmd(['openssl', 'x509', '-in', str(tmp_cert), '-checkend', '86400'], 10)
        key_code, key_public, _ = run_cmd(['openssl', 'pkey', '-in', str(tmp_key), '-pubout'], 10)
        cert_code, cert_public, _ = run_cmd(
            ['openssl', 'x509', '-in', str(tmp_cert), '-pubkey', '-noout'], 10)
        if valid_code != 0 or key_code != 0 or cert_code != 0 or key_public.strip() != cert_public.strip():
            raise RuntimeError('生成的证书和私钥不匹配或有效期不足')
        tmp_key.replace(KEY_PATH)
        tmp_cert.replace(CERT_PATH)
    finally:
        tmp_key.unlink(missing_ok=True)
        tmp_cert.unlink(missing_ok=True)


def cert_public_key_pin():
    """Return the base64 SHA-256 of the certificate's DER-encoded public key.

    sing-box uses this value for ``certificate_public_key_sha256``. It differs
    from the whole-certificate fingerprint returned by ``cert_pin``.
    """
    cache_key = _cert_cache_key()
    if not cache_key:
        return ''
    with _CERT_CACHE_LOCK:
        if _CERT_CACHE.get('key') == cache_key and 'public_pin' in _CERT_CACHE:
            return _CERT_CACHE['public_pin']
    code, public_pem, _ = run_cmd(
        ['openssl', 'x509', '-in', str(CERT_PATH), '-pubkey', '-noout'], 10)
    if code != 0:
        return ''
    try:
        body = ''.join(line for line in public_pem.splitlines() if not line.startswith('---'))
        der = base64.b64decode(body, validate=True)
        value = base64.b64encode(hashlib.sha256(der).digest()).decode()
    except (ValueError, TypeError):
        return ''
    with _CERT_CACHE_LOCK:
        if _CERT_CACHE.get('key') != cache_key:
            _CERT_CACHE.clear()
            _CERT_CACHE['key'] = cache_key
        _CERT_CACHE['public_pin'] = value
    return value


def cert_pin():
    """Return the uppercase SHA-256 certificate fingerprint for share links."""
    cache_key = _cert_cache_key()
    if not cache_key:
        return ''
    with _CERT_CACHE_LOCK:
        if _CERT_CACHE.get('key') == cache_key and 'cert_pin' in _CERT_CACHE:
            return _CERT_CACHE['cert_pin']
    try:
        der = ssl.PEM_cert_to_DER_cert(CERT_PATH.read_text())
        value = hashlib.sha256(der).hexdigest().upper()
    except (OSError, ValueError):
        return ''
    with _CERT_CACHE_LOCK:
        if _CERT_CACHE.get('key') != cache_key:
            _CERT_CACHE.clear()
            _CERT_CACHE['key'] = cache_key
        _CERT_CACHE['cert_pin'] = value
    return value


def _cert_cache_key():
    try:
        stat = CERT_PATH.stat()
        return (str(CERT_PATH), stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Configuration rendering
# ---------------------------------------------------------------------------
def enabled_keys(settings=None):
    settings = settings if settings is not None else db.all_settings()
    wanted = [k.strip() for k in settings.get('protocols', '').split(',') if k.strip()]
    keys = [k for k in wanted if k in BY_KEY]
    return keys or list(DEFAULT_KEYS)


def _tls_block(host, alpn=None):
    block = {
        'enabled': True,
        'certificate_path': str(CERT_PATH),
        'key_path': str(KEY_PATH),
    }
    if alpn:
        block['alpn'] = list(alpn)
    return block


def _reality_block(settings, sni):
    return {
        'enabled': True,
        'server_name': sni,
        'reality': {
            'enabled': True,
            'handshake': {'server': sni, 'server_port': 443},
            'private_key': settings['reality_private_key'],
            'short_id': [settings['reality_short_id']],
            'max_time_difference': '2h',
        },
    }


def _inbound(key, clients, settings):
    proto = BY_KEY[key]
    sni = settings.get('reality_sni') or FALLBACK_SNI
    base = {'tag': key, 'listen': '::', 'listen_port': protocol_port(proto, settings)}
    if key == 'vless-reality':
        return {**base, 'type': 'vless',
                'users': [{'name': c['name'], 'uuid': c['uuid'], 'flow': 'xtls-rprx-vision'} for c in clients],
                'tls': _reality_block(settings, sni)}
    if key == 'hysteria2':
        inbound = {**base, 'type': 'hysteria2',
                   'users': [{'name': c['name'], 'password': c['password']} for c in clients],
                   'masquerade': 'https://www.bing.com',
                   'tls': _tls_block(public_host(settings), ['h3'])}
        if settings.get('hy2_obfs'):
            inbound['obfs'] = {'type': 'salamander', 'password': settings['hy2_obfs']}
        return inbound
    if key == 'anytls':
        return {**base, 'type': 'anytls',
                'users': [{'name': c['name'], 'password': c['password']} for c in clients],
                'tls': _tls_block(public_host(settings))}
    if key == 'tuic':
        return {**base, 'type': 'tuic',
                'users': [{'name': c['name'], 'uuid': c['uuid'], 'password': c['password']} for c in clients],
                'congestion_control': 'bbr',
                # Disable replayable 0-RTT handshakes.
                'zero_rtt_handshake': False,
                'auth_timeout': '3s',
                'heartbeat': '10s',
                'tls': _tls_block(public_host(settings), ['h3'])}
    raise RuntimeError(f'未知协议：{key}')


def has_ipv6():
    """Check for both a global IPv6 address and a default route.

    Probe failures are treated as IPv6-capable to avoid unintended blocking.
    """
    code, out, _ = run_cmd(['ip', '-6', 'route', 'show', 'default'], 5)
    if code != 0:
        return True
    has_route = bool(out.strip())
    code, out, _ = run_cmd(['ip', '-6', 'addr', 'show', 'scope', 'global'], 5)
    if code != 0:
        return True
    return has_route and 'inet6' in out


@functools.lru_cache(maxsize=1)
def stats_supported():
    """Detect the optional V2Ray statistics build tag for this process lifetime."""
    code, out, _ = run_cmd([SB_BIN, 'version'], 10)
    return code == 0 and 'with_v2ray_api' in out


def render_config(settings=None, clients=None, ipv6=None):
    settings = settings if settings is not None else db.all_settings()
    clients = clients if clients is not None else db.list_clients()
    ipv6 = has_ipv6() if ipv6 is None else ipv6
    active = [c for c in clients if c['enabled']]
    # Do not expose protocol listeners until an enabled client exists.
    inbounds = [_inbound(key, active, settings) for key in enabled_keys(settings)] if active else []
    experimental = {}
    if active and stats_supported():
        # Empty users disables per-user counters; names must match inbound users.
        experimental['v2ray_api'] = {
            'listen': STATS_ADDR,
            'stats': {'enabled': True, 'users': [c['name'] for c in active]},
        }
    outbounds, final_outbound, relay_route = _outbound_chain(settings)
    return {
        'log': {'level': 'warn', 'timestamp': True},
        # Use the host resolver instead of a separately configured upstream.
        # 这里刻意**不**给 dns 加 detour=relay：本地解析走 UDP，而中转（尤其
        # 只开 TCP 的 HTTP 代理或不支持 UDP ASSOCIATE 的住宅代理）承载不了 UDP，
        # 会导致 DNS 直接失败、整条链路不可用（socks5: request rejected, code=7）。
        # 落地匿名性由 route.final=relay 保证，域名解析留在本机即可。
        'dns': {'servers': [{'type': 'local', 'tag': 'local'}]},
        'inbounds': inbounds,
        'outbounds': outbounds,
        'route': {
            # Prefer IPv4 without disabling IPv6 on hosts with a working route.
            'default_domain_resolver': {'server': 'local', 'strategy': 'prefer_ipv4'},
            'rules': [
                # Resolve before IP-based rules to reject private DNS answers.
                {'action': 'resolve', 'strategy': 'prefer_ipv4'},
            ] + ([] if ipv6 else [
                # Fail fast when the host has no usable IPv6 egress.
                {'ip_version': 6, 'action': 'reject'},
            ]) + [
                # Reject private and cloud-instance metadata destinations.
                {'ip_cidr': ['169.254.0.0/16'], 'action': 'reject'},
                {'ip_is_private': True, 'action': 'reject'},
            ] + relay_route,
            'final': final_outbound,
        },
        **({'experimental': experimental} if experimental else {}),
    }


def _outbound_chain(settings):
    """构建出网链路，返回 (outbounds, final_tag, 额外的路由规则)。

    默认直连（保持原行为）。两个出口功能各自独立，可以只用其一，也可以叠加：

        用户 → 本机入口 → [uplink: 落地机] → [relay: 住宅代理] → 目标

    叠加时 relay 是**最后一跳**（决定真正的出口 IP），uplink 是它前面的隧道；
    relay 的 detour 指向 uplink，表示"住宅代理这条连接本身由落地机拨出"，
    这样住宅代理看到的源 IP 是落地机而不是本机。

    关于 detour：sing-box 1.14 里它表示"本 outbound 的拨号流量交给 tag
    指定的上游 outbound"。不要指向普通 direct（会被判
    "detour to an empty direct outbound makes no sense"），也不要让 direct
    自己带 detour（"`detour` is not supported in direct context"）。
    不写 detour 就是直连出网。
    """
    uplink = system_ops.uplink_outbound(settings)
    relay = system_ops.relay_outbound(settings)
    if not uplink and not relay:
        return [{'type': 'direct', 'tag': 'direct'}], 'direct', []

    chain = [{'type': 'direct', 'tag': 'direct'}]
    if uplink:
        chain.append(uplink)
    if relay:
        if uplink:
            # 让落地机去连住宅代理，出口 IP 才是住宅代理的。
            relay['detour'] = 'uplink'
        chain.append(relay)

    # 最后一跳就是真正的出口。
    final = 'relay' if relay else 'uplink'
    udp_ok = _chain_carries_udp(settings, uplink, relay)
    rules = [] if udp_ok else [{'network': 'udp', 'action': 'reject'}]
    return chain, final, rules


def _chain_carries_udp(settings, uplink, relay):
    """整条出口链能否承载 UDP。

    必须**每一跳**都能承载：链路上任何一跳不支持 UDP，UDP 就会绕过出口直连，
    暴露本机或落地机的真实 IP。
      - 只有 uplink：取决于 uplink 协议（hysteria2 可以，vless 不可以）。
      - 只有 relay ：取决于 relay 配置。
      - 两者都有   ：两跳都要可以。
    """
    if uplink and relay:
        return (system_ops.uplink_carries_udp(settings)
                and system_ops.relay_carries_udp(settings))
    if uplink:
        return system_ops.uplink_carries_udp(settings)
    return system_ops.relay_carries_udp(settings)


# ---------------------------------------------------------------------------
# Configuration activation
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def config_lock():
    """Serialize database-to-runtime configuration changes across processes."""
    depth = getattr(_LOCK_STATE, 'depth', 0)
    if depth:
        _LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _LOCK_STATE.depth -= 1
        return
    APPLY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with APPLY_LOCK_PATH.open('a+') as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        _LOCK_STATE.depth = 1
        try:
            yield
        finally:
            _LOCK_STATE.depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f'.{path.name}.{secrets.token_hex(6)}.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    _share_with_core(tmp)     # Credentials: owner + sing-box group only.
    tmp.replace(path)


def collect_stats():
    """Persist and return per-user deltas from sing-box.

    Queries reset core counters, so a database failure can lose the sampled
    interval. Statistics failures do not affect panel availability.
    """
    if not stats_supported():
        return {}
    try:
        deltas = stats.query(STATS_ADDR)
    except Exception:
        return {}
    db.add_traffic(deltas)
    return deltas


def apply(restart=True):
    """Apply rendered configuration and restore the previous version on failure."""
    with config_lock():
        if restart:
            collect_stats()
        settings = db.all_settings()
        ensure_cert(public_host(settings))
        config = render_config(settings)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        previous = CONFIG_PATH.read_bytes() if CONFIG_PATH.exists() else None
        tmp = CONFIG_PATH.parent / f'.config.{secrets.token_hex(6)}.tmp'
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        _share_with_core(tmp)
        code, out, err = run_cmd([SB_BIN, 'check', '-c', str(tmp)], 25)
        if code != 0:
            tmp.unlink(missing_ok=True)
            raise ConfigError(err or out or 'sing-box check 失败')
        tmp.replace(CONFIG_PATH)
        if restart:
            try:
                restart_service()
                if not service_running():
                    raise RuntimeError(f'{SERVICE} 重启后未进入 active 状态')
            except Exception as exc:
                if previous is not None:
                    rollback = CONFIG_PATH.parent / f'.config.rollback.{secrets.token_hex(6)}.tmp'
                    rollback.write_bytes(previous)
                    _share_with_core(rollback)
                    rollback.replace(CONFIG_PATH)
                    try:
                        restart_service()
                    except Exception as rollback_exc:
                        raise RuntimeError(
                            f'新配置启动失败，恢复旧配置也失败：{rollback_exc}'
                        ) from exc
                raise
        return config


def restart_service():
    if os.geteuid() != 0:
        system_ops.privileged_action({'action': 'restart_singbox'})
        return
    code, out, err = run_cmd(['systemctl', 'restart', SERVICE], 30)
    if code != 0:
        raise RuntimeError(err or out or f'{SERVICE} 重启失败')


def service_running():
    code, _, _ = run_cmd(['systemctl', 'is-active', SERVICE], 5)
    return code == 0


def sync_firewall(settings=None, close_disabled=True):
    """Reconcile firewall rules owned by the configured and legacy protocols.

    Unrelated rules and cloud-provider security groups are outside this scope.
    """
    settings = settings if settings is not None else db.all_settings()
    keep = {(protocol_port(BY_KEY[key], settings), BY_KEY[key].proto)
            for key in enabled_keys(settings)}
    # Reserve the panel and ACME ports regardless of protocol rule changes.
    reserved = {panel_port(), ACME_PORT}
    for port, proto in keep:
        open_port(port, proto)
    if close_disabled:
        managed = {(protocol.port, protocol.proto) for protocol in PROTOCOLS}
        for port, proto in managed | REMOVED_PROTOCOL_RULES:
            if (port, proto) not in keep and port not in reserved:
                close_port(port, proto)


def firewall_rules(settings=None):
    """Return deduplicated, sorted protocol ports for the panel UI."""
    seen = {}
    for key in enabled_keys(settings):
        proto = BY_KEY[key]
        seen[(protocol_port(proto, settings), proto.proto)] = True
    return [f'{port}/{kind.upper()}' for port, kind in sorted(seen)]

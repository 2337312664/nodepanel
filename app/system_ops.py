"""System operations and bootstrap settings.

settings.json contains values required before database initialization. Runtime
application settings are stored in the database.
"""

import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
from pathlib import Path

from . import brand
from shutil import which

DATA_ROOT = Path(os.environ.get('IVPN_DATA_DIR') or brand.DATA_ROOT)
WEB_SETTINGS = DATA_ROOT / 'web' / 'settings.json'
HELPER_SOCKET = os.environ.get(
    'IVPN_HELPER_SOCKET', os.path.join(brand.RUN_DIR, 'helper.sock'))
HOST_LABEL_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')
ASSET_PATH_RE = re.compile(r'^assets-[A-Za-z0-9]{32}$')
_FALLBACK_ASSET_PATH = 'assets-' + os.urandom(16).hex()


def canonical_host(value):
    raw = str(value or '').strip()
    # Permit the preview placeholder while rejecting other invalid hostnames.
    if raw == 'SERVER_IP':
        return raw
    if raw.startswith('['):
        if not raw.endswith(']'):
            raise ValueError('服务器地址格式不正确')
        raw = raw[1:-1]
    if not raw:
        raise ValueError('服务器地址不能为空')
    try:
        return ipaddress.ip_address(raw).compressed
    except ValueError:
        host = raw.rstrip('.').lower()
        if len(host) > 253 or not all(HOST_LABEL_RE.fullmatch(label or '')
                                      for label in host.split('.')):
            raise ValueError('服务器地址格式不正确')
        return host


def authority(host, port=None):
    canonical = canonical_host(host)
    rendered = f'[{canonical}]' if ':' in canonical else canonical
    return rendered if port is None else f'{rendered}:{int(port)}'


def canonical_public_ipv4(value):
    """Require a public IPv4 address for the supported IP certificate profile."""
    host = canonical_host(value)
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError('当前版本只支持服务器公网 IPv4，暂不支持域名') from exc
    if address.version != 4:
        raise ValueError('当前版本只支持服务器公网 IPv4')
    return address.compressed


# ---------------------------------------------------------------------------
# 中转 / 落地出口
# ---------------------------------------------------------------------------
RELAY_TYPES = ('socks5', 'http')
# 代理的账号密码会写进 config.json 的 outbound。虽然 json.dumps 会正确转义，
# 仍把可用字符收紧到 RFC 1929 允许的可见 ASCII（0x21-0x7E，即不含空格），
# 排除引号与反斜杠，避免生成出人眼难以核对、且容易被日志系统误解的凭据。
RELAY_CREDENTIAL_RE = re.compile(r'^[!#-\[\]-~]{1,255}$')


def canonical_relay_host(value):
    """接受域名或公网 IP，用于中转/落地代理地址。

    与 canonical_public_ipv4 不同，这里允许域名：住宅代理通常只提供域名入口。
    拒绝环回、私有和链路本地地址——它们要么指回本机（形成自环），要么是
    内网扫描的跳板，对"经住宅代理落地"这个用途没有正当场景。
    """
    host = canonical_host(value)
    if host == 'SERVER_IP':
        raise ValueError('请填写真实的中转服务器地址')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host          # 域名，已由 canonical_host 校验过标签格式
    if (address.is_loopback or address.is_private or address.is_link_local
            or address.is_multicast or address.is_reserved
            or address.is_unspecified):
        raise ValueError('中转服务器地址不能是内网、环回或保留地址')
    return address.compressed


def canonical_relay_port(value):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError('中转端口必须是数字') from exc
    if not 1 <= port <= 65535:
        raise ValueError('中转端口必须在 1 到 65535 之间')
    return port


def canonical_relay_credential(value, label, required=False):
    """校验代理账号密码。

    空值在"不需要认证"时合法；一旦填了用户名，密码就不能为空，否则
    sing-box 会按无认证去连，表现为连不通而不是配置错误，很难排查。
    """
    raw = str(value or '')
    if not raw:
        if required:
            raise ValueError(f'{label}不能为空')
        return ''
    if not RELAY_CREDENTIAL_RE.match(raw):
        raise ValueError(f'{label}只能使用可见字符（不含空格），最长 255 个字符')
    return raw


def relay_settings(settings):
    """从设置里解析出中转出口参数；未启用时返回 None。

    返回 (kind, server, port, username, password, udp)。
    """
    kind = str(settings.get('relay_type') or '').strip().lower()
    if not kind:
        return None
    if kind not in RELAY_TYPES:
        raise ValueError('中转类型只支持 socks5 或 http')
    server = canonical_relay_host(settings.get('relay_server') or '')
    port = canonical_relay_port(settings.get('relay_port') or '')
    username = canonical_relay_credential(settings.get('relay_username'), '中转用户名')
    password = canonical_relay_credential(settings.get('relay_password'), '中转密码',
                                         required=bool(username))
    udp = str(settings.get('relay_udp') or '0') == '1'
    return kind, server, port, username, password, udp


def relay_outbound(settings):
    """渲染中转 outbound，供 sing-box 的出网链路使用；未启用时返回 None。"""
    parsed = relay_settings(settings)
    if not parsed:
        return None
    kind, server, port, username, password, udp = parsed
    if kind == 'socks5':
        outbound = {
            'type': 'socks',
            'tag': 'relay',
            'server': server,
            'server_port': port,
            'version': '5',
            # sing-box 要求 network 是数组，不是逗号分隔的字符串。
            'network': ['tcp', 'udp'] if udp else ['tcp'],
        }
        if udp:
            # UDP over TCP：住宅代理普遍不允许直发 UDP 包，改用 TCP 隧道承载。
            outbound['udp_over_tcp'] = True
    else:
        # HTTP 代理只有 CONNECT 一种方式，天然只支持 TCP，没有 network 字段；
        # 传了会被 sing-box 以 "unknown field" 拒绝。
        outbound = {
            'type': 'http',
            'tag': 'relay',
            'server': server,
            'server_port': port,
        }
    if username:
        outbound['username'] = username
        outbound['password'] = password
    return outbound


def relay_carries_udp(settings):
    """该中转配置能否承载 UDP。HTTP 代理只看 CONNECT，永远不能。"""
    try:
        parsed = relay_settings(settings)
    except ValueError:
        return False
    if not parsed:
        return False
    kind, _server, _port, _user, _password, udp = parsed
    return kind == 'socks5' and udp


def relay_available(settings):
    """判断中转配置是否完整可用（不做网络探测）。"""
    try:
        return relay_settings(settings) is not None
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 中转落地（本机作为入口机，把流量推给另一台落地机）
# ---------------------------------------------------------------------------
UPLINK_TYPES = ('vless-reality', 'hysteria2')
# 面板客户端的名称规则，复用同一套（落地机会用它建一个专用用户）。
UPLINK_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
UPLINK_SHORT_ID_RE = re.compile(r'^[0-9a-fA-F]{2,16}$')
# REALITY 公钥是 x25519 的 base64url 编码，固定 43 个字符且不带填充。
UPLINK_PUBLIC_KEY_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
UPLINK_SNI_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$')


def canonical_uplink_host(value, allow_private=False):
    """校验落地机地址。

    与住宅代理不同，入口机和落地机经常部署在同一个内网/机房专网里，
    因此内网地址是合理用法——但默认仍然拒绝，必须由用户显式打开
    「允许内网地址」开关才放行，避免无心配置把流量引到内网。
    环回地址在任何情况下都不允许：它没有任何正当用途，只会把链路指向自己。
    """
    host = canonical_host(value)
    if host == 'SERVER_IP':
        raise ValueError('请填写真实的落地机地址')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host          # 域名，已由 canonical_host 校验过标签格式
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError('落地机地址不能是环回、未指定或组播地址')
    if (address.is_private or address.is_link_local or address.is_reserved) and not allow_private:
        raise ValueError('落地机地址是内网地址。确认要在内网互联的话，'
                         '请勾选「允许内网地址」')
    return address.compressed


def canonical_uplink_public_key(value):
    """校验 REALITY 公钥(pbk)。

    必须是 43 字符的 base64url；长度不对时 sing-box 会在启动阶段直接
    报错，面板提前拦住能省掉一次「配置没生效」的排查。
    """
    raw = str(value or '').strip()
    if not raw:
        raise ValueError('落地机公钥（pbk）不能为空')
    if not UPLINK_PUBLIC_KEY_RE.match(raw):
        raise ValueError('落地机公钥（pbk）格式不正确，应为 43 个字符')
    return raw


def canonical_uplink_short_id(value):
    """REALITY shortId：2 到 16 个十六进制字符。"""
    raw = str(value or '').strip()
    if not raw:
        raise ValueError('shortId 不能为空')
    if not UPLINK_SHORT_ID_RE.match(raw):
        raise ValueError('shortId 必须是 2 到 16 个十六进制字符')
    return raw


def canonical_uplink_sni(value):
    raw = str(value or '').strip()
    if not raw:
        raise ValueError('伪装域名不能为空')
    if not UPLINK_SNI_RE.match(raw):
        raise ValueError('伪装域名格式不正确')
    return raw


def uplink_settings(settings):
    """解析中转落地参数；未启用时返回 None。

    返回 dict，含 kind / server / port 以及该协议需要的其余字段。
    """
    kind = str(settings.get('uplink_type') or '').strip().lower()
    if not kind:
        return None
    if kind not in UPLINK_TYPES:
        raise ValueError('中转落地只支持 vless-reality 或 hysteria2')
    allow_private = str(settings.get('uplink_allow_private') or '0') == '1'
    server = canonical_uplink_host(settings.get('uplink_server') or '',
                                   allow_private=allow_private)
    port = canonical_relay_port(settings.get('uplink_port') or '')
    parsed = {'kind': kind, 'server': server, 'port': port}
    if kind == 'vless-reality':
        uuid = str(settings.get('uplink_uuid') or '').strip()
        if not UPLINK_UUID_RE.match(uuid):
            raise ValueError('落地机 UUID 格式不正确')
        parsed['uuid'] = uuid
        parsed['sni'] = canonical_uplink_sni(settings.get('uplink_sni'))
        parsed['public_key'] = canonical_uplink_public_key(
            settings.get('uplink_public_key'))
        parsed['short_id'] = canonical_uplink_short_id(
            settings.get('uplink_short_id'))
    else:
        password = canonical_relay_credential(
            settings.get('uplink_password'), '落地机密码', required=True)
        parsed['password'] = password
        # Hysteria2 强制走 UDP，因此中转落地为 hysteria2 时天然承载 UDP。
        parsed['hy2_obfs'] = canonical_relay_credential(
            settings.get('uplink_hy2_obfs'), '落地机混淆密码')
    return parsed


def uplink_outbound(settings):
    """渲染指向落地机的 outbound；未启用时返回 None。"""
    parsed = uplink_settings(settings)
    if not parsed:
        return None
    if parsed['kind'] == 'vless-reality':
        return {
            'type': 'vless',
            'tag': 'uplink',
            'server': parsed['server'],
            'server_port': parsed['port'],
            'uuid': parsed['uuid'],
            'flow': 'xtls-rprx-vision',
            'tls': {
                'enabled': True,
                'server_name': parsed['sni'],
                'utls': {'enabled': True, 'fingerprint': 'chrome'},
                'reality': {
                    'enabled': True,
                    'public_key': parsed['public_key'],
                    'short_id': parsed['short_id'],
                },
            },
        }
    outbound = {
        'type': 'hysteria2',
        'tag': 'uplink',
        'server': parsed['server'],
        'server_port': parsed['port'],
        'password': parsed['password'],
        # 落地机的传输证书是自签的。面板在订阅里对 Hysteria2 用
        # certificate_public_key_sha256 做公钥固定，但那是**本机**证书的 pin；
        # 入口机连的是落地机的证书，本机拿不到它，因此这里只能 insecure。
        # 传输本身已经由 Hysteria2 的 QUIC + 密码认证保护，风险可控；
        # 若要严格校验，可后续支持手工填入落地机证书的 pin。
        'tls': {'enabled': True, 'server_name': parsed['server'],
                'insecure': True, 'alpn': ['h3']},
    }
    if parsed.get('hy2_obfs'):
        outbound['obfs'] = {'type': 'salamander', 'password': parsed['hy2_obfs']}
    return outbound


def uplink_carries_udp(settings):
    """落地链路能否承载 UDP。Hysteria2 跑在 QUIC 上，天然可以。"""
    try:
        parsed = uplink_settings(settings)
    except ValueError:
        return False
    return bool(parsed) and parsed['kind'] == 'hysteria2'


def uplink_available(settings):
    """判断中转落地配置是否完整可用（不做网络探测）。"""
    try:
        return uplink_settings(settings) is not None
    except ValueError:
        return False


def privileged_action(payload):
    """Request an allowlisted operation from the privileged helper."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(35)
    try:
        client.connect(HELPER_SOCKET)
        client.sendall(json.dumps(payload, separators=(',', ':')).encode() + b'\n')
        client.shutdown(socket.SHUT_WR)
        data = bytearray()
        while b'\n' not in data:
            chunk = client.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise RuntimeError('root helper 返回内容异常过大')
    except OSError as exc:
        raise RuntimeError(f'无法连接权限助手：{exc}')
    finally:
        client.close()
    try:
        response = json.loads(bytes(data).split(b'\n', 1)[0])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError(f'权限助手响应损坏：{exc}')
    if not response.get('ok'):
        raise RuntimeError(response.get('error') or '权限助手执行失败')
    return response.get('result') or {}


def run_cmd(cmd, timeout=20):
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return process.returncode, process.stdout.strip(), process.stderr.strip()
    except Exception as exc:
        return 1, '', str(exc)


def is_secure(request):
    if request.url.scheme == 'https':
        return True
    forwarded = request.headers.get('x-forwarded-proto', '')
    return forwarded.split(',')[0].strip().lower() == 'https'


def open_port(port, proto):
    """Open a host firewall port; cloud security-group rules remain external."""
    proto = proto.lower()
    port = int(port)
    if proto not in ('tcp', 'udp') or not 1 <= port <= 65535:
        return False
    if os.geteuid() != 0:
        return bool(privileged_action({
            'action': 'firewall', 'operation': 'add', 'port': port, 'proto': proto,
        }).get('changed'))
    changed = False
    if which('ufw'):
        code, out, err = run_cmd(['ufw', 'allow', f'{port}/{proto}'], 15)
        if code != 0:
            raise RuntimeError(err or out or 'ufw 放行端口失败')
        changed = True
    if which('firewall-cmd'):
        code, out, err = run_cmd(['firewall-cmd', '--permanent', f'--add-port={port}/{proto}'], 15)
        if code != 0:
            raise RuntimeError(err or out or 'firewalld 放行端口失败')
        code, out, err = run_cmd(['firewall-cmd', '--reload'], 15)
        if code != 0:
            raise RuntimeError(err or out or 'firewalld 重载失败')
        changed = True
    return changed


def close_port(port, proto):
    """Remove a host firewall rule, tolerating rules that are already absent."""
    proto = proto.lower()
    port = int(port)
    if proto not in ('tcp', 'udp') or not 1 <= port <= 65535:
        return False
    if os.geteuid() != 0:
        return bool(privileged_action({
            'action': 'firewall', 'operation': 'remove', 'port': port, 'proto': proto,
        }).get('changed'))
    changed = False
    if which('ufw'):
        code, out, err = run_cmd(['ufw', '--force', 'delete', 'allow', f'{port}/{proto}'], 15)
        if code != 0 and 'not exist' not in (out + err).lower():
            raise RuntimeError(err or out or 'ufw 收回端口失败')
        changed = True
    if which('firewall-cmd'):
        code, out, err = run_cmd(['firewall-cmd', '--permanent', f'--remove-port={port}/{proto}'], 15)
        if code != 0 and 'not enabled' not in (out + err).lower():
            raise RuntimeError(err or out or 'firewalld 收回端口失败')
        code, out, err = run_cmd(['firewall-cmd', '--reload'], 15)
        if code != 0:
            raise RuntimeError(err or out or 'firewalld 重载失败')
        changed = True
    return changed


def web_settings():
    # Only a missing file means "use defaults". An unreadable one must fail loudly:
    # falling back would serve the panel on 2083 at the public /login path.
    try:
        return json.loads(WEB_SETTINGS.read_text())
    except FileNotFoundError:
        return {}


def write_private_file(path, data, mode=0o600):
    """Atomically replace ``path`` inside the web-owned data tree.

    Root (SSH CLI, the helper) writes here too, so never follow links the web user
    could plant: open the directory without following it, create the temp file
    exclusively, give it to the directory owner (else the panel cannot read it), and
    rename within that same directory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tmp = f'.{path.name}.{secrets.token_hex(8)}.tmp'
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode,
                     dir_fd=dir_fd)
        try:
            with os.fdopen(fd, 'wb') as output:
                # chmod before chown: once the file belongs to the web user, the helper
                # (no CAP_FOWNER in its sandbox) may no longer change its mode.
                os.fchmod(output.fileno(), mode)
                if os.geteuid() == 0:
                    owner = os.fstat(dir_fd)
                    os.fchown(output.fileno(), owner.st_uid, owner.st_gid)
                output.write(data)
            os.replace(tmp, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(dir_fd)


def save_web_settings(values):
    merged = {**web_settings(), **values}
    write_private_file(WEB_SETTINGS, json.dumps(merged, ensure_ascii=False, indent=2).encode())
    return merged


def login_path():
    path = str(web_settings().get('login_path', 'login')).strip().strip('/')
    return path or 'login'


def asset_path():
    path = str(web_settings().get('asset_path', '')).strip().strip('/')
    return path if ASSET_PATH_RE.fullmatch(path) else _FALLBACK_ASSET_PATH


def panel_port():
    """Return the configured panel port, falling back to 2083 if unavailable."""
    try:
        return int(web_settings().get('panel_port', 2083))
    except (TypeError, ValueError):
        return 2083


# ACME HTTP-01 uses port 80; the panel itself does not listen on this port.
ACME_PORT = 80

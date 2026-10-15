"""Administrative account, server, protocol, and REALITY SNI settings."""

import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import brand, db, singbox, system_ops
from ..db import log_action, verify_admin
from ..web import (LOGIN_VERIFY_SLOTS, apply_config, csrf_token, login_blocked, login_url,
                   notices, preview_mode, record_login, redirect_with_message,
                   require_user, templates, verify_csrf)

router = APIRouter()

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{1,32}$')


def _update_status():
    # 由以 root 运行的更新服务写入（brand.SERVICE_UPDATE）。
    try:
        status = json.loads(db.get_setting('update_status') or '{}')
    except ValueError:
        return {}
    return status if isinstance(status, dict) else {}


@router.get('/settings')
def settings_page(request: Request, user: str = Depends(require_user)):
    settings = db.all_settings()
    return templates.TemplateResponse('settings.html', {
        'request': request,
        'active': 'settings',
        'csrf_token': csrf_token(request),
        'admin_username': user,
        'settings': settings,
        'enabled_keys': singbox.enabled_keys(settings),
        'all_protocols': singbox.PROTOCOLS,
        # The current request origin is the address the admin should bookmark.
        'login_full_url': str(request.base_url) + system_ops.login_path(),
        'panel_port': system_ops.panel_port(),
        'acme_port': system_ops.ACME_PORT,
        'firewall_rules': singbox.firewall_rules(settings),
        'update_status': _update_status(),
        'relay_types': system_ops.RELAY_TYPES,
        'relay_active': system_ops.relay_available(settings),
        'uplink_types': system_ops.UPLINK_TYPES,
        'uplink_active': system_ops.uplink_available(settings),
        **notices(request),
    })


@router.post('/settings/update')
def start_update(request: Request, user: str = Depends(require_user),
                 _: None = Depends(verify_csrf)):
    """Hand the update to a root oneshot unit; it restarts this panel midway."""
    if preview_mode():
        return redirect_with_message('/settings', msg='预览模式不会实际更新。')
    db.set_setting('update_status', json.dumps({
        'state': 'queued', 'message': '正在启动更新…',
        'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }, ensure_ascii=False))
    try:
        system_ops.privileged_action({'action': 'start_update'})
    except Exception as exc:
        db.set_setting('update_status', '')
        log_action('start update', 'panel', 'error', str(exc))
        return redirect_with_message('/settings', error=f'没能启动更新：{exc}')
    log_action('start update', 'panel')
    return templates.TemplateResponse('update.html', {
        'request': request,
        'active': 'settings',
        'csrf_token': csrf_token(request),
    })


@router.get('/settings/update-status')
def update_status(user: str = Depends(require_user)):
    return _update_status()


@router.post('/settings/account')
def change_account(request: Request, user: str = Depends(require_user), _: None = Depends(verify_csrf),
                   current_password: str = Form(...), username: str = Form(...),
                   password: str = Form('')):
    username = username.strip()
    # Share the login throttle so a stolen session cannot brute-force the current password.
    ip = request.client.host if request.client else ''
    if login_blocked(ip, user):
        return redirect_with_message('/settings', error='失败次数太多，请 10 分钟后再试。')
    if not LOGIN_VERIFY_SLOTS.acquire(blocking=False):
        return redirect_with_message('/settings', error='请求较多，请稍后重试。')
    try:
        ok = verify_admin(user, current_password)
        record_login(user, ip, ok)
    finally:
        LOGIN_VERIFY_SLOTS.release()
    if not ok:
        log_action('change account', user, 'error', 'wrong current password')
        return redirect_with_message('/settings', error='当前密码不正确，请重新输入。')
    if not USERNAME_RE.match(username):
        return redirect_with_message('/settings', error='管理账号只能用英文、数字和下划线。')
    if password:
        try:
            db.validate_admin_password(password)
        except ValueError as exc:
            return redirect_with_message('/settings', error=str(exc))
    try:
        db.update_admin_credentials(user, username, password)
    except Exception:
        return redirect_with_message('/settings', error='这个管理账号不能用，换一个。')
    db.bump_session_epoch()
    log_action('change account', username)
    resp = RedirectResponse(login_url(), 302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.post('/settings/server')
def change_server(request: Request, user: str = Depends(require_user), _: None = Depends(verify_csrf),
                  public_host: str = Form(''), session_days: str = Form('30'),
                  hy2_obfs: str = Form(''), protocols: list[str] = Form(default=[]),
                  reality_sni: str = Form(''), panel_port: str = Form(''),
                  relay_type: str = Form(''), relay_server: str = Form(''),
                  relay_port: str = Form(''), relay_username: str = Form(''),
                  relay_password: str = Form(''), relay_udp: str = Form(''),
                  uplink_type: str = Form(''), uplink_server: str = Form(''),
                  uplink_port: str = Form(''), uplink_uuid: str = Form(''),
                  uplink_password: str = Form(''), uplink_sni: str = Form(''),
                  uplink_public_key: str = Form(''), uplink_short_id: str = Form(''),
                  uplink_hy2_obfs: str = Form(''), uplink_allow_private: str = Form('')):
    """Rebuild sing-box only when a setting changes its configuration."""
    try:
        host = system_ops.canonical_public_ipv4(public_host)
    except ValueError:
        return redirect_with_message('/settings', error='服务器地址格式不正确。')
    try:
        days = max(1, min(365, int(session_days)))
    except ValueError:
        days = 30
    try:
        requested_port = int(panel_port or system_ops.panel_port())
    except (TypeError, ValueError):
        return redirect_with_message('/settings', error='管理页面端口必须是数字。')
    if not 1024 <= requested_port <= 65535 or requested_port == system_ops.ACME_PORT:
        return redirect_with_message('/settings', error='管理页面端口必须在 1024 到 65535 之间。')
    old_port = system_ops.panel_port()
    port_changed = requested_port != old_port
    keys = [k for k in protocols if k in singbox.BY_KEY] or list(singbox.DEFAULT_KEYS)
    sni = reality_sni.strip()
    if sni and not re.match(r'^[A-Za-z0-9.\-]{1,253}$', sni):
        return redirect_with_message('/settings', error='伪装域名格式不正确。')

    # 中转参数先在提交前整体校验，任何一项不合法都直接回显，不落库。
    relay_values = {
        'relay_type': relay_type.strip().lower(),
        'relay_server': relay_server.strip(),
        'relay_port': relay_port.strip(),
        'relay_username': relay_username.strip(),
        'relay_password': relay_password,
        'relay_udp': '1' if relay_udp in ('1', 'on', 'true') else '0',
    }
    if relay_values['relay_type']:
        if relay_values['relay_type'] not in system_ops.RELAY_TYPES:
            return redirect_with_message('/settings', error='中转类型只支持 SOCKS5 或 HTTP。')
        if not relay_values['relay_server']:
            return redirect_with_message('/settings', error='开启中转后必须填写中转服务器地址。')
        try:
            system_ops.relay_settings(relay_values)
        except ValueError as exc:
            return redirect_with_message('/settings', error=f'中转设置有问题：{exc}')
    else:
        # 关闭中转时清空全部关联字段，避免残留的代理凭据留在数据库里。
        relay_values.update({'relay_server': '', 'relay_port': '',
                             'relay_username': '', 'relay_password': ''})

    # 中转落地：与住宅代理独立，同样先整体校验再落库。
    uplink_values = {
        'uplink_type': uplink_type.strip().lower(),
        'uplink_server': uplink_server.strip(),
        'uplink_port': uplink_port.strip(),
        'uplink_uuid': uplink_uuid.strip(),
        'uplink_password': uplink_password,
        'uplink_sni': uplink_sni.strip(),
        'uplink_public_key': uplink_public_key.strip(),
        'uplink_short_id': uplink_short_id.strip(),
        'uplink_hy2_obfs': uplink_hy2_obfs.strip(),
        'uplink_allow_private': '1' if uplink_allow_private in ('1', 'on', 'true') else '0',
    }
    if uplink_values['uplink_type']:
        if not uplink_values['uplink_server'] or not uplink_values['uplink_port']:
            return redirect_with_message(
                '/settings', error='开启中转落地后必须填写落地机地址和端口。')
        try:
            system_ops.uplink_settings(uplink_values)
        except ValueError as exc:
            return redirect_with_message('/settings', error=f'中转落地设置有问题：{exc}')
    else:
        # 关闭时清空凭据，但保留「允许内网地址」开关，免得每次重开都要再勾一次。
        uplink_values.update({k: '' for k in (
            'uplink_server', 'uplink_port', 'uplink_uuid', 'uplink_password',
            'uplink_sni', 'uplink_public_key', 'uplink_short_id', 'uplink_hy2_obfs')})

    with singbox.config_lock():
        before = db.all_settings()
        if (not preview_mode() and before.get('public_host')
                and host != before.get('public_host')):
            return redirect_with_message(
                '/settings',
                error=f'更换公网 IP 还需要重新签发面板证书。请先 SSH 运行 {brand.CLI_NAME} set-host，'
                      '再立即重跑安装器；网页中不会只改一半。')
        updates = {
            'public_host': host,
            'session_days': days,
            'hy2_obfs': hy2_obfs.strip(),
            'protocols': ','.join(keys),
            **relay_values,
            **uplink_values,
        }
        # 连通性只在真正启用且字段有变化时才探测，避免每次保存都等一次超时。
        relay_changed = any(before.get(k) != relay_values[k] for k in relay_values)
        if relay_values['relay_type'] and relay_changed:
            updates['relay_ok'] = ('1' if preview_mode() else
                                   ('1' if singbox.relay_usable(relay_values) else '0'))
        if not relay_values['relay_type']:
            updates['relay_ok'] = '1'
        uplink_changed = any(before.get(k) != uplink_values[k] for k in uplink_values)
        if uplink_values['uplink_type'] and uplink_changed:
            updates['uplink_ok'] = ('1' if preview_mode() else
                                    ('1' if singbox.uplink_usable(uplink_values) else '0'))
        if not uplink_values['uplink_type']:
            updates['uplink_ok'] = '1'
        if sni:
            updates['reality_sni'] = sni
            updates['reality_sni_ok'] = ('1' if preview_mode() else
                                         ('1' if singbox.sni_usable(sni) else '0'))
        db.set_settings(updates)
        after = db.all_settings()

        config_changed = any(before.get(k) != after.get(k) for k in db.CONFIG_KEYS)
        if config_changed:
            try:
                if not preview_mode():
                    singbox.sync_firewall(after, close_disabled=False)
                apply_config()
                if not preview_mode():
                    singbox.sync_firewall(after)
            except Exception as exc:
                db.set_settings({key: before.get(key, '') for key in updates})
                restored = True
                try:
                    # apply_config may already have switched sing-box to the new settings.
                    apply_config()
                except Exception:
                    restored = False
                if not preview_mode():
                    try:
                        singbox.sync_firewall(before)
                    except Exception:
                        restored = False
                log_action('change server', 'settings', 'error', str(exc))
                if not restored:
                    return redirect_with_message(
                        '/settings', error='设置没有生效，恢复原配置时也出错了。'
                                           '请在首页点一次「检查并修复」。')
                return redirect_with_message('/settings', error='设置没有生效，已恢复原来的配置。')

    if port_changed:
        if preview_mode():
            return redirect_with_message('/settings', msg='预览模式不会实际切换管理端口。')
        try:
            system_ops.privileged_action({
                'action': 'change_panel_port', 'port': requested_port,
            })
        except Exception as exc:
            log_action('change panel port', str(requested_port), 'error', str(exc))
            return redirect_with_message('/settings', error=f'其它设置已保存，但端口没有修改：{exc}')
        host = singbox.public_host()
        new_url = f'https://{system_ops.authority(host, requested_port)}/settings'
        log_action('change panel port', f'{old_port} -> {requested_port}')
        return templates.TemplateResponse('port-switch.html', {
            'request': request,
            'active': 'settings',
            'csrf_token': csrf_token(request),
            'new_port': requested_port,
            'old_port': old_port,
            'new_url': new_url,
        })
    log_action('change server', 'settings')
    message = ('设置已保存，所有连接信息已更新，请重新发给用户。'
               if config_changed else '设置已保存。')
    return redirect_with_message('/settings', msg=message)


@router.post('/settings/rescan-sni')
def rescan_sni(request: Request, user: str = Depends(require_user),
               _: None = Depends(verify_csrf), next: str = Form('/settings')):
    """Replace the REALITY target with a different reachable candidate.

    Existing standalone links contain the old SNI and must be redistributed.
    Subscription clients receive the change on their next refresh.
    """
    target = '/settings' if next != 'home' else '/'
    if preview_mode():
        return redirect_with_message(target, msg='预览模式不执行扫描。')
    try:
        with singbox.config_lock():
            before = db.all_settings()
            current = before.get('reality_sni', '')
            candidates = tuple(host for host in singbox.SNI_CANDIDATES if host != current)
            if not candidates:
                return redirect_with_message(
                    target, error='没有找到其他可用的伪装域名，原设置保持不变。')
            try:
                sni = singbox.pick_sni(candidates)
            except RuntimeError as exc:
                log_action('rescan sni', 'settings', 'error', str(exc))
                return redirect_with_message(
                    target, error='没有找到其他可用的伪装域名，原设置保持不变。')
            if sni == current:
                raise RuntimeError('扫描结果与当前伪装域名相同')
            if not singbox.sni_usable(sni):
                raise RuntimeError('挑选到的伪装域名复检失败')
            db.set_settings({'reality_sni': sni, 'reality_sni_ok': '1'})
            try:
                apply_config()
            except Exception:
                db.set_settings({
                    'reality_sni': before.get('reality_sni', ''),
                    'reality_sni_ok': before.get('reality_sni_ok', '0'),
                })
                raise
    except Exception as exc:
        log_action('rescan sni', 'settings', 'error', str(exc))
        return redirect_with_message(target, error='扫描失败，原来的伪装域名没有改变。')
    log_action('rescan sni', 'settings', 'ok', sni)
    return redirect_with_message(
        target, msg=(f'伪装域名已更换为 {sni}。订阅链接地址不变，客户端刷新后生效；'
                     '手动导入单条链接的用户需要重新获取连接。'))


# ---------------------------------------------------------------------------
# 中转落地：落地机侧导出接入参数
# ---------------------------------------------------------------------------
# 落地机专门为中转入口机建一个用户，下面把「入口机该填什么」算出来。
# 参数完全来自现有设置，不新增任何状态：入口机填的东西就是落地机用户
# 本来就有的凭据。
@router.post('/settings/export-uplink')
def export_uplink(request: Request, user: str = Depends(require_user),
                  _: None = Depends(verify_csrf), key: str = Form(...),
                  client_name: str = Form('')):
    """为入口机生成一份可直接填进「中转落地」的参数。"""
    settings = db.all_settings()
    key = key.strip()
    if key not in singbox.BY_KEY or key not in system_ops.UPLINK_TYPES:
        return redirect_with_message(
            '/settings', error='中转落地只支持「日常连接」(VLESS+REALITY) 和「弱网连接」(Hysteria2)。')

    name = (client_name or '').strip() or 'entry'
    if not db.CLIENT_NAME_RE.match(name):
        return redirect_with_message(
            '/settings', error='用于中转的用户名只能用英文、数字和下划线。')

    client = db.get_client(name)
    if not client:
        # 落地机上没有这个用户就自动建一个，省得用户来回跳两次页面。
        if not db.add_client(name):
            return redirect_with_message('/settings', error=f'用户 {name} 已存在，请刷新后重试。')
        try:
            apply_config()
        except Exception as exc:
            db.delete_client(name)
            log_action('export uplink', name, 'error', str(exc))
            return redirect_with_message(
                '/settings', error='创建中转用户失败，请先在首页点一次「检查并修复」。')
        client = db.get_client(name)
        log_action('add client', name, 'ok', 'uplink')
    if not client['enabled']:
        return redirect_with_message('/settings', error=f'用户 {name} 已暂停，请先恢复再导出。')

    values = {
        'uplink_type': key,
        'uplink_server': settings.get('public_host', ''),
        'uplink_port': str(singbox.BY_KEY[key].port),
    }
    if key == 'vless-reality':
        values.update({
            'uplink_uuid': client['uuid'],
            'uplink_sni': settings.get('reality_sni', '') or singbox.FALLBACK_SNI,
            'uplink_public_key': settings.get('reality_public_key', ''),
            'uplink_short_id': settings.get('reality_short_id', ''),
        })
    else:
        values.update({
            'uplink_password': client['password'],
            'uplink_hy2_obfs': settings.get('hy2_obfs', ''),
        })

    if not values.get('uplink_server'):
        return redirect_with_message(
            '/settings', error='本机还没有配置服务器地址，先在上面「服务器地址」填好再导出。')
    if key == 'vless-reality' and not values.get('uplink_public_key'):
        return redirect_with_message(
            '/settings', error='本机还没有生成 REALITY 密钥，先在首页点一次「检查并修复」。')

    log_action('export uplink', name, 'ok', key)
    return templates.TemplateResponse('uplink-export.html', {
        'request': request,
        'active': 'settings',
        'csrf_token': csrf_token(request),
        'client_name': name,
        'tech_name': singbox.BY_KEY[key].tech_name,
        'values': values,
        'notes': _export_notes(key, settings),
    })


def _export_notes(key, settings):
    """导出页上的注意事项，按协议和当前配置动态生成。"""
    notes = ['把下面的参数原样填到入口机的「设置 → 高级设置 → 中转落地」里，'
             '在那台机器上保存即可。']
    if key == 'vless-reality':
        notes.append('「日常连接」走 TCP，UDP 不会经过本机；如果入口机还要给用户开 '
                     'Hysteria2，请改用「弱网连接」导出，或在入口机的中转落地里选 Hysteria2。')
        notes.append('本机的伪装域名或 REALITY 密钥以后如果变了，入口机需要重新填写，'
                     '否则连不上。')
    else:
        notes.append('「弱网连接」跑在 QUIC/UDP 上，请确认入口机能访问本机的 '
                     f'{singbox.BY_KEY[key].port}/UDP，云厂商安全组也要放行。')
        if settings.get('hy2_obfs'):
            notes.append('本机启用了混淆密码，入口机必须填上同样的值。')
    notes.append(f'入口机在本机上的身份是用户 {key} 对应的账号：'
                 '在「用户」页暂停或重置它，入口机会立刻断开。')
    return notes

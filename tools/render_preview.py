#!/usr/bin/env python3
"""渲染界面预览图，供 README 使用。

用真实的 Jinja 模板与 CSS 渲染，而不是手画示意图 —— 这样截图与
用户装完看到的界面一致，不会出现「文档与实物不符」。

用法：
    python3 tools/render_preview.py [输出目录]

需要在有 Chromium/Chrome 的环境里运行（headless 截图）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 让 `import app.*` 可用 —— 必须在任何 app 导入之前执行
sys.path.insert(0, str(ROOT))
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'docs' / 'images'

# 演示数据：用一个不存在的域名和占位 IP，避免把真实部署信息写进仓库。
DEMO = {
    'public_host': 'panel.example.com',
    'reality_sni': 'www.example-cdn.com',
    'session_days': '30',
    'protocols': 'vless-reality,hysteria2',
    'panel_port': '20001',
    'reality_public_key': 'DEMOpubKEYplaceholderDEMOpubKEYplaceholder12',
    'reality_short_id': 'a1b2c3d4',
}

CHROME_CANDIDATES = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    shutil.which('google-chrome') or '',
    shutil.which('chromium') or '',
    shutil.which('chromium-browser') or '',
]


def _human(n):
    """把字节数格式化成面板里那种可读形式（与 app.web.human_bytes 风格一致）。"""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024


def find_chrome():
    for c in CHROME_CANDIDATES:
        if c and Path(c).is_file():
            return c
    return None


def render_html(name, template, ctx_builder, css, js=''):
    """用真实模板渲染出独立的 HTML 文件。"""
    os.environ.setdefault('IVPN_DATA_DIR', tempfile.mkdtemp(prefix='nodepanel-preview-'))

    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(ROOT / 'app' / 'templates')),
                      autoescape=True)
    env.filters['download_href'] = lambda u: '/assets' + u if u.startswith('/download/') else u
    env.filters['tojson'] = lambda o, **k: json.dumps(o, ensure_ascii=False)
    env.globals['static_path'] = '/assets'

    ctx = ctx_builder()
    html = env.get_template(template).render(**ctx)
    html = html.replace('<link rel="stylesheet" href="/assets/app.css?v=20261009a">',
                        f'<style>{css}</style>')
    html = re.sub(r'<script src="/assets/app\.js[^"]*"></script>',
                  f'<script>{js}</script>' if js else '', html)
    # 去掉对图标文件的引用（预览图里不需要，缺失也无妨）
    html = re.sub(r'<img[^>]*renrenvpn-source-logo[^>]*>', '', html)
    # 二维码由后端动态生成，静态渲染时是裂图 —— 换成一个占位方块，
    # 让预览图看起来完整（真实界面这里就是二维码）。
    html = re.sub(
        r'<img class="sub-qr"[^>]*>',
        '<div class="sub-qr" style="display:flex;align-items:center;justify-content:center;'
        'background:#f6f8fa;border:1px solid #e6eaef;border-radius:8px;'
        'color:#8a919b;font-size:12px;text-align:center;padding:8px">二维码<br>（扫码导入）</div>',
        html)
    # 展开折叠区块，否则截图里只看得到标题
    for cls in ('user-card', 'advanced-block', 'sub-alt', 'more-actions'):
        html = html.replace(f'<details class="{cls}"', f'<details class="{cls}" open')
    path = OUT / f'.{name}.html'
    path.write_text(html, encoding='utf-8')
    return path


def shoot(chrome, html_path, out_png, size='1200x900'):
    subprocess.run([chrome, '--headless', '--disable-gpu', '--no-sandbox',
                    '--hide-scrollbars', '--force-device-scale-factor=2',
                    f'--window-size={size}',
                    '--default-background-color=FFFFFFFF',
                    f'--screenshot={out_png}', f'file://{html_path}'],
                   check=True, capture_output=True, timeout=120)


def main():
    chrome = find_chrome()
    if not chrome:
        sys.exit('找不到 Chrome/Chromium，无法截图。请安装其中一个后重试。')
    print(f'使用浏览器：{chrome}')

    OUT.mkdir(parents=True, exist_ok=True)
    css = (ROOT / 'app' / 'static' / 'app.css').read_text(encoding='utf-8')
    js = (ROOT / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')

    # 让预览页的演示表单能显示内容
    bootstrap = f'''
    <script>
    window.__DEMO__ = {json.dumps(DEMO)};
    </script>
    '''

    def base_ctx(active, extra=None):
        ctx = {
            'csrf_token': 'preview',
            'active': active,
            'msg': '', 'error': '',
            'request': type('R', (), {
                'base_url': f'https://{DEMO["public_host"]}:{DEMO["panel_port"]}/',
                'query_params': {},
            })(),
        }
        ctx.update(extra or {})
        return ctx

    # --- 登录页 ---
    login = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>NodePanel 登录</title><style>{css}</style></head><body>
<div class="page-shell" style="max-width:420px;padding-top:80px">
  <h1 style="text-align:center;margin-bottom:6px">节点面板</h1>
  <p class="kv-muted" style="text-align:center;margin-bottom:28px">请输入管理账号登录</p>
  <div class="card"><div style="padding:26px 24px">
    <div class="field"><label>账号</label>
      <input value="admin" readonly></div>
    <div class="field"><label>密码</label>
      <div class="password-input"><input type="password" value="demo-password" readonly>
      <button type="button">显示</button></div></div>
    <button class="button primary" style="width:100%">登录</button>
  </div></div>
</div></body></html>'''
    (OUT / '.login.html').write_text(login, encoding='utf-8')
    shoot(chrome, OUT / '.login.html', OUT / 'login.png', '900x700')
    print('  ✅ login.png')

    # --- 设置页（截「高级设置」里的出口配置）---
    os.environ.setdefault('IVPN_DATA_DIR', tempfile.mkdtemp(prefix='nodepanel-preview-'))
    from app import db, singbox, system_ops as so
    db.init_db()
    db.set_settings(DEMO)
    db.add_client('myphone')

    settings = db.all_settings()
    ctx = base_ctx('settings', {
        'admin_username': 'admin',
        'settings': settings,
        'enabled_keys': singbox.enabled_keys(settings),
        'all_protocols': singbox.PROTOCOLS,
        'login_full_url': f'https://{DEMO["public_host"]}:{DEMO["panel_port"]}/login-xxxxxxxx',
        'panel_port': int(DEMO['panel_port']),
        'acme_port': 80,
        'firewall_rules': singbox.firewall_rules(settings),
        'update_status': {},
        'relay_types': so.RELAY_TYPES,
        'relay_active': False,
        'uplink_types': so.UPLINK_TYPES,
        'uplink_active': False,
    })
    html = render_html('settings', 'settings.html', lambda: ctx, css, js)
    shoot(chrome, html, OUT / 'settings.png', '1100x1400')
    print('  ✅ settings.png')

    # --- 中转/落地页 ---
    ctx_e = base_ctx('egress', {
        'settings': settings,
        'relay_types': so.RELAY_TYPES,
        'relay_active': False,
        'uplink_types': so.UPLINK_TYPES,
        'uplink_active': False,
    })
    html = render_html('egress', 'egress.html', lambda: ctx_e, css, js)
    shoot(chrome, html, OUT / 'egress.png', '1100x1300')
    print('  ✅ egress.png')

    # --- 用户页 ---
    from app import web as webmod
    ctx2 = base_ctx('users', {
        'clients': webmod.client_rows(ctx['request']),
        'downloads': {},
    })
    html = render_html('users', 'users.html', lambda: ctx2, css, js)
    shoot(chrome, html, OUT / 'users.png', '1100x1300')
    print('  ✅ users.png')

    # --- 概览页 ---
    # 概览页字段较多，逐项对齐 main.py 里的 dashboard 上下文，
    # 否则截图会出现「未启动」「暂无用户」这类误导性的空状态。
    up = 2_400_000_000      # 2.4 GB 上行
    down = 18_600_000_000   # 18.6 GB 下行
    ctx3 = base_ctx('home', {
        'running': True,
        'ready': True,
        'client_count': 1,
        'active_count': 1,
        'recent_count': 1,
        'traffic_total': _human(up + down),
        'traffic_uplink': _human(up),
        'traffic_downlink': _human(down),
        'has_traffic': True,
        'clients': [{
            'name': 'myphone', 'enabled': True, 'online': True,
            'traffic': _human(up + down),
            'uplink': _human(up), 'downlink': _human(down),
            'last_seen': '刚刚',
        }],
        'protocols': [singbox.BY_KEY[k] for k in singbox.enabled_keys(settings)],
        'firewall_rules': singbox.firewall_rules(settings),
        'panel_port': int(DEMO['panel_port']),
        'acme_port': 80,
        'guide': '', 'first_visit': False,
        'sni_broken': False,
        'reality_sni': DEMO['reality_sni'],
        'server_host': DEMO['public_host'],
        'my_platform': None,
        'downloads': {},
        'platform_help_url': '',
    })
    html = render_html('dashboard', 'dashboard.html', lambda: ctx3, css, js)
    shoot(chrome, html, OUT / 'dashboard.png', '1100x1200')
    print('  ✅ dashboard.png')

    # 清理中间产物
    for f in OUT.glob('.*.html'):
        f.unlink()
    print(f'\n完成，图片在 {OUT}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""NodePanel 发布工具：生成签名密钥、构建并签名 release 资产。

面板的「一键更新」会校验 Ed25519 签名与逐文件 SHA-256。用本脚本产出的
密钥对签名，更新时才会通过校验。

用法（在自己电脑上跑，不要在服务器上）：

    # 1. 首次：生成密钥对，并把私钥妥善保管
    python3 release.py keygen
    #    → 生成 nodepanel-release-private.pem（务必离线保存）
    #    → 更新包内的 app/release-public-key.pem

    # 2. 构建某个 tag 的 release 资产并签名
    python3 release.py build --tag v1.0.0

    # 3. 把 build 产物传到 GitHub Releases
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIVATE_KEY = ROOT / 'nodepanel-release-private.pem'
PUBLIC_KEY = ROOT / 'app' / 'release-public-key.pem'
# 面板源码包要包含的顶层条目：安装器会检查 app/ 与 requirements.txt 是否存在。
PANEL_ITEMS = ('app', 'requirements.txt', 'requirements.lock')

# 与安装器里的 ACME 摘要策略一致：单文件 ≤ 64MB。
MAX_ASSET = 64 * 1024 * 1024


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def _genkey_openssl():
    """用 openssl 生成。macOS 自带的 LibreSSL 3.x 不含 Ed25519，会失败。"""
    try:
        run(['openssl', 'genpkey', '-algorithm', 'ed25519',
             '-out', str(PRIVATE_KEY)])
    except subprocess.CalledProcessError:
        PRIVATE_KEY.unlink(missing_ok=True)
        return False
    run(['openssl', 'pkey', '-in', str(PRIVATE_KEY), '-pubout',
         '-out', str(PUBLIC_KEY)])
    return True


def _genkey_python():
    """回退方案：直接用 cryptography 生成，不依赖 openssl 的算法支持。"""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        return False
    key = ed25519.Ed25519PrivateKey.generate()
    PRIVATE_KEY.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    PUBLIC_KEY.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))
    return True


def keygen():
    """生成 Ed25519 密钥对。"""
    if PRIVATE_KEY.exists():
        print(f'已存在 {PRIVATE_KEY.name}，不覆盖。删除它才会重新生成。')
        return
    if not (_genkey_openssl() or _genkey_python()):
        sys.exit('无法生成 Ed25519 密钥：openssl 不支持 Ed25519，'
                 '且未安装 cryptography。请 pip install cryptography。')
    os.chmod(PRIVATE_KEY, 0o600)
    print(f'✅ 私钥：{PRIVATE_KEY}')
    print(f'✅ 公钥：{PUBLIC_KEY}（已写进面板包）')
    print()
    print('⚠️  私钥丢失 = 以后无法发布更新；私钥泄露 = 别人能伪造更新包。')
    print('   请离线备份，不要提交进 Git。')
    if (ROOT / '.gitignore').exists():
        print('   （.gitignore 已存在，确认其中包含 ' + PRIVATE_KEY.name + '）')


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _collect_sources(dst):
    """把面板源码拷进临时目录，并剔除不该进包的东西。

    包内必须同时有 app/ 与 requirements.txt —— 安装器据此识别源码根目录。
    """
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for item in PANEL_ITEMS:
        src = ROOT / item
        if not src.exists():
            sys.exit(f'缺少必需文件：{item}')
        if src.is_dir():
            shutil.copytree(src, dst / item)
        else:
            shutil.copy2(src, dst / item)
    # 清理构建产物与缓存
    for path in list(dst.rglob('__pycache__')):
        shutil.rmtree(path, ignore_errors=True)
    for path in list(dst.rglob('*.pyc')):
        path.unlink()
    return dst


def build(args):
    """构建 release 资产、生成清单并签名。"""
    if not PRIVATE_KEY.exists():
        sys.exit(f'缺少私钥 {PRIVATE_KEY.name}，先执行：python3 release.py keygen')
    if not PUBLIC_KEY.exists():
        sys.exit(f'缺少公钥 {PUBLIC_KEY.name}')

    tag = args.tag
    if not tag.startswith('v'):
        tag = 'v' + tag.lstrip('v')
    version = tag[1:]

    out = ROOT / 'build' / tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = _collect_sources(tmp / 'nodepanel')

        # 1) 面板源码包（顶层目录名要和安装器探测的一致）
        panel_zip = out / f'nodepanel-panel-{version}.zip'
        with zipfile.ZipFile(panel_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(src.rglob('*')):
                if path.is_file():
                    zf.write(path, f'nodepanel/{path.relative_to(src)}')

        # 2) 安装脚本
        installer = ROOT / 'install-webui.sh'
        if not installer.is_file():
            sys.exit('缺少 install-webui.sh')
        shutil.copy2(installer, out / 'install-webui.sh')

        # 3) sing-box 内核：优先用本地已下载的，否则从官方 release 取
        sb_ver = args.singbox_version
        sb_assets = {}
        for arch in ('amd64', 'arm64'):
            cached = list(ROOT.glob(f'sing-box-{sb_ver}-linux-{arch}.tar.gz'))
            if cached:
                sb_assets[arch] = cached[0]
                continue
            url = (f'https://github.com/SagerNet/sing-box/releases/download/'
                   f'v{sb_ver}/sing-box-{sb_ver}-linux-{arch}.tar.gz')
            dest = out / f'sing-box-{sb_ver}-linux-{arch}.tar.gz'
            print(f'下载内核 {arch} …')
            run(['curl', '-fsSL', '--connect-timeout', '20', '--retry', '3',
                 '-o', str(dest), url])
            sb_assets[arch] = dest

        # 4) 清单：记录安装脚本、面板包与两个架构内核的摘要
        manifest = {
            'schema': 1,
            'tag': tag,
            'source_asset': panel_zip.name,
            'source_sha256': sha256_file(panel_zip),
            'installer_sha256': sha256_file(out / 'install-webui.sh'),
            'singbox': {
                'version': sb_ver,
                'amd64_sha256': sha256_file(sb_assets['amd64']),
                'arm64_sha256': sha256_file(sb_assets['arm64']),
            },
        }
        manifest_path = out / 'nodepanel-update.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding='utf-8')

        # 5) 签名：对清单的原始字节签名（CLI 用 openssl pkeyutl -rawin 校验）
        sig_path = out / 'nodepanel-update.json.sig'
        run(['openssl', 'pkeyutl', '-sign', '-inkey', str(PRIVATE_KEY),
             '-rawin', '-in', str(manifest_path), '-out', str(sig_path)])

    # 6) 自校验：用公钥验证签名，避免发出一个验不过的包
    run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(PUBLIC_KEY),
         '-rawin', '-in', str(manifest_path), '-sigfile', str(sig_path)])

    print(f'\n✅ release 资产已生成并通过签名自校验：{out}')
    for f in sorted(out.iterdir()):
        print(f'   {f.name:44} {f.stat().st_size:>10,} 字节')
    print('\n下一步：')
    print(f'  1. 把 {out} 里的文件上传到 GitHub Release（tag = {tag}）')
    print('  2. 把 app/release-public-key.pem 提交进仓库（公钥必须随代码走）')
    print(f'  3. 在面板执行：nodepanel set-update-repo <账号>/<仓库>')


def main():
    ap = argparse.ArgumentParser(description='NodePanel 发布工具')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('keygen', help='生成 Ed25519 签名密钥对')

    b = sub.add_parser('build', help='构建并签名 release 资产')
    b.add_argument('--tag', required=True, help='版本号，如 v1.0.0')
    b.add_argument('--singbox-version', default='1.14.2', help='sing-box 版本')

    args = ap.parse_args()
    if args.cmd == 'keygen':
        keygen()
    elif args.cmd == 'build':
        build(args)


if __name__ == '__main__':
    main()

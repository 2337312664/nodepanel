"""产品标识与部署路径。

集中定义是为了让品牌名和服务路径只有这一处来源：界面文案、CLI、安装脚本
从同一组常量派生，避免出现「界面叫 A、systemd 叫 B、目录叫 C」的分裂。

所有值都可用环境变量覆盖，便于在不改代码的情况下做私有部署或并行测试：

    IVPN_PRODUCT_NAME    界面显示名
    IVPANEL_DATA_DIR     数据根目录
    IVPANEL_RUN_DIR      运行期 socket 目录
    IVPANEL_CERT_STORE   面板证书库
    IVPANEL_WEB_USER     面板服务用户
    IVPANEL_SB_USER      sing-box 服务用户
"""

import os

# ---- 品牌 ---------------------------------------------------------------
# 界面标题、CLI 描述、订阅标题都用它。
PRODUCT_NAME = os.environ.get('IVPN_PRODUCT_NAME', 'NodePanel')
# CLI 程序名（argparse prog、帮助文案）。
CLI_NAME = os.environ.get('IVPN_CLI_NAME', 'nodepanel')

# ---- 部署路径 -----------------------------------------------------------
# 全部默认走 /etc 与 /run，改名后与旧版本互不干扰，便于平滑迁移。
DATA_ROOT = os.environ.get('IVPANEL_DATA_DIR', '/etc/nodepanel')
RUN_DIR = os.environ.get('IVPANEL_RUN_DIR', '/run/nodepanel')
CERT_STORE = os.environ.get('IVPANEL_CERT_STORE', '/etc/nodepanel-cert')

# ---- 服务账号 -----------------------------------------------------------
WEB_USER = os.environ.get('IVPANEL_WEB_USER', 'nodepanel-web')
SB_USER = os.environ.get('IVPANEL_SB_USER', 'nodepanel-sb')

# ---- systemd 单元名 -----------------------------------------------------
SERVICE_WEB = 'nodepanel-web'
SERVICE_HELPER = 'nodepanel-helper'
SERVICE_UPDATE = 'nodepanel-update'

# ---- 更新机制 -----------------------------------------------------------
# 更新清单与签名文件名随产品走；改名后旧的更新产物不再被识别，这是预期的
# （换名等同于换了一个产品，需要自己的 release 与签名密钥）。
UPDATE_MANIFEST = 'nodepanel-update.json'
UPDATE_SIGNATURE = UPDATE_MANIFEST + '.sig'
# 发布这个仓库需要上传的签名公钥。换成自己的仓库时替换它。
RELEASE_PUBLIC_KEY = 'release-public-key.pem'
# 默认更新仓库：填 owner/repo，或留空由安装时指定。
DEFAULT_UPDATE_REPO = os.environ.get('IVPANEL_UPDATE_REPO', '')
# 迁移自旧项目时，把这些仓库识别为「未设置」以便改指向新源。
LEGACY_UPDATE_REPOS = (
    'iwantruncom/renrenVPN',
    'iwantruncom/iwantrun.com-VPN-Web-Manager',
)

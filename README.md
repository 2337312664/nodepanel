# NodePanel

自建 sing-box 的节点管理面板。给一台自己的 VPS 装上，用网页管理连接、用户和订阅，
不需要敲命令。

> **第一次安装？** 看 [安装指南](INSTALL.md) —— 手把手步骤，不需要 Linux 经验。
> 下面是给开发者和运维看的速查。

## 特点

- **一条安装命令**，装完给一个网址就能用；证书、防火墙、协议参数全自动配置
- **两个出口功能**，各自独立、可以叠加
  - **住宅代理落地** —— 出网流量交给 SOCKS5 / HTTP 代理，网站看到的是代理的 IP
  - **中转落地** —— 本机作为入口机，把流量推给另一台落地机，由它出网
- **智能订阅** —— 同一个链接，Karing / sing-box / v2rayN 拿完整配置，
  Clash Verge 拿 Clash 配置，也可加 `?target=clash` 强制指定
- **适配受限环境** —— 公网 IPv4、端口受限（NAT）、容器均可部署
- **凭据签名更新** —— 每次更新校验 Ed25519 签名与逐文件 SHA-256

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Debian 12+ / Ubuntu 22.04+，需 systemd |
| 架构 | x86_64 或 ARM64 |
| 内存 | 1GB 建议；512MB 可用 |
| 网络 | 公网 IPv4；端口受限环境见下文 |
| 端口 | `80/TCP`（证书）、`443/TCP`+`443/UDP`（协议）、面板端口 |

## 安装

**没有 Linux 经验？** 看 [安装指南](INSTALL.md)（手把手，含截图级说明）。

### 普通服务器（有公网 IP）

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install-webui.sh)
```

自动申请 Let's Encrypt 证书、配置防火墙、启动服务。结束时打印访问地址与密码
—— **密码只显示这一次**。

### 端口受限（NAT）服务器

商家共享 IP、只映射若干高位端口时，必须先看**商家后台的端口映射**，
把三个端口都填上，否则面板取随机端口、外部访问不了：

```bash
IVPN_NAT_MODE=1 \
IVPN_PANEL_PORT=20001 \
IVPN_LISTEN_PORT=20002 \
IVPN_PUBLIC_PORT=20002 \
  bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install-webui.sh)
```

| 变量 | 含义 | 说明 |
| --- | --- | --- |
| `IVPN_NAT_MODE` | 开启受限环境适配 | 跳过 ACME（宿主机占着 80 端口，HTTP-01 无法通过），改用自签证书 |
| `IVPN_PANEL_PORT` | 面板端口 | 必须与商家映射的公网端口一致 |
| `IVPN_LISTEN_PORT` | 内核监听的内网端口 | 即商家写的「容器端口」 |
| `IVPN_PUBLIC_PORT` | 客户端连接的公网端口 | 分享链接里使用这个 |

`IVPN_LISTEN_PORT` 与 `IVPN_PUBLIC_PORT` 不同时（例如商家写 `20002→10000`），
监听填 `10000`、公网填 `20002`。面板会在分享链接里写后者。

### 有域名：申请可信证书

有域名（Cloudflare 托管）可以走 DNS-01，不需要任何入站端口就能拿到
Let's Encrypt 证书，浏览器不再报警告：

```bash
IVPN_NAT_MODE=1 \
IVPANEL_CERT_DOMAIN="panel.example.com" \
IVPN_CF_TOKEN="<Cloudflare API Token>" \
IVPN_PANEL_PORT=20001 \
IVPN_LISTEN_PORT=20002 \
IVPN_PUBLIC_PORT=20002 \
  bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install-webui.sh)
```

Token 需要 `Zone → DNS → Edit` 权限。安装器会一并配置好每天两次的自动续期。

> 访问时端口号仍需带上（`https://panel.example.com:20001`）。
> 想去掉端口，需要把宿主机 443 映射到容器，这取决于服务商。

### 安装参数汇总

所有参数都可通过环境变量覆盖，便于私有部署或并行测试：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `IVPN_PRODUCT_NAME` | `NodePanel` | 界面显示名 |
| `IVPN_CLI_NAME` | `nodepanel` | CLI 命令名 |
| `IVPANEL_DATA_DIR` | `/etc/nodepanel` | 数据目录 |
| `IVPANEL_APP_DIR` | `/opt/nodepanel` | 程序目录 |
| `IVPANEL_CERT_STORE` | `/etc/nodepanel-cert` | 证书库 |
| `IVPANEL_WEB_USER` | `nodepanel-web` | 面板服务用户 |
| `IVPANEL_SB_USER` | `nodepanel-sb` | 内核服务用户 |
| `IVPANEL_REPO` | `2337312664/nodepanel` | 下载源码与内核的仓库 |
| `IVPANEL_UPDATE_REPO` | 同上 | 面板内更新检查的仓库 |

## 常用命令

装完后可用 `nodepanel`：

| 命令 | 作用 |
| --- | --- |
| `nodepanel info` | 查看面板地址、账号、需要放行的端口 |
| `nodepanel reset-password` | 重置管理密码 |
| `nodepanel reset-path` | 更换面板登录地址 |
| `nodepanel repair` | 连不上时重建配置并重启 |
| `nodepanel backup [路径]` | 备份全部用户与设置 |
| `nodepanel restore <路径>` | 从备份恢复 |
| `nodepanel relay <类型> <地址> <端口>` | 设置住宅代理出口 |
| `nodepanel uplink <协议> <地址> <端口>` | 设置中转落地 |
| `nodepanel nat --show` | 查看 NAT 端口映射 |

## 出口配置

### 住宅代理落地

面板「设置 → 高级设置 → 住宅代理落地」，或：

```bash
nodepanel relay socks5 proxy.example.com 1080 --username U --password P
nodepanel relay --off          # 关闭，恢复直连
```

### 中转落地

两台机器：入口机（给用户连）+ 落地机（负责出网）。

1. 落地机上：面板「我是落地机，导出接入参数」→ 拿到参数
2. 入口机上：面板「中转落地」→ 填入 → 保存

```bash
nodepanel uplink vless-reality <落地机IP> 443 --uuid U --sni S --public-key K --short-id I
nodepanel uplink --off
```

两者可叠加：`用户 → 入口机 → 落地机 → 住宅代理 → 目标`。

## 客户端

| 平台 | 推荐 |
| --- | --- |
| iOS | Karing、sing-box MT |
| Android | Karing、v2rayNG |
| Windows / macOS | sing-box、v2rayN |

Clash Verge 也支持（面板会按 UA 返回 Clash 配置）。

## 安全设计

- 面板以非特权用户运行，特权操作经 Unix socket 交给一个**只接受白名单操作**的 root helper
- sing-box 内核独立低权限运行 + systemd 沙箱，配置以只读方式挂载给它
- 数据库仅面板可读写；私钥 `0640` 限同组
- 密码 bcrypt 哈希；登录失败限流并临时锁定；表单带 CSRF 防护
- 更新走 Ed25519 签名 + 逐文件 SHA-256 双重校验，失败自动回滚
- 防火墙操作限定在协议端口白名单内

## 许可

基于 [iwantruncom/renrenVPN](https://github.com/iwantruncom/renrenVPN)（GPL-3.0）
修改而来，遵循 **GPL-3.0**。完整第三方许可见 `NOTICE.md`。

自用不受限制；对外分发时须保留许可声明并提供完整源码。

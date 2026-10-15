# 安装指南

写给不熟悉 Linux 的人。照着做就行，不需要懂命令。

---

## 最快的方式：用安装向导

连上服务器后，只需要敲这一行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install.sh)
```

它会把该问的都问一遍（服务器是什么类型、端口怎么填），
然后自动装好。**不想读长文档的话，用这个就够了。**

> 想先看看它会执行什么命令，不加参数跑一次：加 `--dry-run`
>
> ```bash
> bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install.sh) --dry-run
> ```

下面是从头开始的完整步骤 —— 第一次接触服务器的话，建议照顺序看一遍。

---

## 开始之前

你需要准备：

1. **一台服务器**（VPS），装 **Debian 12 或 Ubuntu 22.04**
   - 内存 1GB 够用，512MB 也能跑
   - 必须能连外网
2. **服务器的 IP、SSH 端口、root 密码**（买服务器时商家会给）

> 用云服务商的话，安全组里要放行 `22/TCP`，不然连不上服务器。

---

## 第一步：连上服务器

**Windows**：下载 [Xshell](https://www.xshell.com/) 或 [PuTTY](https://www.putty.org/)，
新建连接，填商家给的 IP、端口、密码。

**Mac / Linux**：打开「终端」，输入：

```bash
ssh root@你的服务器IP -p SSH端口
```

回车后输入密码（输入时不显示字符，正常现象）。

看到类似 `root@ubuntu:~#` 就说明连上了。

---

## 第二步：安装

### 情况 A：你的服务器有独立公网 IP

直接复制这一行，粘贴进终端，回车：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install-webui.sh)
```

等待 3-10 分钟。装完会显示：

```
============================================================
安装完成
============================================================
访问地址：https://你的IP:端口/login-xxxxxxxx
管理账号：admin
管理密码：xxxxxxxxxxxxxxxx
```

> ⚠️ **密码只显示一次，立刻截图或记下来。** 丢了可以用 `nodepanel reset-password` 重设。

### 情况 B：你的服务器是 NAT 机器（共享 IP）

这种机器的特征：商家给你的是**内网 IP**（如 `10.x`、`172.16-31.x`），
你需要通过商家分配的几个**公网端口**访问。

**先去商家后台找「端口映射」页面**，通常长这样：

```
中心端口转发规则
  20001 → 20001    TCP
  20002 → 20002    TCP/UDP
  30864 → 22       TCP
```

**看左边那个数字**（公网端口）和**右边那个数字**（容器端口），照抄进命令：

```bash
IVPN_NAT_MODE=1 \
IVPN_PANEL_PORT=20001 \
IVPN_LISTEN_PORT=20002 \
IVPN_PUBLIC_PORT=20002 \
bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install-webui.sh)
```

**怎么填这三个数**：

| 变量 | 填什么 | 上面例子 |
| --- | --- | --- |
| `IVPN_PANEL_PORT` | 面板的端口（商家映射的第一个 TCP） | `20001` |
| `IVPN_LISTEN_PORT` | 「→」**右边**的数字（内网端口） | `20002` |
| `IVPN_PUBLIC_PORT` | 「→」**左边**的数字（公网端口，客户端连的） | `20002` |

> 如果商家写的是 `20002 → 10000`（两边数字不同），
> 那就 `IVPN_LISTEN_PORT=10000`、`IVPN_PUBLIC_PORT=20002`。

**如果命令提示端口被占用**，换一个商家映射给你的端口重试。

**NAT 机器装完会有证书警告** —— 浏览器提示「不安全」，点「继续访问」即可。
这是正常的：这类环境无法申请正规证书。

---

## 第三步：放行端口

安装完成会打印「需要在云厂商安全组放行」的端口，比如：

```
20002/TCP, 20002/UDP, 80/TCP, 20001/TCP
```

**去服务器的商家后台**，在「安全组」或「防火墙」里，把这些端口**全部放行**。

> 这是新手最常踩的坑：装好了但打不开，九成是端口没放行。

---

## 第四步：打开面板

浏览器访问安装完成时显示的那个**访问地址**（含 `login-xxxx` 那段）。
输入 `admin` 和密码登录。

**首次进入**会有新手引导，跟着做：

1. **添加用户** —— 给你自己或家人起个名字，比如 `myphone`
2. **装客户端** —— 按提示下载（推荐 **Karing**）
3. **导入连接** —— 扫码或复制链接到客户端

---

## 第五步：把连接发给家人

在面板「用户」页面，每个用户都有两组信息：

```
教程链接  https://你的地址/guide/xxxxx
访问码    ABC123XYZ
```

把这两个**分开**发给对方（比如链接用微信、访问码用短信），
对方打开链接、输入访问码，照着页面提示装客户端就行。

> 分开发送是为了安全：万一链接被别人看到，没有访问码也打不开。

---

## 常见问题

### 面板打不开

1. **端口没放行** —— 去商家后台检查安全组（最常见原因）
2. **服务器没装好** —— SSH 上去执行 `nodepanel info` 看看状态
3. **面板服务挂了** —— 执行 `nodepanel repair`

### 客户端连不上

1. **协议端口没放行** —— 检查 `20002/TCP`（和 UDP）是否放行
2. **用订阅链接而不是手输** —— 面板「用户」页有二维码或链接，直接导入
3. **伪装域名失效** —— 面板首页点「检查并修复」

### 忘记密码 / 忘记访问地址

SSH 上去执行：

```bash
nodepanel info              # 查看访问地址
nodepanel reset-password    # 重设密码（会显示新密码）
```

### 想换一个登录地址

面板地址在聊天记录里发过、担心泄露了：

```bash
nodepanel reset-path
```

会生成新的随机地址，旧地址立刻失效。

---

## 常用命令

SSH 登录服务器后可用：

| 命令 | 作用 |
| --- | --- |
| `nodepanel info` | 查看访问地址、账号、要放行的端口 |
| `nodepanel reset-password` | 重置管理密码 |
| `nodepanel reset-path` | 换一个登录地址 |
| `nodepanel repair` | 连不上时重建配置并重启 |
| `nodepanel backup` | 备份全部用户和设置 |
| `nodepanel restore <路径>` | 从备份恢复 |

> 把 `nodepanel` 换成你安装时的 `IVPN_CLI_NAME`（如果改过）。

---

## 进阶：用自己的域名（可选）

有域名的话能申请正规证书，浏览器不再报警告。

**前提**：域名用 Cloudflare 解析，并且有一个 API Token
（Cloudflare 后台 → My Profile → API Tokens → 权限选 `Zone → DNS → Edit`）。

```bash
IVPN_NAT_MODE=1 \
IVPANEL_CERT_DOMAIN=panel.你的域名.com \
IVPN_CF_TOKEN=你的Cloudflare令牌 \
IVPN_PANEL_PORT=20001 \
IVPN_LISTEN_PORT=20002 \
IVPN_PUBLIC_PORT=20002 \
bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install-webui.sh)
```

安装器会自动：申请证书、装好、配置每天自动续期。

> 注意：访问时端口号还是要带上，比如 `https://panel.你的域名.com:20001`。
> 想要不带端口，得让商家把宿主机 443 映射到你的容器。

---

## 出问题了怎么办

装的过程中报错，先把**最后 20 行输出**保存下来，然后：

1. 重新执行一次安装命令 —— 脚本是幂等的，会接着装，不会搞坏已有数据
2. 还是不行，SSH 上去执行 `nodepanel info` 看状态
3. 需要帮助时，把报错信息发出来

> 安装中断**不会丢失数据**。重新跑一遍即可。

#!/bin/bash
# NodePanel 安装向导
#
# 给不熟悉命令的人用：问几个问题，自动组装好安装参数，确认后执行。
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/2337312664/nodepanel/main/install.sh)
#
# 加 --dry-run 只打印将要执行的命令，不实际安装（想先看看效果时用）。
#
# 本脚本不直接安装，它负责检测环境、收集参数，然后把参数交给
# install-webui.sh 执行 —— 真正的校验与安装逻辑都在那边，避免两处实现分叉。

set -uo pipefail

REPO="${IVPANEL_REPO:-2337312664/nodepanel}"
INSTALLER_URL="https://raw.githubusercontent.com/${REPO}/main/install-webui.sh"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# ---------------------------------------------------------------------------
# 输出样式：只在终端是 TTY 时上色，重定向到文件时保持纯文本。
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'
  C=$'\033[0;36m'; N=$'\033[0m'
else
  B=''; G=''; Y=''; R=''; C=''; N=''
fi

title() { printf '\n%s%s%s\n' "$B$C" "$1" "$N"; printf '%s\n' "────────────────────────────────────────────"; }
ok()    { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn()  { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
err()   { printf '  %s✗%s %s\n' "$R" "$N" "$1" >&2; }
info()  { printf '    %s\n' "$1"; }

die() { printf '\n%s错误：%s%s\n' "$B$R" "$1" "$N" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 交互读取。非交互环境（比如被管道喂进来）直接取默认值，避免脚本卡死。
# ---------------------------------------------------------------------------
ask() {
  # ask <提示> <默认值> → 回显答案
  local prompt="$1" default="${2:-}" answer=""
  if [[ ! -t 0 ]]; then
    printf '%s' "$default"
    return
  fi
  if [[ -n "$default" ]]; then
    printf '  %s %s[%s]%s: ' "$prompt" "$B" "$default" "$N" >&2
  else
    printf '  %s: ' "$prompt" >&2
  fi
  read -r answer || answer=""
  printf '%s' "${answer:-$default}"
}

confirm() {
  # confirm <提示> <默认 y/n>
  #
  # 非交互环境（管道、重定向）下按默认值决定，而不是一律返回真 ——
  # 一律为真会让「想手动指定端口吗」这类问题的默认「否」被反转成「是」，
  # 静默装出意料之外的配置。
  local prompt="$1" default="${2:-y}" answer
  if [[ ! -t 0 ]]; then
    [[ "$default" =~ ^[Yy] ]]
    return
  fi
  answer="$(ask "$prompt (y/n)" "$default")"
  [[ "$answer" =~ ^[Yy] ]]
}

# ---------------------------------------------------------------------------
# 环境检测
# ---------------------------------------------------------------------------
detect_public_ip() {
  local url out
  for url in https://api4.ipify.org https://ipv4.icanhazip.com https://4.ident.me; do
    out="$(curl -s4 --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]')"
    if [[ "$out" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
      printf '%s' "$out"; return 0
    fi
  done
  return 1
}

detect_local_ip() {
  # 取默认路由对应的本机地址 —— 比列全部网卡更准。
  local out
  out="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')"
  [[ -n "$out" ]] && { printf '%s' "$out"; return 0; }
  out="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -n "$out" ]] && { printf '%s' "$out"; return 0; }
  return 1
}

port_valid() {
  local p="$1"
  [[ "$p" =~ ^[0-9]+$ ]] && (( p >= 1 && p <= 65535 )) && [[ "$p" != "80" ]]
}

port_free() {
  local p="$1" holder
  # 端口可用则返回 0。ss 缺失时保守地认为可用。
  command -v ss >/dev/null 2>&1 || return 0
  holder="$(ss -ltnpH "sport = :$p" 2>/dev/null)"
  [[ -z "$holder" ]] && return 0
  # 重装/升级时，本面板自己的服务（面板进程、内核）仍占着原端口。
  # 这不算冲突 —— 安装器会重启它们。把它们判为「占用」会让老用户无法重装。
  case "$holder" in
    *nodepanel*|*sing-box*|*app.serve*|*python*|*iwantrun*|*renrenvpn*) return 0 ;;
  esac
  return 1
}

ask_port() {
  # ask_port <提示> <默认值> [是否检查占用]
  #
  # 重试次数有上限：非交互环境（stdin 被重定向或耗尽）下 read 会立刻返回空，
  # 没有上限就会无限循环刷屏。超限后直接终止并说明原因，比卡死有用。
  local prompt="$1" default="$2" check="${3:-1}" value tries=0
  while (( tries < 5 )); do
    tries=$((tries + 1))
    value="$(ask "$prompt" "$default")"
    if [[ -z "$value" ]]; then
      if [[ ! -t 0 ]]; then
        die "非交互环境下无法读取「$prompt」。请改用 install-webui.sh 并通过环境变量传参。"
      fi
      err "不能为空，请输入一个端口号。"
      continue
    fi
    if ! port_valid "$value"; then
      err "必须是 1-65535 的数字，且不能是 80（留给证书续期）。"
      default=""
      continue
    fi
    if [[ "$check" == "1" ]] && ! port_free "$value"; then
      err "端口 $value 已被占用，换一个。"
      # 占用提示要说明「谁」在用，否则用户看到自己刚填的端口被占用会困惑。
      local holder
      holder="$(ss -ltnpH "sport = :$value" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -1)"
      [[ -n "$holder" ]] && info "占用者：$holder"
      default=""
      continue
    fi
    printf '%s' "$value"; return
  done
  die "端口输入重试次数过多，已中止。"
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
main() {
  printf '\n%s' "$B"
  cat <<'BANNER'
  ╭──────────────────────────────────────────╮
  │   NodePanel 安装向导                     │
  │                                          │
  │   回答几个问题，剩下的自动完成           │
  ╰──────────────────────────────────────────╯
BANNER
  printf '%s' "$N"

  # --- 权限 ---
  if [[ "$(id -u)" != "0" ]]; then
    die "需要 root 权限。请用 root 登录服务器后再运行，或前面加 sudo。"
  fi

  # --- 依赖 ---
  command -v curl >/dev/null 2>&1 || die "缺少 curl，无法继续。请先安装：apt install -y curl"

  # --- 系统检查 ---
  title "环境检查"
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    info "系统：${PRETTY_NAME:-未知}"
    case "${ID:-}" in
      debian|ubuntu) ok "系统受支持" ;;
      *) warn "本面板只在 Debian 12+ / Ubuntu 22.04+ 上测试过，其他系统可能失败" ;;
    esac
  fi

  if ! command -v systemctl >/dev/null 2>&1; then
    die "未检测到 systemd。本面板依赖 systemd 管理服务，当前系统不支持。"
  fi
  ok "systemd 可用"

  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64|aarch64|arm64) ok "架构：$arch" ;;
    *) die "不支持的架构：$arch（仅支持 x86_64 / ARM64）" ;;
  esac

  local mem_mb
  mem_mb="$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
  if (( mem_mb > 0 )); then
    if (( mem_mb < 500 )); then
      warn "内存 ${mem_mb}MB 偏小（建议 1GB）。安装依赖时可能被系统终止，"
      info "如遇失败，先执行：rm -rf /tmp/*  再重试"
    else
      ok "内存：${mem_mb}MB"
    fi
  fi

  # --- 判断是否 NAT ---
  title "网络类型"
  local pub_ip local_ip is_nat=0
  pub_ip="$(detect_public_ip)" || pub_ip=""
  local_ip="$(detect_local_ip)" || local_ip=""

  # 检测结果只作为默认建议：探测手段（问外部服务拿出口 IP）在多层 NAT、
  # 代理出网等环境里可能误判，用户对自己服务器的了解比探测更可靠。
  local detected_nat=0 detected_hint=""
  if [[ -z "$pub_ip" ]]; then
    detected_hint="无法探测公网 IP（可能被防火墙拦住）"
    detected_nat=1
  elif [[ -n "$local_ip" && "$pub_ip" != "$local_ip" ]]; then
    info "本机地址：$local_ip"
    info "出口地址：$pub_ip"
    detected_hint="出口地址与本机地址不同，疑似共享 IP"
    detected_nat=1
  else
    info "服务器公网 IP：${pub_ip:-未知}"
    detected_hint="出口地址与本机地址一致，疑似独立 IP"
    detected_nat=0
  fi

  info "$detected_hint"
  printf '\n'
  printf '  你的服务器有%s独立的公网 IP%s吗？（购买时商家会说明）\n' "$B" "$N"
  printf '  · 独立 IP：浏览器直连 IP 就能访问，无需端口映射\n'
  printf '  · 共享 IP：需要商家的「端口映射」，你只能通过指定端口访问\n\n'

  local default_answer
  (( detected_nat == 0 )) && default_answer="y" || default_answer="n"
  if confirm "有独立公网 IP 吗？" "$default_answer"; then
    is_nat=0
  else
    is_nat=1
  fi

  # --- 收集参数 ---
  local env_args=()

  if (( is_nat == 1 )); then
    title "端口配置"
    printf '  请打开%s服务器商家的后台%s，找到「端口映射」页面\n' "$B" "$N"
    printf '  它通常长这样（左边是外网端口，右边是内网端口）：\n\n'
    printf '      20001  →  20001    TCP\n'
    printf '      20002  →  20002    TCP/UDP\n\n'
    printf '  按你的实际映射填写下面的问题。\n\n'

    env_args+=("IVPN_NAT_MODE=1")

    # 面板与内核各占一个端口，默认按商家最常见的映射给（20001 面板、20002 协议）。
    # 两个默认值不能相同 —— 内核会绑定失败。
    local panel_port listen_port public_port
    panel_port="$(ask_port "面板端口（外网端口那一列）" "20001")"
    local proto_default="20002"
    [[ "$panel_port" == "20002" ]] && proto_default="20003"
    listen_port="$(ask_port "内核监听端口（内网端口那一列）" "$proto_default")"
    public_port="$(ask_port "客户端连接端口（一般为外网端口）" "$listen_port" 0)"

    if [[ "$listen_port" == "$panel_port" ]]; then
      die "内核监听端口不能与面板端口相同（都是 $panel_port）。请为它们分配不同端口。"
    fi

    env_args+=("IVPN_PANEL_PORT=$panel_port")
    env_args+=("IVPN_LISTEN_PORT=$listen_port")
    env_args+=("IVPN_PUBLIC_PORT=$public_port")

    printf '\n'
    ok "面板端口：$panel_port"
    ok "内核监听：$listen_port"
    ok "对外端口：$public_port"
  else
    title "端口配置"
    info "使用默认行为："
    info "· 面板端口：随机取一个空闲高位端口"
    info "· 协议端口：443"
    printf '\n'
    if confirm "想手动指定面板端口吗？（一般不需要）" "n"; then
      local custom
      custom="$(ask_port "面板端口" "2083")"
      env_args+=("IVPN_PANEL_PORT=$custom")
      ok "面板端口：$custom"
    fi
  fi

  # --- 域名与证书（可选） ---
  title "证书（可选）"
  printf '  默认用 Let'"'"'s Encrypt 给服务器 IP 签证书，浏览器不报警告。\n'
  printf '  如果你有域名（Cloudflare 托管），可以签域名证书，效果更好。\n\n'
  if confirm "要使用域名吗？" "n"; then
    local domain token
    domain="$(ask "域名（例如 panel.example.com）" "")"
    if [[ -n "$domain" ]]; then
      printf '  Cloudflare Token 获取：后台 → My Profile → API Tokens → 权限选 Zone → DNS → Edit\n'
      token="$(ask "Cloudflare API Token" "")"
      if [[ -n "$token" ]]; then
        env_args+=("IVPANEL_CERT_DOMAIN=$domain")
        env_args+=("IVPN_CF_TOKEN=$token")
        ok "将申请证书：$domain"
      else
        warn "没填 Token，跳过域名证书"
      fi
    fi
  fi

  # --- 汇总 ---
  title "确认"
  local cmd="bash <(curl -fsSL $INSTALLER_URL)"
  printf '  将要执行：\n\n'
  if (( ${#env_args[@]} > 0 )); then
    for a in "${env_args[@]}"; do
      # Token 打码
      if [[ "$a" == IVPN_CF_TOKEN=* ]]; then
        printf '    %s%s...（已隐藏）%s \\\n' "$B" "${a:0:24}" "$N"
      else
        printf '    %s%s%s \\\n' "$B" "$a" "$N"
      fi
    done
  fi
  printf '    %s\n\n' "$cmd"

  if (( DRY_RUN == 1 )); then
    printf '  %s（--dry-run：只显示，不执行）%s\n\n' "$Y" "$N"
    return 0
  fi

  printf '  安装需要 3-10 分钟，期间请勿关闭窗口。\n\n'
  if ! confirm "开始安装吗？" "y"; then
    printf '\n  已取消，未做任何改动。\n\n'
    return 0
  fi

  # --- 下载安装器并执行 ---
  title "开始安装"
  local tmp
  tmp="$(mktemp /tmp/nodepanel-install.XXXXXX.sh)" || die "无法创建临时文件。"
  # 退出时清理；只有装完才保留（便于排查失败原因）
  cleanup() { [[ -f "$tmp" ]] && rm -f "$tmp"; }
  trap cleanup EXIT

  info "下载安装脚本…"
  if ! curl -fsSL --max-time 60 -o "$tmp" "$INSTALLER_URL"; then
    die "下载安装脚本失败。请检查网络，或稍后重试。"
  fi
  # 至少确认拿到的是个像样的 shell 脚本
  if ! grep -q 'IVPN_NAT_MODE' "$tmp" 2>/dev/null; then
    die "下载到的安装脚本内容异常，已中止。"
  fi
  ok "下载完成（$(wc -c < "$tmp") 字节）"

  printf '\n'
  # Token 通过环境变量传递，不出现在命令行里（避免被 ps 看到）
  env "${env_args[@]}" bash "$tmp"
  local rc=$?

  printf '\n'
  if (( rc == 0 )); then
    printf '%s%s安装完成%s\n' "$B" "$G" "$N"
    printf '  运行 %snodepanel info%s 可随时查看访问地址。\n\n' "$B" "$N"
  else
    printf '%s%s安装未完成（退出码 %s）%s\n' "$B" "$R" "$rc" "$N"
    printf '  你的数据没有丢失。重新运行本向导即可继续。\n'
    printf '  日志：%s\n\n' "$tmp"
    # 失败时保留脚本便于排查
    trap - EXIT
  fi
  return "$rc"
}

main "$@"

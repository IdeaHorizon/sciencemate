#!/bin/sh
# ScienceMate —— 内网安装（macOS，不经浏览器，不弹 Gatekeeper）
#
#   curl -fsSL <发布目录>/install.sh | sh
#
# ## 为什么这条路不会弹「无法验证开发者」
#
# Gatekeeper 只检查带「隔离标记」（com.apple.quarantine）的文件，而那个标记是
# 浏览器 / 邮件 / AirDrop 在保存文件时打上去的。curl 不打。所以经这条路装的
# 应用，系统根本不会去看它签没签 —— 这不是绕过，是 macOS 自己划的线：从哪来的
# 文件由谁负责。内网发布目录由我们自己负责，就像共享盘拷过来一样。
#
# 我们在最后还是会把隔离标记（若有）去掉一次。理由：万一有人先用浏览器下了 dmg
# 再手动跑这个脚本，装出来的 .app 会带着标记；那时用户看到的是四步走的系统设置。
#
# ## 它做什么
#
# 下载 dmg → 用发布目录里的 SHA256SUMS 核对 → 挂载 → 复制进「应用程序」（写不了就
# 进 ~/Applications）→ 卸载 → 打开。每一步失败就停，不留半个应用。
#
# 环境变量：AFS_RELEASE_URL（发布目录；打包器已经烧了默认值）、AFS_INSTALL_DIR、AFS_AUTH（私有仓库）、
# AFS_DRY_RUN=1（只打印要做什么，不做）。
set -eu

# 下面两行由打包器整行替换（占位符只能出现在这里，别处不许提它 —— 第一版把占位符
# 也写进了 case 的模式里，替换后要么语法错、要么把正确的地址当成"没填"拒掉）。
DEFAULT_URL="__RELEASE_URL__"
DEFAULT_DMG="__DMG_NAME__"
BASE="${AFS_RELEASE_URL:-$DEFAULT_URL}"
DMG="$DEFAULT_DMG"
APP="ScienceMate.app"
DEST="${AFS_INSTALL_DIR:-/Applications}"
DRY="${AFS_DRY_RUN:-}"
# 私有 Forgejo 仓库的 release 资产要登录才下得到：AFS_AUTH="user:token"（只读 token 即可）。
AUTH="${AFS_AUTH:-}"

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }
run() { if [ -n "$DRY" ]; then say "  \$ $*"; else "$@"; fi; }

if [ -z "$BASE" ] || [ "$BASE" = "$(printf '__RELEASE''_URL__')" ]; then
  die "没有发布目录地址。打包时传 --release-url，或运行前 export AFS_RELEASE_URL=http://<内网主机>/afs"
fi
[ "$(uname -s)" = "Darwin" ] || die "这个脚本只装 macOS；Windows 用 install.ps1"
[ "$(uname -m)" = "arm64" ] || die "这个包是 Apple Silicon（arm64）的，这台是 $(uname -m)"

BASE="${BASE%/}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/afs-install.XXXXXX")"
cleanup() {
  [ -d "$WORK/mnt" ] && hdiutil detach "$WORK/mnt" -quiet >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

say "① 下载 ${DMG}（curl，不会打隔离标记）"
run curl -fsSL --retry 3 ${AUTH:+-u "$AUTH"} "$BASE/$DMG" -o "$WORK/$DMG"
run curl -fsSL --retry 3 ${AUTH:+-u "$AUTH"} "$BASE/SHA256SUMS" -o "$WORK/SHA256SUMS"

say "② 核对 SHA-256"
if [ -z "$DRY" ]; then
  expected="$(grep " $DMG\$" "$WORK/SHA256SUMS" | awk '{print $1}')"
  [ -n "$expected" ] || die "SHA256SUMS 里没有 $DMG 这一行"
  actual="$(shasum -a 256 "$WORK/$DMG" | awk '{print $1}')"
  [ "$expected" = "$actual" ] || die "校验和不对：发布目录说 ${expected}，下载到的是 $actual —— 不装"
  say "  ✓ $actual"
else
  say "  \$ shasum -a 256 -c（对 SHA256SUMS 里 $DMG 那一行）"
fi

say "③ 装进 $DEST"
if [ -z "$DRY" ] && [ ! -w "$DEST" ]; then
  DEST="$HOME/Applications"; mkdir -p "$DEST"
  say "  /Applications 写不了，改装到 $DEST"
fi
if [ -z "$DRY" ] && pgrep -x ScienceMate >/dev/null 2>&1; then
  say "  应用正在运行，先退出它"
  osascript -e 'tell application "ScienceMate" to quit' >/dev/null 2>&1 || true
  sleep 2
fi
run hdiutil attach "$WORK/$DMG" -nobrowse -readonly -quiet -mountpoint "$WORK/mnt"
run rm -rf "$DEST/$APP"
run ditto "$WORK/mnt/$APP" "$DEST/$APP"
run hdiutil detach "$WORK/mnt" -quiet
[ -n "$DRY" ] || rmdir "$WORK/mnt" 2>/dev/null || true

say "④ 去掉隔离标记（若有）"
run xattr -dr com.apple.quarantine "$DEST/$APP" 2>/dev/null || true

say "✅ 装好了：$DEST/$APP"
run open -a "$DEST/$APP"

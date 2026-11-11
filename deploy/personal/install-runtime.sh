#!/usr/bin/env bash
# Headless personal runtime. SSH configures it; systemd owns its lifetime.
set -Eeuo pipefail
umask 077

SOURCE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="$HOME/.sciencemate-runtime"
SERVICE="sciencemate-runtime"
mkdir -p "$INSTALL"
fail() { printf 'SCIENCEMATE_RUNTIME_ERROR=%s\n' "$1" >&2; exit 1; }

if [ "$(uname -s)" != Linux ]; then
  fail linux_required
fi
# Kernel-owned locks release after interruption, including a killed SSH session.
exec 9>"$INSTALL/install.lock"
flock -n 9 || fail install_in_progress
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
systemctl --user show-environment >/dev/null 2>&1 || fail user_service_unavailable
# A daemon which dies on SSH logout does not satisfy this installer's contract.
loginctl --no-ask-password enable-linger "$(id -un)" >/dev/null 2>&1 || fail linger_required
[ "$(loginctl show-user "$(id -un)" --property=Linger --value)" = yes ] || fail linger_required

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 120 \
    -LsSf https://astral.sh/uv/install.sh -o "$INSTALL/install-uv.sh"
  sh "$INSTALL/install-uv.sh" >/dev/null
fi
command -v git >/dev/null || fail git_required
VERSION="$(cat "$SOURCE/VERSION")"
BUILD="$(cat "$SOURCE/BUILD_ID")"
[[ "$VERSION" =~ ^[0-9A-Za-z.+_-]+$ ]] && [[ "$BUILD" =~ ^[0-9a-f]{16}$ ]]
RELEASE="$INSTALL/releases/$VERSION-$BUILD"
ENVIRONMENT="$INSTALL/runtime.env"

# Never replace an active release underneath its interpreter. Same-version
# reconnects reuse it; upgrades stage a different version before switching.
if [ ! -f "$RELEASE/.installed" ]; then
  mkdir -p "$RELEASE"
  cp -R "$SOURCE/harness/." "$RELEASE/"
  (cd "$RELEASE/platform/backend"; uv sync --frozen --no-dev --python 3.12 >/dev/null)
  # Harness-owned tools run in the backend interpreter and need the root lock's dependencies.
  (cd "$RELEASE"; uv export --frozen --no-dev --no-emit-project --format requirements-txt \
    -o "$RELEASE/harness-requirements.txt" >/dev/null)
  uv pip install --python "$RELEASE/platform/backend/.venv/bin/python" \
    -r "$RELEASE/harness-requirements.txt" >/dev/null
  touch "$RELEASE/.installed"
fi
PYTHON="$RELEASE/platform/backend/.venv/bin/python"
if [ ! -f "$ENVIRONMENT" ]; then
  "$PYTHON" - "$INSTALL" > "$ENVIRONMENT" <<'PY'
import pathlib, secrets, shlex, socket, sys
root = pathlib.Path(sys.argv[1])
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
print("PLATFORM_PROFILE=personal")
print("PLATFORM_DATA_ROOT=" + shlex.quote(str(root / "data")))
print("HARNESS_FRAMEWORK_HOME=" + shlex.quote(str(root / "data")))
print("DATABASE_URL=" + shlex.quote("sqlite+aiosqlite:///" + str(root / "data/db.sqlite")))
print("HARNESS_BRIDGE_ENABLED=true")
print("REMOTE_RUNTIME_TOKEN=" + secrets.token_urlsafe(48))
print("RUNTIME_PORT=" + str(port))
PY
fi
set -a; . "$ENVIRONMENT"; set +a

# The service resolves current only when it starts. SSH is never its parent.
PREVIOUS="$(readlink "$INSTALL/current" || true)"
rm -f "$INSTALL/current.next"
ln -s "$RELEASE" "$INSTALL/current.next"
mv -Tf "$INSTALL/current.next" "$INSTALL/current"
cat > "$INSTALL/start.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$HOME/.sciencemate-runtime"
set -a; . "$ROOT/runtime.env"; set +a
export HARNESS_ROOT="$ROOT/current"
export PATH="$HOME/.local/bin:$PATH"
cd "$ROOT/current/platform/backend"
exec .venv/bin/python -m app.launcher start --host 127.0.0.1 --port "$RUNTIME_PORT" --no-browser
SH
chmod 700 "$INSTALL/start.sh"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$SERVICE.service" <<UNIT
[Unit]
Description=ScienceMate personal research runtime
After=network-online.target

[Service]
Type=simple
ExecStart="$INSTALL/start.sh"
Restart=on-failure
RestartSec=5
# Backend restarts detach from workers; they must not kill the research cgroup.
KillMode=process
TimeoutStopSec=90
UMask=0077

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable "$SERVICE.service" >/dev/null

if [ "$PREVIOUS" != "$RELEASE" ] || ! systemctl --user is-active --quiet "$SERVICE.service"; then
  systemctl --user restart "$SERVICE.service"
fi

if ! "$PYTHON" - "$RUNTIME_PORT" <<'PY'
import json, os, sys, time, urllib.request
port, token = sys.argv[1], os.environ["REMOTE_RUNTIME_TOKEN"]
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        req = urllib.request.Request("http://127.0.0.1:" + port + "/api/v1/remote-runtime",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.load(response)
        if data.get("protocol") == 1 and data.get("personal"):
            sys.exit(0)
    except (OSError, ValueError):
        pass
    time.sleep(1)
sys.exit(1)
PY
then
  if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$RELEASE" ]; then
    ln -s "$PREVIOUS" "$INSTALL/current.rollback"
    mv -Tf "$INSTALL/current.rollback" "$INSTALL/current"
    systemctl --user restart "$SERVICE.service"
  fi
  echo "The installed runtime did not become ready." >&2
  exit 1
fi

# Private machine-readable result, consumed by the desktop, never shown as a log.
"$PYTHON" - "$RUNTIME_PORT" "$VERSION" <<'PY'
import json, os, sys
print("SCIENCEMATE_RUNTIME=" + json.dumps(dict(protocol=1, port=int(sys.argv[1]),
      token=os.environ["REMOTE_RUNTIME_TOKEN"], version=sys.argv[2], service_manager="systemd")))
PY

#!/usr/bin/env bash
# Run the dedicated offline Docker daemon behind RootlessKit and bubblewrap.
set -euo pipefail

readonly BASE="${HOME}/.cache/hermes-test-runtime"
readonly RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hermes-test-runtime"
readonly TOOLS="${HOME}/.hermes/toolchains"
readonly BWRAP="${TOOLS}/bubblewrap/usr/bin/bwrap"
readonly ETC="${BASE}/tools/etc"
readonly STATE="${BASE}/rootlesskit-state"
SERVICE_NAME="$(basename "$HOME")"
readonly SERVICE_NAME
SERVICE_UID="${HERMES_TEST_SERVICE_UID:-$(id -u)}"
readonly SERVICE_UID
SCRIPT_PATH="$(realpath "$0")"
readonly SCRIPT_PATH
readonly SOCKET="${RUNTIME}/docker.sock"

fail() { printf 'hermes-rootless-test-docker: %s\n' "$*" >&2; exit 1; }

prepare() {
  [[ "$(id -u)" != 0 ]] || fail 'run as the unprivileged service user'
  command -v rootlesskit >/dev/null || fail 'rootlesskit is missing'
  command -v dockerd >/dev/null || fail 'dockerd is missing'
  [[ -x "$BWRAP" ]] || fail "pinned bubblewrap is missing: $BWRAP"
  [[ -r /etc/subuid && -r /etc/subgid ]] || fail 'subuid/subgid files are missing'
  grep -qE "^$(id -un):[0-9]+:[0-9]+$" /etc/subuid || fail 'service user has no subordinate UID range'
  grep -qE "^$(id -un):[0-9]+:[0-9]+$" /etc/subgid || fail 'service user has no subordinate GID range'

  umask 077
  [[ ! -L "${HOME}/.cache" && ! -L "$BASE" ]] || fail 'cache path must not be a symlink'
  install -d -m 0700 "$BASE" "$BASE/data" "$BASE/docker-exec" "$STATE" \
    "$RUNTIME" "$ETC"
  for path in "$BASE" "$BASE/data" "$BASE/docker-exec" "$BASE/tools" "$STATE" "$RUNTIME" "$ETC"; do
    [[ ! -L "$path" && -d "$path" && -O "$path" ]] || fail "invalid runtime path: $path"
    chmod 0700 "$path"
  done

  # The daemon gets a small private /etc tree. It never sees the host's /etc or home.
  chmod -R u+rwX "$ETC"
  rm -rf "${ETC:?}"
  install -d -m 0700 "$ETC/ssl"
  printf 'root:x:0:0:root:/root:/bin/sh\n%s:x:%s:%s:Hermes:/home/%s:/bin/sh\n' \
    "$(id -un)" "$(id -u)" "$(id -g)" "$(id -un)" > "$ETC/passwd"
  printf 'root:x:0:\n%s:x:%s:\n' "$(id -gn)" "$(id -g)" > "$ETC/group"
  printf 'passwd: files\ngroup: files\nhosts: files dns\n' > "$ETC/nsswitch.conf"
  printf '127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n' > "$ETC/hosts"
  printf 'nameserver 127.0.0.1\noptions attempts:1 timeout:1\n' > "$ETC/resolv.conf"
  cp -a /etc/ssl/certs "$ETC/ssl/certs"
  chmod -R a-w "$ETC"
}

run_sandboxed_daemon() {
  export HOME="/home/${SERVICE_NAME}" USER="${SERVICE_NAME}" LOGNAME="${SERVICE_NAME}"
  export PATH=/usr/bin:/bin XDG_RUNTIME_DIR="$RUNTIME"
  export ROOTLESSKIT_STATE_DIR="$STATE"
  export DOCKER_HOST="unix://${SOCKET}"

  exec "$BWRAP" --clearenv --cap-add ALL --unshare-pid --die-with-parent \
    --dir /usr --ro-bind /usr /usr \
    --symlink usr/bin /bin --symlink usr/sbin /sbin \
    --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
    --proc /proc --dev /dev --tmpfs /tmp \
    --tmpfs /etc --ro-bind "$ETC" /etc \
    --dir /sys --ro-bind /sys /sys --tmpfs /sys/kernel/security \
    --tmpfs /run --dir /run/user --dir "/run/user/${SERVICE_UID}" \
    --bind "$RUNTIME" "$RUNTIME" \
    --dir /home --dir "/home/${SERVICE_NAME}" --dir "/home/${SERVICE_NAME}/.cache" \
    --bind "$BASE" "$BASE" \
    --setenv HOME "$HOME" --setenv USER "$USER" --setenv LOGNAME "$LOGNAME" \
    --setenv PATH "$PATH" --setenv XDG_RUNTIME_DIR "$RUNTIME" --setenv TMPDIR /tmp \
    --setenv LANG C.UTF-8 --setenv LC_ALL C.UTF-8 \
    --setenv ROOTLESSKIT_STATE_DIR "$STATE" --setenv DOCKER_HOST "$DOCKER_HOST" \
    --dir /var --symlink ../run /var/run \
    /usr/bin/dockerd --rootless \
    --host="unix://${SOCKET}" \
    --data-root="${BASE}/data" \
    --exec-root="${BASE}/docker-exec" \
    --pidfile=/run/dockerd.pid \
    --storage-driver=vfs --iptables=false --userland-proxy=true
}

run() {
  prepare
  export HERMES_TEST_SERVICE_UID="$SERVICE_UID"
  # RootlessKit owns the user and no-uplink network namespaces. Bubblewrap then
  # narrows the filesystem inside that existing namespace and masks securityfs.
  exec rootlesskit \
    --state-dir="$STATE" --net=none --port-driver=none \
    --copy-up=/etc --copy-up=/run --propagation=rslave \
    "$SCRIPT_PATH" sandbox
}

status() {
  [[ -S "$SOCKET" ]] || fail 'dedicated Docker socket is not available'
  docker --host "unix://${SOCKET}" info --format \
    'Docker={{.ServerVersion}} root={{.DockerRootDir}} security={{json .SecurityOptions}}'
}

wait_ready() {
  for ((attempt=0; attempt<30; attempt++)); do
    if [[ -S "$SOCKET" ]] && docker --host "unix://${SOCKET}" info >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail 'dedicated Docker API did not become ready within 30 seconds'
}

case "${1:-}" in
  prepare) prepare ;;
  run) run ;;
  sandbox) run_sandboxed_daemon ;;
  status) status ;;
  wait-ready) wait_ready ;;
  *) fail 'usage: hermes-rootless-test-docker.sh {prepare|run|status|wait-ready}' ;;
esac

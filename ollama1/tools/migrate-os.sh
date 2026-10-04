#!/bin/bash
# migrate-os.sh: move this server's operating system from one NVMe drive to
# the other, in software (no drive swapping), keeping the old drive untouched
# as a fallback until the new system has booted and been checked (6b362).
#
#   sudo bash migrate-os.sh --from-serial <S> --to-serial <S>             show the plan; changes NOTHING
#   sudo bash migrate-os.sh --from-serial <S> --to-serial <S> --run [--reboot]
#         do it. You type the serial of the drive to be erased first (or give
#         --confirm-serial <S>, which must equal --to-serial: then nothing is
#         asked, no terminal is needed). It then starts itself again inside a
#         detached tmux session ("migrate") and runs every stage by itself;
#         --reboot reboots into the new drive at the end (10 s countdown).
#   sudo bash migrate-os.sh --resume [--reboot]                          carry on after a crash, hang or power cycle
#   sudo bash migrate-os.sh --finish                                     after rebooting into the new drive
#         (--delete-parked, --disable-old-entry, --remove-sudoers: see README)
#   bash migrate-os.sh --status                                          what the run is doing (no sudo needed)
#   --root-size 300G    size of / on the new drive (default 300G)
#   --bwlimit KiB/s     copy speed limit (default 200000; 0 = none)
#
# Installing it for a user who has no root: as the account that has sudo,
#   sudo bash ollama1/tools/migrate-os.sh --install-remote --sudoers-user <admin-user>
# copies it to /usr/local/lib/ollama1-migrate (root-owned, nothing there that a
# user can write) and adds ONE sudoers rule that lets <admin-user> run that
# copy as root without a password, and nothing else. Remove the rule when the
# migration is done:  sudo rm /etc/sudoers.d/90-ollama1-migrate
# As root this script runs only from a place where every folder and file on the
# way to it is owned by root and not writable by group or others.
#
# The serials: lsblk -d -o NAME,SIZE,MODEL,SERIAL  (nvme0 and nvme1 swap places
# between boots; the serial is what identifies a drive). FROM is the drive the
# system runs from now; TO is the drive that is ERASED. The details, the stages
# and the rollback are in ollama1/README.md ("Moving the system to the other
# drive") and in lib/o1migrate.py. Progress: /srv/data/migrate-os.status.
set -euo pipefail
SELF="$(realpath "${BASH_SOURCE[0]}" 2>/dev/null || readlink -f "${BASH_SOURCE[0]}")"
HERE="$(dirname "$SELF")"
INSTALL_DIR=/usr/local/lib/ollama1-migrate
SUDOERS=/etc/sudoers.d/90-ollama1-migrate
die() { echo "STOPPED: $*" >&2; exit 1; }

# path_chain_unsafe PATH: prints why and succeeds if PATH, or any folder above
# it, is not owned by root or is writable by group or others; fails if all is well.
path_chain_unsafe() {
  local c
  c=$(realpath "$1" 2>/dev/null || readlink -f "$1") || { echo "cannot resolve $1"; return 0; }
  while :; do
    if [ -n "$(find "$c" -maxdepth 0 \( ! -user root -o -perm -g+w -o -perm -o+w \) -print 2>/dev/null)" ]; then
      echo "$c is not owned by root, or is writable by someone else"
      return 0
    fi
    [ "$c" = / ] && break
    c=$(dirname "$c")
  done
  return 1
}

PY=""
for d in "$HERE" "$HERE/../lib"; do
  if [ -f "$d/o1migrate.py" ]; then PY="$d/o1migrate.py"; break; fi
done
[ -n "$PY" ] || die "o1migrate.py not found next to this script"

install=0 passthrough=0 user=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --install-remote) install=1 ;;
    -h|--help|--status|--print-sudoers) passthrough=1 ;;
    --sudoers-user) user="${args[$((i + 1))]:-}" ;;
  esac
done

if [ "$passthrough" = 1 ]; then exec python3 -I "$PY" "$@"; fi

install_remote() {
  [ "$(id -u)" -eq 0 ] || die "--install-remote needs sudo (a password, once)."
  [ "$HERE" != "$INSTALL_DIR" ] || die "this is the installed copy already; --install-remote runs from a checkout"
  [ -z "$user" ] || python3 -I "$PY" --print-sudoers --sudoers-user "$user" >/dev/null || die "'$user' is not a user name a sudoers rule can be written for"
  install -d -o root -g root -m 0755 "$INSTALL_DIR"
  install -o root -g root -m 0644 "$PY" "$INSTALL_DIR/o1migrate.py.new"
  mv -f "$INSTALL_DIR/o1migrate.py.new" "$INSTALL_DIR/o1migrate.py"
  install -o root -g root -m 0755 "$SELF" "$INSTALL_DIR/migrate-os.sh.new"
  mv -f "$INSTALL_DIR/migrate-os.sh.new" "$INSTALL_DIR/migrate-os.sh"
  cmp -s "$PY" "$INSTALL_DIR/o1migrate.py" && cmp -s "$SELF" "$INSTALL_DIR/migrate-os.sh" || die "the installed copy differs from the source"
  local x why
  for x in "$INSTALL_DIR" "$INSTALL_DIR/migrate-os.sh" "$INSTALL_DIR/o1migrate.py"; do
    if why=$(path_chain_unsafe "$x"); then die "$why; the installed copy would not be safe to run as root"; fi
  done
  echo "Installed in $INSTALL_DIR (root:root; script 0755, library 0644)."
  if [ -n "$user" ]; then
    local tmp
    tmp=$(mktemp -d)
    python3 -I "$INSTALL_DIR/o1migrate.py" --print-sudoers --sudoers-user "$user" >"$tmp/90-ollama1-migrate"
    if ! visudo -cf "$tmp/90-ollama1-migrate" >/dev/null; then rm -f "$tmp/90-ollama1-migrate"; rmdir "$tmp"; die "visudo rejected the sudoers file; nothing was installed"; fi
    install -o root -g root -m 0440 "$tmp/90-ollama1-migrate" "$SUDOERS"
    rm -f "$tmp/90-ollama1-migrate"; rmdir "$tmp"
    if ! visudo -c >/dev/null; then rm -f "$SUDOERS"; die "visudo rejected the sudoers set; the new rule was removed"; fi
    echo "Sudoers rule installed ($SUDOERS): $user may run  sudo $INSTALL_DIR/migrate-os.sh <arguments>  as root, without a password, and nothing else."
    echo "REMOVE IT when the migration is done:  sudo rm $SUDOERS"
  else
    echo "No sudo rule was added. To let one user run it without a password, run this again with --sudoers-user <name>."
  fi
}

if [ "$install" = 1 ]; then install_remote; exit 0; fi

# Running as root: only from a root-owned place, and only its own library.
if [ "$(id -u)" -eq 0 ] || [ -n "${O1M_FORCE_PATH_CHECK:-}" ]; then
  for x in "$SELF" "$PY"; do
    if why=$(path_chain_unsafe "$x"); then
      die "refusing to run as root from here: $why. Install a root-owned copy:  sudo bash <checkout>/ollama1/tools/migrate-os.sh --install-remote  and run that"
    fi
  done
fi
[ "$(id -u)" -eq 0 ] || die "Run it with sudo."
command -v python3 >/dev/null || die "needs python3"
exec env O1_MIGRATE_SCRIPT="$SELF" python3 -I -u "$PY" "$@"

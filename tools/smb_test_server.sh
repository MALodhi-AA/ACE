#!/usr/bin/env bash
# Start a local Samba server that mimics the Synology Drive NAS (AA-RS) for
# the SMB integration tests (tests/test_nas_smb.py). Linux only, run as root:
#
#   sudo bash tools/smb_test_server.sh
#   ACE_TEST_SMB=127.0.0.1:4445 ACE_TEST_SMB_PASSWORD=AcePass123 pytest
#
# Shares: "Food Box" (ace read-only), "Adore Beauty" + "Azhar Backup" (ace no
# access), "ACE" (ace read/write, inbox/ + reports/). SMB3 minimum, like DSM.
set -euo pipefail
ROOT=${ACE_TEST_SMB_ROOT:-/srv/smbtest}
PORT=${ACE_TEST_SMB_PORT:-4445}
HERE="$(cd "$(dirname "$0")/.." && pwd)"

# stop any previous test server, including Samba's on-demand RPC helpers
pkill -x smbd 2>/dev/null || true
for p in $(pgrep -f '^/usr/libexec/samba/' || true); do kill "$p" 2>/dev/null || true; done
sleep 1
rm -rf /run/samba/ncalrpc "$ROOT"
mkdir -p "$ROOT"/{foodbox,adore,backup,ace/inbox,ace/reports,private,lock,state,cache,log} /run/samba
id ace   >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin ace
id other >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin other

mkdir -p "$ROOT/foodbox/2026/09 Sep"
cp "$HERE/templates/Sample_Restaurant_Sep2026_TB.xlsx" "$ROOT/foodbox/2026/09 Sep/Food Box TB Sep 2026.xlsx"
echo "secret" > "$ROOT/adore/secret.txt"
echo "backup" > "$ROOT/backup/b.txt"
chmod 755 "$ROOT"
chmod -R a+rX "$ROOT/foodbox" "$ROOT/adore" "$ROOT/backup"
chmod -R 777 "$ROOT/ace"

cat > "$ROOT/smb.conf" <<CONF
[global]
  workgroup = WORKGROUP
  server role = standalone server
  security = user
  map to guest = never
  smb ports = $PORT
  disable netbios = yes
  server min protocol = SMB3
  private dir = $ROOT/private
  lock directory = $ROOT/lock
  state directory = $ROOT/state
  cache directory = $ROOT/cache
  pid directory = $ROOT/lock
  log file = $ROOT/log/%m.log
  passdb backend = tdbsam:$ROOT/private/passdb.tdb
[Food Box]
  path = $ROOT/foodbox
  read only = yes
  valid users = ace other
[Adore Beauty]
  path = $ROOT/adore
  read only = yes
  valid users = other
[Azhar Backup]
  path = $ROOT/backup
  valid users = other
[ACE]
  path = $ROOT/ace
  read only = no
  valid users = ace
  force user = ace
  create mask = 0777
  directory mask = 0777
CONF

printf 'AcePass123\nAcePass123\n' | smbpasswd -c "$ROOT/smb.conf" -s -a ace   >/dev/null
printf 'Other123\nOther123\n'     | smbpasswd -c "$ROOT/smb.conf" -s -a other >/dev/null
smbd -s "$ROOT/smb.conf" -D </dev/null >/dev/null 2>&1
sleep 2
echo "Samba test server running on 127.0.0.1:$PORT (root $ROOT)"

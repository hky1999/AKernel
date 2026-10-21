#!/usr/bin/env bash
set -euo pipefail
umask 077
D=/a0/ceph
S=/a0/secrets
mkdir -p "$D"/{mon,osd,mgr,run,log}
CONF="$D/ceph.conf"
MON_PORT="${A0_MON_PORT:?}"
RGW_PORT="${A0_RGW_PORT:?}"
FSID=$(cat "$D/fsid")
cat > "$CONF" <<EOF
[global]
fsid = $FSID
mon initial members = a0
mon host = 127.0.0.1:$MON_PORT
public addr = 127.0.0.1
cluster addr = 127.0.0.1
auth cluster required = cephx
auth service required = cephx
auth client required = cephx
keyring = $D/admin.keyring
run dir = $D/run
log to file = false
log to stderr = true
admin socket = $D/run/\$name.asok
ms bind port min = 19800
ms bind port max = 19840
osd pool default size = 1
osd pool default min size = 1
osd pool default pg num = 1
osd pool default pgp num = 1
osd pool default pg autoscale mode = off
debug rocksdb = 0
mon allow pool size one = true
mon warn on pool no redundancy = false
[mon.a0]
mon data = $D/mon
[mgr.a0]
mgr data = $D/mgr
[osd.0]
osd data = $D/osd
osd objectstore = bluestore
bluestore block size = 34359738368
bluestore block create = true
bluestore cache autotune = false
bluestore cache size = 268435456
osd memory target = 1073741824
[client.admin]
keyring = $D/admin.keyring
EOF
if [ ! -f "$D/admin.keyring" ]; then
  ceph-authtool --create-keyring "$D/admin.keyring" --gen-key -n client.admin --cap mon 'allow *' --cap osd 'allow *' --cap mgr 'allow *' > "$D/log/auth-init.log" 2>&1
fi
if [ ! -d "$D/mon/store.db" ]; then
  ceph-authtool --create-keyring "$D/mon.keyring" --gen-key -n mon. --cap mon 'allow *' >> "$D/log/auth-init.log" 2>&1
  ceph-authtool "$D/mon.keyring" --import-keyring "$D/admin.keyring" >> "$D/log/auth-init.log" 2>&1
  monmaptool --create --add a0 "127.0.0.1:$MON_PORT" --fsid "$FSID" "$D/monmap"
  ceph-mon --mkfs -i a0 -c "$CONF" --monmap "$D/monmap" --keyring "$D/mon.keyring" --setuser root --setgroup root > "$D/log/mon-mkfs.log" 2>&1
fi
CHILDREN=()
cleanup() {
  trap - TERM INT EXIT
  for ((i=${#CHILDREN[@]}-1;i>=0;i--)); do kill -TERM "${CHILDREN[i]}" 2>/dev/null || true; done
  for p in "${CHILDREN[@]}"; do wait "$p" 2>/dev/null || true; done
}
trap cleanup TERM INT EXIT
ceph-mon -f -i a0 -c "$CONF" --setuser root --setgroup root > "$D/log/mon.log" 2>&1 &
CHILDREN+=("$!")
ceph_cmd() { timeout 10 ceph -c "$CONF" "$@"; }
for n in $(seq 1 60); do ceph_cmd status > "$D/log/status.json" 2>/dev/null && break; sleep 1; done
ceph_cmd status > /dev/null
if [ ! -f "$D/osd/fsid" ]; then
  OSD_UUID=$(cat /proc/sys/kernel/random/uuid)
  OSD_ID=$(ceph_cmd osd new "$OSD_UUID")
  [ "$OSD_ID" = 0 ]
  ceph-authtool --create-keyring "$D/osd/keyring" --gen-key -n osd.0 > "$D/log/osd-auth.log" 2>&1
  ceph_cmd auth add osd.0 mon 'allow profile osd' osd 'allow *' -i "$D/osd/keyring" >> "$D/log/osd-auth.log" 2>&1
  ceph-osd --mkfs -i 0 -c "$CONF" --osd-uuid "$OSD_UUID" --keyring "$D/osd/keyring" --setuser root --setgroup root > "$D/log/osd-mkfs.log" 2>&1
fi
ceph-osd -f -i 0 -c "$CONF" --keyring "$D/osd/keyring" --setuser root --setgroup root > "$D/log/osd.log" 2>&1 &
CHILDREN+=("$!")
if [ ! -f "$D/mgr/keyring" ]; then
  ceph-authtool --create-keyring "$D/mgr/keyring" --gen-key -n mgr.a0 > "$D/log/mgr-auth.log" 2>&1
  ceph_cmd auth add mgr.a0 mon 'allow profile mgr' osd 'allow *' mds 'allow *' -i "$D/mgr/keyring" >> "$D/log/mgr-auth.log" 2>&1
fi
ceph-mgr -f -i a0 -c "$CONF" --keyring "$D/mgr/keyring" --setuser root --setgroup root > "$D/log/mgr.log" 2>&1 &
CHILDREN+=("$!")
for n in $(seq 1 90); do
  if ceph_cmd osd stat 2>/dev/null | grep -q '1 up'; then break; fi
  sleep 1
done
ceph_cmd osd stat | grep -q '1 up'
ceph_cmd osd crush reweight osd.0 1
if [ ! -s "$S/s3-user.json" ]; then
  timeout 60 radosgw-admin -c "$CONF" user create --uid a0 --display-name 'A0 isolated lab' > "$S/s3-user.json"
fi
radosgw -f -n client.admin -c "$CONF" --rgw-frontends "beast endpoint=127.0.0.1:$RGW_PORT" --setuser root --setgroup root > "$D/log/rgw.log" 2>&1 &
CHILDREN+=("$!")
touch "$D/started"
wait -n "${CHILDREN[@]}"
exit 1


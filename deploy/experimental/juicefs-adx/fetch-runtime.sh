#!/usr/bin/env bash
set -euo pipefail
umask 077
D="${1:?task directory}"
[ -f "$D/lab.json" ]
[ -d "$D/profile" ]
mkdir -p "$D/artifacts" "$D/runtime/gvisor" "$D/runtime/fc" "$D/runtime/adx"
source "$D/profile/runtime-versions.env"
fetch() {
  local url="$1" hash="$2" file="$3" kind="$4"
  if [ ! -f "$file" ]; then
    curl -fL --retry 2 --connect-timeout 15 --max-time 300 "$url" -o "$file.partial"
    mv "$file.partial" "$file"
  fi
  if [ "$kind" = 512 ]; then
    printf '%s  %s\n' "$hash" "$file" | sha512sum -c -
  else
    printf '%s  %s\n' "$hash" "$file" | sha256sum -c -
  fi
}
fetch "$GVISOR_AMD64_URL" "$GVISOR_AMD64_SHA512" "$D/artifacts/gvisor.tar.bz2" 512
bash "$D/profile/install-gvisor.sh" "$D/artifacts/gvisor.tar.bz2" "$GVISOR_AMD64_SHA512" "$D/runtime/gvisor"
fetch "$FIRECRACKER_AMD64_URL" "$FIRECRACKER_AMD64_SHA256" "$D/artifacts/firecracker.tgz" 256
tar -xzf "$D/artifacts/firecracker.tgz" -C "$D/runtime/fc"
(cd "$D/runtime/fc/release-$FIRECRACKER_RELEASE-x86_64"; sha256sum -c SHA256SUMS)
fetch "https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/adx/daily/20261014092247-ce80a189f352/linux/amd64/adx-release.tar.gz" "2639ff62149ce915a173d5b205309d5bcfa8e85c49495bb453be024e791a093d" "$D/artifacts/adx-release.tar.gz" 256
tar -xzf "$D/artifacts/adx-release.tar.gz" -C "$D/runtime/adx"
fetch "https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/adx/daily/20261014092247-ce80a189f352/linux/amd64/adx-execd.tar.gz" "c169abc604e19f69ffc9d4e9714ed4e4609d0510a92a533ebd0a1263c1af882c" "$D/artifacts/adx-execd.tar.gz" 256
mkdir -p "$D/runtime/execd"
tar -xzf "$D/artifacts/adx-execd.tar.gz" -C "$D/runtime/execd"
"$D/runtime/gvisor/runsc" --version


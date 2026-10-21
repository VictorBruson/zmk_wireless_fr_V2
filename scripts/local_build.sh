#!/usr/bin/env bash
# Runs inside the zmkfirmware/zmk-build-arm container (started by build-local.ps1).
# Builds every target listed under `include:` in build.yaml, like the GitHub workflow.
#   /src        this repo (read-only)
#   /workspace  west workspace (zmk, zephyr, modules), kept in a Docker volume
#   /out        where the .uf2 files are written
# Optional argument: only build targets whose name contains this text (e.g. "left").
set -euo pipefail

filter="${1:-}"
ws=/workspace
cd "$ws"

# Fresh copy of the config so deleted files don't linger.
rm -rf "$ws/config"
cp -R /src/config "$ws/config"

if [ ! -d .west ]; then
  west init -l config
fi

# Only re-download ZMK/Zephyr when west.yml changes.
hash=$(sha256sum config/west.yml | cut -d' ' -f1)
if [ "$(cat .west_hash 2>/dev/null || true)" != "$hash" ]; then
  west update --fetch-opt=--filter=tree:0
  echo "$hash" > .west_hash
fi
west zephyr-export > /dev/null

python3 - > /tmp/targets <<'EOF'
import yaml
data = yaml.safe_load(open('/src/build.yaml')) or {}
for t in data.get('include', []):
    print('|'.join(str(t.get(k, '') or '') for k in
                   ('board', 'shield', 'snippet', 'cmake-args', 'artifact-name')))
EOF

built=0
while IFS='|' read -r -u 3 board shield snippet cmake_args artifact; do
  name="${artifact:-${shield:+$shield-}$board-zmk}"
  if [ -n "$filter" ] && [[ "$name" != *"$filter"* ]]; then
    continue
  fi
  echo "=== Building $name"
  west_args=()
  [ -n "$snippet" ] && west_args+=(-S "$snippet")
  # shellcheck disable=SC2086  # cmake_args is intentionally word-split
  west build -p auto -s zmk/app -d "build/$name" -b "$board" ${west_args[@]+"${west_args[@]}"} -- \
    -DZMK_CONFIG="$ws/config" ${shield:+-DSHIELD="$shield"} $cmake_args
  cp "build/$name/zephyr/zmk.uf2" "/out/$name.uf2"
  built=$((built + 1))
done 3< /tmp/targets

echo "=== Done: $built firmware file(s) in the firmware folder"

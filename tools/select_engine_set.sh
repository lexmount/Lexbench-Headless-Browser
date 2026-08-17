#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
artifacts_root="$repo_root/build_artifacts"
set_name="${1:-}"

if [[ -z "$set_name" || ! "$set_name" =~ ^[a-z0-9_-]+$ ]]; then
  echo "usage: tools/select_engine_set.sh <baseline|latest|...>" >&2
  exit 2
fi

set_root="$artifacts_root/sets/$set_name"
manifest="$set_root/set.json"
if [[ ! -f "$manifest" ]]; then
  echo "engine set manifest not found: $manifest" >&2
  exit 1
fi

# Conventional path -> path inside the set. Every engine the manifest can
# declare needs an entry here; a missing one silently leaves the old binary in
# place while active-set.json claims the new one.
declare -A links=(
  ["chrome-for-testing/bin"]="sets/$set_name/chrome-for-testing/bin"
  ["chromedriver/bin"]="sets/$set_name/chromedriver/bin"
  ["moli/bin"]="sets/$set_name/moli/bin"
  ["lightpanda/zig-out/bin"]="sets/$set_name/lightpanda/zig-out/bin"
  ["obscura/bin"]="sets/$set_name/obscura/bin"
)

# Validate everything before mutating anything, so a partial set cannot leave
# half the conventional paths pointing at the new set and half at the old one.
errors=0
for link_path in "${!links[@]}"; do
  source_path="$artifacts_root/${links[$link_path]}"
  dest_path="$artifacts_root/$link_path"

  if [[ ! -d "$source_path" ]]; then
    echo "error: set is missing $source_path" >&2
    errors=1
  fi

  # ln -sfn only replaces a symlink. Against a real directory it creates a
  # nested link inside it and leaves the conventional executable untouched,
  # which then mismatches the active-set.json copied below.
  if [[ -e "$dest_path" && ! -L "$dest_path" ]]; then
    echo "error: $dest_path is a real directory, not a symlink managed by this script." >&2
    echo "       move it aside first:  mv $dest_path ${dest_path}.manual" >&2
    errors=1
  fi
done

# Check the executables inside the prospective set, not through the conventional
# paths — those still point at the current set until the links below are moved.
# A set whose directories exist but whose binaries are missing must fail here,
# while the previous selection is still intact.
while IFS= read -r binary; do
  prospective="$artifacts_root/sets/$set_name/${binary#build_artifacts/}"
  if [[ ! -x "$prospective" ]]; then
    echo "error: set declares $binary but $prospective is missing or not executable" >&2
    errors=1
  fi
done < <(jq -r '.engines[].binary' "$manifest")

[[ "$errors" -eq 0 ]] || exit 1

for link_path in "${!links[@]}"; do
  mkdir -p "$(dirname "$artifacts_root/$link_path")"
  ln -sfn "$artifacts_root/${links[$link_path]}" "$artifacts_root/$link_path"
done

cp "$manifest" "$artifacts_root/active-set.json"

# Invariant check: pre-validation above already proved each executable exists in
# the set, so this only catches a link that failed to take.
missing=0
while IFS= read -r binary; do
  if [[ ! -x "$repo_root/$binary" ]]; then
    echo "error: active set declares $binary but it does not resolve" >&2
    missing=1
  fi
done < <(jq -r '.engines[].binary' "$manifest")
[[ "$missing" -eq 0 ]] || exit 1

echo "active engine set: $set_name"
jq -r '.engines | to_entries[] | "\(.key): \(.value.version) sha12=\(.value.sha256_12)"' "$manifest"
echo "run 'python3 -m runner.run doctor' to verify the binaries against these pins"

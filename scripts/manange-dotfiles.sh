#!/usr/bin/env bash
set -euo pipefail

BEGIN_MARKER="# >>> dotfiles bashrc >>>"
END_MARKER="# <<< dotfiles bashrc <<<"

die() {
  echo "Error: $1" >&2
  exit 1
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
source_file="$repo_root/.local/dotfiles/.bashrc"
target_file="${HOME:?HOME is not set}/.bashrc"

if [ ! -f "$source_file" ]; then
  die "dotfiles bashrc not found: $source_file"
fi

if [ -e "$target_file" ] && [ ! -f "$target_file" ]; then
  die "target is not a regular file: $target_file"
fi

home_prefix="${HOME%/}/"
home_relative_path() {
  local path="$1"
  if [[ "$path" == "$HOME" ]]; then
    printf '~\n'
  elif [[ "$path" == "$home_prefix"* ]]; then
    printf '~/%s\n' "${path#"$home_prefix"}"
  else
    printf '%s\n' "$path"
  fi
}

if [[ "$source_file" == "$home_prefix"* ]]; then
  printf -v source_expr '%q' "${source_file#"$home_prefix"}"
  source_expr="~/$source_expr"
else
  printf -v source_expr '%q' "$source_file"
fi
source_display="$(home_relative_path "$source_file")"
target_display="$(home_relative_path "$target_file")"

cleaned_file="$(mktemp)"
desired_file="$(mktemp)"
trap 'rm -f "$cleaned_file" "$desired_file"' EXIT

if [ -f "$target_file" ]; then
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
    !skip { lines[++count] = $0 }
    END {
      while (count > 0 && lines[count] == "") {
        count--
      }
      for (i = 1; i <= count; i++) {
        print lines[i]
      }
    }
  ' "$target_file" >"$cleaned_file"
else
  : >"$cleaned_file"
fi

{
  cat "$cleaned_file"
  if [ -s "$cleaned_file" ]; then
    printf '\n'
  fi
  printf '%s\n' "$BEGIN_MARKER"
  printf 'managed_dotfiles_bashrc=%s\n' "$source_expr"
  printf 'if [ -f "$managed_dotfiles_bashrc" ]; then\n'
  printf '  . "$managed_dotfiles_bashrc"\n'
  printf 'fi\n'
  printf 'unset managed_dotfiles_bashrc\n'
  printf '%s\n' "$END_MARKER"
} >"$desired_file"

if [ -f "$target_file" ] && cmp -s "$target_file" "$desired_file"; then
  echo "dotfiles bashrc already up to date"
  echo "source: $source_display"
  echo "target: $target_display"
  exit 0
fi

timestamp="$(date +%Y%m%d%H%M%S)"
backup_file="$target_file.dotfiles.bak.$timestamp"

if [ -f "$target_file" ]; then
  cp -p "$target_file" "$backup_file"
else
  : >"$backup_file"
  : >"$target_file"
fi

cp "$desired_file" "$target_file"

echo "updated dotfiles bashrc"
echo "source: $source_display"
echo "target: $target_display"
echo "backup: $(home_relative_path "$backup_file")"

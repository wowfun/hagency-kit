#!/usr/bin/env bash
set -euo pipefail

# --- Defaults ---
INPUT_FMT="mov"
OUTPUT_FMT="mp4"
QUALITY="medium"
JOBS=""
OUTPUT_DIR=""
SOURCE_DIR=""
FORCE=false

# --- CRF presets ---
get_crf() {
  case "$1" in
    low)    echo 28 ;;
    medium) echo 23 ;;
    high)   echo 18 ;;
    *)      echo "" ;;
  esac
}

# --- Supported formats ---
VALID_INPUT="mov avi mkv webm flv wmv"
VALID_OUTPUT="mp4 mkv webm gif"

usage() {
  cat <<'EOF'
Usage: video-convert.sh [OPTIONS]

Batch convert video files using ffmpeg.

Options:
  -i FORMAT    Input format (default: mov)
               Supported: mov, avi, mkv, webm, flv, wmv
  -o FORMAT    Output format (default: mp4)
               Supported: mp4, mkv, webm, gif
  -q PRESET    Quality preset: low, medium, high (default: medium)
               low    → crf 28 (small file, fast)
               medium → crf 23 (balanced)
               high   → crf 18 (high quality, large file)
  -j N         Number of parallel jobs (default: CPU core count)
  -d DIR       Output directory (default: same as source)
  -s DIR       Source directory (default: current directory)
  -f           Force overwrite existing files
  -h           Show this help message

Examples:
  video-convert.sh                        # .mov → .mp4 in current dir
  video-convert.sh -i avi -o mp4          # .avi → .mp4
  video-convert.sh -q high -j 4 -d out/   # high quality, 4 jobs, output to out/
  video-convert.sh -s ~/Videos -o mkv     # convert all .mov in ~/Videos to .mkv
  video-convert.sh -f                     # force overwrite
EOF
  exit 0
}

die() {
  echo "Error: $1" >&2
  exit 1
}

# --- Parse args ---
while getopts "i:o:q:j:d:s:fh" opt; do
  case $opt in
    i) INPUT_FMT="$OPTARG" ;;
    o) OUTPUT_FMT="$OPTARG" ;;
    q) QUALITY="$OPTARG" ;;
    j) JOBS="$OPTARG" ;;
    d) OUTPUT_DIR="$OPTARG" ;;
    s) SOURCE_DIR="$OPTARG" ;;
    f) FORCE=true ;;
    h) usage ;;
    *) die "Unknown option. Use -h for help." ;;
  esac
done

# --- Validate ---
command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg is not installed."

INPUT_FMT="${INPUT_FMT#.}"
OUTPUT_FMT="${OUTPUT_FMT#.}"

if [[ ! " $VALID_INPUT " =~ " $INPUT_FMT " ]]; then
  die "Unsupported input format: $INPUT_FMT (valid: $VALID_INPUT)"
fi

if [[ ! " $VALID_OUTPUT " =~ " $OUTPUT_FMT " ]]; then
  die "Unsupported output format: $OUTPUT_FMT (valid: $VALID_OUTPUT)"
fi

CRF=$(get_crf "$QUALITY")
if [[ -z "$CRF" ]]; then
  die "Invalid quality preset: $QUALITY (valid: low, medium, high)"
fi

[[ -z "$SOURCE_DIR" ]] && SOURCE_DIR="."
[[ -z "$OUTPUT_DIR" ]] && OUTPUT_DIR="$SOURCE_DIR"

[[ -d "$SOURCE_DIR" ]] || die "Source directory not found: $SOURCE_DIR"
mkdir -p "$OUTPUT_DIR"

[[ -z "$JOBS" ]] && JOBS=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)

# --- Collect files ---
shopt -s nullglob nocaseglob
files=("$SOURCE_DIR"/*."$INPUT_FMT")
shopt -u nullglob nocaseglob

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No .$INPUT_FMT files found in $SOURCE_DIR"
  exit 0
fi

# --- Stream-copy compatible container pairs ---
can_stream_copy() {
  local a="$1" b="$2"
  [[ "$a" == "$b" ]] && return 0
  # mov ↔ mp4 share the same codec support (h264/h265 + aac)
  { [[ "$a" == "mov" && "$b" == "mp4" ]] || [[ "$a" == "mp4" && "$b" == "mov" ]]; } && return 0
  return 1
}

# --- Conversion function ---
convert_one() {
  local idx="$1"
  local src="$2"
  local base
  base="$(basename "${src%.$INPUT_FMT}")"
  local dst="$OUTPUT_DIR/${base}.$OUTPUT_FMT"

  if [[ -f "$dst" && "$FORCE" != true ]]; then
    echo "[$idx/$total] SKIP: $base.$OUTPUT_FMT already exists"
    return 0
  fi

  local rc=0
  if [[ "$OUTPUT_FMT" == "gif" ]]; then
    # Two-pass gif generation for better quality
    local palette="/tmp/vc_palette_${idx}_${BASHPID}.png"
    ffmpeg -y -i "$src" -vf "fps=15,palettegen" "$palette" 2>/dev/null
    ffmpeg -y -i "$src" -i "$palette" -lavfi "fps=15 [x]; [x][1:v] paletteuse" "$dst" 2>/dev/null || rc=$?
    rm -f "$palette"
  elif [[ "$QUALITY" == "high" ]] && can_stream_copy "$INPUT_FMT" "$OUTPUT_FMT"; then
    # Compatible containers + high quality → stream copy (lossless)
    ffmpeg -y -i "$src" -c copy "$dst" 2>/dev/null || rc=$?
  else
    ffmpeg -y -i "$src" -c:v libx264 -crf "$CRF" -c:a aac -b:a 128k "$dst" 2>/dev/null || rc=$?
  fi

  if [[ $rc -eq 0 ]]; then
    echo "[$idx/$total] DONE: $base.$OUTPUT_FMT"
  else
    echo "[$idx/$total] FAIL: $base.$OUTPUT_FMT" >&2
  fi
  return $rc
}
export -f convert_one
export INPUT_FMT OUTPUT_FMT OUTPUT_DIR FORCE QUALITY CRF

# --- Run with progress ---
total=${#files[@]}
export total

# Write indexed file list to temp file (handles spaces in filenames)
tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT
for i in "${!files[@]}"; do
  printf '%d\t%s\n' "$((i + 1))" "${files[$i]}" >> "$tmpfile"
done

# Run conversions with parallel jobs
active=0
pids=()
while IFS=$'\t' read -r idx src; do
  convert_one "$idx" "$src" &
  pids+=($!)
  active=$((active + 1))
  if [[ $active -ge $JOBS ]]; then
    wait "${pids[0]}" || true
    pids=("${pids[@]:1}")
    active=$((active - 1))
  fi
done < "$tmpfile"

# Wait for remaining jobs
for pid in "${pids[@]}"; do
  wait "$pid" || true
done

echo ""
echo "Done. Processed $total file(s)."

#!/usr/bin/env bash
# Prepare evidence assets for yyl-benchmark-breakdown.
# Output: metadata.json, video/audio/transcript, contact sheets, and manifest.txt.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/prepare-assets.sh "<url>" [output_dir]

Environment:
  DOUYIN_API_BASE       Default: http://localhost:80
  XHS_API_BASE          Default: http://127.0.0.1:5556
  XHS_CONTAINER_NAME    Default: xhs-downloader-api
  XHS_DOWNLOAD_DIR      Default: inferred from Docker mount, then $HOME/.xhs-downloader/Volume/Download
  BENCHMARK_WORKDIR     Default: /tmp/yyl-benchmark
  WHISPER_MODEL         Default: medium
  WHISPER_LANGUAGE      Default: Chinese
  ASR_API_BASE          Optional OpenAI-compatible API base, e.g. https://host/v1
  ASR_API_KEY           Bearer key for ASR_API_BASE
  ASR_MODEL             Default: Systran/faster-whisper-medium
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 0
fi

URL="$1"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
if [[ -n "${BENCHMARK_WORKDIR:-}" ]]; then
  WORK_ROOT="$BENCHMARK_WORKDIR"
elif [[ -d /data && -w /data ]]; then
  WORK_ROOT="/data/benchmarks/_work"
else
  WORK_ROOT="/tmp/yyl-benchmark"
fi
RUN_DIR="${2:-$WORK_ROOT/$RUN_ID}"
DOUYIN_API_BASE="${DOUYIN_API_BASE:-http://localhost:80}"
XHS_API_BASE="${XHS_API_BASE:-http://127.0.0.1:5556}"
XHS_CONTAINER_NAME="${XHS_CONTAINER_NAME:-xhs-downloader-api}"
WHISPER_MODEL="${WHISPER_MODEL:-medium}"
WHISPER_LANGUAGE="${WHISPER_LANGUAGE:-Chinese}"
ASR_MODEL="${ASR_MODEL:-Systran/faster-whisper-medium}"
ASR_API_KEY_EFFECTIVE="${ASR_API_KEY:-${API_KEY:-}}"

infer_xhs_download_dir() {
  if [[ -n "${XHS_DOWNLOAD_DIR:-}" ]]; then
    printf '%s\n' "$XHS_DOWNLOAD_DIR"
    return 0
  fi

  if command -v docker >/dev/null 2>&1; then
    local volume_dir
    volume_dir="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/Volume"}}{{.Source}}{{end}}{{end}}' "$XHS_CONTAINER_NAME" 2>/dev/null || true)"
    if [[ -n "$volume_dir" ]]; then
      printf '%s\n' "$volume_dir/Download"
      return 0
    fi
  fi

  printf '%s\n' "$HOME/.xhs-downloader/Volume/Download"
}

XHS_DOWNLOAD_DIR="$(infer_xhs_download_dir)"

mkdir -p "$RUN_DIR/frames" "$RUN_DIR/xhs_images"
MANIFEST="$RUN_DIR/manifest.txt"
METADATA="$RUN_DIR/metadata.json"

log() { printf '%s\n' "$*" | tee -a "$MANIFEST" >&2; }
need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "missing dependency: $1"
    exit 1
  fi
}

need curl
need ffmpeg
need ffprobe
need python3

DOUYIN_USER_AGENT="${DOUYIN_USER_AGENT:-Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36}"

extract_douyin_aweme_id() {
  python3 - "$1" <<'PY'
import re, sys
from urllib.parse import unquote

url = unquote(sys.argv[1])
for pattern in (
    r"/(?:share/)?video/(\d+)",
    r"[?&](?:modal_id|vid)=(\d+)",
):
    match = re.search(pattern, url)
    if match:
        print(match.group(1))
        raise SystemExit(0)
raise SystemExit(1)
PY
}

resolve_douyin_url() {
  local raw_url="$1"
  local aweme_id effective_url

  # Full iesdouyin/share/video and douyin/video links already contain the ID.
  # Do not probe them at all: a HEAD request can follow the redirect to a valid
  # Douyin page that deliberately answers HEAD with 404 while GET answers 200.
  aweme_id="$(extract_douyin_aweme_id "$raw_url" 2>/dev/null || true)"
  if [[ -n "$aweme_id" ]]; then
    printf 'https://www.douyin.com/video/%s\n' "$aweme_id"
    return 0
  fi

  # Short links need redirect resolution. Use a real GET and judge validity by
  # whether the effective URL contains a video ID, not by the final status of a
  # HEAD request. Do not use --fail: some redirect targets return an unusual
  # status for probes even though the Location/effective URL is usable.
  effective_url="$(curl -sS -L --request GET --compressed \
    --max-redirs 10 --connect-timeout 10 --max-time 30 \
    -A "$DOUYIN_USER_AGENT" -o /dev/null -w '%{url_effective}' \
    "$raw_url" 2>>"$MANIFEST" || true)"
  aweme_id="$(extract_douyin_aweme_id "$effective_url" 2>/dev/null || true)"
  if [[ -n "$aweme_id" ]]; then
    printf 'https://www.douyin.com/video/%s\n' "$aweme_id"
    return 0
  fi

  printf '%s\n' "$raw_url"
}

metadata_api_error_summary() {
  python3 - "$1" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
detail = payload.get("detail") if isinstance(payload, dict) else None
if not isinstance(detail, dict):
    raise SystemExit(1)
code = detail.get("code")
message = detail.get("message") or "metadata API error"
if isinstance(code, int) and code >= 400:
    print(f"code={code} message={message}")
    raise SystemExit(0)
raise SystemExit(1)
PY
}

detect_platform() {
  case "$URL" in
    *xiaohongshu.com*|*xhslink.com*) echo "xhs" ;;
    *douyin.com*|*v.douyin.com*) echo "douyin" ;;
    *tiktok.com*) echo "tiktok" ;;
    *bilibili.com*|*b23.tv*) echo "bilibili" ;;
    *youtube.com*|*youtu.be*) echo "youtube" ;;
    *mp.weixin.qq.com*) echo "weixin" ;;
    *) echo "unknown" ;;
  esac
}

json_payload() {
  local download="${1:-true}"
  python3 - "$URL" "$download" <<'PY'
import json, sys
download = sys.argv[2].lower() == "true"
print(json.dumps({"url": sys.argv[1], "download": download, "skip": False}, ensure_ascii=False))
PY
}

xhs_settings_file() {
  local volume_dir
  volume_dir="$(dirname "$XHS_DOWNLOAD_DIR" 2>/dev/null || true)"
  if [[ -n "$volume_dir" && -f "$volume_dir/settings.json" ]]; then
    printf '%s\n' "$volume_dir/settings.json"
    return 0
  fi

  if command -v docker >/dev/null 2>&1; then
    local mounted_volume
    mounted_volume="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/Volume"}}{{.Source}}{{end}}{{end}}' "$XHS_CONTAINER_NAME" 2>/dev/null || true)"
    if [[ -n "$mounted_volume" && -f "$mounted_volume/settings.json" ]]; then
      printf '%s\n' "$mounted_volume/settings.json"
      return 0
    fi
  fi

  return 1
}

xhs_cookie_configured() {
  local settings
  settings="$(xhs_settings_file || true)"
  [[ -n "$settings" ]] || return 1
  python3 - "$settings" <<'PY'
import json, sys
try:
    cookie = json.load(open(sys.argv[1], encoding="utf-8")).get("cookie") or ""
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if cookie.strip() else 1)
PY
}

warn_xhs_cookie_if_needed() {
  if ! xhs_cookie_configured; then
    log "warning: XHS API returned no data and XHS-Downloader cookie is empty"
    log "warning: official docs say cookie is optional, but configure/update Volume/settings.json cookie when XHS functions are abnormal"
  fi
}

extract_video_url() {
  python3 - "$METADATA" <<'PY'
import json, sys
from collections.abc import Mapping, Sequence

with open(sys.argv[1], "r", encoding="utf-8") as f:
    root = json.load(f)

def direct_url(node):
    if isinstance(node, str) and node.startswith(("http://", "https://")):
        return node
    if isinstance(node, Mapping):
        for key in ("url", "download_url", "master_url"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        for key in ("url_list", "urls"):
            value = node.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    found = direct_url(item)
                    if found:
                        return found
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for item in node:
            found = direct_url(item)
            if found:
                return found
    return None

# Douyin API v5 normalized response.
media = root.get("media") if isinstance(root, Mapping) else None
if isinstance(media, Mapping):
    found = direct_url(media.get("video"))
    if found:
        print(found)
        raise SystemExit(0)
    found = direct_url(media.get("streams"))
    if found:
        print(found)
        raise SystemExit(0)

# Legacy APIs: only inspect known media-address fields, never arbitrary source/support URLs.
def walk(node):
    if isinstance(node, Mapping):
        for key in ("download_addr", "play_addr", "play_addr_h264", "play_addr_265", "video_url", "master_url"):
            if key in node:
                found = direct_url(node[key])
                if found:
                    return found
        for value in node.values():
            found = walk(value)
            if found:
                return found
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for value in node:
            found = walk(value)
            if found:
                return found
    return None

found = walk(root)
if found:
    print(found)
    raise SystemExit(0)
raise SystemExit(1)
PY
}

download_video_from_url() {
  local video_url="$1"
  local referer="$2"
  curl -L -A "Mozilla/5.0" -H "Referer: $referer" "$video_url" -o "$RUN_DIR/video.mp4"
}

prepare_video_assets() {
  [[ -f "$RUN_DIR/video.mp4" ]] || return 0

  log "video: $RUN_DIR/video.mp4"
  ffmpeg -y -i "$RUN_DIR/video.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$RUN_DIR/audio.wav" -loglevel error || true

  if [[ -s "$RUN_DIR/audio.txt" ]]; then
    log "transcript: $RUN_DIR/audio.txt"
  elif [[ -s "$RUN_DIR/audio.wav" ]]; then
    if [[ -n "${ASR_API_BASE:-}" && -n "$ASR_API_KEY_EFFECTIVE" ]]; then
      local asr_response="$RUN_DIR/asr-response.json"
      if curl -sS --fail --connect-timeout 20 --max-time 1800 \
        -H "Authorization: Bearer $ASR_API_KEY_EFFECTIVE" \
        -F "file=@$RUN_DIR/audio.wav;type=audio/wav" \
        -F "model=$ASR_MODEL" \
        -F "language=zh" \
        -F "response_format=verbose_json" \
        -F "timestamp_granularities[]=segment" \
        "${ASR_API_BASE%/}/audio/transcriptions" -o "$asr_response"; then
        python3 - "$asr_response" "$RUN_DIR/audio.txt" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
text = payload.get("text") or ""
if not text.strip():
    raise SystemExit("ASR response did not contain text")
open(sys.argv[2], "w", encoding="utf-8").write(text.strip() + "\n")
PY
        log "transcript: $RUN_DIR/audio.txt (remote ASR)"
      else
        log "warning: remote ASR failed; trying local whisper fallback"
      fi
    fi
    if [[ ! -s "$RUN_DIR/audio.txt" ]] && command -v whisper >/dev/null 2>&1; then
      whisper "$RUN_DIR/audio.wav" --model "$WHISPER_MODEL" --language "$WHISPER_LANGUAGE" \
        --output_dir "$RUN_DIR" --output_format all --task transcribe
    elif [[ ! -s "$RUN_DIR/audio.txt" ]]; then
      log "warning: whisper not found; transcript skipped"
    fi
  fi

  ffmpeg -i "$RUN_DIR/video.mp4" -vf "select='gt(scene,0.3)',showinfo" \
    -fps_mode vfr "$RUN_DIR/frames/cut_%03d.jpg" 2>"$RUN_DIR/cuts.log" || true
  ffmpeg -i "$RUN_DIR/video.mp4" -t 3 -vf "fps=1" \
    -fps_mode vfr "$RUN_DIR/frames/hook_%03d.jpg" -loglevel error || true
  ffmpeg -i "$RUN_DIR/video.mp4" -vf "fps=1/3" \
    -fps_mode vfr "$RUN_DIR/frames/t_%03d.jpg" -loglevel error || true

  if find "$RUN_DIR/frames" -type f -name '*.jpg' | grep -q .; then
    ffmpeg -pattern_type glob -i "$RUN_DIR/frames/*.jpg" \
      -filter_complex "scale=320:-1,tile=6x6:padding=4:color=white" \
      -fps_mode vfr "$RUN_DIR/sheet_%03d.jpg" -loglevel error || true
  fi

  local duration cuts avg
  duration="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$RUN_DIR/video.mp4" 2>/dev/null || echo 0)"
  cuts="$(grep -c "pts_time" "$RUN_DIR/cuts.log" 2>/dev/null || echo 0)"
  avg="$(awk -v d="$duration" -v c="$cuts" 'BEGIN { if (c > 0) printf "%.2f", d/c; else printf "未检测到切点" }')"
  printf 'duration=%s\ncuts=%s\navg_cut=%s\n' "$duration" "$cuts" "$avg" > "$RUN_DIR/video_stats.txt"
}

prepare_image_sheet() {
  if ! find "$RUN_DIR/xhs_images" -type f | grep -q .; then
    return 0
  fi

  ffmpeg -pattern_type glob -i "$RUN_DIR/xhs_images/*" \
    -filter_complex "scale=360:-1,tile=4x4:padding=8:color=white" \
    -fps_mode vfr "$RUN_DIR/xhs_sheet_%03d.jpg" -loglevel error || true
}

download_xhs_assets_from_json() {
  local json_file="$1"
  local url_list="$RUN_DIR/xhs_download_urls.tsv"
  python3 - "$json_file" "$url_list" <<'PY'
import json, sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
payload = json.load(src.open(encoding="utf-8"))
data = payload.get("data") or {}
urls = data.get("下载地址") or []
kind = data.get("作品类型") or ""
rows = []
if "视频" in kind and urls:
    rows.append(("video", urls[0]))
else:
    for url in urls:
        rows.append(("image", url))
dst.write_text("\n".join(f"{kind}\t{url}" for kind, url in rows) + ("\n" if rows else ""), encoding="utf-8")
PY

  local i=0 kind url ext
  while IFS=$'\t' read -r kind url; do
    [[ -n "$url" ]] || continue
    case "$kind" in
      video)
        [[ -f "$RUN_DIR/video.mp4" ]] || curl -L -A "Mozilla/5.0" -H "Referer: https://www.xiaohongshu.com/" "$url" -o "$RUN_DIR/video.mp4"
        ;;
      image)
        i=$((i + 1))
        ext="${url%%\?*}"
        ext="${ext##*.}"
        case "$ext" in
          jpg|jpeg|png|webp|heic|avif) ;;
          *) ext="jpg" ;;
        esac
        curl -L -A "Mozilla/5.0" -H "Referer: https://www.xiaohongshu.com/" "$url" -o "$RUN_DIR/xhs_images/$(printf '%03d' "$i").$ext"
        ;;
    esac
  done < "$url_list"
}

srt_to_text() {
  local srt="$1"
  local out="$2"
  python3 - "$srt" "$out" <<'PY'
import re, sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
lines = []
for raw in src.read_text(encoding="utf-8", errors="ignore").splitlines():
    line = raw.strip()
    if not line:
        continue
    if line.isdigit():
        continue
    if re.match(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}", line):
        continue
    lines.append(line)

dst.write_text("\n".join(lines).strip() + ("\n" if lines else ""), encoding="utf-8")
PY
}

copy_new_xhs_files() {
  local marker="$1"
  if [[ ! -d "$XHS_DOWNLOAD_DIR" ]]; then
    log "warning: XHS_DOWNLOAD_DIR not found: $XHS_DOWNLOAD_DIR"
    log "set XHS_DOWNLOAD_DIR to the mounted XHS-Downloader Volume/Download directory to collect downloaded files"
    return 0
  fi

  local newest_video=""
  newest_video="$(find "$XHS_DOWNLOAD_DIR" -type f -newer "$marker" -iname '*.mp4' -print 2>/dev/null | sort | tail -1 || true)"
  if [[ -n "$newest_video" ]]; then
    cp "$newest_video" "$RUN_DIR/video.mp4"
  fi

  local i=0 ext base
  while IFS= read -r file; do
    i=$((i + 1))
    base="$(basename "$file")"
    ext="${base##*.}"
    cp "$file" "$RUN_DIR/xhs_images/$(printf '%03d' "$i").$ext"
  done < <(find "$XHS_DOWNLOAD_DIR" -type f -newer "$marker" \( \
      -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o -iname '*.heic' \
    \) -print 2>/dev/null | sort)
}

json_has_data() {
  python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        payload = json.load(f)
except Exception:
    raise SystemExit(1)
data = payload.get("data")
raise SystemExit(0 if data else 1)
PY
}

prepare_xhs_web_fallback() {
  local replace_metadata="${1:-true}"
  local page="$RUN_DIR/xhs_page.html"
  local web_meta="$RUN_DIR/xhs_web_meta.json"
  curl -sS -L -A "Mozilla/5.0" "$URL" -o "$page"

  python3 - "$page" "$web_meta" "$URL" <<'PY'
import json, re, sys
from pathlib import Path
from urllib.parse import urlparse

page = Path(sys.argv[1])
out = Path(sys.argv[2])
source_url = sys.argv[3]
s = page.read_text(encoding="utf-8", errors="ignore")
normalized = s.replace("\\u002F", "/")

def first(pattern, text=normalized, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(1) if m else ""

def json_string(key, text):
    value = first(rf'"{re.escape(key)}":"((?:\\.|[^"\\])*)"', text)
    if not value:
        return ""
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value

def json_number(key, text):
    return first(rf'"{re.escape(key)}":([0-9]+)', text)

path = urlparse(source_url).path
note_id = ""
for pattern in (
    r"/explore/([0-9a-f]+)",
    r"/discovery/item/([0-9a-f]+)",
    r"/user/profile/[a-z0-9]+/([0-9a-f]+)",
):
    note_id = first(pattern, path) or note_id
note_id = note_id or first(r'"firstNoteId":"([^"]+)"') or first(r'"noteId":"([^"]+)"')

note_marker = f'"noteDetailMap":{{"{note_id}"' if note_id else '"noteDetailMap":{'
note_pos = normalized.find(note_marker)
if note_pos < 0 and note_id:
    note_pos = normalized.find(f'"noteId":"{note_id}"')
segment = normalized[max(0, note_pos - 2000): note_pos + 50000] if note_pos >= 0 else normalized

video_urls = []
for pattern in (r'"master_url":"([^"]+)"', r'"masterUrl":"([^"]+)"'):
    video_urls.extend(re.findall(pattern, normalized))

subtitle_url = ""
subtitle_lang = ""
media_v2_raw = json_string("mediaV2", segment)
if media_v2_raw:
    try:
        media_v2 = json.loads(media_v2_raw)
        subtitles = (((media_v2.get("video") or {}).get("subtitles")) or {})
        for lang in ("source", "zh-CN", "en-US"):
            items = subtitles.get(lang) or []
            if items and items[0].get("url"):
                subtitle_url = items[0]["url"].replace("\\u002F", "/")
                subtitle_lang = items[0].get("language") or lang
                break
    except Exception:
        pass
if not subtitle_url:
    subtitle_url = first(r'https://sns-subtitle[^"\\]+', normalized)

meta = {
    "platform": "xhs",
    "type": first(r'"type":"([^"]+)"', segment),
    "noteId": note_id,
    "title": json_string("title", segment),
    "desc": json_string("desc", segment),
    "author": json_string("nickname", segment),
    "time": json_number("time", segment),
    "lastUpdateTime": json_number("lastUpdateTime", segment),
    "likedCount": first(r'"likedCount":"([^"]+)"', segment),
    "collectedCount": first(r'"collectedCount":"([^"]+)"', segment),
    "commentCount": first(r'"commentCount":"([^"]+)"', segment),
    "shareCount": first(r'"shareCount":"([^"]+)"', segment),
    "videoUrl": video_urls[0] if video_urls else "",
    "subtitleUrl": subtitle_url,
    "subtitleLanguage": subtitle_lang,
    "fallback": "xhs_web_html",
}

out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(meta, ensure_ascii=False))
PY

  local video_url subtitle_url
  video_url="$(python3 - "$web_meta" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("videoUrl", ""))
PY
)"
  subtitle_url="$(python3 - "$web_meta" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("subtitleUrl", ""))
PY
)"

  if [[ -n "$video_url" && ! -f "$RUN_DIR/video.mp4" ]]; then
    curl -L -A "Mozilla/5.0" -H "Referer: https://www.xiaohongshu.com/" "$video_url" -o "$RUN_DIR/video.mp4"
  fi
  if [[ -n "$subtitle_url" ]]; then
    curl -L -A "Mozilla/5.0" -H "Referer: https://www.xiaohongshu.com/" "$subtitle_url" -o "$RUN_DIR/subtitle.srt" || true
    if [[ -s "$RUN_DIR/subtitle.srt" ]]; then
      srt_to_text "$RUN_DIR/subtitle.srt" "$RUN_DIR/audio.txt"
    fi
  fi
  if [[ "$replace_metadata" == "true" ]]; then
    cp "$web_meta" "$METADATA"
  fi
}

prepare_xhs_detail_retry() {
  local detail="$RUN_DIR/xhs_detail_retry.json"
  curl -sS -X POST "$XHS_API_BASE/xhs/detail" \
    -H "Content-Type: application/json" \
    -d "$(json_payload false)" \
    -o "$detail"

  if ! json_has_data "$detail"; then
    return 1
  fi

  cp "$detail" "$METADATA"
  download_xhs_assets_from_json "$detail"
}

fetch_douyin_v5() {
  local source_url="$1"
  local response="$RUN_DIR/dtk5-response.json"
  local key_file="${DOUYIN_API_V5_KEY_FILE:-/run/secrets/dtk5-api-key}"
  [[ -n "${DOUYIN_API_V5_BASE:-}" && -r "$key_file" ]] || return 1

  local api_key payload
  api_key="$(cat "$key_file")"
  payload="$(python3 - "$source_url" <<'PYJSON'
import json, sys
print(json.dumps({"url": sys.argv[1]}, ensure_ascii=False))
PYJSON
)"

  local http_code curl_status=0 diagnostics line
  # Clear a previous attempt so a transport failure cannot diagnose stale data.
  : > "$response"
  http_code="$(curl -sS --connect-timeout 10 --max-time 90 \
      -H "X-API-Key: $api_key" \
      -H "Content-Type: application/json" \
      --data "$payload" \
      "${DOUYIN_API_V5_BASE%/}/api/v1/parse?wait=30" \
      -o "$response" -w '%{http_code}')" || curl_status=$?
  [[ "$http_code" =~ ^[0-9]{3}$ ]] || http_code=000
  log "douyin_v5_http_status: $http_code"
  if (( curl_status != 0 )); then
    log "douyin_v5_transport_error: curl_exit=$curl_status"
    return 1
  fi
  if [[ ! "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    # Responses are untrusted data. Redact before truncation, and escape log
    # delimiters/control characters to prevent forged diagnostic lines.
    if ! diagnostics="$(python3 - "$response" "$http_code" "$key_file" <<'PYDIAG'
import json, math, re, sys
from pathlib import Path
src, status, key_file = sys.argv[1:]
with open(src, "rb") as response_file:
    raw = response_file.read(65536).decode("utf-8", errors="replace")
secret = Path(key_file).read_text(encoding="utf-8").strip()
def scrub(text):
    if secret:
        for value in (secret, json.dumps(secret)[1:-1]):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)((?:x[-_]?api[-_]?key|api[-_]?key|access[-_]?token|refresh[-_]?token|token|authorization|cookie|password|secret)[\"']?\s*[:=]\s*)(?:\"[^\"]*(?:\"|$)|'[^']*(?:'|$)|[^\s,;&<>}]+)",
        lambda m: m.group(1) + "[REDACTED]", text)
    return text
try:
    payload = json.loads(raw)
except (ValueError, RecursionError):
    payload = None
error = payload.get("error") if isinstance(payload, dict) else None
error = error if isinstance(error, dict) else {}
code = error.get("code")
if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", code):
    print("douyin_v5_error_code: " + scrub(code))
retry = error.get("retry_after")
try:
    retry = float(retry) if not isinstance(retry, bool) else float("nan")
    retry = math.ceil(retry) if math.isfinite(retry) and 0 <= retry <= 86400 * 365 else None
except (ValueError, TypeError, OverflowError):
    retry = None
risk = code == "UPSTREAM_RISK_CONTROL"
if risk or (status == "429" and retry is not None):
    print("douyin_v5_identity_cooling: retry_after=" + (str(retry) if retry is not None else "unknown"))
    print("douyin_v5_cooling_scope: " + ("upstream_risk_control" if risk else "rate_limit"))
# Redact entire structured values (including nested/escaped values) before
# producing the excerpt; raw malformed/HTML responses use best-effort scrubbing.
def redact_fields(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.fullmatch(
            r"(?i)(?:x[-_]?api[-_]?key|api[-_]?key|access[-_]?token|refresh[-_]?token|token|authorization|cookie|password|secret)", k)
            else redact_fields(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_fields(v) for v in value]
    return value
try:
    redacted = json.dumps(redact_fields(payload), ensure_ascii=False) if payload is not None else raw
except RecursionError:
    redacted = "[response nesting too deep; excerpt omitted]"
# JSON escaping keeps each excerpt on one line, including invalid/HTML bodies.
preview = scrub(redacted).encode("utf-8")[:500].decode("utf-8", errors="ignore")
print("douyin_v5_error_body: " + json.dumps(preview, ensure_ascii=True))
PYDIAG
)"; then
      log "douyin_v5_diagnostic_error: response_unavailable"
      return 1
    fi
    while IFS= read -r line; do
      [[ -z "$line" ]] || log "$line"
    done <<< "$diagnostics"
    return 1
  fi

  python3 - "$response" "$METADATA" <<'PYJSON'
import json, sys
src, dst = sys.argv[1:]
payload = json.load(open(src, encoding="utf-8"))
if not payload.get("success") or not isinstance(payload.get("data"), dict):
    raise SystemExit(1)
data = payload["data"]
endpoint = (payload.get("meta") or {}).get("endpoint")
if endpoint == "douyin.author_profile":
    # An author profile is a successful, non-video response. Do not send it
    # to the legacy video endpoint or attempt ASR/timeline processing.
    if not any(isinstance(data.get(k), str) and data[k].strip() for k in ("sec_uid", "uid", "unique_id")):
        raise SystemExit(1)
    from pathlib import Path
    from datetime import datetime, timezone
    profile = {k: data[k] for k in ("uid", "sec_uid", "unique_id", "nickname", "signature", "web_url", "avatar", "stats", "platform", "verified") if k in data}
    json.dump(data, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    result = {
        "resource_type": "account_profile", "status": "profile_only",
        "profile": profile, "profile_fetched": True,
        "works_fetched": False, "works_status": "not_requested",
        "next_tool": None, "provider_endpoint": endpoint,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "limitations": ["仅取得账号资料和接口返回的当前统计，未获取作品列表、历史涨粉或流量来源；账号资料可能来自接口缓存。"],
    }
    json.dump(result, open(Path(dst).with_name("prepare-result.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    raise SystemExit(0)
media = data.get("media") or {}
video = media.get("video") or {}
if not isinstance(video, dict) or not video.get("url"):
    raise SystemExit(1)
json.dump(data, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYJSON
}

PLATFORM="$(detect_platform)"
: > "$MANIFEST"
printf '%s\n' "$URL" > "$RUN_DIR/source.url"
log "run_dir: $RUN_DIR"
log "platform: $PLATFORM"

case "$PLATFORM" in
  xhs)
    marker="$RUN_DIR/download.marker"
    : > "$marker"
    curl -sS -X POST "$XHS_API_BASE/xhs/detail" \
      -H "Content-Type: application/json" \
      -d "$(json_payload false)" \
      -o "$METADATA"
    if json_has_data "$METADATA"; then
      download_xhs_assets_from_json "$METADATA"
      prepare_xhs_web_fallback false
    else
      warn_xhs_cookie_if_needed
      log "warning: XHS API detail-only returned no data; retrying download=true mode"
      curl -sS -X POST "$XHS_API_BASE/xhs/detail" \
        -H "Content-Type: application/json" \
        -d "$(json_payload true)" \
        -o "$METADATA"
      copy_new_xhs_files "$marker"
      if ! json_has_data "$METADATA"; then
        log "warning: XHS API download=true returned no data; trying web HTML fallback for metadata/subtitles"
        prepare_xhs_web_fallback
      elif json_has_data "$METADATA"; then
        if [[ ! -f "$RUN_DIR/video.mp4" ]] && ! find "$RUN_DIR/xhs_images" -type f | grep -q .; then
          download_xhs_assets_from_json "$METADATA"
        fi
        prepare_xhs_web_fallback false
      fi
    fi
    ;;
  douyin|tiktok|bilibili)
    request_url="$URL"
    if [[ "$PLATFORM" == "douyin" ]]; then
      request_url="$(resolve_douyin_url "$URL")"
      log "douyin_resolved_url: $request_url"
    fi

    if [[ "$PLATFORM" == "douyin" ]] && fetch_douyin_v5 "$request_url"; then
      log "metadata: Douyin API v5"
    else
      if [[ "$PLATFORM" == "douyin" ]]; then
        log "warning: Douyin API v5 failed; trying legacy API"
      fi
      encoded_url="$(python3 - "$request_url" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=""))
PY
)"
      api_http_status="$(curl -sS \
        "$DOUYIN_API_BASE/api/hybrid/video_data?url=$encoded_url&minimal=false" \
        -o "$METADATA" -w '%{http_code}')" || {
          log "error: metadata API transport failed: $DOUYIN_API_BASE"
          exit 22
        }
      log "metadata_api_http_status: $api_http_status"
      if [[ ! "$api_http_status" =~ ^2[0-9][0-9]$ ]]; then
        api_error="$(metadata_api_error_summary "$METADATA" 2>/dev/null || true)"
        log "error_scope: metadata_provider"
        log "source_url_status: resolved_or_canonicalized"
        log "error: metadata API failed after URL resolution${api_error:+ ($api_error)}; do not classify the source URL as invalid"
        exit 22
      fi
      if api_error="$(metadata_api_error_summary "$METADATA" 2>/dev/null)"; then
        log "error: metadata API returned an application error ($api_error)"
        exit 22
      fi
    fi

    if [[ -f "$RUN_DIR/prepare-result.json" ]] && python3 - "$RUN_DIR/prepare-result.json" <<'PYPROFILE'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if result.get("resource_type") == "account_profile" and result.get("status") == "profile_only" else 1)
PYPROFILE
    then
      log "resource_type: account_profile"
      log "status: profile_only"
      log "profile_fetched: true"
      log "works_fetched: false (not requested)"
      log "result: $RUN_DIR/prepare-result.json"
      log "done: account profile retrieved; no video processing required"
      exit 0
    fi

    if video_url="$(extract_video_url)"; then
      case "$PLATFORM" in
        douyin) referer="https://www.douyin.com/" ;;
        tiktok) referer="https://www.tiktok.com/" ;;
        bilibili) referer="https://www.bilibili.com/" ;;
      esac
      download_video_from_url "$video_url" "$referer"
    else
      log "error: metadata API succeeded but returned no media URL"
      exit 22
    fi
    ;;
  *)
    log "warning: platform not automated by this script; use TikHub/Jina/browser fallback from fetch-playbook"
    printf '{"url": %s, "platform": %s, "message": "manual fallback required"}\n' \
      "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$URL")" \
      "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$PLATFORM")" > "$METADATA"
    ;;
esac

prepare_video_assets
prepare_image_sheet

{
  echo ""
  echo "outputs:"
  find "$RUN_DIR" -maxdepth 2 -type f | sort
} >> "$MANIFEST"

log "done: $RUN_DIR"


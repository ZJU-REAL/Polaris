#!/usr/bin/env bash
# 从 build/icon.svg 生成三平台图标（icon.png / icon.icns / icon.ico）。
# 只依赖 macOS 自带的 qlmanage / sips / iconutil 与 python3——生成的产物已入库，
# 平时不需要跑；只有品牌标变了才需要在 macOS 上重新生成。
set -euo pipefail

cd "$(dirname "$0")/.."
SVG=build/icon.svg
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

[ -f "$SVG" ] || { echo "缺少 $SVG" >&2; exit 1; }

# SVG → 1024 PNG
qlmanage -t -s 1024 -o "$TMP" "$SVG" >/dev/null 2>&1
BASE="$TMP/$(basename "$SVG").png"
[ -f "$BASE" ] || { echo "qlmanage 未能渲染 SVG" >&2; exit 1; }
sips -z 1024 1024 "$BASE" --out build/icon.png >/dev/null

# macOS .icns
ICONSET="$TMP/icon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 64 128 256 512 1024; do
  sips -z $size $size build/icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
done
# iconutil 要求的 @2x 命名
for size in 16 32 128 256 512; do
  cp "$ICONSET/icon_$((size * 2))x$((size * 2)).png" "$ICONSET/icon_${size}x${size}@2x.png" 2>/dev/null || true
done
rm -f "$ICONSET/icon_64x64.png" "$ICONSET/icon_1024x1024.png"
iconutil -c icns "$ICONSET" -o build/icon.icns

# Windows .ico —— PNG-embedded ICO，格式简单，不值得为它装 ImageMagick
for size in 16 32 48 64 128 256; do
  sips -z $size $size build/icon.png --out "$TMP/ico_${size}.png" >/dev/null
done
python3 - "$TMP" <<'PY'
import struct, sys, pathlib
tmp = pathlib.Path(sys.argv[1])
sizes = [16, 32, 48, 64, 128, 256]
images = [(s, (tmp / f"ico_{s}.png").read_bytes()) for s in sizes]
header = struct.pack("<HHH", 0, 1, len(images))
offset = 6 + 16 * len(images)
entries, blobs = b"", b""
for size, data in images:
    entries += struct.pack("<BBBBHHII", size if size < 256 else 0, size if size < 256 else 0,
                           0, 0, 1, 32, len(data), offset)
    blobs += data
    offset += len(data)
pathlib.Path("build/icon.ico").write_bytes(header + entries + blobs)
PY

echo "生成完成："
ls -la build/icon.png build/icon.icns build/icon.ico

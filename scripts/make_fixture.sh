#!/usr/bin/env bash
# Build the synthetic "screen recording" reel is tested against.
#
#   0.0 -  3.0   static colour        (leading dead time)
#   3.0 -  7.0   testsrc motion       (the demo)
#   7.0 - 12.0   frozen frame         (you, reading)
#  12.0 - 15.0   a different scene    (scene cut + motion)
#  15.0 - 17.0   static colour        (trailing dead time)
#
# Usage: scripts/make_fixture.sh [output.mov]
set -euo pipefail

OUT="${1:-tests/fixtures/fixture.mov}"
FPS=30
W=640
H=400
THREADS="${REEL_FIXTURE_THREADS:-2}"

FFMPEG="$(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)"
if [ ! -x "$FFMPEG" ]; then
  echo "make_fixture.sh: ffmpeg not found (brew install ffmpeg)" >&2
  exit 127
fi

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1. leading dead time: a flat colour, nothing moving.
"$FFMPEG" -hide_banner -loglevel error -threads "$THREADS" \
  -f lavfi -i "color=c=0x12141a:s=${W}x${H}:r=${FPS}:d=3" \
  -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -y "$TMP/a.mp4"

# 2. the demo: real motion.
"$FFMPEG" -hide_banner -loglevel error -threads "$THREADS" \
  -f lavfi -i "testsrc=s=${W}x${H}:r=${FPS}:d=4" \
  -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -y "$TMP/b.mp4"

# 3. frozen: one frame of the demo held for 5 seconds.
"$FFMPEG" -hide_banner -loglevel error -threads "$THREADS" \
  -f lavfi -i "testsrc=s=${W}x${H}:r=${FPS}:d=1" \
  -vf "select='eq(n\,20)',loop=loop=-1:size=1:start=0,trim=duration=5,fps=${FPS}" \
  -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -y "$TMP/c.mp4"

# 4. a visually different scene, also moving -> a scene cut at 12s.
"$FFMPEG" -hide_banner -loglevel error -threads "$THREADS" \
  -f lavfi -i "smptebars=s=${W}x${H}:r=${FPS}:d=3" \
  -vf "geq=r='r(X\,Y)':g='g(X\,Y)':b='b(X\,Y)',hue=H=2*PI*t/3" \
  -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -y "$TMP/d.mp4"

# 5. trailing dead time.
"$FFMPEG" -hide_banner -loglevel error -threads "$THREADS" \
  -f lavfi -i "color=c=0x1d2230:s=${W}x${H}:r=${FPS}:d=2" \
  -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -y "$TMP/e.mp4"

for part in a b c d e; do
  echo "file '$TMP/$part.mp4'" >> "$TMP/list.txt"
done

# QuickTime container, like a real screen recording.
"$FFMPEG" -hide_banner -loglevel error -threads "$THREADS" \
  -f concat -safe 0 -i "$TMP/list.txt" \
  -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
  -x264-params "keyint=30:scenecut=0" \
  -movflags +faststart -f mov -y "$OUT"

echo "wrote $OUT"

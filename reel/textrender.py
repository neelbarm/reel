"""A tiny TrueType rasteriser and RGBA canvas, in pure stdlib Python.

Why this exists: `drawtext` is only compiled into ffmpeg when it is built
against libfreetype, and plenty of builds (including the current Homebrew
bottle on this machine) ship without it. Rather than tell people to rebuild
ffmpeg, reel rasterises caption text itself, writes an RGBA PNG, and
composites it with the `overlay` filter - which every build has.

The side effect is a nicer result than `drawtext` could give: genuinely
rounded corners, real antialiasing, and pixel-exact text metrics so the
banner can be laid out properly instead of guessed at.

Supported: TTF and TTC, cmap format 4 and format 12, simple and composite
glyphs, quadratic outlines, nonzero winding fill with 4x vertical /
analytic horizontal antialiasing.
"""

import math
import struct
import zlib

ON_CURVE = 0x01
X_SHORT = 0x02
Y_SHORT = 0x04
REPEAT = 0x08
X_SAME = 0x10
Y_SAME = 0x20

ARG_1_AND_2_ARE_WORDS = 0x0001
ARGS_ARE_XY_VALUES = 0x0002
WE_HAVE_A_SCALE = 0x0008
MORE_COMPONENTS = 0x0020
WE_HAVE_AN_X_AND_Y_SCALE = 0x0040
WE_HAVE_A_TWO_BY_TWO = 0x0080

SUBSAMPLES = 4          # vertical supersampling for glyph fill
CURVE_STEPS = 10        # flattening steps per quadratic segment


class FontError(Exception):
    """Raised when a font file cannot be parsed."""


class Font(object):
    """Just enough of a TrueType font to lay out and draw a line of text."""

    def __init__(self, path, index=0):
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.tables = {}
        self._glyph_cache = {}
        self._bitmap_cache = {}
        self._parse_directory(index)
        self._parse_head()
        self._parse_cmap()
        self._parse_hmtx()

    # ------------------------------------------------------------ parsing --

    def _u16(self, off):
        return struct.unpack_from(">H", self.data, off)[0]

    def _s16(self, off):
        return struct.unpack_from(">h", self.data, off)[0]

    def _u32(self, off):
        return struct.unpack_from(">I", self.data, off)[0]

    def _parse_directory(self, index):
        if len(self.data) < 12:
            raise FontError("%s is too small to be a font." % self.path)
        tag = self.data[:4]
        base = 0
        if tag == b"ttcf":
            num_fonts = self._u32(8)
            if index >= num_fonts:
                index = 0
            base = self._u32(12 + 4 * index)
        elif tag not in (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO"):
            raise FontError(
                "%s is not a TrueType font reel can read (tag %r)." % (self.path, tag)
            )
        if self.data[base : base + 4] == b"OTTO":
            raise FontError(
                "%s is a CFF/OpenType font; reel needs a TrueType outline font "
                "(.ttf/.ttc). Try --font /System/Library/Fonts/Supplemental/Arial.ttf"
                % self.path
            )
        num_tables = self._u16(base + 4)
        for i in range(num_tables):
            rec = base + 12 + 16 * i
            name = self.data[rec : rec + 4]
            offset = self._u32(rec + 8)
            length = self._u32(rec + 12)
            self.tables[name] = (offset, length)
        for needed in (b"head", b"maxp", b"cmap", b"glyf", b"loca", b"hhea", b"hmtx"):
            if needed not in self.tables:
                raise FontError(
                    "%s is missing the %s table; reel needs a TrueType outline font."
                    % (self.path, needed.decode("ascii"))
                )

    def _parse_head(self):
        head = self.tables[b"head"][0]
        self.units_per_em = self._u16(head + 18) or 1000
        self.index_to_loc = self._s16(head + 50)
        self.num_glyphs = self._u16(self.tables[b"maxp"][0] + 4)
        self.ascent = self._s16(self.tables[b"hhea"][0] + 4)
        self.descent = self._s16(self.tables[b"hhea"][0] + 6)
        loca_off, loca_len = self.tables[b"loca"]
        self.loca = []
        if self.index_to_loc == 0:
            count = min(self.num_glyphs + 1, loca_len // 2)
            for i in range(count):
                self.loca.append(self._u16(loca_off + 2 * i) * 2)
        else:
            count = min(self.num_glyphs + 1, loca_len // 4)
            for i in range(count):
                self.loca.append(self._u32(loca_off + 4 * i))

    def _parse_cmap(self):
        cmap_off = self.tables[b"cmap"][0]
        num = self._u16(cmap_off + 2)
        best = None
        best_score = -1
        for i in range(num):
            rec = cmap_off + 4 + 8 * i
            platform = self._u16(rec)
            encoding = self._u16(rec + 2)
            offset = self._u32(rec + 4)
            score = {
                (3, 10): 5, (3, 1): 4, (0, 4): 3, (0, 3): 3, (0, 6): 3,
                (0, 1): 2, (0, 0): 2, (3, 0): 1,
            }.get((platform, encoding), 0)
            if score > best_score:
                best_score = score
                best = cmap_off + offset
        if best is None:
            raise FontError("%s has no usable cmap subtable." % self.path)
        fmt = self._u16(best)
        self._cmap = {}
        if fmt == 4:
            self._parse_cmap4(best)
        elif fmt == 12:
            self._parse_cmap12(best)
        elif fmt == 6:
            first = self._u16(best + 6)
            count = self._u16(best + 8)
            for i in range(count):
                self._cmap[first + i] = self._u16(best + 10 + 2 * i)
        else:
            raise FontError("%s uses cmap format %d, which reel cannot read." % (self.path, fmt))

    def _parse_cmap4(self, off):
        seg_x2 = self._u16(off + 6)
        segs = seg_x2 // 2
        ends = off + 14
        starts = ends + seg_x2 + 2
        deltas = starts + seg_x2
        ranges = deltas + seg_x2
        for i in range(segs):
            end = self._u16(ends + 2 * i)
            start = self._u16(starts + 2 * i)
            delta = self._s16(deltas + 2 * i)
            range_off = self._u16(ranges + 2 * i)
            if start > end:
                continue
            for code in range(start, min(end, 0xFFFF) + 1):
                if range_off == 0:
                    gid = (code + delta) & 0xFFFF
                else:
                    addr = ranges + 2 * i + range_off + 2 * (code - start)
                    if addr + 1 >= len(self.data):
                        continue
                    gid = self._u16(addr)
                    if gid:
                        gid = (gid + delta) & 0xFFFF
                if gid:
                    self._cmap[code] = gid

    def _parse_cmap12(self, off):
        groups = self._u32(off + 12)
        for i in range(groups):
            rec = off + 16 + 12 * i
            start = self._u32(rec)
            end = self._u32(rec + 4)
            gid = self._u32(rec + 8)
            if end - start > 0x10000:
                end = start + 0x10000
            for c in range(start, end + 1):
                self._cmap[c] = gid + (c - start)

    def _parse_hmtx(self):
        self.num_hmetrics = self._u16(self.tables[b"hhea"][0] + 34) or 1
        self.hmtx_off = self.tables[b"hmtx"][0]

    def advance(self, gid):
        i = min(gid, self.num_hmetrics - 1)
        return self._u16(self.hmtx_off + 4 * i)

    def glyph_id(self, char):
        return self._cmap.get(ord(char), 0)

    # ----------------------------------------------------------- outlines --

    def glyph_contours(self, gid, depth=0):
        """-> [[(x, y), ...], ...] in font units, curves already flattened."""
        if gid in self._glyph_cache:
            return self._glyph_cache[gid]
        contours = []
        if 0 <= gid < len(self.loca) - 1:
            glyf_off = self.tables[b"glyf"][0]
            start = glyf_off + self.loca[gid]
            end = glyf_off + self.loca[gid + 1]
            if end > start + 9:
                n = self._s16(start)
                if n >= 0:
                    contours = self._simple_glyph(start, n)
                elif depth < 4:
                    contours = self._composite_glyph(start, end, depth)
        self._glyph_cache[gid] = contours
        return contours

    def _simple_glyph(self, off, n_contours):
        pos = off + 10
        end_pts = [self._u16(pos + 2 * i) for i in range(n_contours)]
        pos += 2 * n_contours
        n_points = (end_pts[-1] + 1) if end_pts else 0
        instr_len = self._u16(pos)
        pos += 2 + instr_len

        flags = []
        while len(flags) < n_points:
            flag = self.data[pos]
            pos += 1
            flags.append(flag)
            if flag & REPEAT:
                repeat = self.data[pos]
                pos += 1
                flags.extend([flag] * repeat)
        flags = flags[:n_points]

        xs = []
        value = 0
        for flag in flags:
            if flag & X_SHORT:
                delta = self.data[pos]
                pos += 1
                value += delta if (flag & X_SAME) else -delta
            elif not (flag & X_SAME):
                value += struct.unpack_from(">h", self.data, pos)[0]
                pos += 2
            xs.append(value)
        ys = []
        value = 0
        for flag in flags:
            if flag & Y_SHORT:
                delta = self.data[pos]
                pos += 1
                value += delta if (flag & Y_SAME) else -delta
            elif not (flag & Y_SAME):
                value += struct.unpack_from(">h", self.data, pos)[0]
                pos += 2
            ys.append(value)

        contours = []
        begin = 0
        for stop in end_pts:
            pts = [
                (xs[i], ys[i], bool(flags[i] & ON_CURVE))
                for i in range(begin, min(stop + 1, n_points))
            ]
            begin = stop + 1
            if len(pts) >= 2:
                contours.append(_flatten(pts))
        return contours

    def _composite_glyph(self, off, end, depth):
        pos = off + 10
        contours = []
        while pos + 4 <= end:
            flags = self._u16(pos)
            glyph_index = self._u16(pos + 2)
            pos += 4
            if flags & ARG_1_AND_2_ARE_WORDS:
                a1, a2 = struct.unpack_from(">hh", self.data, pos)
                pos += 4
            else:
                a1, a2 = struct.unpack_from(">bb", self.data, pos)
                pos += 2
            sx = sy = 1.0
            s01 = s10 = 0.0
            if flags & WE_HAVE_A_SCALE:
                sx = sy = _f2dot14(self, pos)
                pos += 2
            elif flags & WE_HAVE_AN_X_AND_Y_SCALE:
                sx = _f2dot14(self, pos)
                sy = _f2dot14(self, pos + 2)
                pos += 4
            elif flags & WE_HAVE_A_TWO_BY_TWO:
                sx = _f2dot14(self, pos)
                s01 = _f2dot14(self, pos + 2)
                s10 = _f2dot14(self, pos + 4)
                sy = _f2dot14(self, pos + 6)
                pos += 8
            dx, dy = (a1, a2) if (flags & ARGS_ARE_XY_VALUES) else (0, 0)
            for contour in self.glyph_contours(glyph_index, depth + 1):
                contours.append(
                    [(x * sx + y * s10 + dx, x * s01 + y * sy + dy) for x, y in contour]
                )
            if not (flags & MORE_COMPONENTS):
                break
        return contours

    # --------------------------------------------------------- rendering --

    def glyph_bitmap(self, char, px):
        """-> (width, height, left, top, coverage[]) for one glyph at `px`.

        `left`/`top` are offsets from the pen position and the baseline.
        `coverage` is a flat list of floats 0..1, row-major.
        """
        key = (char, px)
        cached = self._bitmap_cache.get(key)
        if cached is not None:
            return cached
        gid = self.glyph_id(char)
        scale = float(px) / self.units_per_em
        contours = [
            [(x * scale, y * scale) for x, y in contour]
            for contour in self.glyph_contours(gid)
        ]
        result = _rasterise(contours)
        self._bitmap_cache[key] = result
        return result

    def text_width(self, text, px):
        scale = float(px) / self.units_per_em
        return sum(self.advance(self.glyph_id(ch)) * scale for ch in text)

    def line_metrics(self, px):
        scale = float(px) / self.units_per_em
        return self.ascent * scale, -self.descent * scale


def _f2dot14(font, off):
    return struct.unpack_from(">h", font.data, off)[0] / 16384.0


def _flatten(points):
    """Pure: TrueType point list (with off-curve control points) -> polyline."""
    if not points:
        return []
    # Rotate so the contour starts on an on-curve point.
    start = None
    for i, p in enumerate(points):
        if p[2]:
            start = i
            break
    if start is None:
        # All control points: synthesise the implied midpoint start.
        x0, y0, _ = points[0]
        x1, y1, _ = points[-1]
        points = [((x0 + x1) / 2.0, (y0 + y1) / 2.0, True)] + points
        start = 0
    pts = points[start:] + points[:start]
    pts.append(pts[0])

    out = [(pts[0][0], pts[0][1])]
    i = 1
    while i < len(pts):
        x, y, on = pts[i]
        if on:
            out.append((x, y))
            i += 1
            continue
        # Off-curve: find the segment end (real or implied midpoint).
        if i + 1 < len(pts):
            nx, ny, non = pts[i + 1]
            if non:
                end = (nx, ny)
                step = 2
            else:
                end = ((x + nx) / 2.0, (y + ny) / 2.0)
                step = 1
        else:
            end = out[0]
            step = 1
        out.extend(_quad(out[-1], (x, y), end))
        i += step
    return out


def _quad(p0, p1, p2):
    """Pure: flatten one quadratic bezier into CURVE_STEPS line points."""
    out = []
    for s in range(1, CURVE_STEPS + 1):
        t = s / float(CURVE_STEPS)
        mt = 1.0 - t
        a = mt * mt
        b = 2 * mt * t
        c = t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]))
    return out


def _rasterise(contours):
    """Pure: flattened contours in pixel units -> antialiased coverage bitmap.

    Nonzero winding, `SUBSAMPLES` scanlines per pixel row vertically and exact
    span overlap horizontally.
    """
    edges = []
    min_x = min_y = 1e9
    max_x = max_y = -1e9
    for contour in contours:
        n = len(contour)
        for i in range(n):
            x0, y0 = contour[i]
            x1, y1 = contour[(i + 1) % n]
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
            min_x = min(min_x, x0)
            max_x = max(max_x, x0)
            min_y = min(min_y, y0)
            max_y = max(max_y, y0)
    if not edges:
        return (0, 0, 0, 0, [])

    left = int(math.floor(min_x))
    right = int(math.ceil(max_x))
    bottom = int(math.floor(min_y))
    top = int(math.ceil(max_y))
    width = max(1, right - left)
    height = max(1, top - bottom)
    cover = [0.0] * (width * height)
    weight = 1.0 / SUBSAMPLES

    for row in range(height):
        base = row * width
        # Row 0 of the bitmap is the *top*, i.e. the highest y.
        y_top = top - row
        for s in range(SUBSAMPLES):
            y = y_top - (s + 0.5) * weight
            crossings = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= y < y1) or (y1 <= y < y0):
                    t = (y - y0) / (y1 - y0)
                    crossings.append((x0 + t * (x1 - x0), 1 if y1 > y0 else -1))
            if not crossings:
                continue
            crossings.sort()
            winding = 0
            span_start = 0.0
            for x, direction in crossings:
                if winding == 0:
                    span_start = x
                winding += direction
                if winding == 0:
                    _add_span(cover, base, width, span_start - left, x - left, weight)
    return (width, height, left, top, cover)


def _add_span(cover, base, width, x0, x1, weight):
    if x1 <= x0:
        return
    first = max(0, int(math.floor(x0)))
    last = min(width - 1, int(math.ceil(x1)) - 1)
    for i in range(first, last + 1):
        overlap = min(x1, i + 1.0) - max(x0, float(i))
        if overlap > 0:
            idx = base + i
            value = cover[idx] + overlap * weight
            cover[idx] = 1.0 if value > 1.0 else value


# ------------------------------------------------------------ RGBA canvas --

class Canvas(object):
    """A straight RGBA byte buffer with the few drawing ops reel needs."""

    def __init__(self, width, height, rgba=(0, 0, 0, 0)):
        self.width = int(width)
        self.height = int(height)
        self.buf = bytearray(self.width * self.height * 4)
        if rgba[3]:
            self.fill_rect(0, 0, self.width, self.height, rgba)

    def _blend(self, idx, r, g, b, a):
        if a <= 0:
            return
        buf = self.buf
        if a >= 1.0:
            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b
            buf[idx + 3] = 255
            return
        dst_a = buf[idx + 3] / 255.0
        out_a = a + dst_a * (1 - a)
        if out_a <= 0:
            return
        for k, src in enumerate((r, g, b)):
            dst = buf[idx + k]
            buf[idx + k] = int(round((src * a + dst * dst_a * (1 - a)) / out_a))
        buf[idx + 3] = int(round(out_a * 255))

    def fill_rect(self, x, y, w, h, rgba):
        r, g, b, a = rgba
        alpha = a / 255.0
        x0 = max(0, int(round(x)))
        y0 = max(0, int(round(y)))
        x1 = min(self.width, int(round(x + w)))
        y1 = min(self.height, int(round(y + h)))
        for row in range(y0, y1):
            base = (row * self.width) * 4
            for col in range(x0, x1):
                self._blend(base + col * 4, r, g, b, alpha)

    def rounded_rect(self, x, y, w, h, radius, rgba, samples=4):
        """Antialiased rounded rectangle. Only the corners are supersampled."""
        r, g, b, a = rgba
        alpha = a / 255.0
        radius = max(0.0, min(float(radius), w / 2.0, h / 2.0))
        if radius < 0.5:
            self.fill_rect(x, y, w, h, rgba)
            return
        # Straight middle band, plus the two straight bars top and bottom.
        self.fill_rect(x, y + radius, w, h - 2 * radius, rgba)
        self.fill_rect(x + radius, y, w - 2 * radius, radius, rgba)
        self.fill_rect(x + radius, y + h - radius, w - 2 * radius, radius, rgba)

        corners = (
            (x + radius, y + radius, x, y),
            (x + w - radius, y + radius, x + w - radius, y),
            (x + radius, y + h - radius, x, y + h - radius),
            (x + w - radius, y + h - radius, x + w - radius, y + h - radius),
        )
        step = 1.0 / samples
        size = int(math.ceil(radius))
        for cx, cy, bx, by in corners:
            for py in range(size):
                row = int(math.floor(by)) + py
                if row < 0 or row >= self.height:
                    continue
                base = row * self.width * 4
                for px in range(size):
                    col = int(math.floor(bx)) + px
                    if col < 0 or col >= self.width:
                        continue
                    hits = 0
                    for sy in range(samples):
                        dy = (row + (sy + 0.5) * step) - cy
                        for sx in range(samples):
                            dx = (col + (sx + 0.5) * step) - cx
                            if dx * dx + dy * dy <= radius * radius:
                                hits += 1
                    if hits:
                        self._blend(base + col * 4, r, g, b, alpha * hits / (samples * samples))

    def draw_text(self, font, text, px, x, baseline_y, rgba=(255, 255, 255, 255)):
        """Draw `text` with its left edge at `x` and its baseline at y."""
        r, g, b, a = rgba
        alpha = a / 255.0
        scale = float(px) / font.units_per_em
        pen = float(x)
        for ch in text:
            gid = font.glyph_id(ch)
            gw, gh, gleft, gtop, cover = font.glyph_bitmap(ch, px)
            if gw and gh:
                ox = int(round(pen)) + gleft
                oy = int(round(baseline_y)) - gtop
                for row in range(gh):
                    py = oy + row
                    if py < 0 or py >= self.height:
                        continue
                    base = py * self.width * 4
                    crow = row * gw
                    for col in range(gw):
                        cov = cover[crow + col]
                        if cov <= 0.004:
                            continue
                        pxx = ox + col
                        if 0 <= pxx < self.width:
                            self._blend(base + pxx * 4, r, g, b, alpha * cov)
            pen += font.advance(gid) * scale
        return pen - x

    def to_png(self, path):
        raw = bytearray()
        stride = self.width * 4
        for row in range(self.height):
            raw.append(0)  # filter type: none
            raw.extend(self.buf[row * stride : (row + 1) * stride])
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
            fh.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 6, 0, 0, 0)))
            fh.write(_chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
            fh.write(_chunk(b"IEND", b""))
        return path


def _chunk(tag, payload):
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )

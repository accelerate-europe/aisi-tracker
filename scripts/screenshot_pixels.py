"""Decode and compare Chromium's 8-bit RGB/RGBA PNG screenshots without extra dependencies."""

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Pixels:
    width: int
    height: int
    rgba: bytes

    def at(self, x: int, y: int) -> bytes:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f'pixel ({x}, {y}) outside {self.width}x{self.height} screenshot')
        start = (y * self.width + x) * 4
        return self.rgba[start:start + 4]

    def crop(self, x: int, y: int, width: int, height: int) -> 'Pixels':
        self.at(x, y)
        self.at(x + width - 1, y + height - 1)
        rows = [self.rgba[(row * self.width + x) * 4:(row * self.width + x + width) * 4]
                for row in range(y, y + height)]
        return Pixels(width, height, b''.join(rows))


def decode_png(png: bytes) -> Pixels:
    """Support the non-interlaced truecolour formats emitted by Playwright; reject others."""
    if png[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG screenshot')
    offset, header, data, ended = 8, None, bytearray(), False
    while offset < len(png):
        if offset + 12 > len(png):
            raise ValueError('truncated PNG chunk')
        length, kind = struct.unpack_from('>I4s', png, offset)
        end = offset + 8 + length
        if end + 4 > len(png):
            raise ValueError('truncated PNG chunk')
        payload = png[offset + 8:end]
        if zlib.crc32(kind + payload) != struct.unpack_from('>I', png, end)[0]:
            raise ValueError('invalid PNG checksum')
        if kind == b'IHDR':
            if header is not None or offset != 8 or length != 13:
                raise ValueError('invalid PNG header')
            header = struct.unpack('>IIBBBBB', payload)
        elif kind == b'IDAT':
            data.extend(payload)
        elif kind == b'IEND':
            ended = True
            break
        elif kind in (b'PLTE', b'tRNS') or not kind[0] & 32:
            raise ValueError(f'unsupported PNG chunk {kind!r}')
        offset = end + 4
    if header is None or not ended:
        raise ValueError('incomplete PNG screenshot')
    width, height, depth, colour, compression, filtering, interlace = header
    if not width or not height or depth != 8 or colour not in (2, 6) or any((compression, filtering, interlace)):
        raise ValueError('expected an 8-bit non-interlaced RGB/RGBA PNG screenshot')
    channels = 3 if colour == 2 else 4
    stride = width * channels
    raw = zlib.decompress(data)
    if len(raw) != height * (stride + 1):
        raise ValueError('PNG scanline size differs from its header')
    previous, rgba = bytearray(stride), bytearray()
    for y in range(height):
        start = y * (stride + 1)
        filter_type = raw[start]
        if filter_type > 4:
            raise ValueError(f'unsupported PNG filter {filter_type}')
        row = bytearray(raw[start + 1:start + 1 + stride])
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            above = previous[x]
            upper_left = previous[x - channels] if x >= channels else 0
            if filter_type == 0:
                break
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            else:
                p = left + above - upper_left
                distances = (abs(p - left), abs(p - above), abs(p - upper_left))
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            row[x] = (row[x] + predictor) & 255
        if channels == 4:
            rgba.extend(row)
        else:
            for x in range(0, stride, 3):
                rgba.extend(row[x:x + 3])
                rgba.append(255)
        previous = row
    return Pixels(width, height, bytes(rgba))


CHANNEL_TOLERANCE = 2  # compositing rounds a layered ground by a step or so; text showing through differs by far more


def same_colour(a, b, tolerance: int = CHANNEL_TOLERANCE) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def differing_pixels(before: Pixels, after: Pixels, tolerance: int = CHANNEL_TOLERANCE) -> Iterator[tuple[int, int]]:
    """Yield coordinates of pixels differing by more than the channel tolerance."""
    if (before.width, before.height) != (after.width, after.height):
        raise ValueError('screenshot dimensions changed between stuck positions')
    if before.rgba == after.rgba:
        return
    for index in range(before.width * before.height):
        start = index * 4
        if not same_colour(before.rgba[start:start + 4], after.rgba[start:start + 4], tolerance):
            yield index % before.width, index // before.width


def pixel_difference(before: Pixels, after: Pixels, tolerance: int = CHANNEL_TOLERANCE) -> tuple[int, tuple[int, int, int, int] | None]:
    """Return the count of differing pixels and (left, top, right, bottom), with exclusive right/bottom."""
    count, left, top, right, bottom = 0, before.width, before.height, 0, 0
    for x, y in differing_pixels(before, after, tolerance):
        count += 1
        left, top, right, bottom = min(left, x), min(top, y), max(right, x + 1), max(bottom, y + 1)
    return count, ((left, top, right, bottom) if count else None)

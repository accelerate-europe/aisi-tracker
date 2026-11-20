"""PNG decoding and exact pixel comparisons, without launching a browser."""

import io
import struct
import zlib

import pytest
from PIL import Image

from scripts.screenshot_pixels import Pixels, decode_png, pixel_difference


def chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))


def png_rows(rows, *, width=2, colour=2, depth=8, interlace=0):
    header = struct.pack('>IIBBBBB', width, len(rows), depth, colour, 0, 0, interlace)
    compressed = zlib.compress(b''.join(bytes(row) for row in rows))
    # IDAT boundaries need not align with scanlines or compressed blocks.
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', compressed[:3])
            + chunk(b'IDAT', compressed[3:]) + chunk(b'IEND', b''))


@pytest.mark.parametrize('filtered', [
    [[0, 10, 20, 30, 40, 50, 60], [0, 70, 80, 90, 100, 110, 120]],
    [[1, 10, 20, 30, 30, 30, 30], [1, 70, 80, 90, 30, 30, 30]],
    [[2, 10, 20, 30, 40, 50, 60], [2, 60, 60, 60, 60, 60, 60]],
    [[3, 10, 20, 30, 35, 40, 45], [3, 65, 70, 75, 45, 45, 45]],
    [[4, 10, 20, 30, 30, 30, 30], [4, 60, 60, 60, 30, 30, 30]],
])
def test_all_png_filters_have_the_same_decoded_pixels(filtered):
    decoded = decode_png(png_rows(filtered))
    assert decoded == Pixels(2, 2, bytes([10, 20, 30, 255, 40, 50, 60, 255,
                                       70, 80, 90, 255, 100, 110, 120, 255]))


def test_paeth_uses_upper_left_and_wraps_negative_bytes():
    decoded = decode_png(png_rows([[4, 60, 60, 60, 30, 30, 30], [4, 226, 226, 226, 40, 40, 40]]))
    assert decoded.at(0, 1) == bytes([30, 30, 30, 255])
    assert decoded.at(1, 1) == bytes([100, 100, 100, 255])


def encode(image, **options):
    stream = io.BytesIO()
    image.save(stream, format='PNG', **options)
    return stream.getvalue()


@pytest.mark.parametrize('mode', ['RGB', 'RGBA'])
def test_decode_matches_independent_encoder(mode):
    image = Image.new(mode, (11, 9))
    image.putdata([tuple((x * (channel + 17) + y * 113) % 256 for channel in range(len(mode)))
                   for y in range(9) for x in range(11)])
    assert decode_png(encode(image)) == Pixels(11, 9, image.convert('RGBA').tobytes())


def test_compare_decoded_pixels_not_png_bytes_or_colour_type():
    image = Image.new('RGB', (8, 5), (250, 247, 242))
    before, after = encode(image, compress_level=0), encode(image.convert('RGBA'), compress_level=9)
    assert before != after
    assert pixel_difference(decode_png(before), decode_png(after)) == (0, None)


def test_count_pixels_once_and_report_exclusive_bounds():
    before = Image.new('RGBA', (8, 5), (250, 247, 242, 255))
    after = before.copy()
    after.putpixel((2, 1), (0, 0, 0, 0))
    after.putpixel((6, 3), (250, 247, 242, 250))
    assert pixel_difference(decode_png(encode(before)), decode_png(encode(after))) == (2, (2, 1, 7, 4))


def test_a_rounding_step_is_not_a_difference_but_faint_text_is():
    before = Image.new('RGBA', (4, 2), (243, 230, 218, 255))
    after = before.copy()
    after.putpixel((1, 0), (242, 231, 219, 255))  # compositing noise
    assert pixel_difference(decode_png(encode(before)), decode_png(encode(after))) == (0, None)
    after.putpixel((3, 1), (240, 230, 218, 255))  # three steps: faint content
    assert pixel_difference(decode_png(encode(before)), decode_png(encode(after))) == (1, (3, 1, 4, 2))


def test_crop_excludes_moving_body_reference_and_checks_bounds():
    pixels = Pixels(3, 3, bytes(range(36)))
    assert pixels.crop(1, 1, 2, 1) == Pixels(2, 1, bytes(range(16, 24)))
    with pytest.raises(ValueError, match='outside'):
        pixels.crop(2, 2, 2, 2)
    with pytest.raises(ValueError, match='dimensions changed'):
        pixel_difference(pixels, pixels.crop(0, 0, 1, 1))


@pytest.mark.parametrize('png, message', [
    (b'not png', 'not a PNG'),
    (png_rows([[0, 1, 2, 3, 4, 5, 6]])[:-1], 'truncated'),
    (png_rows([[0, 1, 2, 3, 4, 5, 6]])[:-12], 'incomplete'),
    (png_rows([[5, 1, 2, 3, 4, 5, 6]]), 'filter'),
    (png_rows([[0, 1, 2, 3]]), 'scanline size'),
    (png_rows([[0, 1, 2, 3]], colour=3), 'RGB/RGBA'),
    (png_rows([[0, 1, 2, 3]], depth=16), 'RGB/RGBA'),
    (png_rows([[0, 1, 2, 3]], interlace=1), 'RGB/RGBA'),
])
def test_unsupported_or_incomplete_png_fails_explicitly(png, message):
    with pytest.raises(ValueError, match=message):
        decode_png(png)


def test_checksum_failure_is_not_treated_as_a_pixel_difference():
    png = bytearray(png_rows([[0, 1, 2, 3, 4, 5, 6]]))
    png[16] ^= 1
    with pytest.raises(ValueError, match='checksum'):
        decode_png(png)

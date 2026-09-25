import struct

from {{pkg}}.core import fractal


def test_escape_time() -> None:
    assert fractal.escape_time(0.0, 0.0, 50) == 50  # inside the set
    assert fractal.escape_time(2.0, 2.0, 50) == 1  # escapes immediately


def test_render_rgb_size() -> None:
    raw = fractal.render_rgb(16, 8, 20)
    assert len(raw) == (16 * 3 + 1) * 8


def test_png_header() -> None:
    png = fractal.render_png(16, 8, 20)
    assert png.startswith(fractal.PNG_SIGNATURE)
    assert struct.unpack(">II", png[16:24]) == (16, 8)

"""CSS calculations from measured X keyframes and CSS easing specifications."""

import math
from decimal import ROUND_HALF_UP, Decimal


def fixed_two(value: float) -> float:
    """Number(value.toFixed(2)): decimal rounding of the exact binary64 input."""
    return float(
        Decimal.from_float(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def _hex_number(value: float) -> str:
    """Represent a nonnegative binary64 number in base 16 without a prefix."""
    numerator, denominator = value.as_integer_ratio()
    integer, remainder = divmod(numerator, denominator)
    fraction = ""
    while remainder:
        digit, remainder = divmod(remainder * 16, denominator)
        fraction += "0123456789abcdef"[digit]
    return format(integer, "x") + ("." + fraction if fraction else "")


def easing(control: tuple[float, float, float, float], progress: float) -> float:
    """Invert the monotone x polynomial, then evaluate its y polynomial.

    Only the active animation interval is needed for this observed X client.
    Coefficients are derived from the cubic Bernstein basis, not OSS code.
    """
    if progress <= 0 or progress >= 1:
        return max(0.0, min(1.0, progress))

    def coefficients(first: float, second: float) -> tuple[float, float, float]:
        return 1 - 3 * second + 3 * first, 3 * second - 6 * first, 3 * first

    x = coefficients(control[0], control[2])
    y = coefficients(control[1], control[3])

    def polynomial(values: tuple[float, float, float], t: float) -> float:
        return ((values[0] * t + values[1]) * t + values[2]) * t

    left, right = 0.0, 1.0
    for _ in range(40):
        t = (left + right) * 0.5
        if polynomial(x, t) > progress:
            right = t
        else:
            left = t
    return polynomial(y, (left + right) * 0.5)


def style_key(color: tuple[int, int, int], matrix: tuple[float, ...]) -> str:
    """X extracts CSS numbers, applies toFixed(2), converts to radix 16."""
    return "".join(
        _hex_number(abs(fixed_two(float(number)))).replace(".", "")
        for number in (*color, *matrix)
    )


def animation_key(row: tuple[int, ...], milliseconds: int) -> str:
    controls = tuple(
        fixed_two(value / 255 if position % 2 == 0 else value * 2 / 255 - 1)
        for position, value in enumerate(row[7:])
    )
    if len(controls) != 4 or not 0 <= milliseconds <= 4096:
        raise ValueError("Unsupported CSS animation input")
    progress = easing(
        (controls[0], controls[1], controls[2], controls[3]), milliseconds / 4096
    )
    color = tuple(
        math.floor(
            max(0, min(255, row[index] + (row[index + 3] - row[index]) * progress))
            + 0.5
        )
        for index in range(3)
    )
    angle = math.radians(math.floor(60 + row[6] * 300 / 255) * progress)
    cosine, sine = math.cos(angle), math.sin(angle)
    # Chromium's observed computed-style serialization has six significant digits.
    matrix = tuple(
        float(format(value, ".6g")) for value in (cosine, sine, -sine, cosine, 0, 0)
    )
    return style_key((color[0], color[1], color[2]), matrix)

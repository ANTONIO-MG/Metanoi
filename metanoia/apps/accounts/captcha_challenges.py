"""Custom CAPTCHA challenge for the sign-up form.

Single-digit **addition** only. The built-in ``math_challenge`` uses
``CAPTCHA_MATH_CHALLENGE_OPERATOR`` as the *display* symbol for multiplication,
so setting it to ``'+'`` showed "10+4=" with a stored answer of 40. Wired in via
``CAPTCHA_CHALLENGE_FUNCT``.
"""
import random


def add_challenge():
    """Return ``("a + b =", "<a+b>")``: the image text and the expected answer."""
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    return ('%d + %d =' % (a, b), str(a + b))

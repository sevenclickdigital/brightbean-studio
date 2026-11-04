"""Template filters for the billing screens.

Money is stored in integer cents everywhere in this app, and nothing in
the templates may divide — a template that does `{{ cents|divisibleby }}`
arithmetic is how a price ends up rendered as "5000" or "50.0". These
filters are the only conversion from the storage unit to the display
unit, for both money and bytes.
"""

from __future__ import annotations

from decimal import Decimal

from django import template
from django.utils.formats import number_format

register = template.Library()

# Bytes per GB as *billed*. Must match ``WorkspaceAddon.BYTES_PER_GB``
# (decimal 10^9, not binary 2^30): the customer buys what the price list
# calls a GB, so a quota of 10 GB has to read as 10, not 9.3.
BYTES_PER_GB = 1_000_000_000


@register.filter
def brl(cents):
    """Integer cents as a Brazilian-real amount, e.g. 5000 → ``50,00``.

    Returns only the number; the template supplies the ``R$`` so the
    currency symbol stays visible to a translator rather than being
    baked into a filter.
    """
    try:
        value = Decimal(int(cents)) / 100
    except (TypeError, ValueError):
        return ""
    # ``number_format`` honours the active locale, so pt-BR renders
    # 1.234,56 and en-US 1,234.56 from the same integer.
    return number_format(value, decimal_pos=2, use_l10n=True, force_grouping=True)


@register.filter
def gb(num_bytes):
    """Bytes as a short decimal-GB label, e.g. 10000000000 → ``10``.

    One decimal place only below 10 GB, where the difference between
    "0.5" and "0" actually matters; above that a whole number reads
    better in a meter caption.
    """
    try:
        value = int(num_bytes) / BYTES_PER_GB
    except (TypeError, ValueError):
        return ""
    if value >= 10 or value == int(value):
        return number_format(Decimal(value).quantize(Decimal("1")), decimal_pos=0, use_l10n=True)
    return number_format(Decimal(value).quantize(Decimal("0.1")), decimal_pos=1, use_l10n=True)


@register.filter
def meter_percent(value):
    """A percentage clamped to 0–100 for a CSS width.

    ``WorkspaceStorageStatus.percent_used`` is deliberately uncapped so
    the numbers stay honest when a quota is lowered below current usage;
    a progress bar wider than its track is not, so the clamp happens
    here rather than in the domain object.
    """
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, round(percent)))

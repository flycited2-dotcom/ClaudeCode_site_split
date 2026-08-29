"""Классификация товара по title — считается один раз при синке (apps/sync/*
и apps/sync/management/commands/backfill_product_kind.py), а не пересчитывается
regex'ом на каждый catalog-запрос.

До этого NON_RETAIL_Q/MULTI_SPLIT_BLOCK_Q (apps/catalog/filters.py)
применялись через .exclude() в каждом view/sitemap/quiz_logic — full-table
iregex scan на каждый запрос, и легко забыть добавить .exclude() в одном из
мест (баг с sitemap.py, найден 2026-07-01: индексировал аксессуары, потому
что там не было .exclude(NON_RETAIL_Q), хотя в catalog/views.py было).

Используют те же regex-паттерны (единственный источник истины — filters.py),
просто применяют их один раз к title при записи в Product.kind.
"""
import re

from .filters import MULTI_SPLIT_BLOCK_PATTERN, NON_RETAIL_PATTERN
from .models import Product

_multi_split_re = re.compile(MULTI_SPLIT_BLOCK_PATTERN, re.IGNORECASE)
_non_retail_re = re.compile(NON_RETAIL_PATTERN, re.IGNORECASE)


# Самостоятельные товары, чьё название начинается со слова из денилиста.
# «Очиститель воздуха приточный» (бризеры Ballu ONEAIR и т.п.) — полноценный
# прибор, но слово «очистител» стоит в NON_RETAIL_PATTERN как расходник
# («очиститель системы», чистящее средство). Проверяем такие раньше денилиста.
RETAIL_OVERRIDE_PATTERN = (
    r'очистител\w*\s+воздуха?\s+приточн|приточн\w*\s+очистител'
)
_retail_override_re = re.compile(RETAIL_OVERRIDE_PATTERN, re.IGNORECASE)


def classify_title(title):
    """Возвращает Product.KIND_* по названию товара."""
    if not title:
        return Product.KIND_SPLIT_SYSTEM
    if _multi_split_re.search(title):
        return Product.KIND_MULTI_SPLIT_BLOCK
    if _retail_override_re.search(title):
        return Product.KIND_SPLIT_SYSTEM
    if _non_retail_re.search(title):
        return Product.KIND_ACCESSORY
    return Product.KIND_SPLIT_SYSTEM

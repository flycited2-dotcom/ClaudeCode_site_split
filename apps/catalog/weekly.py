"""Выбор «хита недели» на главной.

Карточка справа в первом экране раньше показывала просто первый товар из
подборки, а порядок там детерминированный (остаток, потом цена). В итоге одна
и та же XIGMA висела с начала лета — владелец заметил 2026-08-29.

Берём тот же список кандидатов, но крутим его по номеру недели: внутри недели
выбор стабилен (не прыгает при каждом обновлении страницы и переживает кэш),
а в понедельник сменяется сам собой.
"""
import hashlib
from datetime import date, datetime


def week_key(today=None):
    """Ключ недели ISO: '2026-35'. Меняется в понедельник."""
    day = today or date.today()
    year, week, _ = day.isocalendar()
    return f'{year}-{week:02d}'


def pick_weekly(items, *, salt='', today=None):
    """Стабильный в пределах недели выбор одного элемента из списка.

    `salt` разводит разные места на странице: с одним и тем же списком
    «хит недели» и, скажем, «выбор редакции» не покажут один товар.
    Пустой список — None, список из одного элемента — он же.
    """
    items = list(items)
    if not items:
        return None
    digest = hashlib.md5(f'{week_key(today)}|{salt}'.encode('utf-8')).hexdigest()
    return items[int(digest, 16) % len(items)]


def hour_key(now=None):
    """Ключ часа: '2026-08-30-14'. Меняется каждый час."""
    moment = now or datetime.now()
    return moment.strftime('%Y-%m-%d-%H')


def pick_hourly(items, *, salt='', now=None):
    """Стабильный в пределах часа выбор одного элемента.

    Ассортимент вырос до 14 тысяч позиций, и недельный шаг стал слишком
    редким — витрина почти не обновляется. Час: посетитель в пределах сессии
    видит одно и то же, а за день карточка сменится два десятка раз.
    """
    items = list(items)
    if not items:
        return None
    digest = hashlib.md5(f'{hour_key(now)}|{salt}'.encode('utf-8')).hexdigest()
    return items[int(digest, 16) % len(items)]

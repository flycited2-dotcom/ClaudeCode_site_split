"""Признак «тепловой насос» у товара — считается один раз при синке.

Тепловой насос воздух-воздух — это маркетинговый ярлык на инверторной сплит-системе
с низкотемпературным обогревом, а не отдельный тип оборудования. Поставщики его почти
не проставляют (характеристика «Тепловой насос» приходит у 796 товаров Бриза, значение
«да» — у 5), зато диапазон работы на обогрев отдают все трое — под разными названиями
и в разных форматах.

Значения хранятся строками, поэтому разбирать их регуляркой на каждый запрос каталога —
полный скан таблицы. Вместо этого результат пишется в Product.heating_min_temp при синке,
как это сделано для Product.kind (см. classify.py).
"""
import re

# Названия характеристики у поставщиков (разведка прода 2026-08-28,
# Профконд добавлен 2026-08-30).
# При смене названия у поставщика товары тихо выпадут из подборки — следить
# по счётчику в команде backfill_heating.
HEATING_SPEC_TITLES = (
    'Рабочие температурные границы наружного воздуха (нагрев)',   # Бриз
    'Диапазон рабочих температур, нагрев, °C',                     # Daichi
    'Мин. рабочая температура воздуха для внешнего блока',         # Rusklimat
    'Нижний диапазон рабочих температурных границ, нагрев, °C',    # Профконд (JAC)
)

# Характеристика Бриза с прямым ответом «да»/«нет».
HEAT_PUMP_SPEC_TITLE = 'Тепловой насос'

# Порог подборки: машина, которая греет до -20 и ниже, продаётся как тепловой насос.
HEAT_PUMP_THRESHOLD = -20

# Пороги фасеты внутри подборки.
HEATING_THRESHOLDS = (-20, -25, -30)

# Минус: ASCII либо юникодный U+2212. Тире-разделители диапазона («-20 – +24»)
# не должны читаться как знак числа, поэтому ищем минус, приклеенный к цифрам.
_MINUS = r'[-−]'
_NEG_RE = re.compile(rf'{_MINUS}\s?(\d+)')


def parse_min_heating_temp(value):
    """'-20 ~ +24' / '-25~30' / '-15' / '−20 ~ +24' -> -20 / -25 / -15.

    Возвращает первое отрицательное число строки — нижнюю границу работы
    на обогрев. None, если отрицательных чисел нет (машина греет только
    в плюс) или строка пустая/мусорная. Ничего не бросает.
    """
    if not value:
        return None
    match = _NEG_RE.search(str(value))
    if not match:
        return None
    try:
        return -int(match.group(1))
    except (TypeError, ValueError):
        return None


def min_heating_temp_for(product):
    """Минимальная (самая холодная) граница обогрева среди характеристик товара.

    У товара может быть две записи от разных источников — берём наименьшую.
    None, если ни одна характеристика не распозналась.
    """
    values = (
        product.tech_values
        .filter(spec__title__in=HEATING_SPEC_TITLES)
        .values_list('value', flat=True)
    )
    temps = [t for t in (parse_min_heating_temp(v) for v in values) if t is not None]
    return min(temps) if temps else None


def _declared_by_spec(product):
    """True, если у товара характеристика «Тепловой насос» со значением «да»."""
    values = (
        product.tech_values
        .filter(spec__title=HEAT_PUMP_SPEC_TITLE)
        .values_list('value', flat=True)
    )
    return any(str(v).strip().lower() in ('да', 'yes', 'true') for v in values)


def apply_heating_fields(product, declared=False):
    """Проставляет heating_min_temp и is_heat_pump товару. True, если что-то изменилось.

    `declared` — поставщик объявил товар тепловым насосом вне характеристик
    (у Rusklimat это название категории). Вызывается синками ПОСЛЕ записи
    ProductTech: характеристики пишутся отдельным шагом после создания товара.
    """
    temp = min_heating_temp_for(product)
    is_pump = bool(declared) or _declared_by_spec(product)

    changed = (product.heating_min_temp != temp) or (product.is_heat_pump != is_pump)
    if changed:
        product.heating_min_temp = temp
        product.is_heat_pump = is_pump
        product.save(update_fields=['heating_min_temp', 'is_heat_pump'])
    return changed

import hashlib

from django.core.cache import cache
from django.db.models import Count, Q

from .models import Brand
from .filters import (
    BTU_VALUES, AREA_CHOICES, INVERTER_CHOICES, COLOR_CHOICES, HEATING_CHOICES,
    ProductFilter, _btu_q, _area_q, _color_q, _heating_q,
)

# Счётчик каждого значения фасеты считается отдельным запросом (11 на BTU, 6 на
# площадь, 4 на цвет, 3 на обогрев, 2 на инвертор + пересборка queryset на
# группу) — 54 запроса к БД и ~1 с на страницу каталога (замер на проде
# 2026-08-29; посетители не дожидались и жали кнопку повторно). Счётчики зависят
# только от данных синка, который идёт раз в час, поэтому результат кэшируется.
FACETS_CACHE_TTL = 600  # 10 минут: после синка цифры обновятся с этой задержкой


def _facets_cache_key(get_data, scope):
    """Ключ кэша: набор фильтров в URL + область выдачи.

    `scope` обязателен: каталог и подборка считают фасеты по разным выборкам,
    и без него страница подборки получила бы счётчики каталога.
    """
    params = sorted((k, sorted(v)) for k, v in get_data.lists() if k not in ('page', 'ordering'))
    raw = f'{scope}|{params}'
    return 'catalog:facets:' + hashlib.md5(raw.encode('utf-8')).hexdigest()


def invalidate_facets_cache():
    """Сбрасывает кэш фасет — вызывается синками после обновления каталога.

    Без этого счётчики в сайдбаре отставали бы до FACETS_CACHE_TTL после
    каждого синка. Работает и для динамических фасет (общий префикс ключа).
    """
    try:
        cache.delete_pattern('catalog:*facets:*')      # django_redis
    except AttributeError:
        cache.clear()                                   # бэкенд без delete_pattern


def _apply_filters_excluding(get_data, exclude_key, base_qs):
    """Re-apply ProductFilter on base_qs, dropping `exclude_key` from query data
    so the resulting queryset shows what *other* filters constrain to.
    """
    data = get_data.copy()
    # Pop both single-value and list-style keys
    while exclude_key in data:
        del data[exclude_key]
    return ProductFilter(data, queryset=base_qs).qs


def compute_facets(get_data, base_qs, scope='catalog'):
    """Return facet groups (with counts) for the catalog sidebar.

    Each group's count is computed against `base_qs` filtered by every other
    active group except itself. Selected entries with count=0 are kept so the
    user can see and un-tick them.

    Результат кэшируется на FACETS_CACHE_TTL — см. комментарий у константы.
    `scope` разделяет кэш каталога и подборок (у них разные base_qs).
    """
    cache_key = _facets_cache_key(get_data, scope)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    selected_brand_ids = {str(v) for v in get_data.getlist('brand')}
    selected_btu = set(get_data.getlist('btu'))
    selected_area = set(get_data.getlist('area'))
    selected_inv = set(get_data.getlist('inverter'))
    selected_color = set(get_data.getlist('color'))
    selected_heating = set(get_data.getlist('heating'))

    qs_no_brand    = _apply_filters_excluding(get_data, 'brand', base_qs)
    qs_no_btu      = _apply_filters_excluding(get_data, 'btu', base_qs)
    qs_no_area     = _apply_filters_excluding(get_data, 'area', base_qs)
    qs_no_inverter = _apply_filters_excluding(get_data, 'inverter', base_qs)
    qs_no_color    = _apply_filters_excluding(get_data, 'color', base_qs)
    qs_no_heating  = _apply_filters_excluding(get_data, 'heating', base_qs)

    # --- Brand: single GROUP BY query --------------------------------------
    brand_counts = dict(
        qs_no_brand.values_list('brand_id').annotate(n=Count('id'))
    )
    visible_brand_ids = {bid for bid, n in brand_counts.items() if n > 0}
    visible_brand_ids.update(int(b) for b in selected_brand_ids if b.isdigit())
    brand_qs = Brand.objects.filter(id__in=visible_brand_ids).order_by('title')

    brand_facet = []
    for b in brand_qs:
        brand_facet.append({
            'value':    str(b.id),
            'label':    b.title,
            'count':    brand_counts.get(b.id, 0),
            'selected': str(b.id) in selected_brand_ids,
        })

    # --- BTU: 11 individual counts ----------------------------------------
    btu_facet = []
    for code in BTU_VALUES:
        n = qs_no_btu.filter(_btu_q([code])).count()
        if n == 0 and code not in selected_btu:
            continue
        btu_facet.append({
            'value':    code,
            'label':    f'{int(code)} 000 BTU',
            'count':    n,
            'selected': code in selected_btu,
        })

    # --- Area (м²): 6 counts -------------------------------------------------
    area_facet = []
    for code, label in AREA_CHOICES:
        n = qs_no_area.filter(_area_q([code])).count()
        if n == 0 and code not in selected_area:
            continue
        area_facet.append({
            'value':    code,
            'label':    label,
            'count':    n,
            'selected': code in selected_area,
        })

    # --- Inverter: 2 counts ------------------------------------------------
    inv_q = Q(title__iregex=r'инвертор|inverter')
    inv_facet = [
        {
            'value':    'inverter',
            'label':    'Инвертор',
            'count':    qs_no_inverter.filter(inv_q).count(),
            'selected': 'inverter' in selected_inv,
        },
        {
            'value':    'onoff',
            'label':    'Он-офф',
            'count':    qs_no_inverter.exclude(inv_q).count(),
            'selected': 'onoff' in selected_inv,
        },
    ]

    # --- Color: 4 counts ---------------------------------------------------
    color_facet = []
    for key, label in COLOR_CHOICES:
        n = qs_no_color.filter(_color_q([key])).count()
        if n == 0 and key not in selected_color:
            continue
        color_facet.append({
            'value':    key,
            'label':    label,
            'count':    n,
            'selected': key in selected_color,
        })

    # --- Heating (обогрев до -20/-25/-30): 3 counts ------------------------
    heating_facet = []
    for code, label in HEATING_CHOICES:
        n = qs_no_heating.filter(_heating_q([code])).count()
        if n == 0 and code not in selected_heating:
            continue
        heating_facet.append({
            'value':    code,
            'label':    label,
            'count':    n,
            'selected': code in selected_heating,
        })

    result = {
        'brand':    brand_facet,
        'btu':      btu_facet,
        'area':     area_facet,
        'inverter': inv_facet,
        'color':    color_facet,
        'heating':  heating_facet,
    }
    cache.set(cache_key, result, FACETS_CACHE_TTL)
    return result

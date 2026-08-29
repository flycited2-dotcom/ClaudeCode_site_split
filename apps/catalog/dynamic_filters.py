"""Динамические фильтры по TechSpec.is_filter=True.

В отличие от filters.py (фасеты захардкожены в коде — brand/btu/color/...),
здесь набор фасет зависит от данных: какие TechSpec с is_filter=True
привязаны к выбранной категории (флаг приходит из Breez API, см.
apps/sync/breeze_tech.py). Позволяет добавлять новые фильтры (Wi-Fi, класс
энергоэффективности и т.п.) без правки кода — просто пометив характеристику
в админке.

Разные источники синка (Breez/Rusklimat/Daichi) создают СВОЙ TechSpec с
одинаковым title вместо переиспользования одной записи — например «Серия»
существует как 3+ разных spec_id (обнаружено 2026-07-02: 3 разных spec для
«Серия» вместе покрывали меньше товаров, чем одно нормальное поле
Product.series). Поэтому специфики группируются по title, а не по spec_id.

URL: ?tech=<group_key>:<value> (повторяется для нескольких значений/групп).
group_key — id любого spec'а группы (используется min() для стабильности).
Внутри одной группы — OR (любое из выбранных значений), между группами —
AND. Значения сравниваются регистронезависимо (iexact) — «Wi-Fi ready» и
«Wi-Fi Ready» считаются одним и тем же значением.
"""
import hashlib

from django.core.cache import cache
from django.db.models import Count, Min, Q
from django.db.models.functions import Lower

from .models import ProductTech, TechSpec

# Специфики с числом уникальных значений выше этого порога почти наверняка
# непрерывные/числовые (площадь, длина трассы и т.п.), а не категориальные —
# живой пример: «Эффективен для помещений площадью до» отдаёт 20, 20.5, 21,
# 21.4... — десятки почти одинаковых значений. Чекбокс-панель для них
# бесполезна (нужен range-slider — отдельная задача), поэтому такие
# специфики просто не показываем как фасету.
_MAX_FACET_OPTIONS = 15

# Специфика-дубль уже существующей захардкоженной фасеты (filters.py) — живой
# пример: TechSpec «Бренд» дублирует ProductFilter.brand (тот же смысл, но по
# сырым текстовым значениям вместо Brand FK). Две панели «Бренд» только
# путают. Сверяется по title без учёта регистра.
#
# «Серия» намеренно НЕ в списке исключений: несмотря на схожесть с «Бренд»,
# у неё нет отдельной нормальной фасеты — оказалось, что после объединения
# дублей (см. _spec_groups) в ней 498 уникальных значений (проверено на
# проде 2026-07-02), т.е. это свободный текст модельного ряда, а не
# категориальный признак. _MAX_FACET_OPTIONS сам скроет её как непригодную
# для чекбокс-панели — see filter_search в filters.py, где title/series
# ищутся текстом.
_EXCLUDED_TITLES = {'бренд'}


def parse_tech_params(get_data):
    """{group_key: {value, ...}} из повторяющихся ?tech=<group_key>:<value>."""
    grouped = {}
    for raw in get_data.getlist('tech'):
        key_str, _, value = raw.partition(':')
        if not value:
            continue
        try:
            group_key = int(key_str)
        except ValueError:
            continue
        grouped.setdefault(group_key, set()).add(value)
    return grouped


def _spec_groups(category):
    """[(group_key, title, [spec_id, ...])] — специфики сгруппированы по title.

    Показываем: глобальные (category=None) всегда + специфичные для выбранной
    категории, если она выбрана (см. compute_tech_facets). group_key —
    минимальный spec_id в группе, стабильный при неизменных данных.
    """
    spec_filter = Q(category__isnull=True)
    if category:
        spec_filter |= Q(category=category)
    specs = TechSpec.objects.filter(spec_filter, is_filter=True).order_by('order', 'id')

    by_title = {}
    order = []
    for spec in specs:
        key = spec.title.strip().lower()
        if key in _EXCLUDED_TITLES:
            continue
        if key not in by_title:
            by_title[key] = {'title': spec.title.strip(), 'spec_ids': []}
            order.append(key)
        by_title[key]['spec_ids'].append(spec.id)

    return [
        (min(by_title[k]['spec_ids']), by_title[k]['title'], by_title[k]['spec_ids'])
        for k in order
    ]


def _resolve_group_spec_ids(group_key):
    """group_key (spec_id) -> все spec_id с тем же title (может быть только он сам)."""
    anchor = TechSpec.objects.filter(pk=group_key).values_list('title', flat=True).first()
    if not anchor:
        return [group_key]
    return list(TechSpec.objects.filter(title__iexact=anchor).values_list('id', flat=True))


def apply_tech_filters(get_data, qs, exclude_group_key=None):
    """Накладывает выбранные tech-фильтры на qs. AND между группами, OR внутри.

    Значения сравниваются регистронезависимо (iexact).
    """
    grouped = parse_tech_params(get_data)
    applied = False
    for group_key, values in grouped.items():
        if group_key == exclude_group_key:
            continue
        spec_ids = _resolve_group_spec_ids(group_key)
        value_q = Q()
        for v in values:
            value_q |= Q(tech_values__value__iexact=v)
        qs = qs.filter(value_q, tech_values__spec_id__in=spec_ids)
        applied = True
    return qs.distinct() if applied else qs


# Каждая группа характеристик — отдельный GROUP BY по catalog_producttech:
# 20 запросов на страницу каталога (замер на проде 2026-08-29). Значения зависят
# только от данных синка, поэтому результат кэшируется — как и статические
# фасеты (см. apps/catalog/facets.FACETS_CACHE_TTL).
TECH_FACETS_CACHE_TTL = 600


def _tech_facets_cache_key(get_data, category, scope):
    params = sorted((k, sorted(v)) for k, v in get_data.lists() if k not in ('page', 'ordering'))
    raw = f'{scope}|{category.pk if category else None}|{params}'
    return 'catalog:techfacets:' + hashlib.md5(raw.encode('utf-8')).hexdigest()


def compute_tech_facets(get_data, category, qs, scope='catalog'):
    """qs — уже отфильтрован по всем статическим ProductFilter-полям (включая
    category), но ДО tech-фильтров.

    Результат кэшируется на TECH_FACETS_CACHE_TTL; `scope` разделяет кэш
    каталога и подборок — у них разные выборки товаров.
    """
    cache_key = _tech_facets_cache_key(get_data, category, scope)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    grouped_selected = parse_tech_params(get_data)
    groups = _spec_groups(category)

    result = []
    for group_key, title, spec_ids in groups:
        qs_excl = apply_tech_filters(get_data, qs, exclude_group_key=group_key)
        rows = list(
            ProductTech.objects
            .filter(product__in=qs_excl, spec_id__in=spec_ids)
            .annotate(norm_value=Lower('value'))
            .values('norm_value')
            .annotate(n=Count('product', distinct=True), sample=Min('value'))
            .order_by('-n')
        )
        if len(rows) > _MAX_FACET_OPTIONS:
            continue
        selected_values = grouped_selected.get(group_key, set())
        selected_norm = {v.lower() for v in selected_values}
        options = []
        for row in rows:
            val = row['sample']
            n = row['n']
            is_selected = row['norm_value'] in selected_norm
            if n == 0 and not is_selected:
                continue
            options.append({'value': val, 'count': n, 'selected': is_selected})
        if options:
            result.append({'spec_id': group_key, 'title': title, 'options': options})

    cache.set(cache_key, result, TECH_FACETS_CACHE_TTL)
    return result

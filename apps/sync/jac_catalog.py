"""Четвёртый поставщик — Профконд (портал b2b-jac.com): MDV, Mitsubishi Heavy,
EUROKLIMAT, THAICON.

API у портала нет. Данные готовит отдельный скрапер (проект `osatakti_mdv_b2b`)
и заливает на сервер тремя файлами:

    jac_stock_latest.json   — позиции: артикул, бренд, серия, цена, остатки
    jac_specs_latest.json   — характеристики карточек (артикул → {название: значение})
    jac_photos_latest.json  — фото серий ({бренд: {серия: URL}})

Тот же набор читает Telegram-бот остатков, поэтому правила отбора здесь
сознательно совпадают с его `jac.py`: только кондиционеры нужных категорий,
без мультисплита и аксессуаров.

Файла нет / он битый → синк возвращает нули и НЕ трогает уже загруженные
товары: устаревшая выгрузка лучше, чем пустой раздел каталога.
"""
import hashlib
import json
import logging
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.text import slugify

from apps.catalog.classify import classify_title
from apps.catalog.models import Brand, Category, Product, ProductImage, ProductTech, TechSpec
from apps.sync.warehouse_stock import write_warehouse_stocks

logger = logging.getLogger(__name__)

JAC_SOURCE = 'jac'

# Категория портала → title нашей мастер-категории. Мультисплит-системы и
# аксессуары не берём: на витрине мульти-блоки скрыты как отдельный kind, а
# аксессуары розничному покупателю в каталоге кондиционеров не нужны.
_CATEGORY_MAP = {
    'Бытовые сплит-системы': 'Бытовые сплит-системы',
    'Полупромышленные системы': 'Полупромышленные сплит-системы',
    'Мобильные': 'Мобильные кондиционеры',
    'Мобильные кондиционеры': 'Мобильные кондиционеры',
}

# Тип оборудования для названия карточки: у портала `name` — это артикул
# («EKSA-70HN / EKOA-70HN»), человеческого названия в выгрузке нет.
_TYPE_BY_CATEGORY = {
    'Бытовые сплит-системы': 'Сплит-система',
    'Полупромышленные системы': 'Полупромышленная сплит-система',
    'Мобильные': 'Мобильный кондиционер',
    'Мобильные кондиционеры': 'Мобильный кондиционер',
}

# Характеристики портала под теми же названиями, что у остальных поставщиков —
# иначе мощность не попадёт в btu_calc (apps/catalog/btu.py ищет по названию),
# а фасета обогрева не увидит температуру (apps/catalog/heating.py).
_SPEC_TITLE_MAP = {
    'Холод, кВт': 'Холодопроизводительность, кВт',
    'Тепло, кВт': 'Теплопроизводительность, кВт',
}

# Склады портала. «Москва (ОП АЯК - Крым)» — это московский склад с
# отгрузкой в Крым; слово «Крым» в названии обмануло бы _CRIMEA_RE из
# warehouse_stock.py и товар «под заказ» показался бы как местный остаток.
_CRIMEA_ATTR = 'Крым'
_MAINLAND_ATTR = 'Москва (ОП АЯК - Крым)'
_CRIMEA_WAREHOUSE = 'Симферополь'
_MAINLAND_WAREHOUSE = 'Москва (ОП АЯК)'

_MORE_RE = re.compile(r'больше\s*(\d+)', re.IGNORECASE)
_NUM_RE = re.compile(r'-?\d+(?:[.,]\d+)?')


def _load_json(path, expect):
    """Читает JSON по пути. Любая проблема → пустой `expect()`, без исключения."""
    if not path:
        return expect()
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning('JAC: не прочитан %s: %s', path, exc)
        return expect()
    if not isinstance(data, type(expect())):
        logger.warning('JAC: неожиданная структура в %s: %s', path, type(data).__name__)
        return expect()
    return data


def _qty(raw):
    """'12' / 'Больше 100' / '' → int. «Больше N» — это минимум N."""
    if raw is None:
        return 0
    text = str(raw).replace('\xa0', ' ').strip()
    if not text:
        return 0
    more = _MORE_RE.search(text)
    if more:
        return int(more.group(1))
    match = _NUM_RE.search(text.replace(' ', ''))
    if not match:
        return 0
    try:
        return max(0, int(float(match.group(0).replace(',', '.'))))
    except (TypeError, ValueError):
        return 0


def _price(raw):
    """37280.0 / '71 700 ₽' → Decimal. Пусто/мусор → None."""
    if raw is None or raw == '':
        return None
    text = str(raw).replace('\xa0', ' ').replace(' ', '').replace(' ', '')
    match = _NUM_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(',', '.'))
    except (InvalidOperation, ValueError):
        return None


NC_CODE_MAX = 50


def _hashed_nc_code(article):
    """Фолбэк-ключ. Считается от полного неизменённого артикула."""
    digest = hashlib.md5(article.encode('utf-8')).hexdigest()[:16]
    return f'jac-{digest}'


def _nc_code(article, taken=frozenset()):
    """Ключ товара: у портала нет NC-кода, поэтому берём сам артикул.

    Читаемый ключ важен за пределами сайта: avito-bridge строит из него
    supplier_sku («jac:MDSA-36HRN1 / MDOA-36HN1») и показывает его в
    диагностике и выгрузках — по хешу модель не опознать.

    Хешируем только когда иначе нельзя: артикул не влезает в поле (50
    символов — таких 23 из 441) либо такой nc_code уже занят другим
    поставщиком. Затереть чужой товар куда хуже, чем нечитаемый ключ.
    """
    if len(article) <= NC_CODE_MAX and article not in taken:
        return article
    return _hashed_nc_code(article)


def _identity_from(row, specs_entry, existing):
    """(бренд, серия) для товара — с защитой от испорченного снимка.

    2026-08-30: портал сменил разметку, и в свежих выгрузках brand и series
    пустые у ВСЕХ позиций (581 из 581). Пустышка затёрла бы бренд у уже
    заведённых карточек, а без бренда рассыпается и название товара, и
    фильтры, и Vendor в фиде Авито.

    Порядок доверия: снимок → файл характеристик → то, что уже в базе.
    """
    brand = (row.get('brand') or '').strip()
    series = (row.get('series') or '').strip()

    if not brand:
        brand = ((specs_entry or {}).get('brand') or '').strip()
    if not series:
        series = ((specs_entry or {}).get('series') or '').strip()

    if existing is not None:
        if not brand and existing.brand_id:
            brand = existing.brand.title
        if not series and existing.series:
            series = existing.series

    return brand, series


def _get_or_create_brand(title):
    if not title:
        return None
    brand = Brand.objects.filter(title__iexact=title).first()
    if brand:
        return brand
    slug = slugify(title, allow_unicode=True) or f'brand-jac-{title[:30]}'
    if Brand.objects.filter(slug=slug).exists():
        slug = f'{slug}-jac'
    return Brand.objects.create(title=title, slug=slug)


def _build_title(brand_title, category_title, series, article):
    """«EUROKLIMAT Сплит-система Alba EKSA-70HN / EKOA-70HN».

    Бренд идёт в title, как у Daichi: шаблон карточки печатает только
    product.title, отдельного места под бренд в заголовке нет.
    """
    type_title = _TYPE_BY_CATEGORY.get(category_title, '')
    parts = [p for p in (brand_title, type_title, series, article) if p]
    return ' '.join(parts)[:500]


def _build_slug(brand_title, article, nc_code):
    parts = [
        slugify(brand_title, allow_unicode=True) if brand_title else '',
        slugify(article, allow_unicode=True) if article else '',
    ]
    base = '-'.join(p for p in parts if p) or nc_code
    return f'{base}-{nc_code[-8:]}'[:500]


def _photo_url(photos, brand_title, series):
    """URL фото серии. Локальные имена файлов (THAICON) пропускаем — на сайте
    нужен адрес, а не путь на диске скрапера."""
    if not photos or not brand_title or not series:
        return None
    by_brand = photos.get(brand_title)
    if not isinstance(by_brand, dict):
        # У скрапера регистр бренда в фото-файле бывает другой.
        for key, value in photos.items():
            if key.lower() == brand_title.lower() and isinstance(value, dict):
                by_brand = value
                break
        else:
            return None
    target = series.strip().lower()
    for key, url in by_brand.items():
        if key.strip().lower() == target and isinstance(url, str) and url.startswith('http'):
            return url
    return None


def _sync_images(product, url):
    """Одно фото серии на товар. Пустой URL — не трогаем уже загруженное."""
    if not url:
        return 0
    existing = list(product.images.values_list('url', flat=True))
    if existing == [url]:
        return 0
    product.images.all().delete()
    ProductImage.objects.create(product=product, url=url, order=0)
    return 1


def _sync_tech_specs(product, characteristics, category, tech_cache):
    """Пишет характеристики карточки. Возвращает количество записанных пар."""
    if not isinstance(characteristics, dict) or not characteristics:
        return 0

    ProductTech.objects.filter(product=product).delete()
    to_create = []
    seen = set()

    for raw_title, raw_value in characteristics.items():
        title = _SPEC_TITLE_MAP.get(str(raw_title).strip(), str(raw_title).strip())
        if not title or raw_value is None or str(raw_value).strip() == '':
            continue
        if title in seen:
            continue
        seen.add(title)

        spec = tech_cache.get(title)
        if spec is None:
            spec = TechSpec.objects.filter(
                title=title, breez_id__isnull=True, external_uuid__isnull=True,
            ).first()
            if spec is None:
                spec = TechSpec.objects.create(title=title, category=category)
            tech_cache[title] = spec
        to_create.append(ProductTech(product=product, spec=spec, value=str(raw_value).strip()[:500]))

    if to_create:
        ProductTech.objects.bulk_create(to_create)
    return len(to_create)


def sync_catalog():
    """Загружает выгрузку скрапера в каталог. Возвращает словарь-счётчик."""
    from django.conf import settings as dj_settings

    stock_path = getattr(dj_settings, 'JAC_STOCK_JSON', '')
    rows = _load_json(stock_path, list)
    if not rows:
        logger.warning('JAC sync: нет данных (%s) — каталог не тронут', stock_path or 'путь не задан')
        return {'created': 0, 'updated': 0, 'skipped_no_category': 0,
                'skipped_no_article': 0, 'deactivated': 0,
                'images_synced': 0, 'specs_synced': 0}

    specs = _load_json(getattr(dj_settings, 'JAC_SPECS_JSON', ''), dict)
    photos = _load_json(getattr(dj_settings, 'JAC_PHOTOS_JSON', ''), dict)

    categories = {}
    for source_title, target_title in _CATEGORY_MAP.items():
        category = Category.objects.filter(
            title__iexact=target_title, sync_enabled=True,
        ).first()
        if category:
            categories[source_title] = category
        else:
            logger.warning('JAC sync: нет включённой категории «%s»', target_title)

    # nc_code уникален по всей таблице: артикул портала не должен затереть
    # товар другого поставщика с таким же кодом.
    taken_nc_codes = set(
        Product.objects.exclude(source=JAC_SOURCE).values_list('nc_code', flat=True)
    )

    created = updated = skipped_no_category = skipped_no_article = 0
    brand_recovered = brand_missing = 0
    images_synced = specs_synced = 0
    seen_nc_codes = set()
    tech_cache = {}
    touched = []

    with transaction.atomic():
        for row in rows:
            if not isinstance(row, dict):
                continue

            article = (row.get('article') or row.get('name') or '').strip()
            if not article:
                skipped_no_article += 1
                continue

            category = categories.get((row.get('category') or '').strip())
            if category is None:
                skipped_no_category += 1
                continue

            attributes = row.get('attributes') or {}

            nc_code = _nc_code(article, taken_nc_codes)
            if nc_code in seen_nc_codes:
                continue
            seen_nc_codes.add(nc_code)

            spec_entry = specs.get(article) or {}
            existing = Product.objects.filter(nc_code=nc_code).first()
            brand_title, series = _identity_from(row, spec_entry, existing)
            if not (row.get('brand') or '').strip():
                if brand_title:
                    brand_recovered += 1
                else:
                    brand_missing += 1
            brand = _get_or_create_brand(brand_title)
            brand_title = brand.title if brand else ''

            title = _build_title(brand_title, (row.get('category') or '').strip(), series, article)
            slug = _build_slug(brand_title, article, nc_code)

            product, is_new = Product.objects.update_or_create(
                nc_code=nc_code,
                defaults={
                    'articul': article[:200],
                    'category': category,
                    'brand': brand,
                    'series': series[:255],
                    'title': title,
                    'slug': slug,
                    'price_wholesale': _price(row.get('price')),
                    'ric': _price(attributes.get('РРЦ')),
                    'ric_currency': (row.get('currency') or 'RUB')[:10],
                    'source': JAC_SOURCE,
                    'is_active': True,
                    'kind': classify_title(title),
                },
            )
            touched.append(product)

            spec_count = _sync_tech_specs(
                product, (specs.get(article) or {}).get('characteristics'),
                category, tech_cache,
            )
            if spec_count:
                specs_synced += 1

            if _sync_images(product, _photo_url(photos, brand_title, series)):
                images_synced += 1

            write_warehouse_stocks(product, [
                (_CRIMEA_WAREHOUSE, _qty(attributes.get(_CRIMEA_ATTR))),
                (_MAINLAND_WAREHOUSE, _qty(attributes.get(_MAINLAND_ATTR))),
            ])

            if is_new:
                created += 1
            else:
                updated += 1

        deactivated = (
            Product.objects.filter(source=JAC_SOURCE)
            .exclude(nc_code__in=seen_nc_codes)
            .update(is_active=False)
        )

    # Пересчёт производных полей — вне транзакции: чтение tech_values и запись
    # двух полей на товар, длинную транзакцию держать незачем.
    from apps.catalog.btu import refresh_btu_calc
    from apps.catalog.heating import apply_heating_fields
    for product in touched:
        refresh_btu_calc(product)
        apply_heating_fields(product)

    from apps.catalog.facets import invalidate_facets_cache
    invalidate_facets_cache()

    result = {
        'created': created, 'updated': updated,
        'skipped_no_category': skipped_no_category,
        'skipped_no_article': skipped_no_article,
        'deactivated': deactivated,
        'images_synced': images_synced, 'specs_synced': specs_synced,
        'brand_recovered': brand_recovered, 'brand_missing': brand_missing,
    }
    if brand_missing:
        # Бренд не удалось взять ни из снимка, ни из характеристик, ни из БД:
        # карточка уедет без производителя — в фиде Авито Vendor обязателен.
        logger.warning('JAC sync: %d позиций без бренда', brand_missing)
    if brand_recovered:
        logger.info('JAC sync: бренд восстановлен для %d позиций', brand_recovered)
    logger.info('JAC sync: %s', result)
    return result

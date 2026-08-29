import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.text import slugify

from apps.catalog.classify import classify_title
from apps.catalog.models import Brand, Category, Product, ProductImage, ProductTech, TechSpec
from apps.stock.models import Stock
from apps.sync.daichi_client import DaichiClient
from apps.sync.warehouse_stock import write_warehouse_stocks

logger = logging.getLogger(__name__)

DAICHI_SOURCE = 'daichi'

# В .env / settings DAICHI_STORE_ID может быть 'default' (партнёр одного склада)
# или конкретный XML_ID (если несколько складов).

# Карта группы товара Daichi → title нашей мастер-категории.
# Если нашей категории с таким title нет — товар пропускается (sync_enabled управляется в admin).
_GOODGROUP_TO_TITLE = {
    'Бытовые сплит-системы': 'Бытовые сплит-системы',
}

# В каталог попадают только готовые комплекты (внутренний + наружный блок продаются вместе).
# Отдельные внутренние/наружные блоки скрываем — конечному покупателю не нужны.
_ALLOWED_GOODTYPES = {'Комплект', 'Сплит-система'}

# Карта валют Daichi → код в нашей БД.
_CURRENCY_MAP = {
    'RUR': 'RUB',
    'RUB': 'RUB',
    'USD': 'USD',
    'EUR': 'EUR',
    'KZT': 'KZT',
}


def _to_decimal(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _get_or_create_brand(title):
    if not title:
        return None
    brand = Brand.objects.filter(title__iexact=title).first()
    if brand:
        return brand
    slug = slugify(title, allow_unicode=True) or f'brand-daichi-{title[:30]}'
    if Brand.objects.filter(slug=slug).exists():
        slug = f'{slug}-daichi'
    return Brand.objects.create(title=title, slug=slug)


def _resolve_category(params):
    """Map Daichi PARAMS to our master Category by title.

    Returns Category or None. None means «нет нашей категории под этот goodgroup —
    товар не показываем».
    """
    goodgroup = (params.get('ATTR_L_GOODGROUP') or '').strip()
    target_title = _GOODGROUP_TO_TITLE.get(goodgroup)
    if not target_title:
        return None
    return Category.objects.filter(title__iexact=target_title, sync_enabled=True).first()


def _is_kit(params):
    goodtype = (params.get('ATTR_L_GOODTYPE') or '').strip()
    return goodtype in _ALLOWED_GOODTYPES


def _extract_prices(prices_obj):
    """Daichi PRICES — это dict {XML_ID-key: {XML_ID, NAME, PRICE, CURRENCY}}.

    BASE = розничная партнёрская (со скидкой), это наш price_wholesale.
    mprc = МПРЦ (рекомендуемая розничная), это наш ric для показа клиенту.
    """
    if not isinstance(prices_obj, dict):
        return None, None, 'RUB'

    base_price = ric_price = None
    currency = 'RUB'

    for entry in prices_obj.values():
        if not isinstance(entry, dict):
            continue
        xml_id = entry.get('XML_ID') or ''
        name = entry.get('NAME') or ''
        price = _to_decimal(entry.get('PRICE'))
        cur = _CURRENCY_MAP.get(entry.get('CURRENCY', 'RUR'), 'RUB')

        if xml_id == 'BASE' or name == 'Розничная цена':
            base_price = price
            currency = cur
        elif name == 'МПРЦ' or 'mprc' in (xml_id or '').lower():
            ric_price = price

    return base_price, ric_price, currency


def _build_slug(brand, articul, xml_id):
    brand_part = slugify(brand.title, allow_unicode=True) if brand else ''
    art_part = slugify(articul, allow_unicode=True) if articul else ''
    short_uuid = (xml_id or '').replace('-', '')[:12]
    parts = [p for p in (brand_part, art_part, short_uuid) if p]
    slug = '-'.join(parts) or f'daichi-{short_uuid}'
    return slug[:500]


def _fetch_all_productparams(client, page_size=500):
    """Собирает все productparams Daichi постранично. Возвращает dict XML_ID → pp_dict.

    Останов по факту неполной страницы (len(data) < page_size), а не по
    resp['total_count'] — если API вернёт это поле пустым/некорректным,
    останов по total_count оборвал бы забор данных уже после первой страницы."""
    pp_map = {}
    page = 1
    while True:
        resp = client.get_productparams(page_size=page_size, page=page)
        data = resp.get('data') if isinstance(resp, dict) else None
        if not data:
            break
        for pp in data.values():
            xml = pp.get('XML_ID')
            if xml:
                pp_map[xml] = pp
        if len(data) < page_size:
            break
        page += 1
    logger.info('Daichi productparams: fetched %d items across %d pages', len(pp_map), page)
    return pp_map


def _sync_images(product, photo_urls):
    """Полностью перезаписывает картинки товара списком URL'ов из Daichi."""
    if not photo_urls:
        return
    # Простой replace — sync идёт пакетно, дубликаты нам не нужны.
    product.images.all().delete()
    ProductImage.objects.bulk_create([
        ProductImage(product=product, url=url, order=i)
        for i, url in enumerate(photo_urls)
        if isinstance(url, str) and url.startswith('http')
    ])


def _attr_value(pp_data, key):
    """Извлекает VALUE из структуры ATTR_* (dict {CODE,NAME,VALUE,GROUP})."""
    attr = pp_data.get(key)
    if isinstance(attr, dict):
        val = attr.get('VALUE')
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _sync_tech_specs(product, pp_data, category, tech_cache):
    """Пишет ProductTech-значения из ATTR_* для одного товара.

    `tech_cache` — словарь title → TechSpec (общий между вызовами в рамках
    одного sync_catalog, чтобы не плодить get_or_create-запросы).
    Возвращает количество записанных пар.
    """
    # Replace-стратегия: удаляем старые tech_values для этого товара.
    ProductTech.objects.filter(product=product).delete()

    to_create = []
    seen_specs = set()
    for key, attr in pp_data.items():
        if not key.startswith('ATTR_') or not isinstance(attr, dict):
            continue
        title = (attr.get('NAME') or '').strip()
        raw_value = attr.get('VALUE')
        if not title or raw_value is None or str(raw_value).strip() == '':
            continue
        group = (attr.get('GROUP') or '').strip()
        value = str(raw_value).strip()

        # Один и тот же title не должен возникнуть дважды у одного товара —
        # unique_together(product, spec) защитит. Но дедуплицируем заранее
        # чтобы не падать на bulk_create.
        if title in seen_specs:
            continue
        seen_specs.add(title)

        spec = tech_cache.get(title)
        if spec is None:
            # Ищем Daichi-spec (без breez_id и без external_uuid) по title.
            # Если дубли уже накопились — берём первого и игнорируем остальные.
            spec = TechSpec.objects.filter(
                title=title, breez_id__isnull=True, external_uuid__isnull=True,
            ).first()
            if spec is None:
                spec = TechSpec.objects.create(
                    title=title, unit='', group=group, category=category,
                )
            elif group and not spec.group:
                spec.group = group
                spec.save(update_fields=['group'])
            tech_cache[title] = spec

        to_create.append(ProductTech(product=product, spec=spec, value=value))

    if to_create:
        ProductTech.objects.bulk_create(to_create)
    from apps.catalog.btu import refresh_btu_calc
    from apps.catalog.heating import apply_heating_fields
    refresh_btu_calc(product)
    apply_heating_fields(product)
    return len(to_create)


def _build_description(pp_data, series, brand_title):
    """Синтезирует human-readable текст из структурированных ATTR_* Daichi.

    API не отдаёт готовое описание — собираем сами из ключевых характеристик.
    """
    intro_bits = []
    if brand_title and series:
        intro_bits.append(f'Сплит-система серии {series} от бренда {brand_title}.')
    elif series:
        intro_bits.append(f'Сплит-система серии {series}.')

    facts = []
    cool = _attr_value(pp_data, 'ATTR_CAPACITY_COOL') or _attr_value(pp_data, 'ATTR_CAP_COOL_NOM')
    if cool:
        facts.append(f'Мощность охлаждения {cool} кВт')

    noise_in = _attr_value(pp_data, 'ATTR_SOUND_PRESSURE_IU_COOL')
    noise_out = _attr_value(pp_data, 'ATTR_SOUND_PRESSURE_COOL')
    if noise_in or noise_out:
        parts = []
        if noise_in: parts.append(f'внутренний блок {noise_in} дБ')
        if noise_out: parts.append(f'наружный {noise_out} дБ')
        facts.append('уровень шума ' + ', '.join(parts))

    refr = _attr_value(pp_data, 'ATTR_L_REFR')
    if refr:
        facts.append(f'хладагент {refr}')

    color = _attr_value(pp_data, 'ATTR_L_COLOR')
    if color:
        facts.append(f'цвет {color.lower()}')

    dims = _attr_value(pp_data, 'ATTR_DIMENS')
    if dims:
        facts.append(f'габариты {dims}')

    weight = _attr_value(pp_data, 'ATTR_WEIGHT_NET')
    if weight:
        facts.append(f'вес {weight} кг')

    country = _attr_value(pp_data, 'ATTR_STRANA')
    if country:
        facts.append(f'страна производства — {country}')

    srok = _attr_value(pp_data, 'ATTR_SROK_EKSP')
    if srok:
        facts.append(f'срок эксплуатации {srok}')

    sections = []
    if intro_bits:
        sections.append(' '.join(intro_bits))
    if facts:
        sections.append('Характеристики: ' + '; '.join(facts) + '.')
    return '\n\n'.join(sections)


def _recover_kit_wholesale(client, store_id, xml_id, articul, name_to_base):
    """Опт (BASE) комплекта Daichi: массовый /products/get отдаёт его пустым, а
    точечный запрос по XML_ID — заполненным. Сначала до-запрашиваем цену комплекта
    точечно; если пусто — берём сумму оптов внутреннего+наружного блоков
    (articul = 'ВНУТР/НАРУЖ'; блоки есть в массовом дампе с ценой)."""
    try:
        resp = client.get_products(store_id=store_id, filter_xml_id=xml_id) or {}
        for e in resp.values():
            if isinstance(e, dict) and e.get('XML_ID') == xml_id:
                w, _, _ = _extract_prices(e.get('PRICES') or e.get('PRICES:') or {})
                if w is not None:
                    return w
    except Exception as exc:
        logger.warning('Daichi kit %s targeted price fetch failed: %s', xml_id, exc)
    parts = [p.strip() for p in (articul or '').split('/') if p.strip()]
    if len(parts) == 2:
        a, b = name_to_base.get(parts[0]), name_to_base.get(parts[1])
        if a is not None and b is not None:
            return a + b
    return None


def _prefetch_kit_wholesales(client, store_id, products):
    """Собирает {xml_id: опт} для комплектов с пустым BASE ДО транзакции — сетевые
    до-запросы не должны держать БД-транзакцию открытой."""
    name_to_base = {}
    for e in products.values():
        if not isinstance(e, dict):
            continue
        name = e.get('NAME')
        if not name:
            continue
        w, _, _ = _extract_prices(e.get('PRICES') or e.get('PRICES:') or {})
        if w is not None:
            name_to_base[name] = w
    recovered = {}
    failed = []
    for e in products.values():
        if not isinstance(e, dict):
            continue
        xml_id = e.get('XML_ID')
        if not xml_id:
            continue
        params = e.get('PARAMS') or e.get('PARAMS:') or {}
        if not _is_kit(params):
            continue
        w, _, _ = _extract_prices(e.get('PRICES') or e.get('PRICES:') or {})
        if w is not None:
            continue
        rec = _recover_kit_wholesale(client, store_id, xml_id, e.get('NAME') or '', name_to_base)
        if rec is not None:
            recovered[xml_id] = rec
        else:
            failed.append(xml_id)
    logger.info('Daichi: restored kit wholesale prices: %d of empty kits', len(recovered))
    if failed:
        # Оба пути восстановления (точечный запрос и сумма блоков) не сработали —
        # товар уедет в БД с price_wholesale=None. Раньше это тонуло в INFO-логе
        # как разница между «найдено пустых» и «восстановлено», без явного счётчика.
        logger.error(
            'Daichi: %d kits still have no wholesale price after recovery: %s',
            len(failed), ', '.join(failed[:20]),
        )
    return recovered


def sync_catalog():
    """Pull /products/get for the configured store, upsert Products + Stock."""
    from django.conf import settings as dj_settings

    client = DaichiClient()
    if not client.access_token:
        logger.warning('Daichi sync: DAICHI_ACCESS_TOKEN not configured')
        return {'created': 0, 'updated': 0, 'skipped': 0, 'deactivated': 0}

    store_id = dj_settings.DAICHI_STORE_ID
    products = client.get_products(store_id=store_id)
    if not products:
        logger.warning('Daichi sync: empty /products/get response')
        return {'created': 0, 'updated': 0, 'skipped': 0, 'deactivated': 0}

    # /products/get/ не отдаёт PHOTOES — они приходят только через /productparams/get/.
    # Тянем все productparams заранее (10 страниц по 500) и матчим по XML_ID.
    pp_map = _fetch_all_productparams(client)
    # Опт (BASE) комплектов Daichi отсутствует в массовом /products/get —
    # восстанавливаем точечно по XML_ID (откат: сумма оптов блоков). До транзакции.
    kit_wholesale = _prefetch_kit_wholesales(client, store_id, products)

    seen_xml_ids = set()
    created = updated = skipped_no_category = skipped_not_kit = 0
    images_synced = specs_synced = 0
    tech_cache = {}  # title → TechSpec, общий для всего sync

    with transaction.atomic():
        for entry in products.values():
            if not isinstance(entry, dict):
                continue

            xml_id = entry.get('XML_ID')
            if not xml_id:
                continue

            params = entry.get('PARAMS') or {}
            # API в реальности отдаёт ключи с двоеточием ("PARAMS:") — пробуем оба
            if not params:
                params = entry.get('PARAMS:') or {}

            if not _is_kit(params):
                skipped_not_kit += 1
                continue

            category = _resolve_category(params)
            if not category:
                skipped_no_category += 1
                continue

            brand = _get_or_create_brand(params.get('BRAND'))

            articul = entry.get('NAME') or ''
            series = (params.get('ATTR_L_SERIA') or '').strip()
            # Title формата «Brand Articul Series». ATTR_RUS_NAME_AX часто
            # generic «Бытовой кондиционер» — оставляем только как fallback,
            # когда ни бренд, ни артикул, ни серия не дали внятной строки.
            brand_title = (brand.title if brand else '').strip()
            title_parts = [p for p in (brand_title, articul, series) if p]
            if title_parts:
                title = ' '.join(title_parts)
            else:
                title = (params.get('ATTR_RUS_NAME_AX') or xml_id).strip()

            prices_obj = entry.get('PRICES') or entry.get('PRICES:') or {}
            wholesale, ric, currency = _extract_prices(prices_obj)
            if wholesale is None:
                wholesale = kit_wholesale.get(xml_id)

            slug = _build_slug(brand, articul, xml_id)
            # уникальность slug по таблице (на случай коллизии с Breeze/Rusklimat)
            base_slug = slug
            counter = 1
            while Product.objects.filter(slug=slug).exclude(nc_code=xml_id).exists():
                counter += 1
                slug = f'{base_slug}-{counter}'

            pp = pp_map.get(xml_id)
            description = ''
            if pp:
                description = _build_description(
                    pp, series, brand.title if brand else ''
                )

            product, is_new = Product.objects.update_or_create(
                nc_code=xml_id,
                defaults={
                    'articul': articul,
                    'category': category,
                    'brand': brand,
                    'series': series,
                    'title': title,
                    'slug': slug,
                    'price_wholesale': wholesale,
                    'ric': ric,
                    'ric_currency': currency,
                    'description': description,
                    'source': DAICHI_SOURCE,
                    'is_active': True,
                    'kind': classify_title(title),
                },
            )
            seen_xml_ids.add(xml_id)

            if pp:
                photos = pp.get('PHOTOES') or pp.get('PHOTOES:') or []
                if photos:
                    _sync_images(product, photos)
                    images_synced += 1
                tech_count = _sync_tech_specs(product, pp, category, tech_cache)
                if tech_count:
                    specs_synced += 1

            store = entry.get('STORE') or entry.get('STORE:') or {}
            if isinstance(store, dict):
                warehouse = (store.get('NAME') or '').strip()
                qty = store.get('STORE_AMOUNT')
                write_warehouse_stocks(product, [(warehouse, qty)] if warehouse else [])

            if is_new:
                created += 1
            else:
                updated += 1

        deactivated = (
            Product.objects.filter(source=DAICHI_SOURCE)
            .exclude(nc_code__in=seen_xml_ids)
            .update(is_active=False)
        )

    logger.info(
        'Daichi sync: created=%d updated=%d skipped_not_kit=%d skipped_no_cat=%d '
        'deactivated=%d images_synced=%d specs_synced=%d',
        created, updated, skipped_not_kit, skipped_no_category, deactivated,
        images_synced, specs_synced,
    )
    from apps.catalog.facets import invalidate_facets_cache
    invalidate_facets_cache()   # фасеты пересчитаются на первом заходе

    return {
        'created': created,
        'updated': updated,
        'skipped_not_kit': skipped_not_kit,
        'skipped_no_category': skipped_no_category,
        'deactivated': deactivated,
        'images_synced': images_synced,
        'specs_synced': specs_synced,
    }

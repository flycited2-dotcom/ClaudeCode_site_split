import logging
from celery import shared_task
from django.db import transaction
from django.utils.text import slugify

from apps.sync.client import BreezClient
from apps.catalog.classify import classify_title
from apps.catalog.models import Category, Brand, Product, ProductImage
from apps.stock.models import Stock

logger = logging.getLogger(__name__)

def _is_ac_category(category, allowed_ids=None):
    """Return True if this category should be synced."""
    if category is None:
        return False
    if allowed_ids is not None:
        return category.id in allowed_ids
    return category.sync_enabled


def _get_client():
    return BreezClient()


@shared_task(name='sync.sync_categories')
def sync_categories():
    client = _get_client()
    data = client.get_categories()
    if not data:
        logger.warning("sync_categories: no data returned from API")
        return {'created': 0, 'updated': 0}

    created = updated = 0
    for item in data:
        item_id = int(item['id'])
        raw_slug = item.get('chpu') or slugify(item.get('title', ''), allow_unicode=True)
        slug = raw_slug or f"cat-{item_id}"
        if Category.objects.filter(slug=slug).exclude(breez_id=item_id).exists():
            slug = f"{slug}-{item_id}"
        _, is_new = Category.objects.update_or_create(
            breez_id=item_id,
            defaults={
                'title': item.get('title', ''),
                'slug': slug,
                'order': int(item.get('order', 0) or 0),
            }
        )
        if is_new:
            created += 1
        else:
            updated += 1

    # Second pass: wire up parent relationships
    for item in data:
        item_id = int(item['id'])
        parent_breez_id = item.get('level')
        if parent_breez_id and int(parent_breez_id) != 0:
            Category.objects.filter(breez_id=item_id).update(
                parent=Category.objects.filter(breez_id=int(parent_breez_id)).first()
            )

    logger.info("sync_categories: created=%d updated=%d", created, updated)
    return {'created': created, 'updated': updated}


@shared_task(name='sync.sync_brands')
def sync_brands():
    client = _get_client()
    data = client.get_brands()
    if not data:
        logger.warning("sync_brands: no data returned from API")
        return {'created': 0, 'updated': 0}

    created = updated = 0
    for item in data:
        item_id = int(item['id'])
        raw_slug = item.get('chpu') or slugify(item.get('title', ''), allow_unicode=True)
        slug = raw_slug or f"brand-{item_id}"
        # Ensure slug uniqueness across different breez_ids
        if Brand.objects.filter(slug=slug).exclude(breez_id=item_id).exists():
            slug = f"{slug}-{item_id}"
        _, is_new = Brand.objects.update_or_create(
            breez_id=item_id,
            defaults={
                'title': item.get('title', ''),
                'slug': slug,
                'logo_url': item.get('image', ''),
                'site_url': item.get('url', ''),
                'order': int(item.get('order', 0) or 0),
            }
        )
        if is_new:
            created += 1
        else:
            updated += 1

    logger.info("sync_brands: created=%d updated=%d", created, updated)
    return {'created': created, 'updated': updated}


@shared_task(name='sync.sync_products')
def sync_products(category_ids=None):
    """
    Sync products from Breeze.
    category_ids: list of Category PKs to sync (manual run). None = use sync_enabled flag.
    """
    client = _get_client()
    data = client.get_products()
    if not data:
        logger.warning("sync_products: no data returned from API")
        return {'created': 0, 'updated': 0, 'deactivated': 0}

    allowed_ids = set(category_ids) if category_ids else None
    nc_codes_seen = set()
    created = updated = skipped = 0

    with transaction.atomic():
        for item in data:
            nc = item.get('nc')
            if not nc:
                continue

            cat_id = item.get('category_id')
            category = Category.objects.filter(breez_id=int(cat_id)).first() \
                if cat_id else None

            if not _is_ac_category(category, allowed_ids):
                skipped += 1
                continue

            nc_codes_seen.add(nc)

            brand_id = item.get('brand')
            brand = Brand.objects.filter(breez_id=int(brand_id)).first() \
                if brand_id else None

            # price is a dict {"ric": "...", "ric_currency": "..."} or None
            price_data = item.get('price') or {}
            ric_val = price_data.get('ric') if isinstance(price_data, dict) else None
            ric_currency = price_data.get('ric_currency', 'RUB') if isinstance(price_data, dict) else 'RUB'

            articul = item.get('articul', '')
            title = item.get('title', '') or articul or nc

            brand_part = slugify(brand.title, allow_unicode=True) if brand else ''
            if brand_part and articul:
                slug = f"{brand_part}-{slugify(articul, allow_unicode=True)}-{nc}"
            else:
                slug = f"{slugify(title, allow_unicode=True)}-{nc}"
            slug = slug[:500] or f"product-{nc}"

            obj, is_new = Product.objects.update_or_create(
                nc_code=nc,
                defaults={
                    'articul': articul,
                    'category': category,
                    'brand': brand,
                    'series': item.get('series', ''),
                    'title': title,
                    'slug': slug,
                    'price_wholesale': ric_val or None,
                    'ric': ric_val or None,
                    'ric_currency': ric_currency,
                    'description': item.get('description', ''),
                    'booklet_url': item.get('booklet', ''),
                    'manual_url': item.get('manual', ''),
                    'video_youtube': item.get('video_youtube', ''),
                    'video_rutube': item.get('video_rutube', ''),
                    'is_active': True,
                    'kind': classify_title(title),
                }
            )
            if is_new:
                created += 1
            else:
                updated += 1

            # Sync images: always replace when API returns a list (even empty)
            images = item.get('images')
            if images is not None:
                obj.images.all().delete()
                if images:
                    ProductImage.objects.bulk_create([
                        ProductImage(product=obj, url=url, order=i)
                        for i, url in enumerate(images)
                    ])

        # Deactivate Breeze products not seen in this sync (never touch Rusklimat products)
        deactivated = Product.objects.filter(source='breeze').exclude(nc_code__in=nc_codes_seen).update(is_active=False)

    logger.info("sync_products: created=%d updated=%d deactivated=%d skipped(non-AC)=%d",
                created, updated, deactivated, skipped)
    return {'created': created, 'updated': updated, 'deactivated': deactivated, 'skipped': skipped}


def _iter_leftoversnew(data):
    """Универсальный итератор по ответу Breez /v1/leftoversnew/.

    Поддерживает оба формата согласно инструкции:
    1. `{"НС-X": {...}, "НС-Y": {...}}` (dict с многими ключами) — текущий.
    2. `[{"НС-X": {...}}, {"НС-Y": {...}}]` (список однолючевых dict) — формат
       из инструкции 2026-05-19, возможно будет включён Бризом позже.
    3. `[{'id': 'НС-X', 'nc': 'НС-X', ...}]` (после _get() flatten — устар.).

    Yield's нормализованных dict'ов с полями nc, stocks, price, ...
    """
    if isinstance(data, dict):
        # Формат 1: ключ — NC, значение — товар
        for key, item in data.items():
            if isinstance(item, dict):
                item.setdefault('nc', key)
                yield item
    elif isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            # Формат 2: {NC: {...}} с одним ключом
            if len(entry) == 1 and isinstance(next(iter(entry.values())), dict):
                key, item = next(iter(entry.items()))
                item.setdefault('nc', key)
                yield item
            # Формат 3: плоский (после _get flatten)
            elif 'nc' in entry or 'id' in entry:
                yield entry


@shared_task(name='sync.sync_stock')
def sync_stock():
    """Sync остатков Breez через /v1/leftoversnew/.

    По инструкции 2026-05-19: типы — quantity:int (>50 трактуется как 50 через
    _parse_qty), for_marketplace:bool, price.base/ric:float. Crimean-склад
    идентифицируется substring-поиском (regex _CRIMEA_RE в warehouse_stock).
    """
    from apps.sync.warehouse_stock import write_warehouse_stocks

    client = _get_client()
    raw = client.get_stock()
    if not raw:
        logger.warning("sync_stock: no data returned from API")
        return {'updated': 0}

    created = updated = skipped_no_product = 0
    warehouse_seen = {}  # name -> {total_qty, nonzero_products}

    for item in _iter_leftoversnew(raw):
        nc = item.get('nc') or item.get('nc_code') or item.get('id')
        if not nc:
            continue
        product = Product.objects.filter(nc_code=nc).first()
        if not product:
            skipped_no_product += 1
            continue

        # stocks: [{stock: name, quantity: int}]
        stocks_list = item.get('stocks') or []
        pairs = []
        for s in stocks_list:
            if not isinstance(s, dict):
                continue
            name = s.get('stock', '')
            qty = s.get('quantity', 0)
            pairs.append((name, qty))
            # Диагностика — копим статистику по складам
            stat = warehouse_seen.setdefault(name, {'total': 0, 'nonzero': 0})
            try:
                qi = int(qty) if not isinstance(qty, str) or not qty.startswith('>') else 50
            except (TypeError, ValueError):
                qi = 0
            stat['total'] += qi
            if qi > 0:
                stat['nonzero'] += 1

        # price: [{base, base_currency}, {ric, ric_currency}]
        price_list = item.get('price') or []
        price_base = None
        if isinstance(price_list, list):
            for p in price_list:
                if isinstance(p, dict) and 'base' in p:
                    price_base = p['base']
                    break
        if price_base is not None and not product.price_wholesale:
            product.price_wholesale = price_base
            product.save(update_fields=['price_wholesale'])

        write_warehouse_stocks(product, pairs)
        updated += 1

    # Логирование сводки по складам — для диагностики «когда Бриз откроет Крым»
    logger.info(
        "Breez sync_stock: updated=%d, skipped_no_product=%d, warehouses=%s",
        updated, skipped_no_product,
        {name: f"{s['nonzero']} товаров / {s['total']} шт." for name, s in warehouse_seen.items()},
    )
    return {
        'created': created,
        'updated': updated,
        'skipped_no_product': skipped_no_product,
        'warehouses': {name: s for name, s in warehouse_seen.items()},
    }


@shared_task(name='sync.sync_catalog')
def sync_catalog():
    """Run full catalog sync: categories -> brands -> products."""
    cats = sync_categories()
    brands = sync_brands()
    products = sync_products()
    from apps.catalog.facets import invalidate_facets_cache
    invalidate_facets_cache()   # счётчики фасет пересчитаются на первом заходе
    logger.info("sync_catalog complete: cats=%s brands=%s products=%s",
                cats, brands, products)
    return {'categories': cats, 'brands': brands, 'products': products}


@shared_task(name='sync.sync_daichi')
def sync_daichi():
    """Pull Daichi B2B API: products, prices and stock in one request."""
    from apps.sync.daichi_catalog import sync_catalog as daichi_sync
    result = daichi_sync()
    logger.info('sync_daichi: %s', result)
    return result


@shared_task(name='sync.refresh_rusklimat_jwt')
def refresh_rusklimat_jwt():
    """Обновляет JWT для Rusklimat REST. Запускается Celery Beat в 23:50 МСК
    (токен сбрасывается строго в 00:00 МСК, не +24ч от выпуска).
    """
    from apps.sync.rusklimat_auth import RusklimatAuthError, fetch_jwt
    try:
        token = fetch_jwt()
    except RusklimatAuthError as exc:
        logger.error('refresh_rusklimat_jwt failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
    return {'ok': True, 'length': len(token)}

"""REST API клиент для internet-partner.rusklimat.com.

Заменяет старый scraping b2b.rusklimat.com (login 403 на VPS).
Документация: https://internet-partner.rusklimat.com/swagger/v1/swagger.json
Локальная копия описания: https://b2b.rusklimat.com/personal/internet_partner/catalog_api/

Endpoints:
  GET  /api/v1/InternetPartner/{partnerId}/requestKey
  GET  /api/v1/InternetPartner/categories/{requestKey}
  GET  /api/v1/InternetPartner/properties/{requestKey}
  GET  /api/v1/InternetPartner/units
  POST /api/v{1|2|3}/InternetPartner/{partnerId}/products/{requestKey}

Авторизация: JWT через `rusklimat_auth.get_jwt()`. Auto-refresh:
  - cache в Redis (см. apps/sync/rusklimat_auth);
  - cron 23:50 МСК (`sync.refresh_rusklimat_jwt` в CELERY_BEAT_SCHEDULE);
  - на 401 — invalidate + refresh + retry один раз (см. `_request`).
Legacy fallback на статичный RUSKLIMAT_JWT_TOKEN сохранён в `rusklimat_auth`.
"""
import base64
import json
import logging
import re
import time

import requests
from django.conf import settings
from django.db import transaction

from apps.catalog.classify import classify_title
from apps.catalog.heating import apply_heating_fields
from apps.catalog.models import Brand, Category, Product, ProductImage, ProductTech, TechSpec
from apps.stock.models import Stock
from apps.sync import rusklimat_auth
from apps.sync.warehouse_stock import write_warehouse_stocks


def _jwt_payload(token):
    """Декодирует payload (middle часть) JWT — без проверки подписи."""
    try:
        _, payload, _ = token.split('.', 2)
    except ValueError:
        return {}
    payload += '=' * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode('utf-8'))
    except Exception:
        return {}

logger = logging.getLogger(__name__)

_RK_BASE = 'https://internet-partner.rusklimat.com'
RUSKLIMAT_SOURCE = 'rusklimat'

# Соответствие master-категорий Rusklimat (по regex по name).
# «Тепловые насосы» добавлены 2026-08-28: воздух-воздух — это те же сплит-системы,
# просто продаются под другим ярлыком (22 позиции, все под заказ). См.
# docs/superpowers/specs/2026-08-28-heat-pumps-collection-design.md
_AC_CATEGORY_RE = re.compile(
    r'кондицион|сплит.?систем|мульти.?сплит|мобильн\w*\s+кондицион|'
    r'теплов\w*\s+насос',
    re.IGNORECASE,
)
# Исключаем категории-аксессуары / запчасти / комплектующие.
_AC_EXCLUDE_RE = re.compile(
    r'аксессуар|запчаст|комплектующ|расходн|чехл|абажур|пульт|фильтр',
    re.IGNORECASE,
)
# Категория, которой поставщик сам объявил товар тепловым насосом.
_HEAT_PUMP_CATEGORY_RE = re.compile(r'теплов\w*\s+насос', re.IGNORECASE)


def _is_heat_pump_category(name):
    """True, если Rusklimat назвал категорию тепловыми насосами."""
    return bool(_HEAT_PUMP_CATEGORY_RE.search(name or ''))


class RusklimatJWTExpired(RuntimeError):
    pass


class RusklimatRestClient:
    """REST-клиент internet-partner.rusklimat.com.

    Кэширует requestKey на ~55 секунд (отдаётся с expire ~60 сек).
    """

    def __init__(self):
        # JWT тянем через auth-модуль: cache → fetch (если есть LOGIN/PASSWORD) →
        # legacy fallback на settings.RUSKLIMAT_JWT_TOKEN.
        self.jwt = rusklimat_auth.get_jwt()
        # partnerId: приоритет settings.RUSKLIMAT_PARTNER_ID (известная константа
        # из спецификации). Fallback на guid из JWT-payload, потом на legacy
        # CONTRACTOR_GUID. Без partnerId catalog API вернёт 403.
        self.guid = (
            settings.RUSKLIMAT_PARTNER_ID
            or _jwt_payload(self.jwt).get('guid')
            or settings.RUSKLIMAT_CONTRACTOR_GUID
        )
        if not self.guid:
            raise RuntimeError('Не удалось определить partnerId (settings + JWT.guid пусты)')
        self._request_key = None
        self._request_key_until = 0.0
        self._session = requests.Session()
        self._session.headers.update({'Accept': 'application/json'})
        self._set_token(self.jwt)

    def _set_token(self, token):
        """Обновляет Authorization-заголовок в session."""
        self.jwt = token
        # Catalog API принимает токен БЕЗ префикса Bearer — так работает текущий прод.
        self._session.headers['Authorization'] = token

    def _check(self, response, where):
        if response.status_code == 401:
            raise RusklimatJWTExpired(
                f'Rusklimat 401 на {where}. JWT истёк и refresh не помог — '
                f'проверьте RUSKLIMAT_LOGIN/PASSWORD в .env.'
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f'Rusklimat {response.status_code} на {where}: {response.text[:200]}'
            )

    def _request(self, method, url, where, **kw):
        """HTTP-запрос с авто-retry на 401 (invalidate + refresh + один retry).

        Используется во всех методах вместо прямого self._session.get/post.
        """
        fn = self._session.post if method == 'POST' else self._session.get
        r = fn(url, **kw)
        if r.status_code == 401:
            logger.warning('Rusklimat 401 на %s — пробую refresh JWT', where)
            rusklimat_auth.invalidate_jwt()
            try:
                new_token = rusklimat_auth.get_jwt()
            except rusklimat_auth.RusklimatAuthError as exc:
                raise RusklimatJWTExpired(
                    f'401 на {where} и refresh не удался: {exc}'
                ) from exc
            self._set_token(new_token)
            r = fn(url, **kw)
        self._check(r, where)
        return r

    def request_key(self, force=False):
        now = time.time()
        if not force and self._request_key and self._request_key_until > now + 3:
            return self._request_key
        url = f'{_RK_BASE}/api/v1/InternetPartner/{self.guid}/requestKey'
        r = self._request('GET', url, 'requestKey', timeout=30)
        data = r.json()
        self._request_key = data['requestKey']
        self._request_key_until = now + 55  # запас от expire ~60 сек
        return self._request_key

    def get_units(self):
        url = f'{_RK_BASE}/api/v1/InternetPartner/units'
        r = self._request('GET', url, '/units', timeout=60)
        return r.json().get('data') or []

    def get_categories(self):
        key = self.request_key()
        url = f'{_RK_BASE}/api/v1/InternetPartner/categories/{key}'
        r = self._request('GET', url, '/categories', timeout=120)
        return r.json().get('data') or []

    def get_properties(self):
        key = self.request_key()
        url = f'{_RK_BASE}/api/v1/InternetPartner/properties/{key}'
        r = self._request('GET', url, '/properties', timeout=120)
        return r.json().get('data') or []

    def get_products(self, *, page=1, page_size=500, version=2,
                     category_ids=None, sort=None):
        key = self.request_key()
        url = (
            f'{_RK_BASE}/api/v{version}/InternetPartner/{self.guid}'
            f'/products/{key}?pageSize={page_size}&page={page}'
        )
        body = {'filter': {}, 'sort': sort or {'nsCode': 'asc'}}
        if category_ids:
            body['filter']['categoryIds'] = list(category_ids)
        r = self._request(
            'POST', url, f'/products page={page}',
            json=body,
            headers={'Content-Type': 'application/json'},
            timeout=180,
        )
        return r.json()


# === Sync helpers ==========================================================


def _find_ac_categories(client):
    """(ids, names) категорий Rusklimat с AC-товарами (по name regex).

    Аксессуары и запчасти исключаются. Карта {id: name} нужна при записи товара:
    в самом товаре есть только categoryId, названия там нет, а по названию мы
    решаем, объявил ли поставщик товар тепловым насосом.
    """
    cats = client.get_categories()
    ids = set()
    names = {}
    for cat in cats:
        name = (cat.get('name') or '').strip()
        names[cat['id']] = name
        if _AC_EXCLUDE_RE.search(name):
            continue
        if _AC_CATEGORY_RE.search(name):
            ids.add(cat['id'])
    logger.info('Rusklimat REST: %d AC-категорий из %d', len(ids), len(cats))
    return ids, names


def _master_category_for(name):
    """Маппинг Rusklimat-name в нашу master-Category."""
    n = name.lower()
    if 'мобильн' in n:
        return Category.objects.filter(breez_id=10).first()
    if 'мульти' in n or 'мульти.?сплит' in n:
        return Category.objects.filter(breez_id=9).first()
    if 'полупромышл' in n or 'канальн' in n or 'кассетн' in n:
        return Category.objects.filter(title__iexact='Полупромышленные кондиционеры').first()
    # default: бытовые сплит-системы
    return Category.objects.filter(breez_id=2).first()


def _build_slug(brand_title, vendor_code, ns_code, slug_cache):
    from django.utils.text import slugify
    parts = []
    if brand_title:
        parts.append(slugify(brand_title, allow_unicode=True))
    if vendor_code:
        parts.append(slugify(vendor_code, allow_unicode=True))
    if ns_code:
        parts.append(ns_code)
    base = '-'.join(p for p in parts if p)[:480] or f'rk-{ns_code}'
    slug = base
    i = 1
    while slug in slug_cache:
        i += 1
        slug = f'{base}-{i}'
    slug_cache.add(slug)
    return slug


def _get_or_create_brand(title, cache):
    from django.utils.text import slugify
    if not title:
        return None
    if title in cache:
        return cache[title]
    brand = Brand.objects.filter(title__iexact=title).first()
    if not brand:
        slug = slugify(title, allow_unicode=True) or f'brand-{title[:30]}'
        if Brand.objects.filter(slug=slug).exists():
            slug = f'{slug}-rk'
        brand = Brand.objects.create(title=title, slug=slug, breez_id=None)
    cache[title] = brand
    return brand


def _sync_images(product, picture_urls):
    if not picture_urls:
        return
    product.images.all().delete()
    ProductImage.objects.bulk_create([
        ProductImage(product=product, url=url, order=i)
        for i, url in enumerate(picture_urls)
        if isinstance(url, str) and url.startswith('http')
    ])


def _sync_stock(product, remains):
    """Пишет per-warehouse breakdown через WarehouseStock + сводный Stock.

    Rusklimat remains.warehouses = {'симферополь склад': '3', 'фрц Киржач': '20', ...}.
    Покупатель в Крыму видит «В наличии» если Симферополь>0; остальные склады
    показываются справочно (можем привезти за ~1 день из Краснодара).
    """
    if not isinstance(remains, dict):
        return
    warehouses_raw = remains.get('warehouses') or {}
    if not isinstance(warehouses_raw, dict):
        warehouses_raw = {}
    pairs = list(warehouses_raw.items())
    write_warehouse_stocks(product, pairs)


def _sync_tech_specs(product, properties_dict, tech_cache, properties_meta, units_meta):
    """Записывает ProductTech из rusklimat-properties dict.

    properties_dict: {uuid: {value, unit}} (формат v2 API).
    properties_meta: {uuid: {id, name, sort}} из /properties/.
    units_meta: {unit_uuid: {name, nameFull}} из /units.
    tech_cache: общий dict uuid → TechSpec.
    """
    if not isinstance(properties_dict, dict) or not properties_dict:
        return 0

    to_create = []
    seen = set()
    for prop_uuid, raw in properties_dict.items():
        # v2: {"value": "...", "unit": "uuid-or-empty"}; v1: значение строкой.
        if isinstance(raw, dict):
            value = raw.get('value')
            unit_uuid = raw.get('unit') or ''
        else:
            value = raw
            unit_uuid = ''
        if value is None or str(value).strip() == '':
            continue

        spec = tech_cache.get(prop_uuid)
        if spec is None:
            meta = properties_meta.get(prop_uuid) or {}
            title = (meta.get('name') or '').strip()
            if not title:
                continue
            unit_meta = units_meta.get(unit_uuid) or {}
            unit_name = (unit_meta.get('name') or '').strip()
            try:
                order = int(meta.get('sort') or 0)
            except (TypeError, ValueError):
                order = 0
            spec, _ = TechSpec.objects.update_or_create(
                external_uuid=prop_uuid,
                defaults={'title': title, 'unit': unit_name, 'order': order},
            )
            tech_cache[prop_uuid] = spec

        if spec.pk in seen:
            continue
        seen.add(spec.pk)
        to_create.append(ProductTech(product=product, spec=spec, value=str(value).strip()))

    if not to_create:
        return 0

    with transaction.atomic():
        ProductTech.objects.filter(product=product).delete()
        ProductTech.objects.bulk_create(to_create)
    from apps.catalog.btu import refresh_btu_calc
    from apps.catalog.heating import apply_heating_fields
    refresh_btu_calc(product)
    apply_heating_fields(product)
    return len(to_create)


def sync_rusklimat_rest(*, max_pages=None):
    """Полная синхронизация каталога Rusklimat через REST API.

    Качаем только товары AC-категорий, обновляем Product/Image/Stock.
    TechSpec/ProductTech синкаем отдельной командой (нужна модель TechSpec
    с поддержкой UUID, что пока не сделано).
    """
    client = RusklimatRestClient()

    ac_ids, category_names = _find_ac_categories(client)
    if not ac_ids:
        logger.warning('AC-категорий не найдено в Rusklimat REST')
        return {'created': 0, 'updated': 0, 'pages': 0}

    # Загружаем словари свойств и единиц измерения (для TechSpec).
    properties_meta = {p['id']: p for p in client.get_properties()}
    units_meta = {u['id']: u for u in client.get_units()}
    logger.info('Rusklimat: %d properties, %d units подгружено',
                len(properties_meta), len(units_meta))

    brand_cache = {}
    tech_cache = {}  # uuid → TechSpec
    slug_cache = set(Product.objects.values_list('slug', flat=True))
    seen_ns = set()
    created = updated = imgs_synced = stock_synced = specs_synced = skipped_no_brand = 0
    page = 1
    page_size = 500

    while True:
        if max_pages and page > max_pages:
            break
        try:
            resp = client.get_products(
                page=page, page_size=page_size, version=2, category_ids=ac_ids,
            )
        except RusklimatJWTExpired:
            raise
        except Exception as exc:
            logger.error('Rusklimat /products page=%d failed: %s', page, exc)
            break

        items = resp.get('data') or []
        total = resp.get('totalCount') or 0
        logger.info('Rusklimat REST page %d: %d items (total %d)', page, len(items), total)
        if not items:
            break

        with transaction.atomic():
            for p in items:
                ns_code = p.get('nsCode')
                if not ns_code:
                    continue

                # Skip аксессуары/расходники: бренд «БЕЗ МАРКИ» или отсутствует.
                brand_title = (p.get('brand') or '').strip()
                if not brand_title or brand_title.upper() == 'БЕЗ МАРКИ':
                    skipped_no_brand += 1
                    continue

                # Дополнительный skip по title — мелочёвка для AC.
                name_for_check = (p.get('name') or '').lower()
                if re.search(
                    r'\bадаптер|чехол|переходник|пульт\b|насадк|кронштейн|'
                    r'фильтр\b|трубк|удлинител|сальник|переключател|клапан',
                    name_for_check,
                ):
                    skipped_no_brand += 1
                    continue

                seen_ns.add(ns_code)
                brand = _get_or_create_brand(brand_title, brand_cache)
                cat = _master_category_for(p.get('categoryId', '') or '')
                vendor_code = (p.get('vendorCode') or '').strip()
                name = (p.get('name') or vendor_code or ns_code).strip()
                description = p.get('description') or ''
                ric = p.get('internetPrice')
                wholesale = p.get('clientPrice') or p.get('price')

                # Нормализуем nc_code как и старый scraper: «НС-XXXXX»
                nc_code = ns_code if ns_code.startswith('НС-') else f'НС-{ns_code}'

                existing = Product.objects.filter(nc_code=nc_code).first()
                if existing:
                    existing.brand = brand or existing.brand
                    existing.category = cat or existing.category
                    existing.articul = vendor_code or existing.articul
                    existing.title = name or existing.title
                    existing.description = description
                    existing.price_wholesale = wholesale
                    existing.ric = ric
                    existing.source = RUSKLIMAT_SOURCE
                    existing.is_active = True
                    existing.kind = classify_title(existing.title)
                    existing.save()
                    product = existing
                    updated += 1
                else:
                    slug = _build_slug(p.get('brand', ''), vendor_code, nc_code, slug_cache)
                    product = Product.objects.create(
                        nc_code=nc_code,
                        articul=vendor_code,
                        category=cat,
                        brand=brand,
                        title=name,
                        slug=slug,
                        description=description,
                        price_wholesale=wholesale,
                        ric=ric,
                        ric_currency='RUB',
                        rusklimat_guid=p.get('id') or '',
                        source=RUSKLIMAT_SOURCE,
                        is_active=True,
                        kind=classify_title(name),
                    )
                    created += 1

                pics = p.get('pictures') or []
                if pics:
                    _sync_images(product, pics)
                    imgs_synced += 1

                remains = p.get('remains') or {}
                if remains:
                    _sync_stock(product, remains)
                    stock_synced += 1

                props = p.get('properties') or {}
                if props:
                    count = _sync_tech_specs(
                        product, props, tech_cache, properties_meta, units_meta,
                    )
                    if count:
                        specs_synced += 1

                # Rusklimat объявляет тепловой насос названием КАТЕГОРИИ, а не
                # характеристикой — _sync_tech_specs про категорию не знает,
                # поэтому флаг доставляем здесь (вызов идемпотентный).
                if _is_heat_pump_category(category_names.get(p.get('categoryId', ''), '')):
                    apply_heating_fields(product, declared=True)

        if len(items) < page_size:
            break
        page += 1

    # Деактивация Rusklimat-товаров, которые исчезли из API. Только при ПОЛНОМ
    # sync (max_pages=None) — иначе мы случайно отключаем товары, которые
    # просто не попали в первые N страниц.
    if max_pages is None:
        deactivated = (
            Product.objects.filter(source=RUSKLIMAT_SOURCE)
            .exclude(nc_code__in={
                (ns if ns.startswith('НС-') else f'НС-{ns}') for ns in seen_ns
            })
            .update(is_active=False)
        )
    else:
        deactivated = 0
        logger.info('Rusklimat REST: deactivation skipped (partial sync, max_pages=%s)', max_pages)

    summary = {
        'created': created,
        'updated': updated,
        'pages': page,
        'imgs_synced': imgs_synced,
        'stock_synced': stock_synced,
        'specs_synced': specs_synced,
        'skipped_no_brand': skipped_no_brand,
        'deactivated': deactivated,
        'tech_specs_total': len(tech_cache),
    }
    from apps.catalog.facets import invalidate_facets_cache
    invalidate_facets_cache()   # фасеты пересчитаются на первом заходе в каталог
    logger.info('Rusklimat REST sync: %s', summary)
    return summary

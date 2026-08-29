import re

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from apps.catalog.management.commands.enable_retail_categories import (
    OWN_CATEGORIES, RETAIL_CATEGORIES,
)
from apps.catalog.models import Category, Product


# Master breez_ids and titles must match ensure_master_categories migration.
# Розничные категории (обогрев, водонагреватели, вентиляция...) добавлены
# 2026-08-29: без них эта команда выключала их сразу после включения, потому
# что «мастером» считались только пять категорий кондиционеров.
MASTER_BREEZ_IDS = {2, 9, 10} | set(RETAIL_CATEGORIES)
MASTER_TITLES = {
    # «Полупромышленные кондиционеры» убраны 2026-08-29: дублировали бризовскую
    # категорию, товары были размазаны между ними (слито merge_categories).
    'Аксессуары для кондиционеров',
} | set(OWN_CATEGORIES)

# Товары этих категорий не переназначаем по названию: правило «настенн|бытов»
# утащило бы настенный конвектор в бытовые сплит-системы.
_KEEP_CATEGORY_BREEZ_IDS = set(RETAIL_CATEGORIES)
_KEEP_CATEGORY_TITLES = set(OWN_CATEGORIES)


# 5 master catalog categories shown in sidebar:
#   1. Бытовые сплит-системы (breez_id=2)
#   2. Мульти-сплит системы (breez_id=9)
#   3. Полупромышленные кондиционеры (master, no breez_id)
#   4. Мобильные кондиционеры (breez_id=10)
#   5. Аксессуары для кондиционеров (master, no breez_id)
_CATEGORY_RULES = [
    (re.compile(
        r'мультисплит|мульти.?сплит|multi.?split|'
        r'внутренн\w*\s+блок|наружн\w*\s+блок|indoor.?unit|outdoor.?unit',
        re.I | re.UNICODE,
     ),
     'breez:9'),
    (re.compile(r'мобильн', re.I | re.UNICODE),
     'breez:10'),
    (re.compile(r'полупромышленн|канальн|кассетн|напольн|потолочн|кровельн',
                re.I | re.UNICODE),
     'breez:9'),   # Полупромышленные сплит-системы
    (re.compile(r'аксессуар|запчаст|комплектующ|расходн',
                re.I | re.UNICODE),
     'master:Аксессуары для кондиционеров'),
    (re.compile(r'сплит|split|кондиционер|инвертор|настенн|бытов|он.?офф|on.?off',
                re.I | re.UNICODE),
     'breez:2'),
]


def _resolve_master_category(path):
    """Return (kind, value): kind='breez_id'|'master', value=int|str."""
    for pattern, target in _CATEGORY_RULES:
        if pattern.search(path):
            kind, val = target.split(':', 1)
            if kind == 'breez':
                return 'breez_id', int(val)
            return 'master', val
    return None, None


def _make_unique_slug(model, base, exclude_pk=None):
    slug = base[:490]
    qs = model.objects.filter(slug=slug)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    if not qs.exists():
        return slug
    slug = f'{base[:480]}-rk'
    if not model.objects.filter(slug=slug).exclude(pk=exclude_pk or 0).exists():
        return slug
    import hashlib
    suffix = hashlib.md5(base.encode()).hexdigest()[:6]
    return f'{base[:480]}-{suffix}'


class Command(BaseCommand):
    help = (
        'Re-map products (Breeze + Rusklimat) into the 5 master catalog '
        'categories and disable sync_enabled on all non-master categories. '
        'Runs in dry-run mode unless --apply is given.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Actually update DB. Without this flag the command only prints summary.')
        parser.add_argument('--source', choices=['breeze', 'rusklimat', 'all'],
                            default='all', help='Limit remap to one source (default: all).')

    def handle(self, *args, **options):
        apply = options['apply']
        source = options['source']

        qs = Product.objects.select_related('category__parent')
        if source != 'all':
            qs = qs.filter(source=source)

        total = qs.count()
        self.stdout.write(f'Scanning {total} products (source={source})...')

        cat_cache = {}
        counters = {
            'remapped_breeze': 0,
            'remapped_rusklimat': 0,
            'unchanged': 0,
            'no_category': 0,
            'no_match': 0,
            'fallback_to_default': 0,
        }
        target_ids = set()

        plan = []  # list of (product_id, src_source, new_cat_id) — applied if --apply

        for product in qs.iterator(chunk_size=500):
            cat = product.category
            if cat is None:
                counters['no_category'] += 1
                continue

            # Розничная техника уже лежит в своей категории — правила
            # переназначения написаны под кондиционеры и только навредят.
            if cat.breez_id in _KEEP_CATEGORY_BREEZ_IDS or cat.title in _KEEP_CATEGORY_TITLES:
                counters['unchanged'] += 1
                continue

            path = f'{cat.parent.title} - {cat.title}' if cat.parent_id else cat.title
            kind, value = _resolve_master_category(path)

            if kind is None:
                counters['no_match'] += 1
                continue

            new_cat = self._resolve_target(kind, value, cat_cache)
            if new_cat is None:
                counters['no_match'] += 1
                continue

            target_ids.add(new_cat.pk)

            # Track fallback-to-default cases (matched on the last "split-system" rule)
            if kind == 'breez_id' and value == 2 and not _looks_like_default_category(path):
                counters['fallback_to_default'] += 1

            if product.category_id == new_cat.pk:
                counters['unchanged'] += 1
                continue

            plan.append((product.pk, product.source, new_cat.pk))
            counters[f'remapped_{product.source}'] = counters.get(f'remapped_{product.source}', 0) + 1

        # Categories to deactivate: everything that is NOT a master
        master_qs = self._master_queryset()
        master_ids = set(master_qs.values_list('id', flat=True))
        non_master_qs = Category.objects.exclude(id__in=master_ids).filter(sync_enabled=True)
        to_deactivate = non_master_qs.count()

        self.stdout.write('')
        self.stdout.write(f"  → remapped (breeze):     {counters['remapped_breeze']}")
        self.stdout.write(f"  → remapped (rusklimat):  {counters['remapped_rusklimat']}")
        self.stdout.write(f"  → unchanged:             {counters['unchanged']}")
        self.stdout.write(f"  → product without cat:   {counters['no_category']}")
        self.stdout.write(f"  → no rule matched:       {counters['no_match']}")
        self.stdout.write(f"  → fallback to default:   {counters['fallback_to_default']}")
        self.stdout.write(f"  → non-master categories to disable: {to_deactivate}")
        self.stdout.write(f"  → master categories used:           {len(target_ids)}")

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDry-run mode. Re-run with --apply to write changes.'
            ))
            return

        with transaction.atomic():
            # Bulk-update products in chunks of 1000
            for i in range(0, len(plan), 1000):
                chunk = plan[i:i + 1000]
                for pid, _src, new_cat_id in chunk:
                    Product.objects.filter(pk=pid).update(category_id=new_cat_id)

            master_qs.update(sync_enabled=True)
            non_master_qs.update(sync_enabled=False)

        self.stdout.write(self.style.SUCCESS(
            f'\nApplied. Products updated: {len(plan)}, '
            f'non-master categories disabled: {to_deactivate}.'
        ))

    def _resolve_target(self, kind, value, cat_cache):
        cache_key = (kind, value)
        if cache_key in cat_cache:
            return cat_cache[cache_key]

        if kind == 'breez_id':
            cat = Category.objects.filter(breez_id=value).first()
        elif kind == 'master':
            cat = Category.objects.filter(title__iexact=value).first()
            if not cat:
                slug = _make_unique_slug(Category, slugify(value, allow_unicode=True) or 'category')
                cat = Category.objects.create(
                    title=value, slug=slug, breez_id=None, sync_enabled=True,
                )
        else:
            cat = None

        cat_cache[cache_key] = cat
        return cat

    def _master_queryset(self):
        return Category.objects.filter(
            breez_id__in=MASTER_BREEZ_IDS,
        ) | Category.objects.filter(title__in=MASTER_TITLES)


def _looks_like_default_category(path):
    """Return True if the path explicitly mentions 'настенн' / 'бытов' / 'инвертор' —
    i.e. the default rule matched intentionally, not as a last-resort fallback."""
    import re as _re
    return bool(_re.search(r'настенн|бытов|инвертор', path, _re.I | _re.UNICODE))

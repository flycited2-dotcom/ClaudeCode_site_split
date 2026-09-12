from django.core.management.base import BaseCommand

from apps.catalog.heating import (
    HEATING_THRESHOLDS, apply_heating_fields, min_heating_temp_for,
)
from apps.catalog.models import Product


class Command(BaseCommand):
    help = (
        'Проставляет Product.heating_min_temp и is_heat_pump существующим товарам '
        'из уже синканных характеристик. Разово, после добавления полей; дальше '
        'их держат в актуальном состоянии сами синки. Dry-run по умолчанию — '
        'печатает сводку, ничего не пишет.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Записать изменения в БД. Без флага — только dry-run.')

    def handle(self, *args, **options):
        apply_changes = options['apply']

        total = with_temp = changed = 0
        by_threshold = {t: 0 for t in HEATING_THRESHOLDS}

        queryset = Product.objects.prefetch_related('tech_values__spec')
        for product in queryset.iterator(chunk_size=500):
            total += 1
            temp = min_heating_temp_for(product)
            if temp is not None:
                with_temp += 1
                for threshold in HEATING_THRESHOLDS:
                    if temp <= threshold:
                        by_threshold[threshold] += 1
            # declared=is_heat_pump — команда дозаполняет признак, но НЕ снимает
            # уже выставленный. Rusklimat объявляет тепловой насос названием
            # КАТЕГОРИИ (apps/sync/rusklimat_rest.py), а категорий поставщика
            # здесь нет, поэтому вызов без declared обнулял такие товары: прогон
            # 2026-09-12 выбил из подборки настенные теплонасосы AKEBONO NORDIC,
            # у которых нет распознанного диапазона обогрева.
            if apply_changes and apply_heating_fields(product, declared=product.is_heat_pump):
                changed += 1

        self.stdout.write(f'Всего товаров: {total}')
        self.stdout.write(f'С распознанной температурой обогрева: {with_temp}')
        for threshold in HEATING_THRESHOLDS:
            self.stdout.write(f'  → до {threshold} °C и ниже: {by_threshold[threshold]}')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                '\nDry-run mode. Запустите с --apply, чтобы записать изменения.'
            ))
            return

        pumps = Product.objects.filter(is_heat_pump=True).count()
        self.stdout.write(f'С признаком «тепловой насос» после записи: {pumps}')
        self.stdout.write(self.style.SUCCESS(f'\nПрименено. Обновлено товаров: {changed}.'))

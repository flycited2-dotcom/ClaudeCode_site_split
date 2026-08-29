from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import Category, Product

# Категории-дубли: товары переносятся в целевую, исходная выключается
# (не удаляется — так откат сводится к возврату sync_enabled).
#
# Разобрано 2026-08-29 по дереву каталога на проде:
#  - «Полупромышленные кондиционеры» создала старая remap_categories, и товары
#    полупрома оказались размазаны между ней и бризовской категорией;
#  - «Двухконтурные газовые котлы» (2 товара) — частный случай отопительных;
#  - ИК-обогреватели были разбиты на «потолочные» и «электрические» без пользы
#    для покупателя.
MERGES = [
    # (что сливаем, куда сливаем, новое название целевой или None)
    ('Полупромышленные кондиционеры', 'Полупромышленные сплит-системы', None),
    ('Двухконтурные газовые котлы',   'Отопительные котлы',              None),
    ('Инфракрасные потолочные обогреватели', 'Инфракрасные электрические обогреватели',
     'Инфракрасные обогреватели'),
]


class Command(BaseCommand):
    help = (
        'Сливает категории-дубли: переносит товары в целевую категорию и '
        'выключает исходную. Dry-run по умолчанию.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Записать изменения в БД. Без флага — только показать.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        moved_total = 0

        for source_title, target_title, rename_to in MERGES:
            source = Category.objects.filter(title=source_title).first()
            target = Category.objects.filter(title=target_title).first()

            if source is None:
                self.stdout.write(f'  пропуск: нет категории «{source_title}»')
                continue
            if target is None:
                self.stdout.write(self.style.WARNING(
                    f'  НЕТ целевой категории «{target_title}» — слияние пропущено'
                ))
                continue
            if source.pk == target.pk:
                self.stdout.write(f'  пропуск: «{source_title}» уже слита')
                continue

            count = Product.objects.filter(category=source).count()
            moved_total += count
            self.stdout.write(self.style.SUCCESS(
                f'  «{source_title}» → «{target_title}»: перенос {count} товаров'
            ))
            if rename_to:
                self.stdout.write(f'      целевая переименуется в «{rename_to}»')

            if not apply_changes:
                continue

            with transaction.atomic():
                Product.objects.filter(category=source).update(category=target)
                source.sync_enabled = False
                source.save(update_fields=['sync_enabled'])
                if rename_to:
                    target.title = rename_to
                    target.save(update_fields=['title'])

        self.stdout.write('')
        self.stdout.write(f'Товаров к переносу: {moved_total}')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                '\nDry-run mode. Запустите с --apply, чтобы записать изменения.'
            ))
            return

        self.stdout.write(self.style.SUCCESS('\nПрименено.'))

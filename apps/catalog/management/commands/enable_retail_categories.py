from django.core.management.base import BaseCommand

from apps.catalog.models import Category

# Категории Бриза, которые продаём в рознице помимо кондиционеров (2026-08-29).
# Ключ — breez_id, значение — название для сверки (если у поставщика поменяется
# название, команда покажет расхождение, а не включит вслепую).
#
# Отобраны по разведке API: розничная климатическая и тепловая техника с
# крымским остатком. НЕ включаем аксессуары, расходники, монтажный инструмент,
# компоненты систем (частотники, приводы, дренажные насосы) и блоки мульти-сплит.
RETAIL_CATEGORIES = {
    # Обогрев
    33:  'Конвекторы',
    34:  'Масляные радиаторы',
    37:  'Тепловые пушки',
    31:  'Тепловентиляторы',
    38:  'Тепловые завесы',
    99:  'Инфракрасные потолочные обогреватели',
    36:  'Инфракрасные электрические обогреватели',
    # Горячая вода и отопление
    40:  'Накопительные водонагреватели',
    118: 'Проточные водонагреватели',
    120: 'Двухконтурные газовые котлы',
    # Вентиляция
    88:  'Бытовые вентиляционные установки',
    45:  'Компактные моноблочные вентиляционные установки',
    90:  'Рекуператоры',
    # Воздух
    27:  'Бытовые увлажнители воздуха',
    25:  'Бытовые осушители воздуха',
}

# Категории, которых нет в дереве Бриза, но товары под них есть у других
# поставщиков. Создаются без breez_id — так же, как «Аксессуары для
# кондиционеров». Ключ — title (по нему маппит синк Daichi), значение — slug.
OWN_CATEGORIES = {
    # У Daichi в Крыму 31 котёл: газовые настенные и электрические вперемешку,
    # поэтому не кладём их в бризовские «Газовые котлы».
    'Отопительные котлы': 'otopitelnye-kotly',
    # Отопление здания — у Rusklimat это полтора десятка мелких разделов
    # (секционные, биметаллические, стальные панельные и трубчатые,
    # алюминиевые). Покупателю такое дробление не нужно, сводим в один.
    'Радиаторы отопления': 'radiatory-otopleniya',
    # Маты, нагревательный кабель и терморегуляторы для пола.
    'Тёплый пол': 'teplyy-pol',
}


class Command(BaseCommand):
    help = (
        'Включает sync_enabled у розничных категорий помимо кондиционеров '
        '(обогрев, водонагреватели, вентиляция, увлажнители, осушители). '
        'После этого нужен sync_breez + sync_stock. Dry-run по умолчанию.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Записать изменения в БД. Без флага — только показать.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        enabled = skipped = missing = 0

        for breez_id, expected_title in RETAIL_CATEGORIES.items():
            category = Category.objects.filter(breez_id=breez_id).first()
            if category is None:
                self.stdout.write(self.style.WARNING(
                    f'  НЕТ в БД: {breez_id} «{expected_title}» — запустите sync_categories'
                ))
                missing += 1
                continue

            if category.title.strip().lower() != expected_title.lower():
                self.stdout.write(self.style.WARNING(
                    f'  Название разошлось: id={breez_id} в БД «{category.title}», '
                    f'ожидалось «{expected_title}» — пропускаю'
                ))
                skipped += 1
                continue

            if category.sync_enabled:
                self.stdout.write(f'  уже включена: {category.title}')
                continue

            self.stdout.write(self.style.SUCCESS(f'  включаю: {category.title}'))
            enabled += 1
            if apply_changes:
                category.sync_enabled = True
                category.save(update_fields=['sync_enabled'])

        created = 0
        for title, slug in OWN_CATEGORIES.items():
            category = Category.objects.filter(title=title).first()
            if category is not None:
                if not category.sync_enabled:
                    self.stdout.write(self.style.SUCCESS(f'  включаю: {title}'))
                    enabled += 1
                    if apply_changes:
                        category.sync_enabled = True
                        category.save(update_fields=['sync_enabled'])
                else:
                    self.stdout.write(f'  уже включена: {title}')
                continue
            self.stdout.write(self.style.SUCCESS(f'  создаю категорию: {title}'))
            created += 1
            if apply_changes:
                Category.objects.create(title=title, slug=slug, sync_enabled=True)

        self.stdout.write('')
        self.stdout.write(
            f'Будет включено: {enabled}, создано: {created}, '
            f'пропущено: {skipped}, нет в БД: {missing}'
        )

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                '\nDry-run mode. Запустите с --apply, чтобы записать изменения.'
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            f'\nПрименено. Дальше: sync_breez (импорт товаров) и sync_stock (остатки).'
        ))

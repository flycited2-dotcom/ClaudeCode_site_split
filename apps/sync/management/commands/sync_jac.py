from django.core.management.base import BaseCommand

from apps.sync.jac_catalog import sync_catalog


class Command(BaseCommand):
    help = 'Загрузить каталог Профконда (b2b-jac.com) из файлов скрапера.'

    def handle(self, *args, **options):
        result = sync_catalog()
        self.stdout.write(self.style.SUCCESS(f'JAC sync: {result}'))

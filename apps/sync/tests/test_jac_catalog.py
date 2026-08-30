"""Тесты синка Профконда (b2b-jac.com).

Данные приходят файлом от скрапера, поэтому в тестах — временный JSON,
никакой сети. Проверяем то, что легко сломать: раскладку по категориям,
разбор «Больше 100» и «71 700 ₽», и главное — что московский склад с
названием «Москва (ОП АЯК - Крым)» не считается крымским остатком.
"""
import json
import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from apps.catalog.models import Brand, Category, Product
from apps.sync.jac_catalog import _price, _qty, _photo_url, sync_catalog


class ParseHelpersTest(SimpleTestCase):

    def test_qty_plain_number(self):
        self.assertEqual(_qty('12'), 12)

    def test_qty_more_than(self):
        """«Больше 100» — портал не показывает точное число, берём минимум."""
        self.assertEqual(_qty('Больше 100'), 100)

    def test_qty_empty_and_zero(self):
        self.assertEqual(_qty(''), 0)
        self.assertEqual(_qty(None), 0)
        self.assertEqual(_qty('0'), 0)

    def test_price_from_number(self):
        self.assertEqual(_price(37280.0), Decimal('37280.0'))

    def test_price_from_rrc_string(self):
        self.assertEqual(_price('71 700 ₽'), Decimal('71700'))

    def test_price_empty(self):
        self.assertIsNone(_price(''))
        self.assertIsNone(_price(None))

    def test_photo_url_case_insensitive(self):
        photos = {'MDV': {'CLASSIC INVERTER': 'https://example.com/a.png'}}
        self.assertEqual(
            _photo_url(photos, 'mdv', 'classic inverter'), 'https://example.com/a.png')

    def test_photo_local_file_ignored(self):
        """Скрапер кладёт локальные файлы THAICON — на сайте нужен URL."""
        photos = {'THAICON': {'Alba': 'alba.png'}}
        self.assertIsNone(_photo_url(photos, 'THAICON', 'Alba'))

    def test_photo_missing_series(self):
        self.assertIsNone(_photo_url({'MDV': {}}, 'MDV', 'Нет такой'))


def _row(article, category='Бытовые сплит-системы', brand='MDV',
         crimea='0', mainland='0', price=1000.0, series='TEST'):
    return {
        'article': article, 'name': article, 'brand': brand, 'series': series,
        'price': price, 'currency': 'RUB', 'category': category,
        'attributes': {
            'Холод, кВт': '2.6', 'РРЦ': '71 700 ₽',
            'Крым': crimea, 'Москва (ОП АЯК - Крым)': mainland,
        },
    }


class SyncCatalogTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        Category.objects.create(title='Бытовые сплит-системы', slug='jac-split',
                                sync_enabled=True)
        Category.objects.create(title='Полупромышленные сплит-системы',
                                slug='jac-semi', sync_enabled=True)

    def _run(self, rows, specs=None, photos=None):
        tmp = Path(tempfile.mkdtemp())
        stock_path = tmp / 'stock.json'
        stock_path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        paths = {'JAC_STOCK_JSON': str(stock_path)}
        if specs is not None:
            specs_path = tmp / 'specs.json'
            specs_path.write_text(json.dumps(specs, ensure_ascii=False), encoding='utf-8')
            paths['JAC_SPECS_JSON'] = str(specs_path)
        if photos is not None:
            photos_path = tmp / 'photos.json'
            photos_path.write_text(json.dumps(photos, ensure_ascii=False), encoding='utf-8')
            paths['JAC_PHOTOS_JSON'] = str(photos_path)
        with override_settings(**paths):
            return sync_catalog()

    def test_product_created_with_prices(self):
        result = self._run([_row('EKSA-70HN / EKOA-70HN', price=37280.0)])
        self.assertEqual(result['created'], 1)
        p = Product.objects.get(source='jac')
        self.assertEqual(p.articul, 'EKSA-70HN / EKOA-70HN')
        self.assertEqual(p.price_wholesale, Decimal('37280.00'))
        self.assertEqual(p.ric, Decimal('71700.00'))

    def test_title_has_brand_and_type(self):
        self._run([_row('EKSA-70HN', brand='EUROKLIMAT', series='Alba')])
        p = Product.objects.get(source='jac')
        self.assertEqual(p.title, 'EUROKLIMAT Сплит-система Alba EKSA-70HN')

    def test_moscow_stock_is_not_crimea(self):
        """«Москва (ОП АЯК - Крым)» содержит слово «Крым» — это склад
        отгрузки, а не местный остаток.

        Если отдать это название как есть, _CRIMEA_RE из warehouse_stock.py
        посчитает его крымским и товар «под заказ» покажется как местный.
        Поэтому склад переименован: витрина ставит «Под заказ из Москвы».
        """
        self._run([_row('ART-MSK', crimea='0', mainland='Больше 100')])
        p = Product.objects.get(source='jac')
        self.assertEqual(p.stock.warehouse, 'Москва (ОП АЯК)')
        self.assertNotEqual(p.stock.warehouse, 'Симферополь')
        warehouses = dict(p.warehouse_stocks.values_list('warehouse', 'quantity'))
        self.assertEqual(warehouses.get('Москва (ОП АЯК)'), 100)
        self.assertEqual(warehouses.get('Симферополь'), 0)

    def test_crimea_stock_written(self):
        self._run([_row('ART-CR', crimea='2', mainland='5')])
        p = Product.objects.get(source='jac')
        self.assertEqual(p.stock.quantity, 2)

    def test_multisplit_and_accessories_skipped(self):
        result = self._run([
            _row('MS-1', category='Мультисплит-системы'),
            _row('ACC-1', category='Аксессуары'),
            _row('OK-1'),
        ])
        self.assertEqual(result['created'], 1)
        self.assertEqual(result['skipped_no_category'], 2)

    def test_semi_industrial_mapped(self):
        self._run([_row('SEMI-1', category='Полупромышленные системы')])
        p = Product.objects.get(source='jac')
        self.assertEqual(p.category.title, 'Полупромышленные сплит-системы')

    def test_brand_created_once(self):
        self._run([_row('A-1', brand='THAICON'), _row('A-2', brand='THAICON')])
        self.assertEqual(Brand.objects.filter(title='THAICON').count(), 1)

    def test_rerun_updates_not_duplicates(self):
        self._run([_row('SAME-1', crimea='1')])
        result = self._run([_row('SAME-1', crimea='4')])
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['updated'], 1)
        self.assertEqual(Product.objects.filter(source='jac').count(), 1)
        self.assertEqual(Product.objects.get(source='jac').stock.quantity, 4)

    def test_missing_product_deactivated(self):
        self._run([_row('GONE-1'), _row('STAY-1')])
        self._run([_row('STAY-1')])
        self.assertFalse(Product.objects.get(articul='GONE-1').is_active)
        self.assertTrue(Product.objects.get(articul='STAY-1').is_active)

    def test_long_article_fits_nc_code(self):
        """У портала артикул бывает длиннее поля nc_code (50 символов)."""
        long_article = '17310900A06402 Wi-Fi модуль для полупромышленных систем ' * 2
        self._run([_row(long_article.strip())])
        p = Product.objects.get(source='jac')
        self.assertLessEqual(len(p.nc_code), 50)

    def test_specs_written_and_btu_computed(self):
        """«Холод, кВт» переименовывается в общее название — иначе мощность
        не попадёт в btu_calc и товар выпадет из фасеты мощности."""
        self._run(
            [_row('SPEC-1')],
            specs={'SPEC-1': {'characteristics': {'Холод, кВт': '2.6'}}},
        )
        p = Product.objects.get(source='jac')
        titles = list(p.tech_values.values_list('spec__title', flat=True))
        self.assertIn('Холодопроизводительность, кВт', titles)
        self.assertEqual(p.btu_calc, 9)

    def test_heating_temp_makes_heat_pump(self):
        """Нижняя граница обогрева −30 → товар попадает в тепловые насосы."""
        self._run(
            [_row('HP-1')],
            specs={'HP-1': {'characteristics': {
                'Нижний диапазон рабочих температурных границ, нагрев, °C': '-30',
            }}},
        )
        p = Product.objects.get(source='jac')
        self.assertEqual(p.heating_min_temp, -30)

    def test_photo_attached(self):
        self._run(
            [_row('PH-1', brand='MDV', series='CLASSIC INVERTER')],
            photos={'MDV': {'CLASSIC INVERTER': 'https://example.com/mdv.png'}},
        )
        p = Product.objects.get(source='jac')
        self.assertEqual([i.url for i in p.images.all()], ['https://example.com/mdv.png'])

    def test_missing_file_is_noop(self):
        """Файла нет → синк не должен гасить уже загруженные товары."""
        self._run([_row('KEEP-1')])
        with override_settings(JAC_STOCK_JSON='/nonexistent/jac.json'):
            result = sync_catalog()
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['deactivated'], 0)
        self.assertTrue(Product.objects.get(articul='KEEP-1').is_active)

    def test_broken_json_is_noop(self):
        tmp = Path(tempfile.mkdtemp()) / 'broken.json'
        tmp.write_text('{не json', encoding='utf-8')
        with override_settings(JAC_STOCK_JSON=str(tmp)):
            result = sync_catalog()
        self.assertEqual(result['created'], 0)

    def test_row_without_article_skipped(self):
        result = self._run([{'article': '', 'name': '', 'category': 'Бытовые сплит-системы',
                             'brand': 'MDV', 'attributes': {}}])
        self.assertEqual(result['skipped_no_article'], 1)

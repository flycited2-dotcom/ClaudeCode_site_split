"""Юнит-тесты для apps/sync/rusklimat_rest._sync_tech_specs.

Функция парсит ответ Rusklimat REST API v1/v2 в TechSpec/ProductTech и
вызывает refresh_btu_calc после записи. refresh_btu_calc мокаем по
адресу определения (`apps.catalog.btu.refresh_btu_calc`), т.к. импорт
сделан внутри функции.
"""
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from apps.catalog.models import Brand, Category, Product, ProductTech, TechSpec
from apps.sync.rusklimat_rest import (
    _AC_CATEGORY_RE, _AC_EXCLUDE_RE, _find_ac_categories, _is_heat_pump_category,
    _sync_tech_specs,
)


class _SyncTechSpecsBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            title='Сплиты', slug='split', sync_enabled=True,
        )
        cls.brand = Brand.objects.create(title='Midea', slug='midea')

    def _make_product(self, nc='NC-1'):
        return Product.objects.create(
            nc_code=nc, title=f'Test {nc}',
            category=self.category, brand=self.brand,
            price_wholesale=Decimal('1000.00'),
        )


class SyncTechSpecsTest(_SyncTechSpecsBase):

    def test_returns_zero_on_empty_dict(self):
        p = self._make_product()
        with patch('apps.catalog.btu.refresh_btu_calc') as m:
            count = _sync_tech_specs(p, {}, {}, {}, {})
        self.assertEqual(count, 0)
        # refresh_btu_calc не вызывается, если нечего записать.
        m.assert_not_called()

    def test_returns_zero_on_non_dict(self):
        p = self._make_product()
        with patch('apps.catalog.btu.refresh_btu_calc'):
            self.assertEqual(_sync_tech_specs(p, None, {}, {}, {}), 0)
            self.assertEqual(_sync_tech_specs(p, 'not dict', {}, {}, {}), 0)

    def test_v2_format_with_dict_value_and_unit(self):
        p = self._make_product()
        props = {'prop-uuid-1': {'value': '2.65', 'unit': 'unit-kw'}}
        meta = {'prop-uuid-1': {'id': 'prop-uuid-1', 'name': 'Мощность охлаждения', 'sort': 5}}
        units = {'unit-kw': {'name': 'кВт', 'nameFull': 'киловатт'}}
        with patch('apps.catalog.btu.refresh_btu_calc') as m:
            count = _sync_tech_specs(p, props, {}, meta, units)
        self.assertEqual(count, 1)
        pt = ProductTech.objects.get(product=p)
        self.assertEqual(pt.value, '2.65')
        self.assertEqual(pt.spec.title, 'Мощность охлаждения')
        self.assertEqual(pt.spec.unit, 'кВт')
        self.assertEqual(pt.spec.order, 5)
        m.assert_called_once_with(p)

    def test_v1_format_string_value(self):
        p = self._make_product()
        props = {'prop-uuid-1': 'просто строка'}
        meta = {'prop-uuid-1': {'name': 'Тип компрессора', 'sort': 0}}
        with patch('apps.catalog.btu.refresh_btu_calc'):
            count = _sync_tech_specs(p, props, {}, meta, {})
        self.assertEqual(count, 1)
        pt = ProductTech.objects.get(product=p)
        self.assertEqual(pt.value, 'просто строка')

    def test_empty_value_skipped(self):
        p = self._make_product()
        props = {
            'a': {'value': '', 'unit': ''},
            'b': {'value': None, 'unit': ''},
            'c': '   ',  # whitespace
        }
        meta = {'a': {'name': 'A'}, 'b': {'name': 'B'}, 'c': {'name': 'C'}}
        with patch('apps.catalog.btu.refresh_btu_calc') as m:
            count = _sync_tech_specs(p, props, {}, meta, {})
        self.assertEqual(count, 0)
        m.assert_not_called()

    def test_meta_missing_name_skipped(self):
        p = self._make_product()
        # prop-uuid-1 нет в meta → spec не создастся.
        props = {'prop-uuid-1': {'value': '5', 'unit': ''}}
        with patch('apps.catalog.btu.refresh_btu_calc'):
            count = _sync_tech_specs(p, props, {}, {}, {})
        self.assertEqual(count, 0)
        self.assertFalse(ProductTech.objects.filter(product=p).exists())

    def test_meta_empty_name_skipped(self):
        p = self._make_product()
        props = {'prop-uuid-1': {'value': '5', 'unit': ''}}
        meta = {'prop-uuid-1': {'name': '   ', 'sort': 0}}
        with patch('apps.catalog.btu.refresh_btu_calc'):
            count = _sync_tech_specs(p, props, {}, meta, {})
        self.assertEqual(count, 0)

    def test_cache_hit_skips_db_create(self):
        # Spec уже в cache → update_or_create в БД не вызывается.
        p = self._make_product()
        existing_spec = TechSpec.objects.create(
            external_uuid='prop-uuid-cached', title='Cached', unit='', order=0,
        )
        cache = {'prop-uuid-cached': existing_spec}
        props = {'prop-uuid-cached': 'value-1'}
        with patch('apps.catalog.btu.refresh_btu_calc'):
            count = _sync_tech_specs(p, props, cache, {}, {})
        self.assertEqual(count, 1)
        # Один TechSpec — тот, что в cache. Дубля не появилось.
        self.assertEqual(TechSpec.objects.filter(external_uuid='prop-uuid-cached').count(), 1)
        pt = ProductTech.objects.get(product=p)
        self.assertEqual(pt.spec_id, existing_spec.pk)

    def test_cache_populated_for_new_spec(self):
        # При cache-miss spec создаётся в БД И добавляется в cache.
        p = self._make_product()
        cache = {}
        props = {'prop-uuid-new': {'value': 'x', 'unit': ''}}
        meta = {'prop-uuid-new': {'name': 'NewSpec', 'sort': 1}}
        with patch('apps.catalog.btu.refresh_btu_calc'):
            _sync_tech_specs(p, props, cache, meta, {})
        self.assertIn('prop-uuid-new', cache)
        self.assertEqual(cache['prop-uuid-new'].title, 'NewSpec')

    def test_replace_strategy_clears_old_product_tech(self):
        p = self._make_product()
        old_spec = TechSpec.objects.create(
            external_uuid='old', title='Old', unit='', order=0,
        )
        ProductTech.objects.create(product=p, spec=old_spec, value='legacy')
        # Новый sync без 'old' — старые ProductTech должны удалиться.
        props = {'new': {'value': '7', 'unit': ''}}
        meta = {'new': {'name': 'New'}}
        with patch('apps.catalog.btu.refresh_btu_calc'):
            _sync_tech_specs(p, props, {}, meta, {})
        # Старый ProductTech ушёл.
        self.assertFalse(ProductTech.objects.filter(product=p, spec=old_spec).exists())
        # Новый есть.
        self.assertEqual(ProductTech.objects.filter(product=p).count(), 1)

    def test_value_stripped(self):
        p = self._make_product()
        props = {'a': {'value': '  9.5  ', 'unit': ''}}
        meta = {'a': {'name': 'A'}}
        with patch('apps.catalog.btu.refresh_btu_calc'):
            _sync_tech_specs(p, props, {}, meta, {})
        pt = ProductTech.objects.get(product=p)
        self.assertEqual(pt.value, '9.5')

    def test_invalid_sort_defaults_to_zero(self):
        p = self._make_product()
        props = {'a': {'value': 'x', 'unit': ''}}
        meta = {'a': {'name': 'A', 'sort': 'not-an-int'}}
        with patch('apps.catalog.btu.refresh_btu_calc'):
            _sync_tech_specs(p, props, {}, meta, {})
        spec = TechSpec.objects.get(external_uuid='a')
        self.assertEqual(spec.order, 0)


class HeatPumpCategoryTest(SimpleTestCase):
    """Категории тепловых насосов Rusklimat должны проходить в синк.

    До 2026-08-28 _AC_CATEGORY_RE их не пропускал, и 23 позиции («Тепловые
    насосы воздух-воздух» — это те же сплит-системы) в каталог не попадали.
    """

    def test_heat_pump_category_passes_filter(self):
        name = 'Тепловые насосы воздух-воздух'
        self.assertTrue(_AC_CATEGORY_RE.search(name))
        self.assertFalse(_AC_EXCLUDE_RE.search(name))

    def test_heat_pump_air_water_passes_filter(self):
        self.assertTrue(_AC_CATEGORY_RE.search('Тепловые насосы воздух-вода. Моноблоки'))

    def test_heat_pump_accessories_still_excluded(self):
        # Аксессуары к теплонасосам в розницу не нужны
        self.assertTrue(_AC_EXCLUDE_RE.search('Аксессуары для тепловых насосов'))

    def test_is_heat_pump_category_by_name(self):
        self.assertTrue(_is_heat_pump_category('Тепловые насосы воздух-воздух'))
        self.assertFalse(_is_heat_pump_category('Бытовые кондиционеры'))

    def test_find_ac_categories_returns_ids_and_names(self):
        client = Mock()
        client.get_categories.return_value = [
            {'id': 'uuid-1', 'name': 'Бытовые кондиционеры'},
            {'id': 'uuid-2', 'name': 'Тепловые насосы воздух-воздух'},
            {'id': 'uuid-3', 'name': 'Шланги садовые'},
        ]
        ids, names = _find_ac_categories(client)
        self.assertEqual(ids, {'uuid-1', 'uuid-2'})
        self.assertEqual(names['uuid-2'], 'Тепловые насосы воздух-воздух')
        self.assertNotIn('uuid-3', ids)


class MasterCategoryMapTest(TestCase):
    """Раскладка товаров Rusklimat по нашим категориям.

    Регрессия 2026-08-29: в _master_category_for передавался categoryId (uuid)
    вместо названия, поэтому не срабатывало ни одно условие и ВСЕ товары
    Rusklimat (2823 штуки) складывались в «Бытовые сплит-системы».
    """

    @classmethod
    def setUpTestData(cls):
        from apps.catalog.models import Category
        # Часть категорий уже создаёт data-миграция каталога — берём или создаём
        for breez_id, title in ((2, 'Бытовые сплит-системы'), (10, 'Мобильные кондиционеры'),
                                (9, 'Полупромышленные сплит-системы'), (40, 'Накопительные водонагреватели'),
                                (33, 'Конвекторы'), (25, 'Бытовые осушители воздуха')):
            Category.objects.get_or_create(
                breez_id=breez_id, defaults={'title': title, 'slug': f'cat-{breez_id}'},
            )

    def test_mobile_goes_to_mobile(self):
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(_master_category_for('Инверторные мобильные кондиционеры').breez_id, 10)

    def test_water_heater_goes_to_water_heaters(self):
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(_master_category_for('Водонагреватели накопительные').breez_id, 40)

    def test_convector_goes_to_convectors(self):
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(_master_category_for('Электрические конвекторы').breez_id, 33)

    def test_dehumidifier_goes_to_dehumidifiers(self):
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(_master_category_for('Бытовые осушители воздуха').breez_id, 25)

    def test_uuid_falls_back_to_split_systems(self):
        # uuid не должен матчиться ни на что — раньше это было единственным
        # поведением и ломало раскладку
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(_master_category_for('53d307a2-1234-5678-9abc-def012345678').breez_id, 2)

    def test_retail_categories_pass_filter(self):
        from apps.sync.rusklimat_rest import _AC_CATEGORY_RE, _AC_EXCLUDE_RE
        for name in ('Водонагреватели накопительные', 'Электрические конвекторы',
                     'Электрические тепловые пушки', 'Бытовые осушители воздуха'):
            self.assertTrue(_AC_CATEGORY_RE.search(name), name)
            self.assertFalse(_AC_EXCLUDE_RE.search(name), name)


class OilRadiatorCategoryTest(TestCase):
    """У Rusklimat раздел называется «Маслонаполненные радиаторы».

    Наш фильтр искал «масляные» и промахивался — 9 позиций с крымским
    остатком не доезжали (поймано владельцем 2026-08-29 по дереву каталога).
    """

    @classmethod
    def setUpTestData(cls):
        from apps.catalog.models import Category
        for breez_id, title in ((2, 'Бытовые сплит-системы'), (34, 'Масляные радиаторы'),
                                (37, 'Тепловые пушки')):
            Category.objects.get_or_create(
                breez_id=breez_id, defaults={'title': title, 'slug': f'cat-{breez_id}'},
            )

    def test_oil_radiators_pass_filter(self):
        from apps.sync.rusklimat_rest import _AC_CATEGORY_RE, _AC_EXCLUDE_RE
        name = 'Маслонаполненные радиаторы'
        self.assertTrue(_AC_CATEGORY_RE.search(name))
        self.assertFalse(_AC_EXCLUDE_RE.search(name))

    def test_oil_radiators_go_to_oil_radiators(self):
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(_master_category_for('Маслонаполненные радиаторы').breez_id, 34)

    def test_gas_heat_guns_pass_filter(self):
        from apps.sync.rusklimat_rest import _AC_CATEGORY_RE
        self.assertTrue(_AC_CATEGORY_RE.search('Газовые тепловые пушки'))

    def test_gas_heat_guns_go_to_heat_guns(self):
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(_master_category_for('Газовые тепловые пушки').breez_id, 37)


class HeatingCategoriesTest(TestCase):
    """Отопление здания: радиаторы и тёплый пол (2026-08-29).

    Берём и то, чего нет на крымском складе — владелец просил показывать
    такие позиции «под заказ», а не прятать.
    """

    @classmethod
    def setUpTestData(cls):
        from apps.catalog.models import Category
        Category.objects.get_or_create(breez_id=34, defaults={
            'title': 'Масляные радиаторы', 'slug': 'cat-34'})
        Category.objects.get_or_create(title='Радиаторы отопления', defaults={
            'slug': 'radiatory-otopleniya', 'sync_enabled': True})
        Category.objects.get_or_create(title='Тёплый пол', defaults={
            'slug': 'teplyy-pol', 'sync_enabled': True})

    def test_radiators_pass_filter(self):
        from apps.sync.rusklimat_rest import _AC_CATEGORY_RE, _AC_EXCLUDE_RE
        for name in ('Радиаторы биметаллические секционные', 'Радиаторы стальные панельные',
                     'Радиаторы секционные', 'Нагревательные маты для теплого пола'):
            self.assertTrue(_AC_CATEGORY_RE.search(name), name)
            self.assertFalse(_AC_EXCLUDE_RE.search(name), name)

    def test_radiator_accessories_still_excluded(self):
        from apps.sync.rusklimat_rest import _AC_CATEGORY_RE, _AC_EXCLUDE_RE
        for name in ('Кронштейны для радиаторов отопления', 'Комплектующие для теплых полов',
                     'Отражатели для радиаторов отопления'):
            passes = _AC_CATEGORY_RE.search(name) and not _AC_EXCLUDE_RE.search(name)
            self.assertFalse(passes, name)

    def test_radiators_go_to_own_category(self):
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(
            _master_category_for('Радиаторы биметаллические секционные').title,
            'Радиаторы отопления',
        )

    def test_warm_floor_goes_to_own_category(self):
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(
            _master_category_for('Нагревательные маты для теплого пола').title,
            'Тёплый пол',
        )

    def test_oil_radiators_not_hijacked_by_radiator_rule(self):
        """«Маслонаполненные радиаторы» должны остаться обогревателями."""
        from apps.sync.rusklimat_rest import _master_category_for
        self.assertEqual(_master_category_for('Маслонаполненные радиаторы').breez_id, 34)

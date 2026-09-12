"""Тесты парсера минимальной температуры обогрева.

SimpleTestCase — БД не нужна: parse_min_heating_temp чистая функция.
Кейсы взяты из реальных значений прода (разведка 2026-08-28): Бриз отдаёт
диапазон «-20 ~ +24», Daichi «-25~30» без пробелов, Rusklimat одно число.
"""
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from apps.catalog.heating import (
    apply_heating_fields, min_heating_temp_for, parse_min_heating_temp,
)
from apps.catalog.models import Brand, Category, Product, ProductTech, TechSpec


class ParseMinHeatingTempTest(SimpleTestCase):

    def test_breez_range(self):
        self.assertEqual(parse_min_heating_temp('-20 ~ +24'), -20)

    def test_daichi_range_without_spaces(self):
        self.assertEqual(parse_min_heating_temp('-25~30'), -25)

    def test_rusklimat_single_number(self):
        self.assertEqual(parse_min_heating_temp('-15'), -15)

    def test_unicode_minus(self):
        # Rusklimat местами присылает юникодный минус U+2212
        self.assertEqual(parse_min_heating_temp('−20 ~ +24'), -20)

    def test_en_dash_separator(self):
        self.assertEqual(parse_min_heating_temp('-30 – +24'), -30)

    def test_positive_only_range_is_none(self):
        # «+17 ~ +30» — машина на обогрев в минус не работает, в подборку не идёт
        self.assertIsNone(parse_min_heating_temp('+17 ~ +30'))

    def test_empty_is_none(self):
        self.assertIsNone(parse_min_heating_temp(''))

    def test_none_is_none(self):
        self.assertIsNone(parse_min_heating_temp(None))

    def test_garbage_is_none(self):
        self.assertIsNone(parse_min_heating_temp('нет данных'))

    def test_degree_suffix(self):
        self.assertEqual(parse_min_heating_temp('-22 °C'), -22)


class ApplyHeatingFieldsTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            title='Сплит-системы', slug='split-heating', sync_enabled=True,
        )
        cls.brand = Brand.objects.create(title='FUNAI', slug='funai-heating')

    def _product(self, nc):
        return Product.objects.create(
            nc_code=nc, articul=nc, category=self.category, brand=self.brand,
            title=f'AC {nc}', slug=f'ac-{nc}',
        )

    def _tech(self, product, spec_title, value):
        spec, _ = TechSpec.objects.get_or_create(title=spec_title)
        ProductTech.objects.create(product=product, spec=spec, value=value)

    def test_breez_spec_fills_field(self):
        p = self._product('NC-B1')
        self._tech(p, 'Рабочие температурные границы наружного воздуха (нагрев)', '-20 ~ +24')
        self.assertTrue(apply_heating_fields(p))
        p.refresh_from_db()
        self.assertEqual(p.heating_min_temp, -20)

    def test_daichi_spec_fills_field(self):
        p = self._product('NC-D1')
        self._tech(p, 'Диапазон рабочих температур, нагрев, °C', '-25~30')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertEqual(p.heating_min_temp, -25)

    def test_rusklimat_spec_fills_field(self):
        p = self._product('NC-R1')
        self._tech(p, 'Мин. рабочая температура воздуха для внешнего блока', '-15')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertEqual(p.heating_min_temp, -15)

    def test_two_specs_take_the_coldest(self):
        # У товара могут оказаться характеристики от двух источников —
        # берём минимальную (более холодную) границу.
        p = self._product('NC-M1')
        self._tech(p, 'Рабочие температурные границы наружного воздуха (нагрев)', '-15 ~ +24')
        self._tech(p, 'Диапазон рабочих температур, нагрев, °C', '-25~30')
        self.assertEqual(min_heating_temp_for(p), -25)

    def test_no_specs_leaves_none(self):
        p = self._product('NC-N1')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertIsNone(p.heating_min_temp)
        self.assertFalse(p.is_heat_pump)

    def test_breez_declared_flag(self):
        p = self._product('NC-F1')
        self._tech(p, 'Тепловой насос', 'да')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertTrue(p.is_heat_pump)

    def test_breez_declared_no_does_not_set_flag(self):
        p = self._product('NC-F2')
        self._tech(p, 'Тепловой насос', 'нет')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertFalse(p.is_heat_pump)

    def test_declared_argument_sets_flag(self):
        # Rusklimat объявляет теплонасос названием категории, не характеристикой
        p = self._product('NC-F3')
        apply_heating_fields(p, declared=True)
        p.refresh_from_db()
        self.assertTrue(p.is_heat_pump)

    def test_heating_range_sets_pump_flag(self):
        # Главный случай ассортимента: характеристику «Тепловой насос» поставщик
        # не прислал, но сплит греет до -20 — это и есть теплонасос воздух-воздух.
        p = self._product('NC-H1')
        self._tech(p, 'Рабочие температурные границы наружного воздуха (нагрев)', '-20 ~ +24')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertTrue(p.is_heat_pump)

    def test_heating_range_above_threshold_does_not_set_flag(self):
        p = self._product('NC-H2')
        self._tech(p, 'Мин. рабочая температура воздуха для внешнего блока', '-15')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertFalse(p.is_heat_pump)

    def test_non_split_kind_does_not_set_flag(self):
        # Порог по обогреву применяется только к сплит-системам: у аксессуаров
        # и компонентов мульти-сплита ярлыка «тепловой насос» быть не должно.
        p = self._product('NC-H3')
        p.kind = Product.KIND_ACCESSORY
        p.save(update_fields=['kind'])
        self._tech(p, 'Диапазон рабочих температур, нагрев, °C', '-25~30')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertEqual(p.heating_min_temp, -25)
        self.assertFalse(p.is_heat_pump)

    def _titled(self, nc, title):
        p = self._product(nc)
        p.title = title
        p.save(update_fields=['title'])
        return p

    def test_title_sets_pump_flag_without_heating_range(self):
        # Живой случай с прода: Rusklimat называет теплонасос прямо в названии,
        # но ни характеристики «Тепловой насос», ни диапазона обогрева не даёт.
        p = self._titled('NC-T1', 'Инверторный тепловой насос настенного типа серии AKEBONO NORDIC (R32)')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertIsNone(p.heating_min_temp)
        self.assertTrue(p.is_heat_pump)

    def test_pool_heat_pump_is_not_air_to_air(self):
        # Бассейновый теплонасос греет воду — в подборку воздух-воздух не идёт.
        p = self._titled('NC-T2', 'Тепловой насос для бассейна Royal Thermo MasterHeat Mini RTM-15MHN8')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertFalse(p.is_heat_pump)

    def test_air_to_water_title_is_not_air_to_air(self):
        p = self._titled('NC-T3', 'Тепловой насос воздух-вода Daichi DHP-8')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertFalse(p.is_heat_pump)

    def test_title_rule_does_not_apply_to_non_split(self):
        p = self._titled('NC-T4', 'Тепловой насос настенного типа, внешний блок')
        p.kind = Product.KIND_MULTI_SPLIT_BLOCK
        p.save(update_fields=['kind'])
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertFalse(p.is_heat_pump)

    def test_drainage_pump_title_does_not_set_flag(self):
        # «Насос» в названии аксессуара не делает его тепловым насосом.
        p = self._titled('NC-T5', 'Дренажный насос для кондиционера Aspen Mini Orange')
        apply_heating_fields(p)
        p.refresh_from_db()
        self.assertFalse(p.is_heat_pump)

    def test_returns_false_when_nothing_changed(self):
        p = self._product('NC-S1')
        self._tech(p, 'Мин. рабочая температура воздуха для внешнего блока', '-15')
        apply_heating_fields(p)
        # второй вызов ничего не меняет — лишнего UPDATE быть не должно
        self.assertFalse(apply_heating_fields(p))


class BackfillHeatingCommandTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            title='Сплит-системы', slug='split-backfill', sync_enabled=True,
        )

    def _product_with_temp(self, nc, value):
        p = Product.objects.create(
            nc_code=nc, articul=nc, category=self.category,
            title=f'AC {nc}', slug=f'ac-{nc}',
        )
        spec, _ = TechSpec.objects.get_or_create(
            title='Рабочие температурные границы наружного воздуха (нагрев)',
        )
        ProductTech.objects.create(product=p, spec=spec, value=value)
        return p

    def test_dry_run_does_not_write(self):
        p = self._product_with_temp('NC-DR', '-25 ~ +24')
        out = StringIO()
        call_command('backfill_heating', stdout=out)
        p.refresh_from_db()
        self.assertIsNone(p.heating_min_temp)
        self.assertIn('Dry-run', out.getvalue())

    def test_apply_writes_fields(self):
        p = self._product_with_temp('NC-AP', '-25 ~ +24')
        out = StringIO()
        call_command('backfill_heating', '--apply', stdout=out)
        p.refresh_from_db()
        self.assertEqual(p.heating_min_temp, -25)

    def test_summary_counts_by_threshold(self):
        self._product_with_temp('NC-T20', '-20 ~ +24')
        self._product_with_temp('NC-T25', '-25 ~ +24')
        self._product_with_temp('NC-T15', '-15 ~ +24')
        out = StringIO()
        call_command('backfill_heating', '--apply', stdout=out)
        text = out.getvalue()
        self.assertIn('до -20', text)
        self.assertIn('до -25', text)

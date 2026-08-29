from decimal import Decimal

from django.http import QueryDict
from django.core.cache import cache
from django.test import TestCase

from apps.catalog.dynamic_filters import (
    _MAX_FACET_OPTIONS, apply_tech_filters, compute_tech_facets, parse_tech_params,
)
from apps.catalog.models import Brand, Category, Product, ProductTech, TechSpec


def _qd(**params):
    qd = QueryDict(mutable=True)
    for key, values in params.items():
        for v in values:
            qd.appendlist(key, v)
    return qd


class ParseTechParamsTest(TestCase):

    def test_single_pair(self):
        qd = _qd(tech=['12:Да'])
        self.assertEqual(parse_tech_params(qd), {12: {'Да'}})

    def test_multiple_values_same_spec_grouped(self):
        qd = _qd(tech=['12:Да', '12:Есть'])
        self.assertEqual(parse_tech_params(qd), {12: {'Да', 'Есть'}})

    def test_different_specs(self):
        qd = _qd(tech=['12:Да', '45:Белый'])
        self.assertEqual(parse_tech_params(qd), {12: {'Да'}, 45: {'Белый'}})

    def test_malformed_ignored(self):
        qd = _qd(tech=['not-a-pair', 'abc:Да', '12:'])
        self.assertEqual(parse_tech_params(qd), {})

    def test_empty(self):
        qd = _qd()
        self.assertEqual(parse_tech_params(qd), {})


class DynamicFiltersDbTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            title='Сплит-системы', slug='split', sync_enabled=True,
        )
        cls.other_category = Category.objects.create(
            title='Мобильные', slug='mobile', sync_enabled=True,
        )
        cls.brand = Brand.objects.create(title='Midea', slug='midea')
        cls.wifi_spec = TechSpec.objects.create(
            title='Wi-Fi', category=cls.category, is_filter=True, order=1,
        )
        cls.color_spec = TechSpec.objects.create(
            title='Цвет корпуса', category=cls.category, is_filter=True, order=2,
        )
        cls.hidden_spec = TechSpec.objects.create(
            title='Внутренний код', category=cls.category, is_filter=False, order=3,
        )
        # Регрессия: реальные is_filter=True TechSpec в проде почти все имеют
        # category=None (глобальные, не привязаны к категории поставщиком) —
        # такие тоже должны показываться, а не отфильтровываться по category.
        cls.global_spec = TechSpec.objects.create(
            title='Тепловой насос', category=None, is_filter=True, order=4,
        )

    def setUp(self):
        # фасеты кэшируются — тесты меняют данные между вызовами
        cache.clear()

    def _make(self, nc, category=None):
        return Product.objects.create(
            nc_code=nc, articul=nc, title=f'AC {nc}',
            category=category or self.category, brand=self.brand,
            ric=Decimal('30000'), is_active=True,
        )

    def test_apply_tech_filters_no_params_returns_same_qs(self):
        self._make('NC-1')
        qs = Product.objects.all()
        result = apply_tech_filters(_qd(), qs)
        self.assertEqual(set(result), set(qs))

    def test_apply_tech_filters_matches_value(self):
        p1 = self._make('NC-wifi')
        p2 = self._make('NC-plain')
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='Да')
        qs = apply_tech_filters(_qd(tech=[f'{self.wifi_spec.id}:Да']), Product.objects.all())
        self.assertEqual(set(qs), {p1})
        self.assertNotIn(p2, qs)

    def test_apply_tech_filters_case_insensitive(self):
        # Регрессия с прода: «SMART Ion»/«Smart Ion» — один и тот же вариант,
        # только разный регистр от разных поставщиков.
        p1 = self._make('NC-1')
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='SMART Ion (4 шт.)')
        qs = apply_tech_filters(
            _qd(tech=[f'{self.wifi_spec.id}:smart ion (4 шт.)']), Product.objects.all(),
        )
        self.assertEqual(set(qs), {p1})

    def test_apply_tech_filters_and_between_specs(self):
        p1 = self._make('NC-both')
        p2 = self._make('NC-wifi-only')
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='Да')
        ProductTech.objects.create(product=p1, spec=self.color_spec, value='Белый')
        ProductTech.objects.create(product=p2, spec=self.wifi_spec, value='Да')
        qd = _qd(tech=[f'{self.wifi_spec.id}:Да', f'{self.color_spec.id}:Белый'])
        qs = apply_tech_filters(qd, Product.objects.all())
        self.assertEqual(set(qs), {p1})

    def test_apply_tech_filters_matches_across_duplicate_spec_ids(self):
        # Регрессия с прода: одинаковый title («Тип хладагента») существует
        # как несколько разных spec_id (разные источники синка). Фильтр по
        # одному из них должен находить товары, привязанные к ЛЮБОМУ spec_id
        # с этим же title.
        dup_spec = TechSpec.objects.create(title='Wi-Fi', category=None, is_filter=True)
        p1 = self._make('NC-1')
        ProductTech.objects.create(product=p1, spec=dup_spec, value='Да')
        qs = apply_tech_filters(_qd(tech=[f'{self.wifi_spec.id}:Да']), Product.objects.all())
        self.assertIn(p1, qs)

    def test_exclude_group_key_drops_own_selection(self):
        p1 = self._make('NC-1')
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='Да')
        qd = _qd(tech=[f'{self.wifi_spec.id}:Нет'])  # selection that wouldn't match p1
        qs = apply_tech_filters(qd, Product.objects.all(), exclude_group_key=self.wifi_spec.id)
        self.assertIn(p1, qs)

    def test_compute_tech_facets_empty_without_category(self):
        self.assertEqual(compute_tech_facets(_qd(), None, Product.objects.all()), [])

    def test_compute_tech_facets_only_is_filter_specs(self):
        p1 = self._make('NC-1')
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='Да')
        ProductTech.objects.create(product=p1, spec=self.hidden_spec, value='XYZ123')
        result = compute_tech_facets(_qd(), self.category, Product.objects.all())
        titles = {g['title'] for g in result}
        self.assertIn('Wi-Fi', titles)
        self.assertNotIn('Внутренний код', titles)

    def test_compute_tech_facets_includes_global_specs_regardless_of_category(self):
        p1 = self._make('NC-1')
        ProductTech.objects.create(product=p1, spec=self.global_spec, value='Есть')
        result = compute_tech_facets(_qd(), self.category, Product.objects.all())
        titles = {g['title'] for g in result}
        self.assertIn('Тепловой насос', titles)

    def test_compute_tech_facets_global_specs_shown_even_without_category(self):
        p1 = self._make('NC-1')
        ProductTech.objects.create(product=p1, spec=self.global_spec, value='Есть')
        result = compute_tech_facets(_qd(), None, Product.objects.all())
        titles = {g['title'] for g in result}
        self.assertIn('Тепловой насос', titles)

    def test_compute_tech_facets_skips_high_cardinality_numeric_spec(self):
        # Регрессия с прода: «Эффективен для помещений площадью до» отдаёт
        # десятки почти уникальных числовых значений (20, 20.5, 21, 21.4...) —
        # такую специфику как чекбокс-фасету показывать нельзя.
        area_spec = TechSpec.objects.create(
            title='Эффективен для помещений площадью до', category=None, is_filter=True,
        )
        for i in range(_MAX_FACET_OPTIONS + 5):
            p = self._make(f'NC-area-{i}')
            ProductTech.objects.create(product=p, spec=area_spec, value=str(20 + i * 0.5))
        result = compute_tech_facets(_qd(), self.category, Product.objects.all())
        titles = {g['title'] for g in result}
        self.assertNotIn('Эффективен для помещений площадью до', titles)

    def test_compute_tech_facets_excludes_brand_duplicate(self):
        # «Бренд» как TechSpec дублирует ProductFilter.brand — не должен
        # рендериться второй раз в динамической панели.
        brand_spec = TechSpec.objects.create(title='Бренд', category=None, is_filter=True)
        p1 = self._make('NC-1')
        ProductTech.objects.create(product=p1, spec=brand_spec, value='Midea')
        result = compute_tech_facets(_qd(), self.category, Product.objects.all())
        titles = {g['title'] for g in result}
        self.assertNotIn('Бренд', titles)

    def test_series_not_specially_excluded_but_capped_by_cardinality(self):
        # «Серия» не в _EXCLUDED_TITLES (в отличие от «Бренд») — если
        # значений мало, группа честно показывается...
        series_spec = TechSpec.objects.create(title='Серия', category=None, is_filter=True)
        p1 = self._make('NC-1')
        ProductTech.objects.create(product=p1, spec=series_spec, value='V-series')
        result = compute_tech_facets(_qd(), self.category, Product.objects.all())
        titles = {g['title'] for g in result}
        self.assertIn('Серия', titles)

        # ...но на реальных данных (проверено на проде: 498 уникальных
        # значений даже после объединения дублей) кардинальность выше
        # порога — группа скрывается общим механизмом, не спецкейсом.
        for i in range(_MAX_FACET_OPTIONS + 5):
            p = self._make(f'NC-series-{i}')
            ProductTech.objects.create(product=p, spec=series_spec, value=f'Series-{i}')
        # Фасеты кэшируются; в проде кэш сбрасывает синк после обновления
        # каталога (facets.invalidate_facets_cache), здесь — руками.
        cache.clear()
        result = compute_tech_facets(_qd(), self.category, Product.objects.all())
        titles = {g['title'] for g in result}
        self.assertNotIn('Серия', titles)

    def test_compute_tech_facets_scoped_to_category(self):
        p1 = self._make('NC-other', category=self.other_category)
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='Да')
        result = compute_tech_facets(_qd(), self.other_category, Product.objects.all())
        self.assertEqual(result, [])

    def test_compute_tech_facets_counts_and_selected(self):
        # ASCII-значения намеренно (не «Да»/«Нет») — SQLite's LOWER()/NOCASE
        # не приводит кириллицу к нижнему регистру (в отличие от Postgres на
        # проде), из-за чего Python-side .lower() и SQL-side Lower() дают
        # разный результат для кириллицы только в тестовом sqlite-окружении.
        p1 = self._make('NC-1')
        p2 = self._make('NC-2')
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='Yes')
        ProductTech.objects.create(product=p2, spec=self.wifi_spec, value='No')
        qd = _qd(tech=[f'{self.wifi_spec.id}:Yes'])
        result = compute_tech_facets(qd, self.category, Product.objects.all())
        wifi_group = next(g for g in result if g['spec_id'] == self.wifi_spec.id)
        options_by_value = {o['value']: o for o in wifi_group['options']}
        self.assertTrue(options_by_value['Yes']['selected'])
        self.assertFalse(options_by_value['No']['selected'])
        self.assertEqual(options_by_value['Yes']['count'], 1)

    def test_compute_tech_facets_merges_duplicate_titled_specs(self):
        # Регрессия с прода: «Тип хладагента» существовал как 3 разных
        # spec_id (разные источники синка) — должны схлопнуться в одну группу
        # с суммарным счётчиком.
        dup_spec = TechSpec.objects.create(title='Wi-Fi', category=None, is_filter=True)
        p1 = self._make('NC-1')
        p2 = self._make('NC-2')
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='Да')
        ProductTech.objects.create(product=p2, spec=dup_spec, value='Да')
        result = compute_tech_facets(_qd(), self.category, Product.objects.all())
        wifi_groups = [g for g in result if g['title'] == 'Wi-Fi']
        self.assertEqual(len(wifi_groups), 1)
        options_by_value = {o['value'].lower(): o for o in wifi_groups[0]['options']}
        self.assertEqual(options_by_value['да']['count'], 2)

    def test_compute_tech_facets_case_insensitive_value_grouping(self):
        # Регрессия с прода: «SMART Ion»/«Smart Ion» — считаем одним значением.
        p1 = self._make('NC-1')
        p2 = self._make('NC-2')
        ProductTech.objects.create(product=p1, spec=self.wifi_spec, value='SMART Ion (4 шт.)')
        ProductTech.objects.create(product=p2, spec=self.wifi_spec, value='Smart Ion (4 шт.)')
        result = compute_tech_facets(_qd(), self.category, Product.objects.all())
        wifi_group = next(g for g in result if g['spec_id'] == self.wifi_spec.id)
        self.assertEqual(len(wifi_group['options']), 1)
        self.assertEqual(wifi_group['options'][0]['count'], 2)

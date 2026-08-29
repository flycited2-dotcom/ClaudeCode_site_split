"""Кэш фасет каталога.

Счётчики в сайдбаре считались отдельным запросом на каждое значение — 54
запроса к БД на страницу каталога и ~1 с ответа (замер на проде 2026-08-29).
Данные меняются только после синка (раз в час), поэтому результат кэшируется.
"""
from django.core.cache import cache
from django.http import QueryDict
from django.test import TestCase

from apps.catalog.facets import compute_facets
from apps.catalog.models import Brand, Category, Product


class FacetsCacheTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            title='Сплит-системы', slug='split-facets-cache', sync_enabled=True,
        )
        cls.brand = Brand.objects.create(title='Midea', slug='midea-facets-cache')

    def setUp(self):
        cache.clear()

    def _product(self, nc, btu=9):
        return Product.objects.create(
            nc_code=nc, articul=nc, category=self.category, brand=self.brand,
            title=f'AC {nc}', slug=f'ac-{nc}', btu_calc=btu,
        )

    def _facets(self, scope='catalog', data=''):
        return compute_facets(QueryDict(data), Product.objects.all(), scope=scope)

    def test_second_call_hits_cache(self):
        self._product('NC-C1')
        first = self._facets()
        # Второй вызов не должен ходить в БД — данные берутся из кэша
        with self.assertNumQueries(0):
            second = self._facets()
        self.assertEqual(first, second)

    def test_different_filters_cached_separately(self):
        self._product('NC-C9', btu=9)
        self._product('NC-C12', btu=12)
        all_facets = self._facets()
        filtered = self._facets(data='btu=9')
        self.assertNotEqual(all_facets, filtered)

    def test_different_scope_cached_separately(self):
        """Каталог и подборка считают фасеты по разным выборкам — кэш не должен
        отдавать счётчики каталога на странице подборки."""
        self._product('NC-S1')
        catalog = compute_facets(QueryDict(''), Product.objects.all(), scope='catalog')
        collection = compute_facets(
            QueryDict(''), Product.objects.filter(nc_code='NC-NOPE'), scope='heat-pumps',
        )
        self.assertNotEqual(catalog, collection)

    def test_cache_cleared_returns_fresh_counts(self):
        self._product('NC-F1')
        before = self._facets()
        self._product('NC-F2')
        cache.clear()
        after = self._facets()
        self.assertNotEqual(before, after)

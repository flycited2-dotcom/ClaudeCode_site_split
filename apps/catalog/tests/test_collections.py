"""Тесты правила отбора подборки «Тепловые насосы».

Товар остаётся в своей категории — подборка это срез поверх каталога,
поэтому проверяем именно queryset-правило, а не принадлежность категории.
"""
from django.test import Client, TestCase
from django.urls import reverse

from apps.catalog.collections import COLLECTIONS, get_collection
from apps.catalog.models import Brand, Category, Product
from apps.stock.models import Stock


class HeatPumpCollectionRuleTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            title='Бытовые сплит-системы', slug='split-coll', sync_enabled=True,
        )
        cls.brand = Brand.objects.create(title='FUNAI', slug='funai-coll')

    def _product(self, nc, temp=None, declared=False, kind=Product.KIND_SPLIT_SYSTEM):
        return Product.objects.create(
            nc_code=nc, articul=nc, category=self.category, brand=self.brand,
            title=f'AC {nc}', slug=f'ac-{nc}',
            heating_min_temp=temp, is_heat_pump=declared, kind=kind,
        )

    def _selected(self):
        rule = COLLECTIONS['heat-pumps'].rule
        return set(Product.objects.filter(rule).values_list('nc_code', flat=True))

    def test_minus_20_included(self):
        self._product('NC-20', temp=-20)
        self.assertIn('NC-20', self._selected())

    def test_minus_25_included(self):
        self._product('NC-25', temp=-25)
        self.assertIn('NC-25', self._selected())

    def test_minus_15_excluded(self):
        self._product('NC-15', temp=-15)
        self.assertNotIn('NC-15', self._selected())

    def test_no_temp_excluded(self):
        self._product('NC-NONE')
        self.assertNotIn('NC-NONE', self._selected())

    def test_declared_included_without_temp(self):
        self._product('NC-DECL', declared=True)
        self.assertIn('NC-DECL', self._selected())

    def test_accessory_excluded_even_if_cold(self):
        self._product('NC-ACC', temp=-30, kind=Product.KIND_ACCESSORY)
        self.assertNotIn('NC-ACC', self._selected())

    def test_multi_split_block_excluded(self):
        self._product('NC-MULTI', temp=-25, kind=Product.KIND_MULTI_SPLIT_BLOCK)
        self.assertNotIn('NC-MULTI', self._selected())

    def test_get_collection_unknown_slug(self):
        self.assertIsNone(get_collection('no-such-collection'))

    def test_get_collection_known_slug(self):
        self.assertEqual(get_collection('heat-pumps').slug, 'heat-pumps')


class CollectionViewTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            title='Бытовые сплит-системы', slug='split-view', sync_enabled=True,
        )
        cls.brand = Brand.objects.create(title='Hisense', slug='hisense-view')

    def _product(self, nc, temp, qty=3, warehouse='Симферополь'):
        p = Product.objects.create(
            nc_code=nc, articul=nc, category=self.category, brand=self.brand,
            title=f'Инверторная сплит-система {nc}', slug=f'ac-{nc}',
            heating_min_temp=temp,
        )
        Stock.objects.create(product=p, quantity=qty, warehouse=warehouse)
        return p

    def setUp(self):
        self.client = Client()

    def test_page_returns_200(self):
        self._product('NC-V20', -20)
        r = self.client.get(reverse('collection', args=['heat-pumps']))
        self.assertEqual(r.status_code, 200)

    def test_unknown_slug_404(self):
        r = self.client.get('/catalog/no-such-collection/')
        self.assertEqual(r.status_code, 404)

    def test_only_matching_products_listed(self):
        self._product('NC-V20', -20)
        self._product('NC-V15', -15)
        r = self.client.get(reverse('collection', args=['heat-pumps']))
        self.assertContains(r, 'NC-V20')
        self.assertNotContains(r, 'NC-V15')

    def test_h1_and_seo_text_rendered(self):
        self._product('NC-V25', -25)
        r = self.client.get(reverse('collection', args=['heat-pumps']))
        self.assertContains(r, 'Тепловые насосы воздух-воздух в Крыму')
        self.assertContains(r, 'воздух-воздух — это инверторная сплит-система')

    def test_no_raw_template_comment_leaks(self):
        """Регрессия 2026-08-29: многострочный {# … #} Django НЕ поддерживает —
        он печатается как текст. На проде такой комментарий вылез поверх
        каталога. Многострочные комментарии только через {% comment %}."""
        self._product('NC-CMT', -25)
        for url in (reverse('catalog'), reverse('collection', args=['heat-pumps'])):
            r = self.client.get(url)
            self.assertNotContains(r, '{#')
            self.assertNotContains(r, '#}')

    def test_under_order_visible_by_default(self):
        """С 2026-08-30 подборка, как и каталог, по умолчанию показывает всё.

        Товар без крымского остатка виден сразу; ?in_stock=1 сужает выдачу
        до склада в Крыму.
        """
        self._product('NC-ORDER', -25, qty=4, warehouse='Шерризон')
        r = self.client.get(reverse('collection', args=['heat-pumps']))
        self.assertContains(r, 'NC-ORDER')
        r2 = self.client.get(reverse('collection', args=['heat-pumps']), {'in_stock': '1'})
        self.assertNotContains(r2, 'NC-ORDER')

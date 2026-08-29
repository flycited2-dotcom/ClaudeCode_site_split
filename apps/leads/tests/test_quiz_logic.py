"""Юнит-тесты для apps/leads/quiz_logic.py."""
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Q
from django.test import SimpleTestCase, TestCase

from apps.catalog.classify import classify_title
from apps.catalog.models import Brand, Category, Product, ProductTech, TechSpec
from apps.leads import quiz_logic
from apps.stock.models import Stock


class BtuFromAreaTest(SimpleTestCase):

    def test_small(self):
        self.assertEqual(quiz_logic.btu_from_area(15), 7)

    def test_boundary_20(self):
        self.assertEqual(quiz_logic.btu_from_area(20), 7)

    def test_25(self):
        self.assertEqual(quiz_logic.btu_from_area(25), 9)

    def test_60(self):
        self.assertEqual(quiz_logic.btu_from_area(60), 24)

    def test_large(self):
        self.assertEqual(quiz_logic.btu_from_area(100), 30)


class BtuCandidatesTest(SimpleTestCase):

    def test_25_no_secondary_within_default(self):
        # 25 — это верхняя граница диапазона 9 kBTU. ±3 → попадает в окно [22..28].
        # Border 25: lower=9, upper=12. main=9 (т.к. area ≤ 25). candidate=12 → secondary.
        main, sec = quiz_logic.btu_candidates(25)
        self.assertEqual(main, 9)
        self.assertEqual(sec, [12])

    def test_22_near_20_border(self):
        # 22 м² попадает в окно ±3 сразу у ДВУХ границ: 20 (lower=7, upper=9)
        # и 25 (lower=9, upper=12). main=9, и в secondary идут оба соседа —
        # «впритык» 7 и «с запасом» 12. Тест раньше ждал только [7], потому что
        # писался до расширения окна до 3 м².
        main, sec = quiz_logic.btu_candidates(22)
        self.assertEqual(main, 9)
        self.assertEqual(sec, [7, 12])

    def test_28_near_25_border(self):
        # 28 в окне границы 25. main=12. candidate=9 (lower) → secondary.
        main, sec = quiz_logic.btu_candidates(28)
        self.assertEqual(main, 12)
        self.assertEqual(sec, [9])

    def test_far_from_border(self):
        # 15 далеко от всех границ → нет secondary.
        main, sec = quiz_logic.btu_candidates(15)
        self.assertEqual(main, 7)
        self.assertEqual(sec, [])

    def test_above_max(self):
        main, sec = quiz_logic.btu_candidates(120)
        self.assertEqual(main, 30)
        self.assertEqual(sec, [])


class BtuQTest(SimpleTestCase):

    def test_returns_q_with_values(self):
        q = quiz_logic._btu_q([7, 9])
        self.assertIsInstance(q, Q)
        self.assertIn('btu_calc__in', str(q))

    def test_empty_returns_neutral_q(self):
        q = quiz_logic._btu_q([])
        self.assertEqual(q, Q())

    def test_skips_none(self):
        q = quiz_logic._btu_q([None, 9])
        self.assertIn('btu_calc__in', str(q))


def _fake_product(pk, source):
    """Имитация Product для _balance_by_source — он читает только .pk и .source."""
    return SimpleNamespace(pk=pk, source=source)


class BalanceBySourceTest(SimpleTestCase):

    def test_empty_returns_empty(self):
        self.assertEqual(quiz_logic._balance_by_source([]), [])

    def test_perfect_2_plus_2_plus_2(self):
        products = [
            _fake_product(1, 'breeze'),
            _fake_product(2, 'breeze'),
            _fake_product(3, 'breeze'),
            _fake_product(4, 'rusklimat'),
            _fake_product(5, 'rusklimat'),
            _fake_product(6, 'rusklimat'),
            _fake_product(7, 'daichi'),
            _fake_product(8, 'daichi'),
            _fake_product(9, 'daichi'),
        ]
        result = quiz_logic._balance_by_source(products, per_source=2, total=6)
        self.assertEqual(len(result), 6)
        sources = [p.source for p in result]
        # По 2 от каждого source.
        self.assertEqual(sources.count('breeze'), 2)
        self.assertEqual(sources.count('rusklimat'), 2)
        self.assertEqual(sources.count('daichi'), 2)

    def test_skew_filled_with_leftovers(self):
        products = [
            _fake_product(1, 'breeze'),
            _fake_product(2, 'breeze'),
            _fake_product(3, 'breeze'),
            _fake_product(4, 'breeze'),
            _fake_product(5, 'breeze'),
            _fake_product(6, 'rusklimat'),
        ]
        result = quiz_logic._balance_by_source(products, per_source=2, total=6)
        self.assertEqual(len(result), 6)
        # Все 6 уникальны (нет дубликатов).
        self.assertEqual(len({p.pk for p in result}), 6)

    def test_dedup_by_pk(self):
        # Два объекта с одинаковым pk — второй пропускается.
        a = _fake_product(1, 'breeze')
        b = _fake_product(1, 'breeze')
        c = _fake_product(2, 'rusklimat')
        result = quiz_logic._balance_by_source([a, b, c], per_source=2, total=6)
        self.assertEqual(len(result), 2)
        self.assertEqual({p.pk for p in result}, {1, 2})

    def test_round_robin_order(self):
        # Первый круг должен взять по 1 от каждого source перед вторым кругом.
        products = [
            _fake_product(1, 'breeze'),
            _fake_product(2, 'breeze'),
            _fake_product(3, 'rusklimat'),
            _fake_product(4, 'rusklimat'),
        ]
        result = quiz_logic._balance_by_source(products, per_source=2, total=4)
        # Первые два — по одному от каждого source.
        self.assertEqual({result[0].source, result[1].source}, {'breeze', 'rusklimat'})

    def test_truncates_to_total(self):
        products = [_fake_product(i, 'breeze') for i in range(10)]
        result = quiz_logic._balance_by_source(products, per_source=2, total=3)
        self.assertEqual(len(result), 3)

    def test_unknown_source_grouped(self):
        # source=None трактуется как 'unknown' и не ломает round-robin.
        a = _fake_product(1, None)
        b = _fake_product(2, 'breeze')
        result = quiz_logic._balance_by_source([a, b], per_source=1, total=2)
        self.assertEqual(len(result), 2)


# === DB-тесты: recommend_products с релаксацией ===

class _RecommendBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            title='Сплит-системы', slug='split-systems', sync_enabled=True,
        )
        cls.brand = Brand.objects.create(title='TestBrand', slug='testbrand')

    def _make(self, *, nc_code, title, source='breeze', btu_calc=9,
              ric=30000, stock_qty=5, is_active=True, brand=None,
              description=''):
        p = Product.objects.create(
            nc_code=nc_code, articul=nc_code,
            category=self.category, brand=brand or self.brand,
            title=title,
            description=description,
            ric=Decimal(str(ric)),
            source=source,
            btu_calc=btu_calc,
            is_active=is_active,
            kind=classify_title(title),
        )
        Stock.objects.create(product=p, quantity=stock_qty, warehouse='Симферополь')
        return p


class RecommendProductsTest(_RecommendBase):

    def test_strict_match_no_relaxation(self):
        self._make(nc_code='NC-1', title='AC Inverter 9',
                   btu_calc=9, ric=40000)
        products, relaxed = quiz_logic.recommend_products(
            9, budget_max=50000, needs_inverter=True,
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(relaxed, [])

    def test_budget_relaxed_when_too_cheap(self):
        # Нет ничего в бюджете 10к, но есть за 40к.
        self._make(nc_code='NC-3', title='AC Inverter 9', btu_calc=9, ric=40000)
        products, relaxed = quiz_logic.recommend_products(
            9, budget_max=10000, needs_inverter=True,
        )
        self.assertEqual(len(products), 1)
        self.assertIn('budget_relaxed', relaxed)

    def test_inverter_relaxed_when_no_inverters(self):
        # Нет инверторов, но есть обычный.
        self._make(nc_code='NC-4', title='AC Standard 9', btu_calc=9, ric=40000)
        products, relaxed = quiz_logic.recommend_products(
            9, budget_max=None, needs_inverter=True,
        )
        self.assertEqual(len(products), 1)
        self.assertIn('inverter_relaxed', relaxed)

    def test_btu_relaxed_when_btu_calc_null(self):
        # Свежий критический фикс: btu_calc=NULL у всех → каскад выше всё равно
        # пуст; 5-й уровень снимает BTU и показывает что есть.
        self._make(nc_code='NC-5', title='AC 9', btu_calc=None, ric=30000)
        products, relaxed = quiz_logic.recommend_products(
            9, budget_max=None, needs_inverter=False,
        )
        self.assertEqual(len(products), 1)
        self.assertIn('btu_relaxed', relaxed)

    def test_nothing_found_when_empty_db(self):
        products, relaxed = quiz_logic.recommend_products(
            9, budget_max=None, needs_inverter=False,
        )
        self.assertEqual(products, [])
        # btu_relaxed добавлен последним перед nothing_found.
        self.assertIn('btu_relaxed', relaxed)
        self.assertIn('nothing_found', relaxed)

    def test_commercial_excludes_mobile(self):
        # Отсев идёт по русскому «мобильн» (_MOBILE_Q) — так товары и называются
        # у поставщиков: в каталоге 122 позиции с «мобильн» и ни одной с латинским
        # Mobile (проверено на проде 2026-08-29). Тест раньше давал латиницу и
        # падал, хотя на живых данных фильтр работает.
        self._make(nc_code='NC-10', title='Мобильный кондиционер 9', btu_calc=9)
        self._make(nc_code='NC-11', title='Сплит-система Стандарт 9', btu_calc=9)
        products, relaxed = quiz_logic.recommend_products(
            9, budget_max=None, room_type='commercial',
        )
        titles = [p.title for p in products]
        self.assertNotIn('Мобильный кондиционер 9', titles)
        self.assertIn('Сплит-система Стандарт 9', titles)

    def test_room_type_none_keeps_mobile(self):
        self._make(nc_code='NC-20', title='Мобильный кондиционер 9', btu_calc=9)
        products, relaxed = quiz_logic.recommend_products(9, room_type=None)
        titles = [p.title for p in products]
        self.assertIn('Мобильный кондиционер 9', titles)

    def test_multi_split_excluded(self):
        # Внутренние блоки мульти-сплита засоряют каталог — должны выпасть.
        self._make(nc_code='NC-30', title='Мульти-блок внутренний 9',
                   btu_calc=9, ric=15000)
        self._make(nc_code='NC-31', title='AC Standard 9', btu_calc=9, ric=15000)
        products, _ = quiz_logic.recommend_products(9)
        titles = [p.title for p in products]
        self.assertNotIn('Мульти-блок внутренний 9', titles)
        self.assertIn('AC Standard 9', titles)

    def test_balanced_across_sources(self):
        # 4 breeze + 4 rusklimat + 4 daichi → итог ≤ 6, по 2 от каждого.
        for i in range(4):
            self._make(nc_code=f'NC-B{i}', title=f'AC B{i}', source='breeze')
        for i in range(4):
            self._make(nc_code=f'NC-R{i}', title=f'AC R{i}', source='rusklimat')
        for i in range(4):
            self._make(nc_code=f'NC-D{i}', title=f'AC D{i}', source='daichi')
        products, _ = quiz_logic.recommend_products(9, per_source=2, total=6)
        sources = [p.source for p in products]
        self.assertEqual(len(products), 6)
        self.assertEqual(sources.count('breeze'), 2)
        self.assertEqual(sources.count('rusklimat'), 2)
        self.assertEqual(sources.count('daichi'), 2)


class WifiFilterTest(_RecommendBase):

    def test_wifi_filter_via_techspec(self):
        # Товар с TechSpec «Wi-Fi управление» = «Да» должен проходить фильтр.
        spec = TechSpec.objects.create(title='Wi-Fi управление')
        with_wifi = self._make(nc_code='NC-W1', title='AC Standard 9 with', btu_calc=9)
        ProductTech.objects.create(product=with_wifi, spec=spec, value='Да')
        # Контрольный товар без Wi-Fi — но в title нет «wi-fi» и tech нет.
        self._make(nc_code='NC-W0', title='AC Standard 9 plain', btu_calc=9)

        products, relaxed = quiz_logic.recommend_products(9, needs_wifi=True)
        titles = [p.title for p in products]
        self.assertIn('AC Standard 9 with', titles)
        self.assertNotIn('AC Standard 9 plain', titles)
        self.assertEqual(relaxed, [])

    def test_wifi_filter_excludes_explicit_no(self):
        """Характеристика «Wi-Fi: Нет» убирает товар из выдачи.

        Тест переписан 2026-08-29. Прежняя версия требовала одновременно, чтобы
        товар был отсеян И чтобы сработала релаксация wifi — но релаксация как
        раз снимает требование Wi-Fi и возвращает этот же товар, когда других
        нет. Условия противоречили друг другу, тест падал при любой реализации.
        Проверяем то, ради чего фильтр написан: при наличии альтернативы модель
        с явным «Нет» в выдачу не попадает и ослаблять ничего не нужно.

        Названия — без подстроки wi-fi: WIFI_Q ищет упоминание и в title, так
        что «no-wifi» в названии сам по себе читался как признак наличия.
        """
        spec = TechSpec.objects.create(title='Wi-Fi')

        no_wifi = self._make(nc_code='NC-WN', title='Сплит-система Стандарт 9', btu_calc=9)
        ProductTech.objects.create(product=no_wifi, spec=spec, value='Нет')

        with_wifi = self._make(nc_code='NC-WY', title='Сплит-система Комфорт 9', btu_calc=9)
        ProductTech.objects.create(product=with_wifi, spec=spec, value='Да')

        products, relaxed = quiz_logic.recommend_products(9, needs_wifi=True)
        titles = [p.title for p in products]
        self.assertIn('Сплит-система Комфорт 9', titles)
        self.assertNotIn('Сплит-система Стандарт 9', titles)
        self.assertEqual(relaxed, [])

    def test_wifi_filter_via_description(self):
        # Товар без TechSpec, но с упоминанием Wi-Fi в description — пройдёт.
        self._make(nc_code='NC-WD', title='AC Standard 9 desc', btu_calc=9,
                   description='Возможность подключения Wi-Fi через опциональный модуль.')
        self._make(nc_code='NC-PD', title='AC Standard 9 plain', btu_calc=9,
                   description='Простой кондиционер без удалёнки.')

        products, _ = quiz_logic.recommend_products(9, needs_wifi=True)
        titles = [p.title for p in products]
        self.assertIn('AC Standard 9 desc', titles)
        self.assertNotIn('AC Standard 9 plain', titles)

    def test_wifi_relaxed_when_nothing_matches(self):
        # Wi-Fi нигде, релаксация вернёт все товары с меткой wifi_relaxed.
        self._make(nc_code='NC-NW', title='AC Standard 9', btu_calc=9)
        products, relaxed = quiz_logic.recommend_products(9, needs_wifi=True)
        self.assertEqual(len(products), 1)
        self.assertIn('wifi_relaxed', relaxed)


class BrandFilterTest(_RecommendBase):

    def test_brand_filter(self):
        other = Brand.objects.create(title='OtherBrand', slug='other')
        self._make(nc_code='NC-BR1', title='AC default 9', btu_calc=9)
        self._make(nc_code='NC-BR2', title='AC other 9', btu_calc=9, brand=other)

        products, relaxed = quiz_logic.recommend_products(9, brand_id=other.id)
        titles = [p.title for p in products]
        self.assertIn('AC other 9', titles)
        self.assertNotIn('AC default 9', titles)
        self.assertEqual(relaxed, [])

    def test_brand_relaxed_first_before_wifi(self):
        # Бренд указан, его товаров нет — снимаем brand, оставляя wifi.
        spec = TechSpec.objects.create(title='Wi-Fi')
        with_wifi = self._make(nc_code='NC-RW', title='AC with wifi 9', btu_calc=9)
        ProductTech.objects.create(product=with_wifi, spec=spec, value='Да')

        empty_brand = Brand.objects.create(title='Empty', slug='empty')
        products, relaxed = quiz_logic.recommend_products(
            9, needs_wifi=True, brand_id=empty_brand.id,
        )
        self.assertEqual(len(products), 1)
        # brand снят, wifi остался строгим — не должен быть в relaxed.
        self.assertIn('brand_relaxed', relaxed)
        self.assertNotIn('wifi_relaxed', relaxed)

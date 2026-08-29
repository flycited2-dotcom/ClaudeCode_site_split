from django.test import SimpleTestCase

from apps.catalog.classify import classify_title
from apps.catalog.models import Product


class ClassifyTitleTest(SimpleTestCase):

    def test_real_split_system(self):
        self.assertEqual(
            classify_title('Инверторная сплит-система серии DAIJIN Inverter RAC-I-DA25HP.D01 (комплект)'),
            Product.KIND_SPLIT_SYSTEM,
        )

    def test_multi_split_block(self):
        self.assertEqual(
            classify_title('Мульти-блок внутренний 9'),
            Product.KIND_MULTI_SPLIT_BLOCK,
        )

    def test_accessory(self):
        self.assertEqual(
            classify_title('Экран для вентиляционной решётки Ballu Квадра 600'),
            Product.KIND_ACCESSORY,
        )

    def test_evaporative_cooler_is_accessory(self):
        self.assertEqual(
            classify_title('Охладитель воздуха Ballu Prime BCOOL-05L PM'),
            Product.KIND_ACCESSORY,
        )

    def test_empty_title_defaults_to_split_system(self):
        self.assertEqual(classify_title(''), Product.KIND_SPLIT_SYSTEM)
        self.assertEqual(classify_title(None), Product.KIND_SPLIT_SYSTEM)

    def test_multi_split_takes_priority_over_non_retail(self):
        # На случай если оба паттерна совпадут — порядок проверки в
        # classify_title важен: мульти-блок проверяется первым.
        self.assertEqual(
            classify_title('Мульти сплит блок внутренний с кронштейном'),
            Product.KIND_MULTI_SPLIT_BLOCK,
        )


class VentilationPartsTest(SimpleTestCase):
    """Комплектующие приточных установок Rusklimat (2026-08-30).

    Фланцы, корпуса, нагревательные элементы и датчики ехали в каталог
    как товар — у них нет ни одного слова из старого денилиста.
    """

    def test_flange_is_accessory(self):
        self.assertEqual(
            classify_title('Фланец ответный SHUFT В-10'), Product.KIND_ACCESSORY)

    def test_heating_element_is_accessory(self):
        self.assertEqual(
            classify_title('Элемент нагревательный Ballu PTC-1200 для электроприборов'),
            Product.KIND_ACCESSORY)

    def test_case_is_accessory(self):
        self.assertEqual(
            classify_title('Корпус нагревательного элемента ASP-200'),
            Product.KIND_ACCESSORY)

    def test_sensor_is_accessory(self):
        self.assertEqual(
            classify_title('Датчик углекислого газа CO2-Z19'), Product.KIND_ACCESSORY)

    def test_silencer_is_accessory(self):
        self.assertEqual(
            classify_title('Шумоглушитель SHUFT SCr 500x250/1000'),
            Product.KIND_ACCESSORY)

    def test_breezer_stays_retail(self):
        """«Очиститель воздуха приточный» — бризер, а не расходник.

        Слово «очистител» лежит в денилисте (чистящие средства), поэтому
        для приточных очистителей работает RETAIL_OVERRIDE_PATTERN.
        """
        for title in (
            'Очиститель воздуха приточный Ballu ONEAIR ASP-200S',
            'Приточный очиститель воздуха Ballu ONEAIR ASP-100 серый',
        ):
            self.assertEqual(classify_title(title), Product.KIND_SPLIT_SYSTEM, title)

    def test_cleaning_agent_still_accessory(self):
        """Белый список не должен пропускать настоящую химию."""
        self.assertEqual(
            classify_title('Очиститель кондиционеров пенный 400 мл'),
            Product.KIND_ACCESSORY)

    def test_supply_unit_stays_retail(self):
        self.assertEqual(
            classify_title('Установка приточная SHUFT ECO 315/1-9,0/ 3-A'),
            Product.KIND_SPLIT_SYSTEM)

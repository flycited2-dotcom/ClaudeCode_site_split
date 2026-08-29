import django_filters
from django import forms
from django.db.models import Q

from .heating import HEATING_THRESHOLDS
from .models import Product, Brand, Category


# Фасета «Работает на обогрев до» — по Product.heating_min_temp (считается при
# синке, см. apps/catalog/heating.py). Значение чекбокса — сам порог в °C.
HEATING_CHOICES = [(str(t), f'до {t} °C') for t in HEATING_THRESHOLDS]


_select_cls ='w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-orange-400'
_input_cls = 'w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-orange-400'


# BTU code (UI key) → 2-digit token expected inside `articul`
BTU_VALUES = ['7', '9', '12', '18', '24', '27', '30', '36', '42', '48', '60']

INVERTER_CHOICES = [
    ('inverter', 'Инвертор'),
    ('onoff', 'Он-офф'),
]

COLOR_CHOICES = [
    ('black', 'Чёрный'),
    ('white', 'Белый'),
    ('silver', 'Silver'),
    ('green', 'Green'),
]

# Тип внутреннего блока — для полупромышленной категории. Подкатегория задаётся
# через URL `?type=cassette` (или duct, или floor_ceiling). Реализован как
# title__iregex, потому что в БД эти варианты живут в одной категории
# id=6 «Полупромышленные сплит-системы», различимы только по названию.
TYPE_CHOICES = [
    ('cassette', 'Кассетные'),
    ('duct', 'Канальные'),
    ('floor_ceiling', 'Напольно-потолочные'),
]
_TYPE_PATTERNS = {
    'cassette':     r'кассет',
    'duct':         r'канальн',
    'floor_ceiling': r'напольн.*потолочн|потолочн.*напольн',
}

# Title substrings used by the color facet. Case-insensitive `icontains`.
_COLOR_NEEDLES = {
    'black':  ['черн', 'black'],
    'white':  ['бел', 'white'],
    'silver': ['silver'],
    'green':  ['green'],
}

# Площадь помещения → мощность BTU. Тот же маппинг, что в квизе
# (apps.leads.quiz_logic.AREA_TO_BTU) — держать в синхроне при изменении;
# для каталога квизовая логика «соседнего BTU у границы» не нужна, тут
# просто прямой выбор одного диапазона.
AREA_CHOICES = [
    ('20',  'до 20 м²'),
    ('25',  'до 25 м²'),
    ('35',  'до 35 м²'),
    ('45',  'до 45 м²'),
    ('65',  'до 65 м²'),
    ('999', 'более 65 м²'),
]
_AREA_TO_BTU = {'20': 7, '25': 9, '35': 12, '45': 18, '65': 24, '999': 30}

# Wi-Fi-управление: гибрид TechSpec-фильтра и regex по тексту. Используется
# только в квизе (apps.leads.quiz_logic) — в каталоге вместо неё показывается
# динамическая фасета «Управление c мобильного приложения по Wi-Fi»
# (dynamic_filters.py), которая точнее (различает встроенное/опцию/нет) и не
# дублирует эту же информацию отдельным чекбоксом (было до 2026-07-02).
# Поставщики могут отдавать характеристику с разными названиями («Wi-Fi»,
# «Wi Fi», «Беспроводное управление», «Удалённое управление»). Значение
# варьируется («Да», «Есть», «опция», «возможность подключения»). Считаем
# как «есть Wi-Fi» всё, что НЕ выглядит явным отрицанием. Если у товара
# tech-характеристики отсутствуют — пробуем найти упоминание в title/description.
_WIFI_TECH_Q = (
    Q(tech_values__spec__title__iregex=r'wi[\s\-]?fi|вай[\s\-]?фай|беспровод|удал.{0,5}управлен')
    & ~Q(tech_values__value__iregex=r'^\s*(нет|no|−|—|-|отсутств)\s*$')
)
_WIFI_TEXT_Q = (
    Q(description__iregex=r'wi[\s\-]?fi|wifi|вай[\s\-]?фай')
    | Q(title__iregex=r'wi[\s\-]?fi|wifi|вай[\s\-]?фай')
)
WIFI_Q = _WIFI_TECH_Q | _WIFI_TEXT_Q

# Компоненты мульти-сплит-систем (внутренний/наружный блок) — не самостоятельные
# кондиционеры. Их артикул содержит BTU-код, поэтому без явного исключения
# розничный каталог и квиз засоряются ими (≈40% активных AC).
# Паттерн — единственный источник истины для Product.kind (см.
# apps.catalog.classify.classify_title, считается один раз при синке) и
# оставшихся мест, где ещё нужен regex Q() напрямую.
MULTI_SPLIT_BLOCK_PATTERN = r'мульти|multi|блок\s+(внутренний|наружный)'
MULTI_SPLIT_BLOCK_Q = Q(title__iregex=MULTI_SPLIT_BLOCK_PATTERN)

# Не-розничные товары: аксессуары, запчасти, кронштейны, инструменты и т.д.
# В розничном каталоге, на главной, в квизе они не нужны — у них нет BTU,
# розничный покупатель их по своей инициативе не ищет. Исключаются по title.
# Regex покрывает первое слово title (^\s*) ИЛИ слово после пробела ((?:^|\s)).
# Список расширен реальными именами товаров с прода (Ballu Super Stars,
# RexFaber — это бренды монтажных инструментов и расходников).
# Паттерн — единственный источник истины для Product.kind (см. classify.py).
NON_RETAIL_PATTERN = (
    r'(?:^|\s)('
    # Монтажные инструменты
    r'кронштей|труборез|труборасширител|расширител|развальцов|вальцов|'
    r'риммер|гребенк|гребёнк|коллектор|манометр|термометр|вакуумм?н|'
    # Расходники/химия для монтажа
    r'хладагент|фреон|припой|флюс|чистящ|очистител|обезжириватель|'
    r'дегазатор|герметик|изолент|изоляц|средство\s+чистящ|мойк|'
    # Крепёж/детали/адаптеры
    r'виброопор|шланг|штуцер|сифон|насадк|переходник|удлинител|'
    r'клапан|переключател|сальник|штатив|держатель|кран\s|сгон|'
    r'адаптер|оконный\s+адаптер|решет|решёт|экран\s+для|'
    # Комплектующие приточных установок Rusklimat (2026-08-30): фланцы,
    # корпуса, нагревательные элементы и датчики ехали в каталог как товар.
    r'фланец|шумоглушител|воздуховод|датчик|'
    r'корпус\s|элемент\s+нагревательн|нагревательн\w*\s+элемент|'
    # Запчасти/аксессуары вообще
    r'запчаст|комплектующ|расходн|чехл|абажур|зарядн|аксессуар|'
    r'комплект\s+зимн|бак\s+для\s+воды|'
    # Пульты как отдельный товар (осторожно)
    r'пульт\s+ду|пульт\s+управлен|'
    # Испарительные охладители воздуха (BCOOL и т.п.) — другая технология,
    # не сплит-система; попали в «Бытовые сплит-системы» из-за категоризации
    # Rusklimat (см. remap_categories.py), в розничном каталоге кондиционеров
    # не нужны.
    r'охладител\w*\s+воздух'
    r')'
)
NON_RETAIL_Q = Q(title__iregex=NON_RETAIL_PATTERN)


def _btu_q(code_keys):
    """Фильтр по Product.btu_calc — посчитан через `compute_btu` из
    TechSpec (мощность охлаждения + площадь), а не из артикула."""
    values = []
    for k in code_keys:
        try:
            values.append(int(k))
        except (TypeError, ValueError):
            continue
    return Q(btu_calc__in=values) if values else Q()


def _inverter_filter(qs, value):
    """value: iterable of 'inverter' / 'onoff'. Both or none → no filter."""
    if not value:
        return qs
    chosen = set(value)
    if 'inverter' in chosen and 'onoff' in chosen:
        return qs
    inv_q = Q(title__iregex=r'инвертор|inverter')
    if 'inverter' in chosen:
        return qs.filter(inv_q)
    if 'onoff' in chosen:
        return qs.exclude(inv_q)
    return qs


def _color_q(color_keys):
    q = Q()
    for key in color_keys:
        for needle in _COLOR_NEEDLES.get(key, []):
            q |= Q(title__icontains=needle)
    return q


def _area_q(area_keys):
    """Фильтр по Product.btu_calc через выбор диапазона площади (см. AREA_CHOICES)."""
    values = {_AREA_TO_BTU[k] for k in area_keys if k in _AREA_TO_BTU}
    return Q(btu_calc__in=values) if values else Q()


def _heating_q(codes):
    """Фильтр по порогам обогрева (Product.heating_min_temp, считается при синке).

    Несколько выбранных порогов = самый тёплый из них: множество «до -25»
    целиком входит в «до -20», поэтому объединение — это lte по максимуму.
    """
    thresholds = []
    for code in codes:
        try:
            thresholds.append(int(code))
        except (TypeError, ValueError):
            continue
    if not thresholds:
        return Q()
    return Q(heating_min_temp__lte=max(thresholds))


class ProductFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(
        method='filter_search', label='Поиск',
        widget=forms.TextInput(attrs={'class': _input_cls, 'placeholder': 'Модель, артикул...'}),
    )
    brand = django_filters.ModelMultipleChoiceFilter(
        queryset=Brand.objects.all().order_by('title'),
        label='Бренд',
    )
    category = django_filters.ModelChoiceFilter(
        queryset=Category.objects.filter(sync_enabled=True).order_by('order', 'title'),
        label='Тип', empty_label='Все типы',
        widget=forms.Select(attrs={'class': _select_cls}),
    )
    # Фильтр и UI работают по ric (РРЦ — цена, которую видит покупатель).
    price_min = django_filters.NumberFilter(
        field_name='ric', lookup_expr='gte', label='Цена от',
        widget=forms.NumberInput(attrs={'class': _input_cls, 'placeholder': 'от ₽'}),
    )
    price_max = django_filters.NumberFilter(
        field_name='ric', lookup_expr='lte', label='Цена до',
        widget=forms.NumberInput(attrs={'class': _input_cls, 'placeholder': 'до ₽'}),
    )
    btu = django_filters.MultipleChoiceFilter(
        choices=[(v, v) for v in BTU_VALUES],
        method='filter_btu', label='Мощность BTU', conjoined=False,
    )
    area = django_filters.MultipleChoiceFilter(
        choices=AREA_CHOICES,
        method='filter_area', label='Площадь помещения', conjoined=False,
    )
    inverter = django_filters.MultipleChoiceFilter(
        choices=INVERTER_CHOICES,
        method='filter_inverter', label='Тип управления', conjoined=False,
    )
    color = django_filters.MultipleChoiceFilter(
        choices=COLOR_CHOICES,
        method='filter_color', label='Цвет', conjoined=False,
    )
    heating = django_filters.MultipleChoiceFilter(
        choices=HEATING_CHOICES,
        method='filter_heating', label='Работает на обогрев до', conjoined=False,
    )
    type = django_filters.ChoiceFilter(
        choices=TYPE_CHOICES,
        method='filter_type', label='Тип блока',
    )

    class Meta:
        model = Product
        fields = ['q', 'brand', 'category', 'price_min', 'price_max',
                  'btu', 'area', 'inverter', 'color', 'type', 'heating']

    def filter_search(self, queryset, name, value):
        # series включён в поиск — «Серия» не выведена отдельной фасетой
        # (слишком высокая кардинальность для чекбокс-панели, 498 уникальных
        # значений на прод-данных), но найти товар по названию серии
        # (V-series, CAMOMIRU и т.п.) через поиск можно.
        return queryset.filter(
            Q(title__icontains=value) | Q(articul__icontains=value) | Q(series__icontains=value)
        )

    def filter_btu(self, queryset, name, value):
        q = _btu_q(value or [])
        return queryset.filter(q) if q else queryset

    def filter_area(self, queryset, name, value):
        q = _area_q(value or [])
        return queryset.filter(q) if q else queryset

    def filter_inverter(self, queryset, name, value):
        return _inverter_filter(queryset, value or [])

    def filter_color(self, queryset, name, value):
        q = _color_q(value or [])
        return queryset.filter(q) if q else queryset

    def filter_heating(self, queryset, name, value):
        q = _heating_q(value or [])
        return queryset.filter(q) if q else queryset

    def filter_type(self, queryset, name, value):
        pattern = _TYPE_PATTERNS.get(value)
        if not pattern:
            return queryset
        return queryset.filter(title__iregex=pattern)

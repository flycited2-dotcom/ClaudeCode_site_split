"""Ротация «хита недели» на главной.

Раньше карточка брала первый товар из отсортированной подборки, и одна и та
же модель висела с начала лета (замечание владельца 2026-08-29).
"""
from datetime import date, datetime

from django.test import SimpleTestCase

from apps.catalog.weekly import hour_key, pick_hourly, pick_weekly, week_key


class WeekKeyTest(SimpleTestCase):

    def test_same_week_same_key(self):
        # понедельник и воскресенье одной недели
        self.assertEqual(week_key(date(2026, 8, 24)), week_key(date(2026, 8, 30)))

    def test_next_week_differs(self):
        self.assertNotEqual(week_key(date(2026, 8, 30)), week_key(date(2026, 8, 31)))


class PickWeeklyTest(SimpleTestCase):

    ITEMS = [f'item-{i}' for i in range(20)]

    def test_stable_within_week(self):
        a = pick_weekly(self.ITEMS, today=date(2026, 8, 26))
        b = pick_weekly(self.ITEMS, today=date(2026, 8, 30))
        self.assertEqual(a, b)

    def test_changes_between_weeks(self):
        """За квартал выбор должен смениться много раз, а не залипнуть."""
        picks = {
            pick_weekly(self.ITEMS, today=date(2026, m, d))
            for m, d in ((6, 1), (6, 8), (6, 15), (6, 22), (7, 1), (7, 8),
                         (7, 15), (7, 22), (8, 1), (8, 8), (8, 15), (8, 22))
        }
        self.assertGreater(len(picks), 4, f'слишком мало вариантов: {picks}')

    def test_salt_separates_slots(self):
        a = pick_weekly(self.ITEMS, salt='home-hit', today=date(2026, 8, 26))
        b = pick_weekly(self.ITEMS, salt='other-slot', today=date(2026, 8, 26))
        self.assertNotEqual(a, b)

    def test_empty_list_returns_none(self):
        self.assertIsNone(pick_weekly([]))

    def test_single_item(self):
        self.assertEqual(pick_weekly(['only']), 'only')


class PickHourlyTest(SimpleTestCase):
    """Ротация раз в час: на 648 позициях недельный шаг слишком редкий."""

    ITEMS = [f'item-{i}' for i in range(50)]

    def test_stable_within_hour(self):
        a = pick_hourly(self.ITEMS, now=datetime(2026, 8, 30, 14, 5))
        b = pick_hourly(self.ITEMS, now=datetime(2026, 8, 30, 14, 59))
        self.assertEqual(a, b)

    def test_changes_next_hour(self):
        a = pick_hourly(self.ITEMS, now=datetime(2026, 8, 30, 14, 30))
        b = pick_hourly(self.ITEMS, now=datetime(2026, 8, 30, 15, 30))
        self.assertNotEqual(a, b)

    def test_many_variants_per_day(self):
        picks = {
            pick_hourly(self.ITEMS, now=datetime(2026, 8, 30, h, 0))
            for h in range(24)
        }
        self.assertGreater(len(picks), 10, f'слишком мало вариантов за сутки: {len(picks)}')

    def test_empty_and_single(self):
        self.assertIsNone(pick_hourly([]))
        self.assertEqual(pick_hourly(['only']), 'only')

    def test_hour_key_format(self):
        self.assertEqual(hour_key(datetime(2026, 8, 30, 9, 15)), '2026-08-30-09')

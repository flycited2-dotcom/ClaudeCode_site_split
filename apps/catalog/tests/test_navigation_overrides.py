"""Наши названия разделов не должны откатываться синком поставщика.

Регрессия 2026-08-30: merge_categories переименовал слитую категорию в
«Инфракрасные обогреватели», ночной sync_categories вернул название из
справочника Бриза, и раздел выпал из группы «Обогрев» в «Другое».
"""
from django.test import SimpleTestCase

from apps.catalog.navigation import CATALOG_GROUPS, CATEGORY_TITLE_OVERRIDES


class CategoryTitleOverridesTest(SimpleTestCase):

    def test_every_override_is_placed_in_a_group(self):
        """Переименовали — значит раздел должен быть в дереве под новым именем,
        иначе он снова окажется в «Другое»."""
        placed = {title for _, titles in CATALOG_GROUPS for title in titles}
        for breez_id, title in CATEGORY_TITLE_OVERRIDES.items():
            self.assertIn(title, placed, f'категория {breez_id} не попала в CATALOG_GROUPS')

    def test_overrides_are_unique(self):
        titles = list(CATEGORY_TITLE_OVERRIDES.values())
        self.assertEqual(len(titles), len(set(titles)))

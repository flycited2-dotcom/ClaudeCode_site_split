"""Тестовые данные не должны уезжать в рабочие каналы.

Регрессия 2026-08-29: прогон тестов регистрации отправил владельцу больше
десяти уведомлений «Новая регистрация» с выдуманными ivan@example.com и
«ООО Тест». Тестовый контейнер делил Redis с продом, задача попала в общую
очередь, и её выполнил боевой воркер.
"""
from unittest.mock import patch

from django.test import TestCase

from apps.notifications.tasks import enqueue_manager_notifications


class NotificationsDoNotLeakInTestsTest(TestCase):

    def test_enqueue_is_noop_while_testing(self):
        with patch('apps.notifications.tasks.send_manager_notifications.apply_async') as sent:
            result = enqueue_manager_notifications(
                subject='Новая регистрация', email_body='...', telegram_text='...',
            )
        self.assertFalse(result)
        sent.assert_not_called()

    def test_registration_signal_sends_nothing(self):
        """Полный путь: создаём пользователя — наружу ничего не уходит."""
        from django.contrib.auth import get_user_model

        with patch('apps.notifications.tasks.send_manager_notifications.apply_async') as sent, \
                patch('apps.notifications.telegram.send_telegram') as tg:
            get_user_model().objects.create_user(
                username='leak-check', email='leak@example.com', password='x',
            )
        sent.assert_not_called()
        tg.assert_not_called()

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from apps.notifications.tasks import (
    enqueue_manager_notifications,
    send_manager_notifications,
)


class ManagerNotificationTaskTest(SimpleTestCase):
    @override_settings(MANAGER_EMAIL='manager@example.test')
    @patch('apps.notifications.tasks.send_mail')
    @patch('apps.notifications.tasks.send_telegram', return_value=True)
    def test_task_delivers_both_channels(self, telegram, email):
        send_manager_notifications(
            subject='Subject', email_body='Body', telegram_text='Telegram',
        )

        telegram.assert_called_once_with('Telegram')
        email.assert_called_once()

    @override_settings(MANAGER_EMAIL='manager@example.test')
    @patch('apps.notifications.tasks.send_mail', side_effect=TimeoutError)
    @patch('apps.notifications.tasks.send_telegram', return_value=False)
    def test_provider_failures_do_not_escape_task(self, telegram, email):
        send_manager_notifications(
            subject='Subject', email_body='Body', telegram_text='Telegram',
        )

        telegram.assert_called_once()
        email.assert_called_once()

    @override_settings(TESTING=False)
    @patch('apps.notifications.tasks.send_manager_notifications.apply_async',
           side_effect=ConnectionError)
    def test_broker_failure_does_not_escape_request_helper(self, apply_async):
        # TESTING=False снимаем намеренно: при обычном прогоне уведомления
        # вообще не ставятся в очередь (защита от утечки тестовых данных в
        # рабочий Telegram, см. apps/notifications/tasks), а здесь нужно
        # проверить именно устойчивость к падению брокера.
        self.assertFalse(enqueue_manager_notifications(telegram_text='Message'))
        apply_async.assert_called_once()


class LeadTemplateContractTest(TestCase):
    def test_full_page_forms_target_their_visible_result_container(self):
        selection = self.client.get('/selection/').content.decode()
        installation = self.client.get('/installation/').content.decode()

        self.assertIn('hx-target="#selection-result"', selection)
        self.assertIn('hx-target="#installation-result"', installation)
        self.assertNotIn('hx-target="body"', selection)
        self.assertNotIn('hx-target="body"', installation)
        self.assertNotIn('hx-on::after-request', selection)
        self.assertNotIn('hx-on::after-request', installation)

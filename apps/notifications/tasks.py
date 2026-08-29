import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

from .telegram import send_telegram

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True, soft_time_limit=20, time_limit=30)
def send_manager_notifications(*, subject='', email_body='', telegram_text=''):
    """Deliver manager notifications outside the user's HTTP request."""
    if telegram_text and not send_telegram(telegram_text):
        logger.warning('Manager Telegram notification was not delivered')

    if subject and email_body and settings.MANAGER_EMAIL:
        try:
            send_mail(
                subject=subject,
                message=email_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[settings.MANAGER_EMAIL],
                fail_silently=False,
            )
        except Exception as exc:
            logger.error('Manager email notification failed: %s', exc)


def enqueue_manager_notifications(*, subject='', email_body='', telegram_text=''):
    """Best-effort enqueue: a broker outage must not reject a saved order/lead."""
    # Во время тестов наружу не пишем. Тестовый прогон может делить брокер с
    # продом (так и случилось 2026-08-29: боевой воркер разослал менеджеру
    # выдуманные регистрации из тестов), поэтому глушим на входе, а не надеемся
    # на то, что у тестового окружения нет токена.
    if getattr(settings, 'TESTING', False):
        logger.debug('TESTING=True — уведомление менеджеру не ставится в очередь')
        return False
    try:
        send_manager_notifications.apply_async(
            kwargs={
                'subject': subject,
                'email_body': email_body,
                'telegram_text': telegram_text,
            },
            retry=False,
        )
        return True
    except Exception as exc:
        logger.error(
            'Could not enqueue manager notification (%s)', type(exc).__name__,
        )
        return False

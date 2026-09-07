import os
from pathlib import Path

from django.conf import settings


def static_version(request):
    """Метка версии CSS для ?v= в base.html.

    Nginx отдаёт /static/ с `max-age=2592000` (30 дней), а имена файлов не
    хэшируются — после деплоя вернувшийся посетитель месяц видел старую
    вёрстку (поймано 2026-08-29: правки квиза не доезжали до браузера).
    Берём mtime собранного tailwind.css: он меняется при каждой пересборке,
    и в кэше появляется новый URL. Файла нет — пустая строка, ссылка
    остаётся прежней.
    """
    candidates = [Path(settings.BASE_DIR) / 'static']
    candidates += [Path(d) for d in getattr(settings, 'STATICFILES_DIRS', [])]
    for base in candidates:
        try:
            return {'STATIC_VERSION': str(int(os.path.getmtime(base / 'css' / 'tailwind.css')))}
        except OSError:
            continue
    return {'STATIC_VERSION': ''}


def yandex_metrika(request):
    return {'YANDEX_METRIKA_ID': getattr(settings, 'YANDEX_METRIKA_ID', '')}


def site_contacts(request):
    """Телефон и мессенджеры для шапки и подвала.

    Раньше номер был захардкожен в contacts.html и warranty.html, а в шапке его
    не было вовсе — посетитель с рекламы попадал на посадочную страницу и не мог
    позвонить в один тап. Для товара за ~27 000 ₽, где половина решений
    принимается голосом, это прямая потеря заявок.

    SITE_PHONE_RAW — для href="tel:", без пробелов и скобок.
    Мессенджеры пустые по умолчанию: кнопка не выводится, пока номер не задан
    в .env, — чтобы не вести людей в несуществующий чат.
    """
    phone_raw = getattr(settings, 'SITE_PHONE_RAW', '')
    return {
        'SITE_PHONE_RAW': phone_raw,
        'SITE_PHONE': getattr(settings, 'SITE_PHONE', ''),
        'SITE_WHATSAPP': getattr(settings, 'SITE_WHATSAPP', ''),
        'SITE_TELEGRAM': getattr(settings, 'SITE_TELEGRAM', ''),
    }


def seo_verification(request):
    """Токены подтверждения прав для панелей вебмастеров (Google/Yandex/Bing).
    Рендерятся в base.html как <meta> только при заполненном значении.
    """
    return {
        'GOOGLE_SITE_VERIFICATION': getattr(settings, 'GOOGLE_SITE_VERIFICATION', ''),
        'YANDEX_VERIFICATION': getattr(settings, 'YANDEX_VERIFICATION', ''),
        'BING_SITE_VERIFICATION': getattr(settings, 'BING_SITE_VERIFICATION', ''),
    }

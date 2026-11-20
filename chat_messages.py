"""Configurable Twitch chat templates."""

import json
import logging
from string import Formatter

log = logging.getLogger("hots-stats-watcher")

DEFAULTS = {
    "orders.accepted": "@{user} заказ героя {hero} принят NOTED",
    "orders.unknown_hero": "@{user} SHTO попробуй еще раз",
    "orders.unavailable_hero": "@{user} заказ данного героя пока недоступен uvi",
    "orders.refunded": " (баллы возвращены)",
    "orders.fulfilled": "Заказ героя {hero} от @{user} выполнен",
    "queue.empty": "Очередь заказов пуста",
    "queue.list": "Очередь заказов: {items}",
    "queue.item": "{number}. {hero} (@{user})",
    "queue.separator": ", ",
    "queue.more": " … и ещё {count}",
    "build.unknown_hero": "Неизвестный герой «{input}». Проверьте название.",
    "build.no_match": "Нет данных о последнем матче.",
    "build.own_unavailable": "Не удалось получить ваш билд для последнего матча.",
    "build.hero_absent": "В последнем матче не было героя {hero}.",
    "build.hero_ambiguous": "В последнем матче было несколько героев {hero}.",
    "build.hero_unavailable": "Не удалось получить билд героя {hero} для этого матча.",
    "build.success": "{build_code}",
    "match.result": "{result} на карте «{map}». Счёт: {wins}–{losses}.",
    "match.win": "Победа",
    "match.loss": "Поражение",
    "match.unknown_map": "Неизвестная карта",
    "heroes.link": "{url}",
    "heroes.no_link": "Для заказа доступны все герои",
}


def load(path):
    """Load valid overrides, keeping defaults available for every message."""
    try:
        with path.open(encoding="utf-8-sig") as stream:
            groups = json.load(stream)
        if not isinstance(groups, dict):
            raise ValueError("Ожидается JSON-объект с группами сообщений")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Не удалось загрузить %s: %s", path, exc)
        return {}

    overrides = {}
    formatter = Formatter()
    for group, entries in groups.items():
        if not isinstance(entries, dict):
            log.warning("%s: группа %s должна быть JSON-объектом", path, group)
            continue
        for name, value in entries.items():
            key = f"{group}.{name}"
            if key not in DEFAULTS:
                log.warning("%s: неизвестный ключ %s", path, key)
                continue

            fields = {field for _, field, _, _ in formatter.parse(DEFAULTS[key])}
            try:
                if not isinstance(value, str):
                    raise ValueError("ожидается строка")
                for _, field, spec, conversion in formatter.parse(value):
                    if field is not None and (field not in fields or spec or conversion):
                        raise ValueError(f"недопустимая подстановка {{{field}}}")
            except ValueError as exc:
                log.warning("%s: некорректный шаблон %s (%s)", path, key, exc)
                continue
            overrides[key] = value
    return overrides


def template(messages, key, legacy=None):
    # The bundled JSON contains the defaults so existing installations can edit it in place.
    default = DEFAULTS[key]
    value = (messages or {}).get(key, default)
    if legacy is not None and value == default:
        return legacy
    return value


def render(messages, key, **values):
    return template(messages, key).format(**values)


def result_label(messages, key, legacy):
    key = f"match.{key}"
    value = template(messages, key)
    return legacy if value == DEFAULTS[key] else value.format()

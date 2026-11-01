#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Минимальный клиент Twitch Helix/OAuth API на urllib (без внешних пакетов) -
нужен для работы с наградой за баллы канала (создание/обновление награды,
чтение активаций, подтверждение и возврат баллов) и для обмена
authorization code / refresh_token на access_token (см. twitch_auth_setup.py
и twitch_token_manager.py).

Все Helix-запросы принимают token_manager (twitch_token_manager.TokenManager)
вместо голого токена - так access_token всегда свежий, а при ответе 401
запрос автоматически повторяется один раз после принудительного обновления.

Эндпоинты /channel_points/* требуют токен АККАУНТА КАНАЛА (стримера) со
scope channel:manage:redemptions, и управлять через них можно только
наградами, созданными тем же client_id.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("hots-stats-watcher")

API_BASE = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

REDEMPTION_FULFILLED = "FULFILLED"
REDEMPTION_CANCELED = "CANCELED"


def _post_token(params):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_code(client_id, client_secret, code, redirect_uri):
    """Разовый обмен authorization code (из twitch_auth_setup.py) на токены."""
    return _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )


def refresh_access_token(client_id, client_secret, refresh_token_value):
    """
    Обновляет access_token по refresh_token. Возвращает dict с ключами
    access_token, refresh_token, expires_in (Twitch может прислать новый
    refresh_token - старый после этого становится недействителен, вызывающий
    код обязан сохранить новое значение).
    """
    return _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
        }
    )


def _request(method, path, params, client_id, access_token, body=None):
    url = "{0}{1}".format(API_BASE, path)
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {
        "Client-Id": client_id,
        "Authorization": "Bearer " + access_token,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def _http_error_text(exc):
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - тело ошибки нужно только для лога
        return ""


def _request_with_retry(method, path, params, client_id, token_manager, body=None, not_found_ok=False):
    """
    Возвращает разобранный JSON ответа, либо None при ошибке (ошибка уже
    залогирована). not_found_ok=True - ответ 404 считается успехом (вернётся {}).
    """
    try:
        access_token = token_manager.get_access_token()
    except Exception as exc:  # noqa: BLE001 - сеть/невалидный refresh_token не должны ронять бота
        log.warning("Не удалось получить Twitch access_token: %s", exc)
        return None

    try:
        try:
            return _request(method, path, params, client_id, access_token, body)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            log.info("Twitch access_token устарел (401), принудительно обновляю")
            try:
                access_token = token_manager.force_refresh()
            except Exception as exc2:  # noqa: BLE001 - тоже не должно ронять бота
                log.warning("Не удалось обновить Twitch access_token после 401: %s", exc2)
                return None
            return _request(method, path, params, client_id, access_token, body)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and not_found_ok:
            return {}
        log.warning("Ошибка запроса к Twitch Helix (%s %s): %s %s", method, path, exc, _http_error_text(exc))
        return None
    except urllib.error.URLError as exc:
        log.warning("Ошибка запроса к Twitch Helix (%s %s): %s", method, path, exc)
        return None
    except (ValueError, KeyError) as exc:
        log.warning("Не удалось разобрать ответ Twitch Helix (%s %s): %s", method, path, exc)
        return None


def get_token_user(client_id, token_manager):
    """Возвращает {"id", "login", ...} пользователя, которому выдан токен, либо None."""
    data = _request_with_retry("GET", "/users", None, client_id, token_manager)
    if not data or not data.get("data"):
        return None
    return data["data"][0]


def list_manageable_rewards(broadcaster_id, client_id, token_manager):
    """Награды канала, созданные этим client_id (только ими можно управлять через API)."""
    data = _request_with_retry(
        "GET",
        "/channel_points/custom_rewards",
        {"broadcaster_id": broadcaster_id, "only_manageable_rewards": "true"},
        client_id,
        token_manager,
    )
    if data is None:
        return None
    return data.get("data", [])


def create_custom_reward(broadcaster_id, settings, client_id, token_manager):
    data = _request_with_retry(
        "POST",
        "/channel_points/custom_rewards",
        {"broadcaster_id": broadcaster_id},
        client_id,
        token_manager,
        body=settings,
    )
    if not data or not data.get("data"):
        return None
    return data["data"][0]


def update_custom_reward(broadcaster_id, reward_id, settings, client_id, token_manager):
    data = _request_with_retry(
        "PATCH",
        "/channel_points/custom_rewards",
        {"broadcaster_id": broadcaster_id, "id": reward_id},
        client_id,
        token_manager,
        body=settings,
    )
    if not data or not data.get("data"):
        return None
    return data["data"][0]


def get_unfulfilled_redemptions(broadcaster_id, reward_id, client_id, token_manager):
    """
    Все активации награды в статусе UNFULFILLED, от старых к новым (со всех
    страниц). Возвращает список dict (id, user_login, user_name, user_input,
    redeemed_at, ...) либо None, если хотя бы один запрос не удался - чтобы
    вызывающий код не принял неполный список за полный.
    """
    result = []
    cursor = None
    while True:
        params = {
            "broadcaster_id": broadcaster_id,
            "reward_id": reward_id,
            "status": "UNFULFILLED",
            "sort": "OLDEST",
            "first": 50,
        }
        if cursor:
            params["after"] = cursor
        data = _request_with_retry(
            "GET", "/channel_points/custom_rewards/redemptions", params, client_id, token_manager
        )
        if data is None:
            return None
        page = data.get("data", [])
        result.extend(page)
        cursor = (data.get("pagination") or {}).get("cursor")
        if not cursor or not page:
            return result


def update_redemptions_status(broadcaster_id, reward_id, redemption_ids, status, client_id, token_manager):
    """
    Переводит активации в FULFILLED или CANCELED (CANCELED возвращает зрителю
    баллы). Twitch принимает до 50 id за раз. Возвращает True при успехе.
    404 (активация не найдена или уже не UNFULFILLED - например, её вручную
    обработали на Twitch) тоже считается успехом: менять там больше нечего.
    """
    ids = list(redemption_ids)
    for i in range(0, len(ids), 50):
        data = _request_with_retry(
            "PATCH",
            "/channel_points/custom_rewards/redemptions",
            {"broadcaster_id": broadcaster_id, "reward_id": reward_id, "id": ids[i:i + 50]},
            client_id,
            token_manager,
            body={"status": status},
            not_found_ok=True,
        )
        if data is None:
            return False
    return True

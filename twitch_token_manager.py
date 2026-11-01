#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Хранит и автоматически обновляет access_token Twitch-бота по refresh_token.

Access-токены Twitch живут несколько часов. Вместо того чтобы просить
пользователя руками их обновлять, храним долгоживущий refresh_token
(полученный один раз через twitch_auth_setup.py) и обновляем access_token
по нему сами - лениво, при первом обращении и когда истекает срок действия,
плюс принудительно при ответе Helix 401 (см. twitch_helix._get_with_retry).

Twitch иногда присылает новый refresh_token при обновлении - старый после
этого становится недействителен, поэтому новое значение нужно сразу же
сохранить, иначе следующий перезапуск утилиты не сможет обновиться. Это и
делает on_refresh_token_changed.
"""

import logging
import threading
import time

import twitch_helix

log = logging.getLogger("hots-stats-watcher")

REFRESH_SAFETY_MARGIN_SECONDS = 300


class TokenManager:
    def __init__(self, client_id, client_secret, refresh_token, on_refresh_token_changed=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token = None
        self._expires_at = 0
        self._lock = threading.Lock()
        self._on_refresh_token_changed = on_refresh_token_changed

    def get_access_token(self):
        """Возвращает валидный access_token, обновляя его по необходимости."""
        with self._lock:
            if self._access_token is None or time.time() >= self._expires_at:
                self._refresh_locked()
            return self._access_token

    def force_refresh(self):
        """Принудительно обновляет access_token (например, после ответа 401) и возвращает новый."""
        with self._lock:
            self._refresh_locked()
            return self._access_token

    def _refresh_locked(self):
        data = twitch_helix.refresh_access_token(self.client_id, self.client_secret, self._refresh_token)
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600) - REFRESH_SAFETY_MARGIN_SECONDS

        new_refresh_token = data.get("refresh_token")
        if new_refresh_token and new_refresh_token != self._refresh_token:
            self._refresh_token = new_refresh_token
            if self._on_refresh_token_changed:
                try:
                    self._on_refresh_token_changed(new_refresh_token)
                except Exception:  # noqa: BLE001 - не смогли сохранить - хотя бы работаем дальше в памяти
                    log.exception("Не удалось сохранить обновлённый twitch_refresh_token")

        log.info("Twitch access_token обновлён (действует ~%d мин.)", data.get("expires_in", 3600) // 60)

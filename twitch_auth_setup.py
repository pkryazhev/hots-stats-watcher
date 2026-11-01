#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разовая утилита: получает первые refresh_token'ы для заказа героев.

Токенов нужно два, запуск делается по разу для каждого:

    python twitch_auth_setup.py
        токен БОТА (чат) -> twitch_refresh_token.
        Открывать ссылку нужно, войдя в браузере ПОД АККАУНТОМ БОТА.

    python twitch_auth_setup.py broadcaster
        токен КАНАЛА (награда за баллы) -> twitch_broadcaster_refresh_token.
        Открывать ссылку нужно, войдя в браузере ПОД АККАУНТОМ КАНАЛА
        (стримера) - управлять наградами за баллы может только владелец.

Если войти не под тем аккаунтом, токен привяжется не к тому пользователю
(на странице Twitch видно, под кем вы вошли - там же можно сменить аккаунт).

twitch_client_id/twitch_client_secret берутся из config.json рядом со
скриптом (спрашиваются, только если их там нет), а полученный refresh_token
сразу записывается туда же. Путь к другому конфигу можно передать вторым
аргументом: python twitch_auth_setup.py broadcaster путь/к/config.json.
Если конфига ещё нет, скрипт просто выведет значения для копирования.

Дальше watch_replays.py обновляет access_token сам, без повторных походов в
браузер (см. twitch_token_manager.py). Оба токена должны быть получены для
одного и того же приложения (twitch_client_id).
"""

import http.server
import json
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import twitch_helix

REDIRECT_PORT = 17563
REDIRECT_URI = "http://localhost:{0}/callback".format(REDIRECT_PORT)
MODES = {
    "bot": {
        "scopes": "chat:read chat:edit",
        "config_key": "twitch_refresh_token",
        "account": "БОТА (не своим основным)",
    },
    "broadcaster": {
        "scopes": "channel:manage:redemptions",
        "config_key": "twitch_broadcaster_refresh_token",
        "account": "КАНАЛА (стримера), на котором будет награда за баллы",
    },
}


def _load_config(config_path):
    if not config_path.exists():
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config_values(config_path, values):
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.update(values)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            body = "Готово, можно закрыть эту вкладку и вернуться в консоль."
        else:
            _CallbackHandler.error = params.get("error_description", ["неизвестная ошибка"])[0]
            body = "Ошибка авторизации: {0}. Вернитесь в консоль.".format(_CallbackHandler.error)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):  # noqa: A002 - подавляем стандартный лог http.server
        pass


def main():
    mode_name = sys.argv[1] if len(sys.argv) > 1 else "bot"
    if mode_name not in MODES:
        print("Неизвестный режим {0!r}, допустимо: {1}".format(mode_name, ", ".join(MODES)), file=sys.stderr)
        sys.exit(1)
    mode = MODES[mode_name]

    config_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent / "config.json"
    config = _load_config(config_path)

    client_id = (config or {}).get("twitch_client_id", "").strip()
    client_secret = (config or {}).get("twitch_client_secret", "").strip()
    if client_id and client_secret:
        print("twitch_client_id и twitch_client_secret взяты из", config_path)
    else:
        client_id = input("twitch_client_id: ").strip()
        client_secret = input("twitch_client_secret: ").strip()
    if not client_id or not client_secret:
        print("Нужно ввести оба значения.", file=sys.stderr)
        sys.exit(1)

    auth_url = "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": mode["scopes"],
            "force_verify": "true",
        }
    )

    print()
    print("В настройках приложения на dev.twitch.tv/console/apps в OAuth Redirect URLs")
    print("должен быть добавлен именно:", REDIRECT_URI)
    print()
    print("Откройте эту ссылку в браузере, ВОЙДЯ ПОД АККАУНТОМ {0}:".format(mode["account"]))
    print(auth_url)
    print()

    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001 - не критично, ссылку можно открыть руками
        pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    print("Жду подтверждения в браузере...")
    while _CallbackHandler.code is None and _CallbackHandler.error is None:
        server.handle_request()

    if _CallbackHandler.error:
        print("Ошибка авторизации:", _CallbackHandler.error, file=sys.stderr)
        sys.exit(1)

    tokens = twitch_helix.exchange_code(client_id, client_secret, _CallbackHandler.code, REDIRECT_URI)

    values = {
        "twitch_client_id": client_id,
        "twitch_client_secret": client_secret,
        mode["config_key"]: tokens["refresh_token"],
    }

    print()
    if config is not None:
        _save_config_values(config_path, values)
        print("Готово! {0} записан в {1}".format(mode["config_key"], config_path))
    else:
        print("Готово! Файл {0} не найден - добавьте в config.json:".format(config_path))
        print(json.dumps(values, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

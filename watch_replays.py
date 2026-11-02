#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hots-stats-watcher
===================

Консольная утилита, которая следит за папками с реплеями Heroes of the Storm,
автоматически определяет по BattleTag'ам из конфига, участвовали ли вы в новом
матче, и ведёт счётчик побед/поражений за текущую сессию работы приложения.

Счётчик пишется в текстовый файл (по умолчанию hots_stats.txt), который можно
подключить как источник "Текст (GDI+)" / "Text (FreeType 2)" в OBS.

Запуск:
    python watch_replays.py [путь_к_config.json]

Если путь не указан, берётся config.json рядом со скриптом.
"""

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Подключаем вендоренные зависимости (mpyq + heroprotocol от Blizzard).
# Ставить их через pip не нужно - они лежат прямо в vendor/.
# ---------------------------------------------------------------------------
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
sys.path.insert(0, str(VENDOR_DIR))

import mpyq  # noqa: E402
import heroprotocol.versions as hv  # noqa: E402

import channel_points
import heroes
import hero_orders
import twitch_chat
import twitch_helix
import twitch_token_manager

log = logging.getLogger("hots-stats-watcher")

RESULT_WIN = 1
RESULT_LOSS = 2

DEFAULT_CONFIG = {
    "match_results_enabled": True,
    "match_results_format": "{result} на карте «{map}». Счёт: {wins}–{losses}.",
    "maps_file": "maps.json",
    "battletags": [],
    "toon_handles": [],
    "replay_folders": [],
    "auto_discover_accounts_root": "",
    "poll_interval_seconds": 20,
    "obs_output_file": "hots_stats.txt",
    "obs_format": "Победы: {wins}\nПоражения: {losses}",
    "known_players_file": "known_players.txt",
    # --- Заказ героя за баллы канала Twitch ---
    "orders_enabled": False,
    "orders_reward_title": "Заказ героя",
    "orders_reward_cost": 1000,
    "orders_reward_prompt": "Напишите имя героя (список героев - команда !герои)",
    "orders_poll_interval_seconds": 5,
    "orders_storage_file": "orders.json",
    "orders_queue_command": "!очередь",
    "heroes_link_command": "!герои",
    "heroes_link_url": "https://docs.google.com/spreadsheets/d/1JjNm3DkDIrqdKG1B8giJxrgRFezx1eyK6Cu2aEv5JY4/edit?usp=sharing",
    "orders_heroes_file": "heroes.json",
    "twitch_channel": "",
    "twitch_bot_username": "",
    "twitch_client_id": "",
    "twitch_client_secret": "",
    "twitch_refresh_token": "",
    "twitch_broadcaster_refresh_token": "",
}


# ---------------------------------------------------------------------------
# Разбор реплея
# ---------------------------------------------------------------------------

def resolve_protocol(base_build):
    """
    Возвращает модуль протокола для конкретного билда игры.

    heroprotocol содержит файлы protocolXXXXX.py только для тех билдов,
    которые были в репозитории на момент его сборки. Если вышел новый патч
    Heroes of the Storm, а обновлённого protocol-файла ещё нет - берём
    ближайший более старый (формат replay.details/tracker почти никогда не
    меняется между соседними патчами, так что это надёжный fallback).
    """
    try:
        return hv.build(base_build)
    except ImportError:
        pass

    base_path = os.path.dirname(hv.__file__)
    files = hv.list_all(base_path)
    builds = []
    for f in files:
        m = re.match(r"protocol(\d+)\.py$", f)
        if m:
            builds.append(int(m.group(1)))
    if not builds:
        raise RuntimeError("В vendor/heroprotocol не найдено ни одного protocolXXXXX.py")

    lower_or_equal = [b for b in builds if b <= base_build]
    chosen = max(lower_or_equal) if lower_or_equal else max(builds)
    log.debug("Билд %s не поддерживается напрямую, использую ближайший protocol%05d", base_build, chosen)
    return hv._import_protocol(base_path, "protocol{0:05d}".format(chosen))


def build_toon_handle(toon):
    """
    Собирает из структуры m_toon реплея строку вида "1-Hero-1-123456789" -
    это тот же формат, в котором Battle.net называет папки аккаунтов
    (Accounts\\<ID>\\<регион>-Hero-<x>-<ID>). В отличие от BattleTag, этот ID
    не меняется при переименовании и однозначно определяет аккаунт, поэтому
    не ломается, если у двух разных игроков совпал ник.
    """
    if not toon:
        return None
    region = toon.get("m_region")
    program_id = toon.get("m_programId")
    realm = toon.get("m_realm")
    toon_id = toon.get("m_id")
    if region is None or toon_id is None:
        return None
    if isinstance(program_id, bytes):
        program_id = program_id.decode("ascii", errors="replace").strip("\x00")
    return "{0}-{1}-{2}-{3}".format(region, program_id, realm, toon_id)


def parse_tracker_heroes(archive, protocol):
    """
    Разбирает replay.tracker.events и возвращает {control_player_id: internal_hero_name}.

    internal_hero_name - это m_unitTypeName самого первого SUnitBornEvent,
    принадлежащего данному control_player_id. Герой рождается в самом начале
    матча, раньше любых других юнитов/призывов, которыми управляет игрок,
    поэтому "первое рождение юнита под этим ID" надёжно даёт героя. Это имя
    - внутренний (движковый) идентификатор героя, который НЕ зависит от
    языка клиента игры, в отличие от m_hero из replay.details.
    """
    if not hasattr(protocol, "decode_replay_tracker_events"):
        return {}
    try:
        contents = archive.read_file("replay.tracker.events")
    except KeyError:
        return {}

    def dec(b):
        return b.decode("utf-8", errors="replace") if isinstance(b, bytes) else b

    hero_by_player = {}
    for event in protocol.decode_replay_tracker_events(contents):
        if event.get("_event") != "NNet.Replay.Tracker.SUnitBornEvent":
            continue
        control_player_id = event.get("m_controlPlayerId")
        if not control_player_id or control_player_id in hero_by_player:
            # 0 - нейтральный контроллер (не игрок); для уже найденного
            # игрока берём именно первое (самое раннее) событие - это и есть герой.
            continue
        hero_by_player[control_player_id] = dec(event.get("m_unitTypeName"))
    return hero_by_player


def parse_replay(path):
    """
    Разбирает .StormReplay и возвращает словарь с картой и списком игроков:
    [{"name": str, "hero": str, "hero_localized": str, "team": int,
      "result": int, "toon_handle": str|None}, ...]

    "hero" - внутреннее, не зависящее от языка клиента имя героя из
    replay.tracker.events (см. parse_tracker_heroes) - именно оно должно
    попадать в поле "replay_hero" в heroes.json. Если tracker events разобрать
    не удалось (старый билд без них, повреждённый файл, герой не найден по
    working set slot) - используется запасной вариант "hero_localized"
    (m_hero из replay.details - локализованное под язык клиента имя).

    m_result: 1 = победа, 2 = поражение, 0 = неизвестно (например, игрок
    покинул матч раньше окончания и результат не был зафиксирован).
    """
    archive = mpyq.MPQArchive(str(path))

    header_contents = archive.header["user_data_header"]["content"]
    header = hv.latest().decode_replay_header(header_contents)
    base_build = header["m_version"]["m_baseBuild"]

    protocol = resolve_protocol(base_build)
    details = protocol.decode_replay_details(archive.read_file("replay.details"))

    def dec(b):
        return b.decode("utf-8", errors="replace") if isinstance(b, bytes) else b

    hero_by_control_player_id = parse_tracker_heroes(archive, protocol)

    players = []
    for p in details.get("m_playerList", []):
        localized_hero = dec(p.get("m_hero", b""))
        slot_id = p.get("m_workingSetSlotId")
        control_player_id = slot_id + 1 if slot_id is not None else None
        internal_hero = hero_by_control_player_id.get(control_player_id)
        if not internal_hero:
            log.debug(
                "Не удалось определить героя %s по replay.tracker.events (control_player_id=%s), "
                "использую локализованное имя из replay.details: %s",
                dec(p.get("m_name", b"")),
                control_player_id,
                localized_hero,
            )
        players.append(
            {
                "name": dec(p.get("m_name", b"")),
                "hero": internal_hero or localized_hero,
                "hero_localized": localized_hero,
                "team": p.get("m_teamId"),
                "result": p.get("m_result"),
                "toon_handle": build_toon_handle(p.get("m_toon")),
            }
        )

    return {
        "map": dec(details.get("m_title", b"")),
        "base_build": base_build,
        "players": players,
    }


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        user_config = json.load(f)
    config = dict(DEFAULT_CONFIG)
    config.update(user_config)
    return config


_persist_config_lock = threading.Lock()


def persist_config_value(config_path, key, value):
    """
    Точечно обновляет один ключ в config.json на диске и сохраняет остальное
    содержимое как есть. Нужно, чтобы после того, как Twitch присылает новый
    refresh_token (старый после этого становится недействителен), он не
    потерялся при перезапуске утилиты. Токенов два (бота и канала), и они
    могут обновиться одновременно из разных потоков - отсюда блокировка.
    """
    with _persist_config_lock:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data[key] = value
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            log.info("Сохранил обновлённый %s в %s", key, config_path)
        except (OSError, ValueError) as exc:
            log.warning("Не удалось сохранить %s в %s: %s", key, config_path, exc)


def discover_replay_folders(accounts_root):
    """
    Автопоиск папок вида .../Accounts/<id>/<x-Hero-x-id>/Replays/Multiplayer
    внутри указанной папки Accounts. Позволяет не перечислять руками все
    аккаунты, если они лежат в стандартном месте профиля Battle.net.
    """
    found = []
    if not accounts_root or not os.path.isdir(accounts_root):
        return found
    for root, dirs, _files in os.walk(accounts_root):
        if os.path.basename(root).lower() == "multiplayer" and os.path.basename(
            os.path.dirname(root)
        ).lower() == "replays":
            found.append(root)
    return found


def resolve_watch_folders(config):
    folders = list(config.get("replay_folders", []))
    folders += discover_replay_folders(config.get("auto_discover_accounts_root", ""))
    # убираем дубликаты, сохраняя порядок
    seen = set()
    result = []
    for f in folders:
        norm = os.path.normpath(f)
        if norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


# ---------------------------------------------------------------------------
# Вывод для OBS
# ---------------------------------------------------------------------------

def write_obs_file(config, stats):
    text = config["obs_format"].format(wins=stats["wins"], losses=stats["losses"])
    out_path = config["obs_output_file"]
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, out_path)  # атомарная замена, чтобы OBS не прочитал полу-записанный файл


# ---------------------------------------------------------------------------
# Файл с известными toon_handle найденных игроков
# ---------------------------------------------------------------------------

def record_known_player(known_players_file, name, toon_handle, map_name):
    """
    Дописывает в отдельный текстовый файл уникальный ID найденного игрока -
    чтобы его можно было скопировать в 'toon_handles' в конфиге и больше не
    зависеть от возможных совпадений BattleTag. Каждый ID сохраняется только
    один раз (при повторных совпадениях строка не дублируется).
    """
    if not toon_handle or not known_players_file:
        return

    marker = '"{0}"'.format(toon_handle)
    existing = ""
    if os.path.exists(known_players_file):
        try:
            with open(known_players_file, "r", encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            pass
    if marker in existing:
        return

    try:
        with open(known_players_file, "a", encoding="utf-8") as f:
            if not existing:
                f.write(
                    "# Здесь утилита сохраняет уникальные ID (toon_handle) найденных игроков.\n"
                    "# Этот ID не меняется при переименовании BattleTag и не путается, если у\n"
                    "# двух игроков одинаковый ник. Если такое совпадение сломало статистику -\n"
                    "# скопируйте нужные строки в кавычках в список 'toon_handles' в config.json,\n"
                    "# и сопоставление будет в первую очередь идти по ним.\n\n"
                )
            f.write(
                "# {0} - карта: {1} ({2})\n\"{3}\",\n".format(
                    name, map_name, time.strftime("%Y-%m-%d %H:%M:%S"), toon_handle
                )
            )
    except OSError as exc:
        log.warning("Не удалось записать %s: %s", known_players_file, exc)


# ---------------------------------------------------------------------------
# Обработка одного нового файла реплея
# ---------------------------------------------------------------------------

def process_file(path, config, battletags_lower, toon_handles, stats, hero_dict=None, order_store=None, bot=None, map_names=None):
    try:
        info = parse_replay(path)
    except Exception as exc:  # noqa: BLE001 - реплей может быть повреждён/неподдерживаемая версия
        log.error("Не удалось разобрать %s: %s", path, exc)
        return

    matches = []
    if toon_handles:
        matches = [p for p in info["players"] if p["toon_handle"] in toon_handles]
    if not matches:
        matches = [p for p in info["players"] if p["name"].strip().lower() in battletags_lower]

    if not matches:
        all_players = [
            {"name": p["name"], "toon_handle": p["toon_handle"]} for p in info["players"]
        ]
        log.warning(
            "Новый реплей (%s, карта: %s) — ни один из отслеживаемых BattleTag/toon_handle не найден.\n"
            "  Игроки этого матча (name можно скопировать в 'battletags', toon_handle - в 'toon_handles'):\n"
            "  %s",
            path,
            info["map"],
            json.dumps(all_players, ensure_ascii=False),
        )
        return

    results = {m["result"] for m in matches}
    if len(results) > 1:
        log.warning(
            "В реплее %s отслеживаемые аккаунты оказались с разными результатами: %s",
            path,
            [(m["name"], m["result"]) for m in matches],
        )
        return

    primary = matches[0]

    for m in matches:
        record_known_player(config.get("known_players_file"), m["name"], m["toon_handle"], info["map"])
        acc = stats["by_account"].setdefault(m["name"], {"wins": 0, "losses": 0, "unknown": 0})
        if m["result"] == RESULT_WIN:
            acc["wins"] += 1
        elif m["result"] == RESULT_LOSS:
            acc["losses"] += 1
        else:
            acc["unknown"] += 1

    if hero_dict is not None and order_store is not None and bot is not None:
        played_heroes = {}
        for m in matches:
            hero = hero_dict.find_by_replay_hero(m["hero"])
            if hero is not None:
                played_heroes[hero["replay_hero"]] = hero
        for hero in played_heroes.values():
            for order in order_store.mark_fulfilled(hero):
                bot.send_message(
                    "Заказ героя {0} от @{1} выполнен".format(hero["display_name"], order.mention)
                )

    if primary["result"] == RESULT_WIN:
        stats["wins"] += 1
        outcome = "ПОБЕДА"
    elif primary["result"] == RESULT_LOSS:
        stats["losses"] += 1
        outcome = "ПОРАЖЕНИЕ"
    else:
        outcome = "результат неизвестен (m_result=%s), не учитываю в счёте" % primary["result"]

    log.info(
        "Новый реплей: карта=%s, аккаунт=%s, герой=%s (локализованно: %s) -> %s | Сессия: Победы=%d Поражения=%d",
        info["map"],
        primary["name"],
        primary["hero"],
        primary["hero_localized"],
        outcome,
        stats["wins"],
        stats["losses"],
    )

    write_obs_file(config, stats)
    if config.get("match_results_enabled") and bot is not None and primary["result"] in (RESULT_WIN, RESULT_LOSS):
        map_name = info["map"]
        translated = (map_names or {}).get(map_name)
        try:
            message = config["match_results_format"].format(
                result="Победа" if primary["result"] == RESULT_WIN else "Поражение",
                map=translated or map_name or "Неизвестная карта",
                wins=stats["wins"], losses=stats["losses"],
            )
            bot.send_message(message)
        except (KeyError, ValueError, IndexError):
            log.exception("Некорректный шаблон match_results_format")


# ---------------------------------------------------------------------------
# Заказ героя за баллы канала Twitch
# ---------------------------------------------------------------------------

QUEUE_MESSAGE_MAX_LEN = 450  # лимит сообщения в чате Twitch - 500 символов


def format_queue_message(pending):
    if not pending:
        return "Очередь заказов пуста"
    text = "Очередь заказов: "
    for i, order in enumerate(pending):
        item = "{0}. {1} (@{2})".format(i + 1, order.display_name, order.mention)
        if i > 0 and len(text) + len(item) + 2 > QUEUE_MESSAGE_MAX_LEN:
            return text + " … и ещё {0}".format(len(pending) - i)
        text += (", " if i > 0 else "") + item
    return text


def setup_chat(config, config_path):
    if not (config.get("orders_enabled") or config.get("match_results_enabled")):
        return None
    required = ("twitch_channel", "twitch_bot_username", "twitch_client_id",
                "twitch_client_secret", "twitch_refresh_token")
    missing = [key for key in required if not config.get(key)]
    if missing:
        log.error("Чат отключён: не заполнены поля %s", missing)
        return None
    token_manager = twitch_token_manager.TokenManager(
        config["twitch_client_id"], config["twitch_client_secret"], config["twitch_refresh_token"],
        on_refresh_token_changed=lambda token: persist_config_value(config_path, "twitch_refresh_token", token),
    )
    bot = twitch_chat.TwitchChatBot(
        config["twitch_channel"], config["twitch_bot_username"], token_manager,
        lambda login, mention, text: None,
    )
    bot.start()
    return bot


def setup_hero_orders(config, config_path, bot):
    """
    Настраивает заказы поверх уже запущенного чат-бота.
    При отключении или ошибке возвращает (None, None, None),
    не останавливая независимые уведомления о матчах.
    """
    if not config.get("orders_enabled") or bot is None:
        return None, None, None

    required = (
        "twitch_channel",
        "twitch_bot_username",
        "twitch_client_id",
        "twitch_client_secret",
        "twitch_refresh_token",
        "twitch_broadcaster_refresh_token",
    )
    missing = [k for k in required if not config.get(k)]
    if missing:
        log.error(
            "orders_enabled=true, но не заполнены поля конфига %s (см. python twitch_auth_setup.py и "
            "python twitch_auth_setup.py broadcaster) — заказы героев отключены",
            missing,
        )
        return None, None, None

    try:
        hero_list = heroes.load_heroes(config["orders_heroes_file"])
    except (OSError, ValueError) as exc:
        log.error("Не удалось загрузить %s: %s — заказы героев отключены", config["orders_heroes_file"], exc)
        return None, None, None
    hero_dict = heroes.HeroDict(hero_list)

    order_store = hero_orders.OrderStore(config["orders_storage_file"])
    try:
        order_store.load()
    except (OSError, ValueError, KeyError) as exc:
        log.error(
            "Не удалось прочитать файл заказов %s: %s — заказы героев отключены (исправьте или удалите файл)",
            config["orders_storage_file"],
            exc,
        )
        return None, None, None

    client_id = config["twitch_client_id"]

    broadcaster_token_manager = twitch_token_manager.TokenManager(
        client_id,
        config["twitch_client_secret"],
        config["twitch_broadcaster_refresh_token"],
        on_refresh_token_changed=lambda new_token: persist_config_value(
            config_path, "twitch_broadcaster_refresh_token", new_token
        ),
    )

    broadcaster = twitch_helix.get_token_user(client_id, broadcaster_token_manager)
    if not broadcaster:
        log.error("Не удалось проверить twitch_broadcaster_refresh_token — заказы героев отключены")
        return None, None, None
    if broadcaster["login"].casefold() != config["twitch_channel"].strip().lstrip("#").casefold():
        log.error(
            "twitch_broadcaster_refresh_token выдан аккаунту %r, а не каналу %r (авторизуйтесь под аккаунтом "
            "канала: python twitch_auth_setup.py broadcaster) — заказы героев отключены",
            broadcaster["login"],
            config["twitch_channel"],
        )
        return None, None, None

    queue_command = config.get("orders_queue_command", "!очередь").strip().casefold()
    heroes_link_command = config.get("heroes_link_command", "!герои").strip().casefold()
    heroes_link_url = config.get("heroes_link_url", "")

    def on_message(login, mention, text):
        text_cf = text.strip().casefold()

        if heroes_link_command and text_cf == heroes_link_command:
            bot.send_message(heroes_link_url)
        elif queue_command and text_cf == queue_command:
            bot.send_message(format_queue_message(order_store.list_pending()))


    orders = channel_points.ChannelPointsOrders(
        client_id,
        broadcaster_token_manager,
        broadcaster["id"],
        order_store,
        hero_dict,
        bot,
        reward_settings={
            "title": config["orders_reward_title"],
            "cost": int(config["orders_reward_cost"]),
            "prompt": config["orders_reward_prompt"],
            "is_user_input_required": True,
            "is_enabled": True,
        },
        poll_interval=max(2, int(config.get("orders_poll_interval_seconds", 5))),
    )
    if not orders.ensure_reward():
        log.error("Награда за баллы канала не настроена — заказы героев отключены")
        return None, None, None

    bot.on_message = on_message
    orders.start()

    log.info(
        "Заказ героя за баллы канала включён: награда %r, стоимость %d, очередь - команда %r, заказы хранятся в %s",
        config["orders_reward_title"],
        int(config["orders_reward_cost"]),
        config.get("orders_queue_command", "!очередь"),
        config["orders_storage_file"],
    )
    if heroes_link_command:
        log.info("Команда %r (ссылка на список героев) доступна всем зрителям", heroes_link_command)

    return hero_dict, order_store, bot


# ---------------------------------------------------------------------------
# Основной цикл наблюдения
# ---------------------------------------------------------------------------

def scan_folder(folder):
    try:
        return [
            os.path.join(folder, name)
            for name in os.listdir(folder)
            if name.lower().endswith(".stormreplay")
        ]
    except OSError as exc:
        log.warning("Не удалось прочитать папку %s: %s", folder, exc)
        return []


def run(config_path):
    config = load_config(config_path)

    battletags_lower = {bt.strip().lower() for bt in config.get("battletags", []) if bt.strip()}
    toon_handles = {t.strip() for t in config.get("toon_handles", []) if t.strip()}
    if not battletags_lower and not toon_handles:
        log.error(
            "В конфиге не указано ни одного battletag/toon_handle — нечего отслеживать. "
            "Заполните 'battletags' в %s",
            config_path,
        )
        return

    folders = resolve_watch_folders(config)
    if not folders:
        log.error(
            "Не найдено ни одной папки с реплеями. Заполните 'replay_folders' и/или "
            "'auto_discover_accounts_root' в %s",
            config_path,
        )
        return

    poll_interval = max(3, int(config.get("poll_interval_seconds", 20)))

    map_names = {}
    maps_path = Path(config_path).resolve().parent / config["maps_file"]
    try:
        with open(maps_path, encoding="utf-8") as f:
            map_names = json.load(f)
        if not isinstance(map_names, dict) or any(not isinstance(v, str) for v in map_names.values()):
            raise ValueError("Ожидается словарь названий карт со строковыми значениями")
    except (OSError, ValueError) as exc:
        log.warning("Не удалось загрузить словарь карт %s: %s", maps_path, exc)
        map_names = {}
    bot = setup_chat(config, config_path)
    hero_dict, order_store, _ = setup_hero_orders(config, config_path, bot)

    stats = {"wins": 0, "losses": 0, "by_account": {}}
    seen = {}
    pending = {}

    log.info("Отслеживаемые BattleTag: %s", ", ".join(sorted(battletags_lower)))
    if toon_handles:
        log.info("Отслеживаемые toon_handle (в приоритете над BattleTag): %s", ", ".join(sorted(toon_handles)))
    log.info("Папки с реплеями (%d):", len(folders))
    for f in folders:
        log.info("  - %s", f)

    # Первичное сканирование: все уже существующие файлы считаем "старыми"
    # и не учитываем в статистике сессии - нас интересуют только новые матчи,
    # сыгранные после запуска утилиты.
    initial_count = 0
    for folder in folders:
        for path in scan_folder(folder):
            try:
                seen[path] = os.path.getsize(path)
                initial_count += 1
            except OSError:
                pass
    log.info("Найдено %d существующих реплеев (пропускаются). Жду новых...", initial_count)

    write_obs_file(config, stats)

    try:
        while True:
            time.sleep(poll_interval)
            for folder in folders:
                for path in scan_folder(folder):
                    if path in seen:
                        continue
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue

                    if path in pending and pending[path] == size:
                        # размер не изменился со времени прошлой проверки -
                        # файл дозаписан игрой, можно разбирать
                        del pending[path]
                        process_file(path, config, battletags_lower, toon_handles, stats, hero_dict, order_store, bot, map_names)
                        seen[path] = size
                    else:
                        # файл только что появился или ещё дозаписывается -
                        # подождём следующего опроса
                        pending[path] = size
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
    finally:
        if bot is not None:
            bot.stop()
        log.info(
            "Итог сессии: Победы=%d Поражения=%d (по аккаунтам: %s)",
            stats["wins"],
            stats["losses"],
            stats["by_account"],
        )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    config_path = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent / "config.json")
    if not os.path.exists(config_path):
        log.error("Файл конфига не найден: %s\nСкопируйте config.example.json в config.json и заполните его.", config_path)
        sys.exit(1)
    run(config_path)


if __name__ == "__main__":
    main()

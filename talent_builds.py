"""Offline conversion of replay talent IDs to in-game build codes."""
import json
import logging
import threading
import time

from chat_messages import render

log = logging.getLogger("hots-stats-watcher")
LEVELS = (1, 4, 7, 10, 13, 16, 20)


def decode(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def collect_talents(events):
    chosen, final = {}, {}
    for event in events:
        name = decode(event.get("m_eventName"))
        if name not in ("TalentChosen", "EndOfGameTalentChoices"):
            continue
        ints = {decode(x["m_key"]): x["m_value"] for x in event.get("m_intData") or []}
        strings = {decode(x["m_key"]): decode(x["m_value"]) for x in event.get("m_stringData") or []}
        player_id = ints.get("PlayerID")
        if player_id is None:
            continue
        if name == "TalentChosen":
            if strings.get("PurchaseName"):
                chosen.setdefault(player_id, []).append(strings["PurchaseName"])
        else:
            final[player_id] = [strings.get("Tier %d Choice" % tier) or None for tier in range(1, 8)]
    return chosen, final


class BuildStore:
    def __init__(self, data_path, hero_dict=None, messages=None, catalog_loader=None):
        with open(data_path, encoding="utf-8") as stream:
            self.catalogs = json.load(stream)["builds"]
        self.hero_dict = hero_dict
        self.messages = messages
        self._snapshot = None
        self._lock = threading.Lock()
        self._catalog_loader = catalog_loader
        self._pending_match = None
        self._refreshing = False
        self._retry_after = {}

    def make_code(self, player, base_build):
        # Never silently apply talent positions from a different patch.
        catalog = self.catalogs.get(str(base_build))
        if catalog is None:
            raise ValueError("Нет справочника для версии %s" % base_build)
        hero = catalog.get(player["hero"])
        if hero is None:
            raise ValueError("Неизвестный герой %s" % player["hero"])
        final = player.get("talents_final")
        chosen = player.get("talents_chosen", [])
        if final is None and not chosen:
            raise ValueError("В реплее нет данных о талантах")
        digits = ["0"] * 7
        for index, talent_id in enumerate(final if final is not None else chosen):
            if not talent_id:
                continue
            talent = hero["talents"].get(talent_id)
            if talent is None:
                raise ValueError("Неизвестный талант %s" % talent_id)
            level, position = talent
            slot = LEVELS.index(level)
            if final is not None and index != slot:
                raise ValueError("Ярус таланта не совпадает со справочником")
            if not 1 <= position <= 9:
                raise ValueError("Некорректная позиция таланта")
            digits[slot] = str(position)
        return "[T%s,%s]" % ("".join(digits), hero["hyperlink_id"])

    def update_match(self, players, primary, base_build):
        with self._lock:
            build = str(base_build)
            self._pending_match = None
            if build not in self.catalogs:
                log.warning("Нет справочника талантов для версии %s", build)
                players = tuple(players)
                self._pending_match = (players, primary, build)
            self._set_snapshot(players, primary, build)
            self._start_refresh()

    def _start_refresh(self):
        if not self._catalog_loader or not self._pending_match or self._refreshing:
            return
        build = self._pending_match[2]
        if time.monotonic() < self._retry_after.get(build, 0):
            return
        self._refreshing = True
        threading.Thread(target=self._refresh_catalog, args=(build,), daemon=True).start()

    def _refresh_catalog(self, build):
        catalog = None
        try:
            catalog = self._catalog_loader(build)
        except Exception as exc:
            log.warning("Не удалось обновить справочник талантов для версии %s: %s", build, exc)
        with self._lock:
            self._refreshing = False
            if catalog:
                self.catalogs[build] = catalog
                log.info("Загружен справочник талантов для версии %s", build)
            else:
                self._retry_after[build] = time.monotonic() + 300
            if self._pending_match and self._pending_match[2] in self.catalogs:
                self._set_snapshot(*self._pending_match)
                self._pending_match = None
            self._start_refresh()

    def _set_snapshot(self, players, primary, base_build):
        entries = []
        primary_code = None
        for player in players:
            code = None
            if base_build in self.catalogs:
                try:
                    code = self.make_code(player, base_build)
                except (ValueError, KeyError) as exc:
                    log.warning("Билд героя %s недоступен: %s", player.get("hero"), exc)
            hero = self.hero_dict.find_by_replay_hero(player["hero"]) if self.hero_dict else None
            entries.append((hero["replay_hero"] if hero else player["hero"], code))
            if player is primary:
                primary_code = code
        self._snapshot = (primary_code, tuple(entries))

    def handle_message(self, bot, command, text):
        command = command.strip().casefold()
        text = text.strip()
        if not command or not text.casefold().startswith(command):
            return False
        argument = text[len(command):]
        if argument and not argument[0].isspace():
            return False
        argument = argument.strip()
        hero = None
        if argument:
            hero = self.hero_dict.resolve(argument) if self.hero_dict else None
            if hero is None:
                bot.send_message(render(self.messages, "build.unknown_hero", input=argument))
                return True
        with self._lock:
            self._start_refresh()
            snapshot = self._snapshot
        if snapshot is None:
            message = render(self.messages, "build.no_match")
        elif hero is None:
            message = (render(self.messages, "build.success", build_code=snapshot[0])
                       if snapshot[0] else render(self.messages, "build.own_unavailable"))
        else:
            matches = [code for hero_id, code in snapshot[1] if hero_id == hero["replay_hero"]]
            name = hero["display_name"]
            if not matches:
                message = render(self.messages, "build.hero_absent", hero=name)
            elif len(matches) > 1:
                message = render(self.messages, "build.hero_ambiguous", hero=name)
            else:
                message = (render(self.messages, "build.success", build_code=matches[0])
                           if matches[0] else render(self.messages, "build.hero_unavailable", hero=name))
        bot.send_message(message)
        return True

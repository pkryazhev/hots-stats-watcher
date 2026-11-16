"""Offline conversion of replay talent IDs to in-game build codes."""
import json
import logging
import threading

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
    def __init__(self, data_path):
        with open(data_path, encoding="utf-8") as stream:
            self.catalogs = json.load(stream)["builds"]
        self._code = None
        self._lock = threading.Lock()

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

    def update(self, player, base_build):
        try:
            code = self.make_code(player, base_build)
        except (ValueError, KeyError) as exc:
            log.warning("Билд последнего матча недоступен: %s", exc)
            code = None
        with self._lock:
            self._code = code

    def handle_message(self, bot, command, text):
        if command and text.strip().casefold() == command.strip().casefold():
            with self._lock:
                code = self._code
            # Until a replay is available, keep the response code-only.
            if code:
                bot.send_message(code)
            return True
        return False

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Словарь героев для фичи "заказ героя".

Каждая запись heroes.json связывает несколько вещей:
  - replay_hero        - внутреннее (движковое) имя героя из
                          replay.tracker.events (см. лог "герой=%s" в
                          process_file из watch_replays.py - это значение, а
                          не то, что в скобках "локализованно"). Оно НЕ
                          зависит от языка клиента игры, которым записан
                          реплей, поэтому одно и то же значение подходит для
                          любого языка клиента.
  - replay_hero_aliases - необязательный список других значений, которые
                          тоже означают этого героя (например, если движковое
                          имя менялось между патчами при переработке героя, а
                          старые реплеи ещё могут встретиться). Учитываются
                          наравне с replay_hero и при определении выполнения
                          заказа, и как алиасы для чата.
  - display_name        - как называть героя в сообщениях бота.
  - aliases             - варианты, которые может написать зритель в чате
                          (регистр и "ё"/"е" не важны).
  - orderable           - можно ли сейчас заказывать этого героя.
"""

import json
import logging

log = logging.getLogger("hots-stats-watcher")

REQUIRED_FIELDS = ("replay_hero", "display_name", "aliases", "orderable")


def normalize_text(s):
    if s is None:
        return ""
    return " ".join(s.strip().casefold().replace("ё", "е").split())


def load_heroes(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    heroes = []
    for entry in raw:
        missing = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing:
            log.warning("В %s пропущена запись без полей %s: %s", path, missing, entry)
            continue
        heroes.append(entry)
    return heroes


class HeroDict:
    def __init__(self, heroes):
        self.by_replay_hero = {}
        for h in heroes:
            for value in [h["replay_hero"]] + list(h.get("replay_hero_aliases", [])):
                self.by_replay_hero[value] = h

        self.by_alias = {}
        for h in heroes:
            names = (
                list(h.get("aliases", []))
                + list(h.get("replay_hero_aliases", []))
                + [h["display_name"], h["replay_hero"]]
            )
            for name in names:
                key = normalize_text(name)
                if not key:
                    continue
                if key in self.by_alias and self.by_alias[key] is not h:
                    log.warning(
                        "Alias %r одновременно указывает на %s и %s - используется первый",
                        key,
                        self.by_alias[key]["display_name"],
                        h["display_name"],
                    )
                    continue
                self.by_alias[key] = h

    def resolve(self, user_text):
        """Ищет героя по тексту из чата. Возвращает dict героя или None."""
        return self.by_alias.get(normalize_text(user_text))

    def find_by_replay_hero(self, replay_hero):
        """Ищет героя по значению m_hero из реплея. Возвращает dict героя или None."""
        return self.by_replay_hero.get(replay_hero)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Хранилище заказов героев за баллы канала.

Заказы живут в JSON-файле (orders_storage_file, по умолчанию orders.json) и
переживают перезапуск утилиты. Каждый заказ привязан к активации награды
Twitch (redemption_id):

  - "pending"   - заказ принят и ждёт матча на этом герое (активация на
                  Twitch остаётся в статусе UNFULFILLED);
  - "fulfilled" - герой сыгран, но Twitch ещё не подтвердил перевод
                  активации в FULFILLED. Как только подтвердит
                  (channel_points.ChannelPointsOrders), заказ удаляется из
                  файла - так файл не растёт бесконечно.

Кроме заказов, в файле хранится reward_id созданной утилитой награды.

Файл пишется атомарно (через .tmp + os.replace), чтобы обрыв посреди записи
не оставил его битым. Если файл всё-таки не удаётся прочитать, load() бросает
исключение, и заказы не включаются - лучше так, чем молча перезаписать
чужую очередь пустой.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger("hots-stats-watcher")

STATUS_PENDING = "pending"
STATUS_FULFILLED = "fulfilled"


class Order:
    def __init__(self, redemption_id, reward_id, username, mention, replay_hero, display_name,
                 redeemed_at, status=STATUS_PENDING):
        self.redemption_id = redemption_id
        self.reward_id = reward_id
        self.username = username
        self.mention = mention
        self.replay_hero = replay_hero
        self.display_name = display_name
        self.redeemed_at = redeemed_at
        self.status = status

    def to_dict(self):
        return {
            "redemption_id": self.redemption_id,
            "reward_id": self.reward_id,
            "username": self.username,
            "mention": self.mention,
            "replay_hero": self.replay_hero,
            "display_name": self.display_name,
            "redeemed_at": self.redeemed_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            d["redemption_id"],
            d.get("reward_id"),
            d.get("username", ""),
            d.get("mention") or d.get("username", ""),
            d["replay_hero"],
            d.get("display_name") or d["replay_hero"],
            d.get("redeemed_at", ""),
            d.get("status", STATUS_PENDING),
        )


class OrderStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._orders = []
        self._reward_id = None

    def load(self):
        if not os.path.exists(self.path):
            log.info("Файл заказов %s ещё не создан - начинаю с пустой очереди", self.path)
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with self._lock:
            self._reward_id = data.get("reward_id")
            self._orders = [Order.from_dict(d) for d in data.get("orders", [])]
        log.info(
            "Загружено заказов из %s: %d в очереди",
            self.path,
            len([o for o in self._orders if o.status == STATUS_PENDING]),
        )

    def _save_locked(self):
        data = {
            "reward_id": self._reward_id,
            "orders": [o.to_dict() for o in self._orders],
        }
        tmp_path = self.path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except OSError as exc:
            log.warning("Не удалось сохранить заказы в %s: %s", self.path, exc)

    @property
    def reward_id(self):
        with self._lock:
            return self._reward_id

    def set_reward_id(self, reward_id):
        with self._lock:
            if self._reward_id == reward_id:
                return
            self._reward_id = reward_id
            self._save_locked()

    def is_known(self, redemption_id):
        with self._lock:
            return any(o.redemption_id == redemption_id for o in self._orders)

    def add_pending(self, redemption_id, reward_id, username, mention, hero, redeemed_at=None):
        """hero - dict героя из heroes.HeroDict (уже распознанный и orderable)."""
        order = Order(
            redemption_id,
            reward_id,
            username.strip().casefold(),
            mention,
            hero["replay_hero"],
            hero["display_name"],
            redeemed_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock:
            self._orders.append(order)
            self._save_locked()
        return order

    def list_pending(self):
        """Невыполненные заказы в порядке поступления - для команды !очередь."""
        with self._lock:
            return [o for o in self._orders if o.status == STATUS_PENDING]

    def list_fulfilled(self):
        """Выполненные заказы, которые ещё нужно подтвердить на Twitch."""
        with self._lock:
            return [o for o in self._orders if o.status == STATUS_FULFILLED]

    def mark_fulfilled(self, hero):
        """
        hero - dict героя (из heroes.HeroDict.find_by_replay_hero), сыгранного
        в только что обработанном реплее. Возвращает список Order, закрытых
        этим результатом: один матч закрывает не больше одного заказа на
        героя - самый ранний невыполненный. Если героя заказали несколько
        раз, остальные заказы ждут следующих матчей на нём.
        """
        if hero is None:
            return []
        replay_heroes = {hero["replay_hero"]} | set(hero.get("replay_hero_aliases", []))
        with self._lock:
            for order in self._orders:
                if order.status == STATUS_PENDING and order.replay_hero in replay_heroes:
                    order.status = STATUS_FULFILLED
                    self._save_locked()
                    return [order]
            return []

    def remove(self, redemption_ids):
        ids = set(redemption_ids)
        if not ids:
            return
        with self._lock:
            self._orders = [o for o in self._orders if o.redemption_id not in ids]
            self._save_locked()

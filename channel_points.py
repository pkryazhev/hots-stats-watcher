#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Заказ героя за баллы канала.

При старте утилита сама создаёт на канале награду (или находит созданную
ранее и обновляет её название/стоимость/подсказку из конфига) - управлять
активациями через API Twitch разрешает только для наград, созданных тем же
client_id, поэтому награду, заведённую руками в панели Twitch, использовать
не получится.

Дальше в отдельном потоке раз в orders_poll_interval_seconds:
  1. выполненные заказы (hero_orders.STATUS_FULFILLED) подтверждаются на
     Twitch (активация -> FULFILLED) и удаляются из файла заказов;
  2. читаются новые активации в статусе UNFULFILLED. Для каждой:
       - герой не распознан или недоступен для заказа -> активация
         отменяется (CANCELED), баллы возвращаются зрителю;
       - иначе заказ сохраняется в очередь, а активация остаётся
         UNFULFILLED до матча на этом герое;
  3. заказы из очереди, чьих активаций больше нет среди UNFULFILLED
     (стример обработал их вручную на Twitch), убираются из очереди.

Активации, сделанные пока утилита не запущена, никуда не деваются - они
ждут в статусе UNFULFILLED и обрабатываются при следующем запуске.
"""

import logging
import threading
import time

import twitch_helix

log = logging.getLogger("hots-stats-watcher")


class ChannelPointsOrders:
    def __init__(self, client_id, token_manager, broadcaster_id, store, hero_dict, bot,
                 reward_settings, poll_interval):
        self.client_id = client_id
        self.token_manager = token_manager
        self.broadcaster_id = broadcaster_id
        self.store = store
        self.hero_dict = hero_dict
        self.bot = bot
        self.reward_settings = reward_settings
        self.poll_interval = poll_interval
        # Активации, которые мы уже пытались отменить: если отмена не прошла,
        # не хотим спамить в чат отказом на каждом опросе.
        self._rejected_ids = set()

    def ensure_reward(self):
        """Находит/создаёт награду и сохраняет её id в хранилище. True при успехе."""
        rewards = twitch_helix.list_manageable_rewards(self.broadcaster_id, self.client_id, self.token_manager)
        if rewards is None:
            log.error("Не удалось получить список наград канала (нет прав channel:manage:redemptions?)")
            return False

        title_cf = self.reward_settings["title"].strip().casefold()
        reward = next((r for r in rewards if r["id"] == self.store.reward_id), None)
        if reward is None:
            reward = next((r for r in rewards if r["title"].strip().casefold() == title_cf), None)

        if reward is None:
            reward = twitch_helix.create_custom_reward(
                self.broadcaster_id, self.reward_settings, self.client_id, self.token_manager
            )
            if reward is None:
                log.error(
                    "Не удалось создать награду %r. Частые причины: у канала нет статуса "
                    "компаньона/партнёра, или награда с таким названием уже заведена вручную "
                    "в панели Twitch (переименуйте/удалите её или смените orders_reward_title)",
                    self.reward_settings["title"],
                )
                return False
            log.info("Создана награда за баллы канала %r (стоимость %d)", reward["title"], reward["cost"])
        else:
            updated = twitch_helix.update_custom_reward(
                self.broadcaster_id, reward["id"], self.reward_settings, self.client_id, self.token_manager
            )
            if updated is None:
                log.warning("Не удалось обновить настройки награды %r - работаю с текущими", reward["title"])
            else:
                reward = updated
            log.info("Использую награду за баллы канала %r (стоимость %d)", reward["title"], reward["cost"])

        self.store.set_reward_id(reward["id"])
        return True

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="channel-points-poll").start()

    def _loop(self):
        while True:
            try:
                self._sync_fulfilled()
                self._poll_new()
            except Exception:  # noqa: BLE001 - ошибка одного опроса не должна останавливать заказы
                log.exception("Ошибка при опросе наград за баллы канала")
            time.sleep(self.poll_interval)

    def _sync_fulfilled(self):
        by_reward = {}
        for order in self.store.list_fulfilled():
            by_reward.setdefault(order.reward_id or self.store.reward_id, []).append(order.redemption_id)
        for reward_id, ids in by_reward.items():
            ok = twitch_helix.update_redemptions_status(
                self.broadcaster_id, reward_id, ids, twitch_helix.REDEMPTION_FULFILLED,
                self.client_id, self.token_manager,
            )
            if ok:
                self.store.remove(ids)

    def _poll_new(self):
        reward_id = self.store.reward_id
        redemptions = twitch_helix.get_unfulfilled_redemptions(
            self.broadcaster_id, reward_id, self.client_id, self.token_manager
        )
        if redemptions is None:
            return

        for r in redemptions:
            if r["id"] in self._rejected_ids or self.store.is_known(r["id"]):
                continue
            self._handle_redemption(reward_id, r)

        unfulfilled_ids = {r["id"] for r in redemptions}
        gone = [
            o for o in self.store.list_pending()
            if o.reward_id == reward_id and o.redemption_id not in unfulfilled_ids
        ]
        if gone:
            for o in gone:
                log.info(
                    "Заказ %s от %s обработан вручную на Twitch - убираю из очереди", o.display_name, o.mention
                )
            self.store.remove(o.redemption_id for o in gone)

    def _handle_redemption(self, reward_id, r):
        login = r.get("user_login", "")
        mention = r.get("user_name") or login
        hero_text = r.get("user_input", "")

        hero = self.hero_dict.resolve(hero_text)
        if hero is None:
            log.info("Заказ от %s: герой %r не распознан - возвращаю баллы", mention, hero_text)
            self._reject(reward_id, r, "@{0} SHTO попробуй еще раз".format(mention))
            return
        if not hero.get("orderable", True):
            log.info("Заказ от %s: герой %s недоступен для заказа - возвращаю баллы", mention, hero["display_name"])
            self._reject(reward_id, r, "@{0} заказ данного героя пока недоступен uvi".format(mention))
            return

        self.store.add_pending(r["id"], reward_id, login, mention, hero, r.get("redeemed_at"))
        log.info("Принят заказ героя %s от %s", hero["display_name"], mention)
        self.bot.send_message("@{0} заказ героя {1} принят NOTED".format(mention, hero["display_name"]))

    def _reject(self, reward_id, r, message):
        self._rejected_ids.add(r["id"])
        ok = twitch_helix.update_redemptions_status(
            self.broadcaster_id, reward_id, [r["id"]], twitch_helix.REDEMPTION_CANCELED,
            self.client_id, self.token_manager,
        )
        if ok:
            message += " (баллы возвращены)"
        else:
            log.error(
                "Не удалось вернуть баллы зрителю %s (активация %s) - верните их вручную в очереди наград Twitch",
                r.get("user_name"), r["id"],
            )
        self.bot.send_message(message)

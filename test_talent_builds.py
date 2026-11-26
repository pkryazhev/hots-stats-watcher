import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import talent_builds
import watch_replays as watcher
from heroes import HeroDict


class TalentBuildTests(unittest.TestCase):
    def setUp(self):
        self.heroes = HeroDict([
            dict(replay_hero="HeroDemonHunter", replay_hero_aliases=["OldValla"],
                 display_name="Валла", aliases=["valla", "валя"], orderable=False),
            dict(replay_hero="HeroRaynor", display_name="Рейнор", aliases=["raynor"], orderable=True),
            dict(replay_hero="HeroLostVikings", display_name="Потерявшиеся викинги",
                 aliases=["трёх героев"], orderable=True),
        ])
        self.store = talent_builds.BuildStore(Path(__file__).with_name("talent_data.json"), self.heroes)

    def reply(self, text):
        bot = Mock()
        handled = self.store.handle_message(bot, "!билд", text)
        return handled, bot.send_message.call_args.args[0] if bot.send_message.called else None

    def player(self, hero="HeroDemonHunter"):
        return {"hero": hero, "talents_final": [None] * 7}

    def test_valla_identifier_and_missing_tiers(self):
        player = {"hero": "HeroDemonHunter", "talents_final": [
            "DemonHunterMasteryPuncturingArrow", None, None, None, None, None, None]}
        self.assertEqual(self.store.make_code(player, 98348), "[T1000000,Valla]")

    def test_final_choices_override_purchase_history(self):
        def event(name, strings):
            return {"m_eventName": name, "m_intData": [{"m_key": b"PlayerID", "m_value": 5}],
                    "m_stringData": [{"m_key": k, "m_value": v} for k, v in strings.items()]}
        chosen, final = talent_builds.collect_talents([
            event(b"TalentChosen", {b"PurchaseName": b"UnknownOldChoice"}),
            event(b"EndOfGameTalentChoices", {b"Tier 1 Choice": b"DemonHunterMasteryPuncturingArrow"})])
        player = {"hero": "HeroDemonHunter", "talents_chosen": chosen[5], "talents_final": final[5]}
        self.assertEqual(self.store.make_code(player, 98348), "[T1000000,Valla]")

    def test_fallback_uses_talent_tier_not_purchase_order(self):
        hero = self.store.catalogs["98348"]["HeroDemonHunter"]
        later = next(t for t, (level, pos) in hero["talents"].items() if level == 4 and pos == 2)
        player = {"hero": "HeroDemonHunter", "talents_chosen": [
            later, "DemonHunterMasteryPuncturingArrow"]}
        self.assertEqual(self.store.make_code(player, 98348), "[T1200000,Valla]")

    def test_unsupported_new_match_clears_previous_code(self):
        player = {"hero": "HeroDemonHunter", "talents_final": [None] * 7}
        self.store.update_match([player], player, 98348)
        self.store.update_match([player], player, 999999)
        bot = Mock()
        self.store.handle_message(bot, "!build", "!build")
        bot.send_message.assert_called_once_with("Не удалось получить ваш билд для последнего матча.")

    def test_background_catalog_updates_current_match(self):
        player = self.player()
        catalog = self.store.catalogs["98348"]
        self.store._catalog_loader = Mock(return_value=catalog)
        with patch.object(talent_builds.threading, "Thread") as thread:
            self.store.update_match([player], player, 999999)
            self.assertIn("Не удалось", self.reply("!билд")[1])
            thread.assert_called_once()
            worker = thread.call_args.kwargs
        worker["target"](*worker["args"])
        self.store._catalog_loader.assert_called_once_with("999999")
        self.assertEqual(self.reply("!билд"), (True, "[T0000000,Valla]"))

    def test_unreadable_tracker_keeps_match_result(self):
        archive = Mock()
        archive.header = {"user_data_header": {"content": b"header"}}
        protocol = Mock()
        protocol.decode_replay_details.return_value = {
            "m_playerList": [{"m_name": b"Player", "m_hero": b"Abathur", "m_result": 1}],
        }
        protocol.decode_replay_tracker_events.side_effect = watcher.CorruptedError("tracker")
        with patch.object(watcher.mpyq, "MPQArchive", return_value=archive), \
             patch.object(watcher.hv, "latest") as latest, \
             patch.object(watcher, "resolve_protocol", return_value=protocol):
            latest.return_value.decode_replay_header.return_value = {"m_version": {"m_baseBuild": 999999}}
            player, = watcher.parse_replay("match.StormReplay")["players"]
        self.assertEqual((player["hero"], player["result"], player["talents_final"]), ("Abathur", 1, None))

    def test_disabled_command_does_not_change_handler(self):
        bot = Mock()
        handler = bot.on_message
        config = dict(watcher.DEFAULT_CONFIG, build_command_enabled=False)
        self.assertIsNone(watcher.setup_build_command(config, __file__, bot))
        self.assertIs(bot.on_message, handler)

    def test_command_works_without_orders_and_sends_only_code(self):
        bot = Mock()
        config = dict(watcher.DEFAULT_CONFIG, build_command_enabled=True, build_command="!build",
                      orders_heroes_file="heroes.example.json")
        store = watcher.setup_build_command(config, __file__, bot)
        player = self.player()
        store.update_match([player], player, 98348)
        bot.on_message("viewer", "Viewer", " !BUILD ")
        bot.send_message.assert_called_once_with("[T0000000,Valla]")

    def test_named_hero_from_opposing_team_and_alias(self):
        primary, valla = self.player("HeroRaynor"), self.player()
        self.store.update_match([primary, valla], primary, 98348)
        self.assertEqual(self.reply(" !БИЛД\t ВаЛя  "), (True, "[T0000000,Valla]"))
        self.assertEqual(self.reply("!билд"), (True, "[T0000000,Raynor]"))

    def test_duplicate_heroes_even_if_one_build_unavailable(self):
        first, second = self.player(), self.player()
        second["talents_final"][0] = "UnknownTalent"
        self.store.update_match([first, second], first, 98348)
        self.assertEqual(self.reply("!билд Валла"),
                         (True, "В последнем матче было несколько героев Валла."))

    def test_missing_unknown_and_no_match(self):
        self.assertEqual(self.reply("!билд Валла"), (True, "Нет данных о последнем матче."))
        self.assertEqual(self.reply("!билд Опечатка"),
                         (True, "Неизвестный герой «Опечатка». Проверьте название."))
        player = self.player("HeroRaynor")
        self.store.update_match([player], player, 98348)
        self.assertEqual(self.reply("!билд Валла"),
                         (True, "В последнем матче не было героя Валла."))

    def test_failed_build_does_not_affect_other_players(self):
        primary, other = self.player("HeroRaynor"), self.player()
        other["talents_final"][0] = "UnknownTalent"
        self.store.update_match([primary, other], primary, 98348)
        self.assertEqual(self.reply("!билд"), (True, "[T0000000,Raynor]"))
        self.assertEqual(self.reply("!билд Валла"),
                         (True, "Не удалось получить билд героя Валла для этого матча."))

    def test_new_match_replaces_all_old_players(self):
        first, second = self.player(), self.player("HeroRaynor")
        self.store.update_match([first], first, 98348)
        self.store.update_match([second], second, 98348)
        self.assertEqual(self.reply("!билд Валла"),
                         (True, "В последнем матче не было героя Валла."))

    def test_multiword_alias_and_yo_normalization(self):
        player = self.player("HeroLostVikings")
        self.store.update_match([player], player, 98348)
        # Hero presence is recognized regardless of whether its build can be generated.
        self.assertEqual(self.reply("!билд   ТРЕХ   героев"), self.reply("!билд Потерявшиеся викинги"))

    def test_replay_alias_matches_canonical_hero(self):
        player = self.player("OldValla")
        with patch.object(self.store, "make_code", return_value="[T0000000,Valla]"):
            self.store.update_match([player], player, 98348)
        self.assertEqual(self.reply("!билд Валла"), (True, "[T0000000,Valla]"))

    def test_command_prefix_does_not_match_other_commands(self):
        self.assertEqual(self.reply("!билддругая Валла"), (False, None))

    def test_build_only_starts_chat(self):
        config = dict(watcher.DEFAULT_CONFIG, build_command_enabled=True,
                      orders_enabled=False, match_results_enabled=False)
        for key in ("twitch_channel", "twitch_bot_username", "twitch_client_id",
                    "twitch_client_secret", "twitch_refresh_token"):
            config[key] = "configured"
        with patch.object(watcher.twitch_chat, "TwitchChatBot"):
            self.assertIsNotNone(watcher.setup_chat(config, "config.json"))


if __name__ == "__main__":
    unittest.main()

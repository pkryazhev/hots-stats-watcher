import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import talent_builds
import watch_replays as watcher


class TalentBuildTests(unittest.TestCase):
    def setUp(self):
        self.store = talent_builds.BuildStore(Path(__file__).with_name("talent_data.json"))

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
        self.store.update(player, 98348)
        self.store.update(player, 999999)
        bot = Mock()
        self.store.handle_message(bot, "!build", "!build")
        bot.send_message.assert_not_called()

    def test_disabled_command_does_not_change_handler(self):
        bot = Mock()
        handler = bot.on_message
        config = dict(watcher.DEFAULT_CONFIG, build_command_enabled=False)
        self.assertIsNone(watcher.setup_build_command(config, __file__, bot))
        self.assertIs(bot.on_message, handler)

    def test_command_works_without_orders_and_sends_only_code(self):
        bot = Mock()
        config = dict(watcher.DEFAULT_CONFIG, build_command_enabled=True, build_command="!build")
        store = watcher.setup_build_command(config, __file__, bot)
        store.update({"hero": "HeroDemonHunter", "talents_final": [None] * 7}, 98348)
        bot.on_message("viewer", "Viewer", " !BUILD ")
        bot.send_message.assert_called_once_with("[T0000000,Valla]")

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

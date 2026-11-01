import unittest
from unittest.mock import Mock, patch

import watch_replays as watcher
from twitch_chat import TwitchChatBot


class MatchResultsTests(unittest.TestCase):
    def process(self, results, translations=None):
        config = dict(watcher.DEFAULT_CONFIG)
        stats = {"wins": 2, "losses": 1, "by_account": {}}
        bot = Mock()
        players = [dict(name="player", toon_handle="id", result=r,
                        hero="Hero", hero_localized="Hero") for r in results]
        with patch.object(watcher, "parse_replay", return_value={"map": "Map", "players": players}), \
                patch.object(watcher, "write_obs_file"), patch.object(watcher, "record_known_player"):
            watcher.process_file("test", config, {"player"}, set(), stats,
                                 bot=bot, map_names=translations)
        return stats, bot

    def test_win_with_orders_disabled(self):
        stats, bot = self.process([1], {"Map": "Карта"})
        self.assertEqual(stats["wins"], 3)
        bot.send_message.assert_called_once_with("Победа на карте «Карта». Счёт сессии: 3–1.")

    def test_loss_with_empty_translation(self):
        _, bot = self.process([2], {"Map": ""})
        bot.send_message.assert_called_once_with("Поражение на карте «Map». Счёт сессии: 2–2.")

    def test_unknown(self):
        stats, bot = self.process([0])
        self.assertEqual((stats["wins"], stats["losses"]), (2, 1))
        bot.send_message.assert_not_called()

    def test_ambiguous(self):
        stats, bot = self.process([1, 2])
        self.assertEqual(stats["by_account"], {})
        bot.send_message.assert_not_called()

    def test_chat_without_orders(self):
        config = dict(watcher.DEFAULT_CONFIG)
        for key in ("twitch_channel", "twitch_bot_username", "twitch_client_id",
                    "twitch_client_secret", "twitch_refresh_token"):
            config[key] = "configured"
        with patch.object(watcher.twitch_chat, "TwitchChatBot") as cls:
            bot = watcher.setup_chat(config, "config.json")
            bot.start.assert_called_once()
            self.assertEqual(watcher.setup_hero_orders(config, "config.json", bot), (None, None, None))

    def test_retry_preserves_message(self):
        bot = TwitchChatBot("channel", "bot", Mock(), Mock())
        bot.send_message("result")
        bot._ready.set()
        attempts = []

        def send(line):
            attempts.append(line)
            if len(attempts) == 2:
                bot.stop()
                return True
            return False

        with patch.object(bot, "_raw_send", side_effect=send):
            bot._send_loop()
        self.assertEqual(attempts, ["PRIVMSG #channel :result"] * 2)

    def test_socket_failure_clears_ready(self):
        bot = TwitchChatBot("channel", "bot", Mock(), Mock())
        self.assertFalse(bot._raw_send("test"))
        bot._sock = Mock()
        bot._sock.sendall.side_effect = OSError("disconnected")
        bot._ready.set()
        self.assertFalse(bot._raw_send("test"))
        self.assertFalse(bot._ready.is_set())


if __name__ == "__main__":
    unittest.main()

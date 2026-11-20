import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import watch_replays as watcher
from twitch_chat import TwitchChatBot


class MatchResultsTests(unittest.TestCase):
    def process(self, results, translations=None, result_names=None, messages=None):
        config = dict(watcher.DEFAULT_CONFIG)
        stats = {"wins": 2, "losses": 1, "by_account": {}}
        bot = Mock()
        players = [dict(name="player", toon_handle="id", result=r,
                        hero="Hero", hero_localized="Hero") for r in results]
        with patch.object(watcher, "parse_replay", return_value={"map": "Map", "players": players}), \
                patch.object(watcher, "write_obs_file"), patch.object(watcher, "record_known_player"):
            watcher.process_file("test", config, {"player"}, set(), stats,
                                 bot=bot, map_names=translations, result_names=result_names,
                                 messages=messages)
        return stats, bot

    def test_win_with_orders_disabled(self):
        stats, bot = self.process([1], {"Map": "Карта"})
        self.assertEqual(stats["wins"], 3)
        bot.send_message.assert_called_once_with("Победа на карте «Карта». Счёт: 3–1.")

    def test_loss_with_empty_translation(self):
        _, bot = self.process([2], {"Map": ""})
        bot.send_message.assert_called_once_with("Поражение на карте «Map». Счёт: 2–2.")

    def test_unknown(self):
        stats, bot = self.process([0])
        self.assertEqual((stats["wins"], stats["losses"]), (2, 1))
        bot.send_message.assert_not_called()

    def test_custom_result_names(self):
        names = {"win": "Победили!", "loss": "Проиграли"}
        for result, text in ((1, "Победили!"), (2, "Проиграли")):
            with self.subTest(result=result):
                stats, bot = self.process([result], result_names=names)
                bot.send_message.assert_called_once_with(
                    f"{text} на карте «Map». Счёт: {stats['wins']}–{stats['losses']}."
                )

    def test_chat_templates_override_legacy_result_labels(self):
        messages = {"match.result": "{wins}:{losses} — {result} ({map})", "match.win": "Ура"}
        _, bot = self.process([1], result_names={"win": "Старый текст"}, messages=messages)
        bot.send_message.assert_called_once_with("3:1 — Ура (Map)")

    def test_chat_templates_keep_valid_entries_when_one_is_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chat_messages.json"
            path.write_text(json.dumps({"queue": {"empty": "Пока пусто", "list": "{unknown}"}}),
                            encoding="utf-8")
            with self.assertLogs(watcher.log, "WARNING") as logs:
                messages = watcher.chat_messages.load(path)
            self.assertIn("queue.list", logs.output[0])
            self.assertEqual(watcher.format_queue_message([], messages), "Пока пусто")
            order = SimpleNamespace(display_name="Валла", mention="viewer")
            self.assertEqual(watcher.format_queue_message([order], messages),
                             "Очередь заказов: 1. Валла (@viewer)")

    def test_custom_queue_reserves_room_for_remaining_count(self):
        messages = {"queue.list": "x" * 430 + "{items}", "queue.item": "{hero}", "queue.more": " +{count}"}
        order = SimpleNamespace(display_name="abcdefghij", mention="viewer")
        response = watcher.format_queue_message([order] * 3, messages)
        self.assertEqual(response, "x" * 430 + "abcdefghij +2")

    def test_result_names_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "custom.json"
            config = dict(watcher.DEFAULT_CONFIG, results_file="custom.json")
            cases = [
                ({"win": "Ура", "loss": "Увы"}, {"win": "Ура", "loss": "Увы"}),
                ({"win": "Ура"}, {"win": "Ура", "loss": "Поражение"}),
                ({"win": "", "loss": "  "}, watcher.DEFAULT_RESULT_NAMES),
                ({"win": 1}, watcher.DEFAULT_RESULT_NAMES),
                ([], watcher.DEFAULT_RESULT_NAMES),
            ]
            for raw, expected in cases:
                with self.subTest(raw=raw):
                    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                    self.assertEqual(watcher.load_result_names(Path(folder) / "config.json", config), expected)
            path.write_text("{", encoding="utf-8")
            self.assertEqual(watcher.load_result_names(Path(folder) / "config.json", config), watcher.DEFAULT_RESULT_NAMES)
            path.unlink()
            self.assertEqual(watcher.load_result_names(Path(folder) / "config.json", config), watcher.DEFAULT_RESULT_NAMES)

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

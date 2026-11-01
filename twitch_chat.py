#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Простой IRC-клиент чата Twitch на стандартной библиотеке (socket + ssl) -
без внешних pip-пакетов, по тому же принципу, что и остальной проект.

Подключается к irc.chat.twitch.tv:6697, слушает PRIVMSG в отдельном потоке и
шлёт исходящие сообщения через очередь во втором потоке (с интервалом, чтобы
не словить rate-limit Twitch).
"""

import logging
import queue
import re
import socket
import ssl
import threading
import time

log = logging.getLogger("hots-stats-watcher")

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697

PRIVMSG_RE = re.compile(
    r"^(?:@(?P<tags>\S+) )?:(?P<nick>[^!]+)!\S+ PRIVMSG #\S+ :(?P<message>.*)$"
)


def _parse_tags(raw):
    tags = {}
    if not raw:
        return tags
    for pair in raw.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            tags[k] = v
    return tags


class TwitchChatBot:
    """
    on_message(login, mention, text) вызывается из читающего потока на
    каждое новое сообщение в чате канала. login - логин зрителя (нижний
    регистр), mention - display-name (может быть кириллическим), text -
    текст сообщения без изменений.
    """

    def __init__(self, channel, bot_username, token_manager, on_message):
        self.channel = channel.lower().lstrip("#")
        self.bot_username = bot_username
        self.token_manager = token_manager
        self.on_message = on_message

        self._send_queue = queue.Queue()
        self._stop = threading.Event()
        self._sock = None
        self._ready = threading.Event()
        self._write_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._read_loop, daemon=True, name="twitch-chat-read").start()
        threading.Thread(target=self._send_loop, daemon=True, name="twitch-chat-send").start()

    def stop(self):
        self._stop.set()

    def send_message(self, text):
        self._send_queue.put(str(text).replace("\r", " ").replace("\n", " "))

    def _connect(self):
        access_token = self.token_manager.get_access_token()

        raw_sock = socket.create_connection((IRC_HOST, IRC_PORT), timeout=15)
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(raw_sock, server_hostname=IRC_HOST)
        sock.sendall(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
        sock.sendall("PASS oauth:{0}\r\n".format(access_token).encode("utf-8"))
        sock.sendall("NICK {0}\r\n".format(self.bot_username.lower()).encode("utf-8"))
        sock.sendall("JOIN #{0}\r\n".format(self.channel).encode("utf-8"))
        sock.settimeout(60)
        log.info("Twitch-бот подключился к чату канала #%s", self.channel)
        return sock

    def _read_loop(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                self._sock = self._connect()
                backoff = 5
                buf = ""
                while not self._stop.is_set():
                    try:
                        chunk = self._sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise ConnectionError("Twitch IRC закрыл соединение")
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\r\n" in buf:
                        line, buf = buf.split("\r\n", 1)
                        self._handle_line(line)
            except Exception as exc:  # noqa: BLE001 - любой обрыв должен приводить к реконнекту, а не падению бота
                if self._stop.is_set():
                    break
                log.warning("Соединение с чатом Twitch прервано (%s), переподключение через %dс", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                self._ready.clear()
                if self._sock:
                    try:
                        self._sock.close()
                    except OSError:
                        pass

    def _handle_line(self, line):
        if not line:
            return
        if line.startswith("PING"):
            self._raw_send("PONG :tmi.twitch.tv")
            return
        if " 366 " in line:  # JOIN completed (end of channel member list)
            self._ready.set()

        m = PRIVMSG_RE.match(line)
        if not m:
            return
        tags = _parse_tags(m.group("tags"))
        login = m.group("nick")
        mention = tags.get("display-name") or login
        message = m.group("message")
        try:
            self.on_message(login, mention, message)
        except Exception:  # noqa: BLE001 - ошибка в одной команде не должна убивать читающий поток
            log.exception("Ошибка в обработчике сообщения чата: %r", message)

    def _raw_send(self, line):
        with self._write_lock:
            sock = self._sock
            if sock is None:
                return False
            try:
                sock.sendall((line + "\r\n").encode("utf-8"))
                return True
            except OSError as exc:
                log.warning("Не удалось отправить в чат Twitch: %s", exc)
                self._ready.clear()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return False

    def _send_loop(self):
        min_interval = 1.3
        last_sent = 0.0
        while not self._stop.is_set():
            try:
                text = self._send_queue.get(timeout=1)
            except queue.Empty:
                continue
            while not self._stop.is_set():
                if not self._ready.wait(timeout=1):
                    continue
                wait = max(0, min_interval - (time.monotonic() - last_sent))
                if self._stop.wait(wait):
                    return
                if self._raw_send("PRIVMSG #{0} :{1}".format(self.channel, text)):
                    last_sent = time.monotonic()
                    break
                self._stop.wait(1)

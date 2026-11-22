"""
Окно управления сборщиком блёсток.

Запуск:  python app.py   (или двойной клик по "Запуск.bat")
Откроется страница в вашем браузере: логин/пароль, Старт/Пауза/Стоп, статистика,
живая картинка из игры и лог. Сама игра крутится в отдельном фоновом браузере.

Сервер слушает только 127.0.0.1 (доступен лишь с этого ПК).
"""
import collections
import json
import os
import secrets
import socket
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sparkle_clicker as sc

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)   # popup.png, sessions_log.csv и т.п. - рядом со скриптом
CONFIG_FILE = os.path.join(HERE, "config.json")
KEYRING_SERVICE = "seaofprime-sparkle-bot"
TOKEN = secrets.token_urlsafe(16)   # защита от чужих сайтов, стучащихся на localhost

try:
    import keyring
except Exception:
    keyring = None


# ---------------- настройки и пароль ----------------
def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def saved_password(username):
    if keyring is None or not username:
        return ""
    try:
        return keyring.get_password(KEYRING_SERVICE, username) or ""
    except Exception:
        return ""


def store_password(username, password):
    if keyring is None or not username:
        return
    try:
        if password:
            keyring.set_password(KEYRING_SERVICE, username, password)
        else:
            keyring.delete_password(KEYRING_SERVICE, username)
    except Exception:
        pass


# ---------------- бот в отдельном потоке ----------------
class BotManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"      # idle | starting | running | paused | stopping | error
        self.message = ""
        self.thread = None
        self.io = None
        self._stop = threading.Event()
        self._paused = False
        self.logs = collections.deque(maxlen=400)
        self.log_seq = 0
        self.started_at = None
        sc.LOG_HOOK = self._on_log

    def _on_log(self, line):
        with self.lock:
            self.log_seq += 1
            self.logs.append((self.log_seq, line))

    # интерфейс управления для движка
    def stopped(self):
        return self._stop.is_set()

    def paused(self):
        return self._paused

    def start(self, username, password, headless):
        # после ошибки браузер ещё может быть открыт (держим его для картинки) - закрываем
        if self.thread and self.thread.is_alive() and self.state == "error":
            self._stop.set()
            self.thread.join(timeout=20)
        if self.thread and self.thread.is_alive():
            return False, "Прошлый запуск ещё закрывается, подождите пару секунд"
        with self.lock:
            if self.state in ("starting", "running", "paused", "stopping"):
                return False, "Уже запущено"
            self.state, self.message = "starting", "Запуск браузера..."
            self._stop.clear()
            self._paused = False
            self.started_at = time.time()
        self.thread = threading.Thread(target=self._run, args=(username, password, headless),
                                       daemon=True)
        self.thread.start()
        return True, ""

    def _run(self, username, password, headless):
        from browser_io import BrowserIO, LoginError, Stopped
        io = None
        sc.STATS = None   # статистика - только этого запуска
        try:
            io = BrowserIO(headless=headless, stop_event=self._stop)
            self.io = io
            self._set("starting", "Вход в игру...")
            io.open_game(username, password)
            if self._stop.is_set():
                return
            self._set("paused" if self._paused else "running", "")
            sc.run_bot(io, ctl=self)
        except Stopped:
            sc.log("остановлено")
        except sc.SessionLost:
            sc.log("бот завершён: игра просит войти снова / переподключиться")
        except LoginError as e:
            sc.log(str(e))
            self._set("error", str(e))
            self._hold_browser_for_preview(io)
        except Exception as e:
            sc.log("Ошибка: " + repr(e))
            traceback.print_exc()
            self._set("error", f"Ошибка: {e}")
        finally:
            sc.finish_session()
            if io is not None:
                io.close()
            self.io = None
            with self.lock:
                if self.state != "error":
                    self.state, self.message = "idle", ""

    def _hold_browser_for_preview(self, io):
        """После ошибки входа держим браузер открытым до нажатия Стоп,
        чтобы в окне управления было видно, что на экране, и можно было покликать."""
        if io is None:
            return
        while not self._stop.is_set():
            io.pump()
            time.sleep(0.2)

    def _set(self, state, msg):
        with self.lock:
            self.state, self.message = state, msg

    def pause(self):
        if self.state == "running":
            self._paused = True
            self._set("paused", "")

    def resume(self):
        if self.state == "paused":
            self._paused = False
            self._set("running", "")

    def stop(self):
        if self.state in ("starting", "running", "paused", "error"):
            self._stop.set()
            if self.thread and self.thread.is_alive():
                self._set("stopping", "Останавливаю...")
            else:
                self._set("idle", "")

    def ui_click(self, fx, fy):
        io = self.io
        if io is not None and 0 <= fx <= 1 and 0 <= fy <= 1:
            io.ui_clicks.put((fx, fy))
            return True
        return False

    def snapshot(self, since):
        st = sc.STATS if self.started_at and sc.STATS and sc.STATS.t0 >= self.started_at - 5 else None
        with self.lock:
            logs = [line for seq, line in self.logs if seq > since]
            data = {"state": self.state, "message": self.message, "log_seq": self.log_seq,
                    "logs": logs}
        if st is not None:
            data["stats"] = {
                "seconds": int(time.time() - st.t0),
                "sparkles": st.sparkles,
                "rewards": st.rewards,
                "unread": st.unread,
            }
        return data


BOT = BotManager()


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):   # не засорять консоль запросами
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _authorized(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            with open(os.path.join(HERE, "ui.html"), encoding="utf-8") as f:
                html = f.read().replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if not self._authorized():
            return self._json({"error": "forbidden"}, 403)
        if path == "/api/state":
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=", 1)[1].split("&")[0])
                except ValueError:
                    pass
            return self._json(BOT.snapshot(since))
        if path == "/api/config":
            cfg = load_config()
            user = cfg.get("username", "")
            return self._json({"username": user, "headless": cfg.get("headless", True),
                               "has_password": bool(saved_password(user)),
                               "keyring": keyring is not None})
        if path == "/api/frame.jpg":
            io = BOT.io
            jpg = io.preview_jpeg if io is not None else None
            if not jpg:
                return self._send(204)
            return self._send(200, jpg, "image/jpeg")
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._json({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        path = self.path
        if path == "/api/start":
            user = (body.get("username") or "").strip()
            pwd = body.get("password") or ""
            remember = bool(body.get("remember"))
            headless = bool(body.get("headless", True))
            if not pwd:
                pwd = saved_password(user)
            elif remember:
                store_password(user, pwd)
            if not remember and body.get("password"):
                store_password(user, "")   # пользователь снял "запомнить"
            save_config({"username": user, "headless": headless})
            ok, msg = BOT.start(user, pwd, headless)
            return self._json({"ok": ok, "message": msg})
        if path == "/api/pause":
            BOT.pause()
        elif path == "/api/resume":
            BOT.resume()
        elif path == "/api/stop":
            BOT.stop()
        elif path == "/api/click":
            ok = BOT.ui_click(float(body.get("x", -1)), float(body.get("y", -1)))
            return self._json({"ok": ok})
        elif path == "/api/forget":
            cfg = load_config()
            store_password(cfg.get("username", ""), "")
        else:
            return self._json({"error": "not found"}, 404)
        self._json({"ok": True})


def free_port(preferred=8765):
    for port in range(preferred, preferred + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("нет свободного порта")


_LOCK = None   # держим открытым, пока программа работает


def already_running():
    """Бот из этой же папки уже запущен? (два бота с одним профилем браузера зависают).
    Возвращает адрес окна управления запущенного бота (или "") либо None, если не запущен."""
    import msvcrt
    global _LOCK
    _LOCK = open(os.path.join(HERE, "app.lock"), "a+")
    try:
        msvcrt.locking(_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
        return None
    except OSError:
        try:
            with open(os.path.join(HERE, "app.url"), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""


def main():
    other = already_running()
    if other is not None:
        print("Бот из этой папки уже запущен (старая консоль ещё открыта или работает в фоне).")
        print("Второй запуск из той же папки невозможен: у них общий профиль браузера.")
        if other:
            print(f"Открываю окно уже запущенного бота: {other}")
            webbrowser.open(other)
        print("Чтобы перезапустить - закройте консоль старого бота и запустите снова.")
        return
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    with open(os.path.join(HERE, "app.url"), "w", encoding="utf-8") as f:
        f.write(url)
    print(f"Окно управления: {url}")
    print("Закройте эту консоль (или Ctrl+C), чтобы полностью выключить программу.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        BOT.stop()
        if BOT.thread:
            BOT.thread.join(timeout=15)
        server.server_close()


if __name__ == "__main__":
    main()

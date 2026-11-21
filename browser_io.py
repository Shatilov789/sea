"""
Фоновый браузер для сборщика блёсток: свой Chromium (Playwright), в котором открыта игра.
Картинку берём прямо со страницы, клики отправляем в страницу - ваши мышь и экран свободны.

Вход сохраняется в папке browser_profile (как "запомнить меня" в обычном браузере),
поэтому логин/пароль нужны только при первом запуске или если сессия слетит.
"""
import base64
import os
import queue
import time

import cv2
import numpy as np
from playwright.sync_api import sync_playwright

import sparkle_clicker as sc

GAME_URL = "https://seaofprime.com/play-unity/?lang=ru"
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profile")
VIEWPORT = {"width": 1920, "height": 1080}
PREVIEW_EVERY = 0.25  # как часто обновлять картинку для окна управления, сек
PREVIEW_SCALE = 0.5   # картинка для окна управления в половину размера - быстрее и легче

CHROME_ARGS = [
    # рисовать игру видеокартой даже без окна (без этого Unity не тянет)
    "--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist", "--enable-webgl",
    # не усыплять страницу в фоне
    "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--mute-audio",
]


class LoginError(Exception):
    pass


class Stopped(Exception):
    """Нажали Стоп, пока шёл вход/загрузка."""


class BrowserIO:
    wait_for_game = True   # ждём, пока в странице появится море

    def __init__(self, headless=True, stop_event=None):
        self.stop_event = stop_event
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=headless, args=CHROME_ARGS,
            viewport=VIEWPORT, device_scale_factor=1, locale="ru-RU")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.cdp = self.ctx.new_cdp_session(self.page)
        self.ui_clicks = queue.Queue()   # клики из окна управления (доли 0..1)
        self.preview_jpeg = None
        self._t_preview = 0.0

    # ---------- вход и запуск игры ----------
    def _login_form_visible(self):
        try:
            return self.page.locator("#loginUser").is_visible()
        except Exception:
            return False

    def open_game(self, username="", password=""):
        sc.log("открываю игру в фоновом браузере...")
        self._goto_game()
        self._sleep_pumping(3)
        if not self._login_form_visible():
            if not self._wait_game_or_expired():
                sc.log("вход уже выполнен (сохранённая сессия)")
                return
            # окно «Сеанс истёк» рисует сама игра (не страница) - сбрасываем старый вход,
            # тогда сайт покажет свою обычную форму входа
            sc.log("игра пишет «Сеанс истёк» - сбрасываю старый вход и вхожу заново")
            self.ctx.clear_cookies()
            self._goto_game()
            self._sleep_pumping(3)
            if not self._login_form_visible():
                raise LoginError("Сеанс истёк, но форма входа сайта не открылась "
                                 "(см. картинку в окне управления).")
        if not username or not password:
            raise LoginError("Нужно войти: введите логин и пароль в окне управления.")
        sc.log("вхожу в аккаунт...")
        # Табличка с формой качается на цепях, и обычные клики Playwright ждут, пока она
        # замрёт (бесконечно). Поэтому анимации выключаем, а жмём через JavaScript страницы.
        self._js_click_button(["Pause animations", "Остановить анимацию"])
        self._dismiss_cookies()
        self.page.fill("#loginUser", username, timeout=10000)
        self.page.fill("#loginPass", password, timeout=10000)
        self.page.evaluate("""() => {
            const box = document.querySelector('#loginRemember');
            if (box && !box.checked) box.click();
            document.querySelector('#loginBtn').click();
        }""")
        sc.log("данные отправлены, жду ответа сайта...")
        # ждём, пока форма входа пропадёт (или страница уйдёт в игру)
        t_end = time.time() + 25
        while time.time() < t_end:
            self._sleep_pumping(1)
            if not self._login_form_visible():
                break
        if self._login_form_visible():
            hint = self._login_error_text()
            raise LoginError("Не удалось войти" + (f": {hint}" if hint else
                             " - проверьте логин и пароль (см. картинку в окне управления)."))
        sc.log("вход выполнен")
        if "play-unity" not in self.page.url:
            self._goto_game()
        self._sleep_pumping(3)
        if self._login_form_visible():
            raise LoginError("Сайт снова просит войти - вход не сохранился.")

    EXPIRED_WORDS = ["войти", "сеанс", "истек", "войдите", "аккаунт", "аккаунты",
                     "забыли", "сохранить", "сохраненные"]
    EXPIRED_WAIT = 45   # сколько секунд ждать, пока загрузится игра или экран входа

    def _wait_game_or_expired(self):
        """Ждёт загрузки игры. True - игра показала свой экран входа («Сеанс истёк»),
        False - видно море (или так и не поняли - тогда ведём себя как раньше)."""
        import difflib
        import re
        t_end = time.time() + self.EXPIRED_WAIT
        while time.time() < t_end:
            self._sleep_pumping(2)
            full, _ = self.grab_full()
            if sc.sea_fraction(full) > 0.03:     # море видно (хотя бы из-под окон) - игра идёт
                return False
            if not sc.OCR_OK:
                continue
            h, w = full.shape[:2]
            crop = full[int(h * 0.55):int(h * 0.95), int(w * 0.3):int(w * 0.75)]
            try:
                text = sc.ocr_win.read_text(cv2.resize(crop, None, fx=2, fy=2,
                                                       interpolation=cv2.INTER_CUBIC))
            except Exception:
                continue
            for word in re.findall(r"[а-яё]{4,}", text.lower().replace("ё", "е")):
                if difflib.get_close_matches(word, self.EXPIRED_WORDS, n=1, cutoff=0.75):
                    return True
        return False

    def _js_click_button(self, texts):
        """Нажать видимую кнопку/ссылку с одним из текстов (без ожидания анимаций)."""
        try:
            return self.page.evaluate("""(texts) => {
                const want = texts.map(t => t.toLowerCase());
                for (const el of document.querySelectorAll('button, a, [role=button]')) {
                    const t = (el.innerText || '').trim().toLowerCase();
                    if (t && want.some(w => t === w || t.includes(w)) && el.offsetParent !== null) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""", texts)
        except Exception:
            return False

    def _dismiss_cookies(self):
        self._js_click_button(["Reject", "Отклонить"])

    def _login_error_text(self):
        """Текст ошибки возле формы входа (например, 'неверный пароль'), если есть."""
        try:
            return self.page.evaluate("""() => {
                const f = document.querySelector('#loginForm');
                if (!f) return '';
                const skip = new Set(['LOG IN', 'Keep me signed in', 'Forgot password',
                                      'Resend verification e-mail']);
                return [...f.querySelectorAll('*')]
                    .filter(e => e.children.length === 0 && e.offsetParent !== null)
                    .map(e => (e.innerText || '').trim())
                    .filter(t => t && !skip.has(t) && t.length < 200)
                    .join(' ');
            }""")[:200]
        except Exception:
            return ""

    # ---------- интерфейс для движка ----------
    def _shot(self, clip=None, fmt="png", quality=None):
        params = {"format": fmt, "optimizeForSpeed": True}
        if quality:
            params["quality"] = quality
        if clip:
            x0, y0, x1, y1 = clip
            params["clip"] = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0, "scale": 1}
        data = base64.b64decode(self.cdp.send("Page.captureScreenshot", params)["data"])
        return data

    def grab_full(self):
        data = self._shot()
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        self._store_preview(img)
        return img, (0, 0)

    def grab_region(self):
        data = self._shot(clip=sc.SEA_REGION)
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

    def move(self, x, y, duration=0.0):
        steps = max(1, int(duration * 60))
        self.page.mouse.move(x, y, steps=steps)

    def click(self, x, y):
        self.page.mouse.click(x, y)

    def key(self, name):
        self.page.keyboard.press(name)

    def pump(self):
        """Обслуживание: клики из окна управления и обновление картинки-превью."""
        while True:
            try:
                fx, fy = self.ui_clicks.get_nowait()
            except queue.Empty:
                break
            x, y = int(fx * VIEWPORT["width"]), int(fy * VIEWPORT["height"])
            self.page.mouse.click(x, y)
            sc.log(f"клик из окна управления ({x}, {y})")
        if time.time() - self._t_preview > PREVIEW_EVERY:
            self.refresh_preview()

    def refresh_preview(self):
        try:
            params = {"format": "jpeg", "quality": 70, "optimizeForSpeed": True,
                      "clip": {"x": 0, "y": 0, "width": VIEWPORT["width"],
                               "height": VIEWPORT["height"], "scale": PREVIEW_SCALE}}
            self.preview_jpeg = base64.b64decode(self.cdp.send("Page.captureScreenshot", params)["data"])
        except Exception:
            pass
        self._t_preview = time.time()

    def _store_preview(self, img):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
        if ok:
            self.preview_jpeg = buf.tobytes()
            self._t_preview = time.time()

    def _check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise Stopped()

    def _goto_game(self):
        """Открыть игру; пока страница грузится, реагируем на Стоп."""
        self.page.goto(GAME_URL, wait_until="commit", timeout=90000)
        t_end = time.time() + 90
        while time.time() < t_end:
            self._check_stop()
            try:
                if self.page.evaluate("document.readyState") != "loading":
                    return
            except Exception:
                pass   # страница ещё переключается
            time.sleep(0.3)

    def _sleep_pumping(self, sec):
        end = time.time() + sec
        while time.time() < end:
            self._check_stop()
            self.pump()
            time.sleep(0.1)

    def close(self):
        for f in (self.ctx.close, self._pw.stop):
            try:
                f()
            except Exception:
                pass

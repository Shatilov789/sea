"""
Кликер блёсток для Sea of Prime.
Ищет на экране жёлто-зелёные блёстки по цвету (OpenCV) и кликает по ним.
Работает локально, без ИИ и без вмешательства в игру.

Запуск:
    python sparkle_clicker.py            - рабочий режим
    python sparkle_clicker.py --dry      - только показывает, что нашёл, не кликает
    python sparkle_clicker.py --test img - проверка на скриншоте, сохраняет debug.png

Остановка: Ctrl+C в консоли или резко увести мышь в левый верхний угол экрана.
"""
import argparse
import ctypes
import os
import random
import re
import sys
import time

import cv2
import mss
import numpy as np
import pyautogui

# --- Настройки ---------------------------------------------------------------
# Область моря в физических пикселях экрана (x0, y0, x1, y1).
# При запуске определяется автоматически (любое разрешение/положение окна).
# Это значение - запасное, если автоопределение не сработало (экран 2560x1440).
SEA_REGION = (736, 371, 1824, 1150)

# Цвет блёстки в HSV (OpenCV: H 0..179)
HSV_LOW = (38, 70, 150)
HSV_HIGH = (75, 200, 255)  # S<=200: блёстка бледная, отсекает яркие зелёные метки

MIN_AREA = 8          # минимум настоящих (не раздутых) пикселей цвета в пятне
MAX_SIZE = 40         # максимальная ширина/высота пятна - отсекает крупные зелёные объекты
SCAN_INTERVAL = 0.3   # как часто смотреть на экран, сек

# Слежение за целью: кликнули по блёстке -> ждём, пока она исчезнет (собрана),
# и только потом берём следующую.
TRACK_RADIUS = 60     # насколько цель может сместиться между кадрами (если камера едет за кораблём)
GONE_AFTER = 1.2      # блёстки нет столько секунд подряд -> проверяем, собрана ли
CONFIRM_GONE = 2.5    # ...и ещё столько сек следим, не вынырнет ли из-под корабля
RECLICK_AFTER = 7.0   # блёстка всё ещё на месте через N сек после клика -> кликнуть ещё раз (промах)
MAX_CLICKS_PER_TARGET = 3
GIVE_UP_AFTER = 30.0  # не собралась за N сек -> бросить её
IGNORE_FOR = 60.0     # и не трогать это место N сек
PAUSE_AFTER_COLLECT = (0.4, 1.0)

# Упреждение при клике по движущейся (из-за камеры) блёстке
AIM_SAMPLE = 0.05     # пауза между двумя замерами позиции, сек
AIM_LATENCY = 0.03    # примерная задержка от решения до клика в игре, сек

# Окно "Вы собрали блёстку": вырезанная картинка-образец рядом со скриптом.
# Если файла нет - сбор определяется только по исчезновению блёстки.
POPUP_TEMPLATE = "popup.png"
POPUP_THRESHOLD = 0.8     # насколько похоже должно быть (0..1)
POPUP_CLOSE_CLICK = False # True, если окно надо закрывать кликом по нему

# Поиск: если блёсток нет PATROL_AFTER сек, плывём в одном направлении.
# Камера едет за кораблём, поэтому клик у края экрана = "плыть туда дальше".
PATROL_AFTER = 4.0
PATROL_EVERY = 5.0        # как часто подтверждать курс кликом, сек
PATROL_TURN_EVERY = 45.0  # раз в N сек менять курс случайно, чтобы обходить новые места
STUCK_SHIFT = 3.0         # обзор сдвинулся меньше чем на N пикс. -> считаем, что не едем
STILL_CHECK = 2.0         # как часто сравнивать кадры, сек
STILL_TIMEOUT = 5.0       # корабль стоит дольше N сек, хотя должен плыть -> пробуем другое место
# Препятствия (монстры, корабли, острова, надписи) = всё, что не цвета моря.
# Море: H~98, S~240, V~50. Полупрозрачные монстры снижают S, текст и острова - яркие.
SEA_H = (92, 104)
SEA_MIN_S = 200
SEA_MAX_V = 80
OBSTACLE_GROW = 25     # склеивает части монстра (картинка + имя) в один объект
OBSTACLE_PAD = 12      # доп. запас вокруг объекта, куда не кликаем
SPARKLE_BOX = 50       # объект не крупнее этого и с блёсткой внутри - сама блёстка
# 8 направлений (доли от зоны моря: 0,0 - левый верх, 1,1 - правый низ)
DIRECTIONS = {
    "север": (0.5, 0.1), "северо-восток": (0.88, 0.12), "восток": (0.9, 0.5),
    "юго-восток": (0.88, 0.85), "юг": (0.5, 0.87), "юго-запад": (0.12, 0.85),
    "запад": (0.1, 0.5), "северо-запад": (0.12, 0.12),
}
# -----------------------------------------------------------------------------

# исходные значения пиксельных настроек (для экрана 2560x1440) - от них считается масштаб
_BASE = dict(MIN_AREA=MIN_AREA, MAX_SIZE=MAX_SIZE, TRACK_RADIUS=TRACK_RADIUS,
             OBSTACLE_GROW=OBSTACLE_GROW, OBSTACLE_PAD=OBSTACLE_PAD, SPARKLE_BOX=SPARKLE_BOX)

pyautogui.FAILSAFE = True  # мышь в угол экрана -> остановка
pyautogui.PAUSE = 0.05


def set_dpi_aware():
    """Чтобы координаты мыши совпадали с пикселями скриншота при масштабе Windows != 100%."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_sparkles(bgr, offset=(0, 0)):
    """Возвращает список центров блёсток [(x, y), ...] в координатах экрана."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, HSV_LOW, HSV_HIGH)
    # склеиваем точки одной блёстки в одно пятно
    grown = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(grown)
    found = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        area = int(np.count_nonzero(mask[labels == i]))
        if area < MIN_AREA or w > MAX_SIZE or h > MAX_SIZE:
            continue
        cx, cy = centroids[i]
        found.append((int(cx) + offset[0], int(cy) + offset[1]))
    # второй способ - по форме (цвет на разных ПК/настройках графики чуть отличается)
    for p in find_sparkles_shape(bgr, hsv, offset):
        if all(dist2(p, f) > (15 * SCALE) ** 2 for f in found):
            found.append(p)
    return found


# Поиск по форме: блёстка = маленькая кучка (1-5) светлых точек, заметно светлее моря,
# цвет - любой от жёлтого до зелёного, и вокруг неё со всех сторон чистое море
# (так отсекаются текст, корабли, монстры, острова).
SHAPE_HUE = (32, 85)       # блёстки на разных ПК: 35..65; золотой значок у имени корабля ~24
SHAPE_MIN_S = 30           # белый текст (имена) отсекается
SHAPE_CONTRAST = 45        # насколько точки ярче моря вокруг (V, 0..255)
SHAPE_MAX_DOTS = 5
SHAPE_SEA_AROUND = 0.85    # доля чистого моря в рамке вокруг кучки


def find_sparkles_shape(bgr, hsv=None, offset=(0, 0)):
    if hsv is None:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    bg = cv2.blur(v, (31, 31))
    bright = (v.astype(np.int16) - bg) > SHAPE_CONTRAST
    mask = (bright & (h >= SHAPE_HUE[0]) & (h <= SHAPE_HUE[1]) & (s >= SHAPE_MIN_S)).astype(np.uint8)
    sea = (h >= SEA_H[0]) & (h <= SEA_H[1]) & (s >= SEA_MIN_S) & (v <= SEA_MAX_V)
    gap = max(3, int(round(7 * SCALE))) | 1   # точки одной блёстки рядом друг с другом
    grown = cv2.dilate(mask, np.ones((gap, gap), np.uint8))
    n, labels, stats, cents = cv2.connectedComponentsWithStats(grown)
    H, W = mask.shape
    pad = max(8, int(MAX_SIZE * 0.6))
    min_area = max(3, MIN_AREA // 2)
    out = []
    for i in range(1, n):
        x, y, w, hh, _ = stats[i]
        if w > MAX_SIZE or hh > MAX_SIZE:
            continue
        own = mask[y:y + hh, x:x + w] & (labels[y:y + hh, x:x + w] == i)
        if int(own.sum()) < min_area:
            continue
        k, _ = cv2.connectedComponents(own.astype(np.uint8))
        if k - 1 > SHAPE_MAX_DOTS:
            continue
        # рамка вокруг кучки (без неё самой) должна быть морем
        X0, Y0, X1, Y1 = max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + hh + pad)
        ring = sea[Y0:Y1, X0:X1].copy()
        inner = np.zeros(ring.shape, bool)
        inner[y - Y0:y - Y0 + hh, x - X0:x - X0 + w] = True
        total = ring.size - inner.sum()
        if total <= 0 or ring[~inner].sum() / total < SHAPE_SEA_AROUND:
            continue
        out.append((int(cents[i][0]) + offset[0], int(cents[i][1]) + offset[1]))
    return out


DIAG_DIR = "diag"     # кадры, где блёстки, похоже, есть, но бот их не взял
DIAG_EVERY = 30.0     # не чаще раза в N сек
DIAG_KEEP = 40        # хранить последние N кадров


def find_sparkles_loose(bgr, offset=(0, 0), zones=()):
    """Сколько пятен (вне препятствий) похожи на блёстку при ослабленных условиях -
    для диагностики, если цвет блёсток на этом ПК немного другой."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (30, 40, 100), (85, 230, 255))
    n, _, stats, cents = cv2.connectedComponentsWithStats(
        cv2.dilate(mask, np.ones((5, 5), np.uint8)))
    count = 0
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        p = (int(cents[i][0]) + offset[0], int(cents[i][1]) + offset[1])
        if w <= MAX_SIZE * 1.5 and h <= MAX_SIZE * 1.5 and area >= max(3, MIN_AREA // 2) \
                and not in_zones(p, zones):
            count += 1
    return count


def save_diag(frame, pts, zones, ignored, kind):
    """Сохраняет кадр с пометками: красные круги - блёстки, оранжевые рамки - препятствия,
    синие круги - пропущенные места."""
    import glob
    import os
    try:
        os.makedirs(DIAG_DIR, exist_ok=True)
        img = frame.copy()
        ox, oy = SEA_REGION[:2]
        for z in zones:
            cv2.rectangle(img, (z[0] - ox, z[1] - oy), (z[2] - ox, z[3] - oy), (0, 165, 255), 1)
        for g in ignored:
            cv2.circle(img, (g[0] - ox, g[1] - oy), TRACK_RADIUS, (255, 0, 0), 1)
        for p in pts:
            cv2.circle(img, (p[0] - ox, p[1] - oy), 14, (0, 0, 255), 2)
        name = os.path.join(DIAG_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}")
        cv2.imwrite(name + ".png", img)
        cv2.imwrite(name + "-raw.png", frame)   # без пометок - для проверки исправлений
        for old in sorted(glob.glob(os.path.join(DIAG_DIR, "*.png")))[:-DIAG_KEEP]:
            os.remove(old)
    except Exception:
        pass


def grab_region(sct):
    x0, y0, x1, y1 = SEA_REGION
    shot = sct.grab({"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0})
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


def run_test(path):
    img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        sys.exit(f"Не удалось открыть {path}")
    auto_calibrate(img)
    x0, y0, x1, y1 = SEA_REGION
    frame = img[y0:y1, x0:x1]
    pts = find_sparkles(frame, offset=(x0, y0))
    zones = find_monsters(frame, offset=(x0, y0), sparkles=pts)
    popup = find_popup(frame, load_popup_template())
    cv2.rectangle(img, (x0, y0), (x1, y1), (255, 0, 255), 2)
    for z in zones:
        cv2.rectangle(img, (int(z[0]), int(z[1])), (int(z[2]), int(z[3])), (0, 165, 255), 2)
    for (x, y) in pts:
        cv2.circle(img, (x, y), 20, (0, 0, 255), 3)
    cv2.imwrite("debug.png", img)
    print(f"Найдено блёсток: {len(pts)} -> {pts}")
    print(f"Препятствий (монстры/острова/надписи): {len(zones)}")
    print(f"Надпись о сборе: {'есть' if popup else 'нет'}")
    print("Сохранено debug.png: фиолетовая рамка - зона поиска, оранжевые - препятствия, красные круги - блёстки")


# --- Подсчёт заработанного ----------------------------------------------------
OCR_PROBLEM = ""   # почему OCR не работает (для лога)
try:
    import ocr_win
    OCR_OK = ocr_win.available()
    if not OCR_OK:
        OCR_PROBLEM = ("в Windows не установлено распознавание русского текста. Исправить: "
                       "PowerShell от администратора -> "
                       "Add-WindowsCapability -Online -Name \"Language.OCR~~~ru-RU~0.0.1.0\" "
                       "(или Параметры -> Язык -> добавить Русский), потом перезапустить бота")
except Exception as e:
    ocr_win = None
    OCR_OK = False
    OCR_PROBLEM = (f"не установлены библиотеки winrt ({type(e).__name__}: {e}). Исправить: "
                   "pip install -r requirements.txt, потом перезапустить бота")

KNOWN_RESOURCES = ["Жемчуг", "Золото", "Порох", "XP", "HP"]   # пополняется сам, когда встречается новое название
STATS_EVERY = 600              # печатать сводку раз в N сек
SESSIONS_LOG = "sessions_log.csv"


class Stats:
    def __init__(self):
        self.t0 = time.time()
        self.sparkles = 0
        self.rewards = {}
        self.unread = 0          # сборы, где награду не удалось прочитать
        self.t_report = time.time()

    def add_reward(self, amount, name):
        self.rewards[name] = self.rewards.get(name, 0) + amount

    def summary(self):
        mins = (time.time() - self.t0) / 60
        hours = mins / 60

        def rate(x):  # в час - только когда прошло хотя бы 5 минут, иначе цифры бессмысленные
            return f" ({x / hours:,.0f}/час)".replace(",", " ") if mins >= 5 else ""

        lines = [f"=== Сессия: {int(mins // 60)} ч {int(mins % 60)} мин, блёсток: {self.sparkles}"
                 f"{rate(self.sparkles)} ==="]
        for name, total in sorted(self.rewards.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name}: {total:,}".replace(",", " ") + rate(total))
        if self.unread:
            lines.append(f"  награда не прочитана: {self.unread} раз (см. папку ocr_fail)")
        return "\n".join(lines)

    def save(self):
        import csv
        import os
        new = not os.path.exists(SESSIONS_LOG)
        with open(SESSIONS_LOG, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["начало", "конец", "минут", "блёсток", "ресурсы", "не прочитано"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(self.t0)),
                        time.strftime("%Y-%m-%d %H:%M"),
                        round((time.time() - self.t0) / 60), self.sparkles,
                        ", ".join(f"{k} {v}" for k, v in self.rewards.items()), self.unread])


# короткие названия (XP, HP) OCR часто читает русскими буквами-двойниками
_LOOKALIKE = str.maketrans("ХРНАВЕКМОСТУхрнавекмосту", "XPHABEKMOCTYXPHABEKMOCTY")


def match_resource(word):
    """Приводит распознанное название к известному ('Жбмчуг' -> 'Жемчуг', 'ХР' -> 'XP').
    Возвращает (название, известное_ли)."""
    import difflib
    word = re.sub(r"[^А-Яа-яЁёA-Za-z]", "", word)
    if len(word) == 2:                      # XP, HP...
        word = word.translate(_LOOKALIKE).upper()
        return word, word in KNOWN_RESOURCES
    word = word.capitalize()
    if len(word) < 3:
        return None, False
    match = difflib.get_close_matches(word, KNOWN_RESOURCES, n=1, cutoff=0.5)
    return (match[0], True) if match else (word, False)


def parse_reward(text):
    """'... Награда: 1.974 Золото' -> (1974, 'Золото') или None."""
    m = re.search(r"Наград\w*\W*([\dOoОо][\dOoОо .,]*?)\s*([А-Яа-яЁёA-Za-z]\S{0,13})", text)
    if not m:
        return None
    digits = re.sub(r"[\s.,]", "", m.group(1)).translate(str.maketrans("OoОо", "0000"))
    if not digits.isdigit():
        return None
    return int(digits), m.group(2)


def read_reward(frame, popup_center):
    """Вырезает строку надписи вокруг 'Вы собрали блёстку' и читает награду.
    Распознаёт несколько вариантов картинки и берёт ответ большинства."""
    from collections import Counter
    if not OCR_OK:
        return None, None
    sc = _popup_scale or SCALE
    tw, th = 156 * sc, 20 * sc   # размер образца popup.png в этом масштабе
    cx, cy = popup_center[0] - SEA_REGION[0], popup_center[1] - SEA_REGION[1]
    x0 = int(max(0, cx - tw / 2 - 10 * sc))
    x1 = int(min(frame.shape[1], cx - tw / 2 + tw * 3.2))
    y0 = int(max(0, cy - th * 0.8))
    y1 = int(min(frame.shape[0], cy + th * 0.8))
    line = frame[y0:y1, x0:x1]
    if line.size == 0:
        return None, line

    gray = cv2.cvtColor(line, cv2.COLOR_BGR2GRAY)
    base = max(3.0, 3.0 / sc)
    variants = []
    for k in (base, base + 1, base + 2):
        g = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
        _, b = cv2.threshold(g, 150, 255, cv2.THRESH_BINARY_INV)   # чёрный текст на белом
        variants.append(cv2.cvtColor(b, cv2.COLOR_GRAY2BGR))
        variants.append(cv2.resize(line, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC))

    amounts, known, unknown = Counter(), Counter(), Counter()
    for img in variants:
        try:
            r = parse_reward(ocr_win.read_text(img))
        except Exception:
            r = None
        if not r:
            continue
        amounts[r[0]] += 1
        name, is_known = match_resource(r[1])
        if name:
            (known if is_known else unknown)[name] += 1
    if not amounts:
        return None, line
    amount = amounts.most_common(1)[0][0]
    if known:
        name = known.most_common(1)[0][0]
    elif unknown and unknown.most_common(1)[0][1] >= 2:
        name = unknown.most_common(1)[0][0]   # новое название, прочитано одинаково 2+ раза
        KNOWN_RESOURCES.append(name)
        print(f"Новый вид награды: {name}")
    else:
        name = "неопознанный ресурс"
    return (amount, name), line


STATS = None


def save_ocr_fail(line):
    """Сохраняет нераспознанную строку награды - пригодится, чтобы подстроить OCR."""
    if line is None or line.size == 0:
        return
    import os
    os.makedirs("ocr_fail", exist_ok=True)
    cv2.imwrite(os.path.join("ocr_fail", time.strftime("%Y%m%d-%H%M%S") + ".png"), line)


def dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


LOG_HOOK = None   # интерфейс подписывается сюда, чтобы показывать лог


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG_HOOK is not None:
        LOG_HOOK(line)


def click_at(p, dry):
    if dry:
        return
    pyautogui.moveTo(p[0] + random.randint(-1, 1), p[1] + random.randint(-1, 1),
                     duration=random.uniform(0.12, 0.3))
    pyautogui.click()


def white_text_mask(bgr):
    """Белый текст (яркий и бесцветный) -> 255, остальное -> 0. Фон моря не мешает."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (0, 0, 190), (179, 60, 255))


def load_popup_template():
    try:
        img = cv2.imdecode(np.fromfile(POPUP_TEMPLATE, np.uint8), cv2.IMREAD_COLOR)
    except (FileNotFoundError, OSError):
        img = None
    if img is None:
        print(f"{POPUP_TEMPLATE} не найден - надпись о сборе не отслеживаю.")
        return None
    print(f"Образец надписи о сборе загружен ({img.shape[1]}x{img.shape[0]}).")
    return img  # цветной: масштабируем его, а потом выделяем белый текст - так точнее


_popup_scale = None  # масштаб надписи, найденный при первом совпадении
_popup_masks = {}    # кэш образцов по масштабам


def find_popup(frame, tpl):
    """Ищет надпись 'Вы собрали блёстку'. Возвращает её центр на экране или None.
    Пока масштаб надписи неизвестен (другое разрешение), пробует несколько масштабов."""
    global _popup_scale
    if tpl is None:
        return None
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if _popup_scale is not None:
        scales = [_popup_scale]
    else:
        base = round(SCALE, 2)
        scales = sorted({base, 1.0} | {round(base * k, 2) for k in (0.8, 0.9, 1.1, 1.25)})
    for sc in scales:
        t = _popup_masks.get(sc)
        if t is None:
            c = tpl if sc == 1.0 else cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
            t = _popup_masks[sc] = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
        if gray.shape[0] < t.shape[0] or gray.shape[1] < t.shape[1] or t.shape[1] < 20:
            continue
        res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score >= POPUP_THRESHOLD:
            if _popup_scale is None:
                _popup_scale = sc
                if sc != 1.0:
                    print(f"Масштаб надписи о сборе: {sc}")
            return (loc[0] + t.shape[1] // 2 + SEA_REGION[0], loc[1] + t.shape[0] // 2 + SEA_REGION[1])
    return None


# --- Автонастройка под разрешение ---------------------------------------------
BASE_SEA_WIDTH = 1094   # ширина поля моря на экране 2560x1440, под который подобраны настройки
SCALE = 1.0
SEA_REGION_FOUND = False   # поле моря найдено автоматически


def detect_sea_region(bgr):
    """Находит на снимке экрана поле моря (однотонная бирюзовая область с пропорциями
    игрового поля). Возвращает (x0, y0, x1, y1) зоны поиска или None, если поле не видно
    целиком (игра грузится, открыто окно поверх моря и т.п.)."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    sea = ((h >= SEA_H[0]) & (h <= SEA_H[1]) & (s >= SEA_MIN_S) & (v <= SEA_MAX_V)).astype(np.uint8) * 255
    sea = cv2.morphologyEx(sea, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(sea)
    if n < 2:
        return None
    i = 1 + int(np.argmax(stats[1:, 4]))
    x, y, w, hh, area = stats[i]
    if w < 300 or hh < 200:
        return None
    # поле игры примерно 1094x805 (1.36); окно поверх моря режет его на куски другой формы
    if not 1.2 <= w / hh <= 1.55:
        return None
    if area / float(w * hh) < 0.6:      # внутри рамки слишком мало моря - что-то перекрывает
        return None
    k = w / BASE_SEA_WIDTH
    # чуть внутрь от рамки; снизу отрезаем полосы HP/EXP, которые лежат поверх моря
    return (int(x + 3 * k), int(y + 2 * k), int(x + w - 3 * k), int(y + hh - 24 * k))


def apply_region(region):
    """Запоминает зону поиска и масштабирует пиксельные настройки под её размер."""
    global SEA_REGION, SCALE, MIN_AREA, MAX_SIZE, TRACK_RADIUS, SEA_REGION_FOUND
    global OBSTACLE_GROW, OBSTACLE_PAD, SPARKLE_BOX
    SEA_REGION = tuple(int(c) for c in region)
    SEA_REGION_FOUND = True
    SCALE = (SEA_REGION[2] - SEA_REGION[0]) / (BASE_SEA_WIDTH - 6)
    s = SCALE
    MIN_AREA = max(3, int(round(_BASE["MIN_AREA"] * s * s)))
    MAX_SIZE = max(10, int(round(_BASE["MAX_SIZE"] * s)))
    TRACK_RADIUS = max(20, int(round(_BASE["TRACK_RADIUS"] * s)))
    OBSTACLE_GROW = max(5, int(round(_BASE["OBSTACLE_GROW"] * s)) | 1)
    OBSTACLE_PAD = max(4, int(round(_BASE["OBSTACLE_PAD"] * s)))
    SPARKLE_BOX = max(15, int(round(_BASE["SPARKLE_BOX"] * s)))


def auto_calibrate(full_bgr, offset=(0, 0), quiet=False):
    region = detect_sea_region(full_bgr)
    if region is None:
        if not quiet:
            log(f"Поле моря не найдено автоматически - беру SEA_REGION из настроек: {SEA_REGION}")
        return False
    region = (region[0] + offset[0], region[1] + offset[1], region[2] + offset[0], region[3] + offset[1])
    apply_region(region)
    log(f"Поле моря найдено: {SEA_REGION}, масштаб {SCALE:.2f}")
    return True


def find_monsters(frame, offset=(0, 0), sparkles=()):
    """Всё, что не похоже на чистое море (монстры - даже без полоски HP и полупрозрачные,
    имена, корабли, острова, надписи), считается препятствием.
    Возвращает запретные для клика прямоугольники (x0, y0, x1, y1) в координатах экрана.
    Маленькие пятна, в которых сидит блёстка, препятствием не считаются."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    sea = (h >= SEA_H[0]) & (h <= SEA_H[1]) & (s >= SEA_MIN_S) & (v <= SEA_MAX_V)
    mask = np.where(sea, 0, 255).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))  # убрать шум волн
    grown = cv2.dilate(mask, np.ones((OBSTACLE_GROW, OBSTACLE_GROW), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(grown)
    zones = []
    for i in range(1, n):
        x, y, w, hh, _ = stats[i]
        x += offset[0]
        y += offset[1]
        z = (x - OBSTACLE_PAD, y - OBSTACLE_PAD, x + w + OBSTACLE_PAD, y + hh + OBSTACLE_PAD)
        # размер пятна без раздувания (dilate добавил OBSTACLE_GROW-1 пикселей)
        raw_w, raw_h = w - (OBSTACLE_GROW - 1), hh - (OBSTACLE_GROW - 1)
        if raw_w <= SPARKLE_BOX and raw_h <= SPARKLE_BOX and in_zones_any(sparkles, z):
            continue  # это сама блёстка
        zones.append(z)
    return zones


MINIMAP_CORNER = (0.86, 0.18)   # правый верхний угол зоны моря (доли), где бывает миникарта


def real_sparkles(frame, pts, offset=(0, 0)):
    """Оставляет только настоящие блёстки. Если блёстку видно, на ней ничего нет,
    поэтому рамки соседних препятствий не важны. Важно другое: пятно, где найдена
    "блёстка", должно быть маленьким и отдельным. Если оно - часть чего-то крупного
    (монстр, корабль, текст, полоска HP, миникарта), это не блёстка."""
    if not pts:
        return []
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    sea = (h >= SEA_H[0]) & (h <= SEA_H[1]) & (s >= SEA_MIN_S) & (v <= SEA_MAX_V)
    nonsea = cv2.dilate(np.where(sea, 0, 255).astype(np.uint8), np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(nonsea)
    H, W = labels.shape
    r = max(3, int(round(6 * SCALE)))
    out = []
    for p in pts:
        x, y = p[0] - offset[0], p[1] - offset[1]
        if x > W * MINIMAP_CORNER[0] and y < H * MINIMAP_CORNER[1]:
            continue   # миникарта (на некоторых аккаунтах лежит поверх моря) - клик туда опасен
        win = labels[max(0, y - r):min(H, y + r + 1), max(0, x - r):min(W, x + r + 1)]
        ids = set(np.unique(win)) - {0}
        if all(stats[i][2] <= SPARKLE_BOX and stats[i][3] <= SPARKLE_BOX for i in ids):
            out.append(p)
    return out


def in_zones_any(points, z):
    return any(z[0] <= p[0] <= z[2] and z[1] <= p[1] <= z[3] for p in points)


def in_zones(p, zones):
    return any(z[0] <= p[0] <= z[2] and z[1] <= p[1] <= z[3] for z in zones)


def direction_point(name, zones=()):
    """Точка у края экрана в нужную сторону, не попадающая на монстров.
    Если у края монстр - пробует точки ближе к центру. Нет свободных -> None."""
    x0, y0, x1, y1 = SEA_REGION
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    fx, fy = DIRECTIONS[name]
    tx, ty = x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy
    for k in (1.0, 0.85, 0.7, 0.55, 0.4):
        p = (int(cx + (tx - cx) * k), int(cy + (ty - cy) * k))
        if not in_zones(p, zones):
            return p
    return None


# --- Окна поверх моря (подарок за вход, рынок, чат...) ---------------------------
COVERED_LIMIT = 0.5        # открытого моря меньше этой доли -> море закрыто окном
POPUP_RETRY = 3.0          # как часто пытаться закрыть окна, сек
CLAIM_WORDS = ["получить", "забрать", "принять"]   # кнопки подарков
CLOSE_TEMPLATE = "close_x.png"   # крестик закрытия окна (с экрана 2560x1440)
CLOSE_THRESHOLD = 0.75
_close_tpl = None


def sea_fraction(frame):
    """Какая доля зоны поиска - чистое море (в обычной игре ~0.9, с окном поверх - мало)."""
    small = cv2.resize(frame, (frame.shape[1] // 4, frame.shape[0] // 4), interpolation=cv2.INTER_AREA)
    h, s, v = cv2.split(cv2.cvtColor(small, cv2.COLOR_BGR2HSV))
    sea = (h >= SEA_H[0]) & (h <= SEA_H[1]) & (s >= SEA_MIN_S) & (v <= SEA_MAX_V)
    return float(sea.mean())


def _scale_guess(full):
    return SCALE if SEA_REGION_FOUND else full.shape[1] / 2560


def find_claim_button(full):
    """Ищет OCR-ом кнопку 'Получить' (подарок). Возвращает (x, y, слово) или None."""
    import difflib
    if not OCR_OK:
        return None
    hh, ww = full.shape[:2]
    x0, y0 = int(ww * 0.15), int(hh * 0.08)       # окна открываются по центру
    crop = full[y0:int(hh * 0.95), x0:int(ww * 0.85)]
    k = min(2.0, 2500 / max(crop.shape[:2]))      # OCR Windows не берёт картинки больше ~2600 px
    big = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    try:
        words = ocr_win.read_words(big)
    except Exception:
        return None
    def is_claim(text):
        t = re.sub(r"[^а-яё]", "", text.lower())
        return len(t) >= 5 and difflib.get_close_matches(t, CLAIM_WORDS, n=1, cutoff=0.6)

    for text, x, y, w, h in words:
        if is_claim(text):
            return (int(x0 + (x + w / 2) / k), int(y0 + (y + h / 2) / k), text)

    # Вторая попытка: зелёные кнопки по отдельности, с сильным увеличением.
    # Жмём только если подпись - "Получить"/"Забрать"/"Принять" (не "Купить"!).
    for bx, by, bw, bh in green_buttons(crop):
        pad = max(2, bh // 4)
        part = crop[max(0, by - pad):by + bh + pad, max(0, bx - pad):bx + bw + pad]
        kk = min(6.0, 2500 / max(part.shape[:2]))
        try:
            text = ocr_win.read_text(cv2.resize(part, None, fx=kk, fy=kk, interpolation=cv2.INTER_CUBIC))
        except Exception:
            continue
        if any(is_claim(wd) for wd in text.split()):
            return (x0 + bx + bw // 2, y0 + by + bh // 2, text.strip())
    return None


def green_buttons(bgr):
    """Зелёные прямоугольные кнопки (не полоски HP): [(x, y, w, h), ...]."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (40, 110, 110), (80, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if h >= 6 and 1.8 <= w / h <= 6 and area >= 0.5 * w * h and w >= 25:
            out.append((x, y, w, h))
    return out


def find_close_button(full):
    """Крестик закрытия окна. Если их несколько (подарок, чат, трекер заданий),
    берёт ближайший к центру экрана - это окно, которое закрывает море.
    Возвращает (x, y) или None."""
    global _close_tpl
    if _close_tpl is None:
        img = cv2.imread(CLOSE_TEMPLATE, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        _close_tpl = img
    gray = cv2.cvtColor(full, cv2.COLOR_BGR2GRAY)
    base = _scale_guess(full)
    found = []   # (x, y)
    for k in (0.85, 0.92, 1.0, 1.08, 1.15, 1.25):
        sc = base * k
        t = cv2.resize(_close_tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        if t.shape[0] < 8 or t.shape[0] >= gray.shape[0]:
            continue
        r = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        for y, x in zip(*np.where(r >= CLOSE_THRESHOLD)):
            cx, cy = int(x + t.shape[1] / 2), int(y + t.shape[0] / 2)
            if cy < gray.shape[0] * 0.08:   # верхнюю панель игры (кнопка Выход и т.п.) не трогаем
                continue
            if all(dist2((cx, cy), f) > (t.shape[0]) ** 2 for f in found):
                found.append((cx, cy))
    if not found:
        return None
    center = (gray.shape[1] / 2, gray.shape[0] / 2)
    return min(found, key=lambda f: dist2(f, center))


TUTORIAL_RETRY = 5.0       # как часто проверять окно "Больше не показывать", сек


def _norm(text):
    return re.sub(r"[^а-яё]", "", text.lower())


REPAIR_TEMPLATE = "repair_hammer.png"   # молоток "Ремонт" в нижней панели (с экрана 2560x1440)
REPAIR_THRESHOLD = 0.75
_repair_tpl = None


def _like(text, word, cutoff=0.75):
    import difflib
    t = _norm(text)
    return len(t) >= 3 and bool(difflib.get_close_matches(t, [word], n=1, cutoff=cutoff))


def handle_dialogs(io, full, dry=False):
    """Небольшие окна, которые море почти не закрывают, поэтому ищем их OCR-ом:
    - вход из другой сессии / «Переподключить» -> ничего не жмём, завершаем бота (SessionLost);
    - корабль потоплен («Починить и вернуться» / «Выйти») -> жмём «Починить и вернуться»;
    - окно с галочкой «Больше не показывать» -> ставим галочку и закрываем.
    Возвращает "death", "dont_show" или None."""
    if not OCR_OK:
        return None
    hh, ww = full.shape[:2]
    # эти окна открываются по центру; узкая зона -> можно сильнее увеличить для OCR
    x0, y0 = int(ww * 0.26), int(hh * 0.2)
    crop = full[y0:int(hh * 0.85), x0:int(ww * 0.74)]
    k = min(2.0, 2500 / max(crop.shape[:2]))
    big = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    try:
        words = ocr_win.read_words(big)
    except Exception:
        return None

    def to_full(x, y):
        return int(x0 + x / k), int(y0 + y / k)

    relogin = _handle_relogin(io, crop, words, to_full, k, dry)
    if relogin:
        return relogin
    if _handle_death(io, crop, words, to_full, k, dry):
        return "death"
    if _handle_dont_show(io, full, words, to_full, dry):
        return "dont_show"
    return None


# Окно «В ваш аккаунт выполнен вход из другой сессии» («Войти снова» / «Выйти из игры»)
# или «Переподключить» - ничего не нажимаем, завершаем бота.
RELOGIN_WORDS = ["вход", "другой", "другого", "сессии", "снова", "завершена",
                 "безопасности", "аккаунт", "выполнен", "переподключить"]


class SessionLost(Exception):
    """Игра просит войти снова / переподключиться - бот завершается."""


def _relogin_button(crop):
    """Зелёная кнопка, справа от которой на той же высоте красная: центр зелёной или None."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    def rects(mask):
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n, _, st, _ = cv2.connectedComponentsWithStats(mask)
        return [st[i][:4] for i in range(1, n)
                if st[i][2] >= 40 and st[i][3] >= 12 and 1.8 <= st[i][2] / st[i][3] <= 7
                and st[i][4] >= 0.5 * st[i][2] * st[i][3]]
    green = rects(cv2.inRange(hsv, (40, 100, 90), (85, 255, 255)))
    red = rects(cv2.inRange(hsv, (0, 120, 90), (8, 255, 255)) | cv2.inRange(hsv, (172, 120, 90), (179, 255, 255)))
    for gx, gy, gw, gh in green:
        for rx, ry, rw, rh in red:
            if rx > gx + gw * 0.8 and rx - (gx + gw) < gw * 1.5 and abs((ry + rh / 2) - (gy + gh / 2)) < gh * 0.6:
                return int(gx + gw / 2), int(gy + gh / 2)
    return None


def _handle_relogin(io, crop, words, to_full, k, dry):
    """Вход из другой сессии / «Переподключить» -> ничего не жмём, SessionLost (бот завершается)."""
    import difflib
    hits = set()
    for text, *_ in words:
        t = _norm(text)
        if len(t) >= 4:
            m = difflib.get_close_matches(t, RELOGIN_WORDS, n=1, cutoff=0.75)
            if m:
                hits.add(m[0])
    if "переподключить" not in hits and (len(hits) < 2 or _relogin_button(crop) is None):
        return None
    log("окно «Войти снова / Переподключить» - ничего не жму, завершаю бота")
    raise SessionLost()


def _handle_death(io, crop, words, to_full, k, dry):
    """Окно потопления: круглая кнопка над подписью «Починить и вернуться»."""
    def is_label(text):
        # OCR иногда склеивает подпись в одно слово ("Починитьивернуться") - ищем и части
        # "Получить" (кнопка подарка) похоже на "починить" на 75% - строгий порог и исключение
        t = _norm(text)
        if "получ" in t:
            return False
        return (_like(text, "вернуться", 0.7) or _like(text, "починить", 0.85)
                or any(s in t for s in ("почин", "вернут", "ернуть")))

    label = None
    for text, x, y, w, h in words:
        if is_label(text):
            label = (x, y, w, h)
            break
    if label is None:
        # подпись не прочиталась, но виден заголовок "Капитан, у вас ещё есть шанс!"
        joined = "".join(_norm(t) for t, *_ in words)
        if "шанс" not in joined and "естьша" not in joined:
            return False
        btn = _death_button(crop)
        if btn is None:
            log("вижу окно потопления, но не нашёл кнопку «Починить и вернуться»; OCR: "
                + " ".join(t for t, *_ in words)[:200])
            return False
        target = to_full(btn[0] * k, btn[1] * k)
        log(f"корабль потоплен - жму «Починить и вернуться» ({target[0]}, {target[1]})")
        if not dry:
            io.click(*target)
        return True
    x, y, w, h = label
    cx, cy = x + w / 2, y + h / 2
    # круглая кнопка (зеленоватое кольцо) над подписью - ищем её центр по цветным пикселям
    lx, ly = cx / k, cy / k      # подпись в координатах crop
    lh = h / k
    area = crop[max(0, int(ly - lh * 12)):max(0, int(ly - lh)),
                max(0, int(lx - lh * 6)):int(lx + lh * 6)]
    target = None
    if area.size:
        hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV)
        mask = (hsv[..., 1] > 70) & (hsv[..., 2] > 60)
        ys, xs = np.nonzero(mask)
        if len(xs) > 50:
            target = to_full((max(0, lx - lh * 6) + xs.mean()) * k,
                             (max(0, ly - lh * 12) + ys.mean()) * k)
    if target is None:
        target = to_full(cx, cy - h * 5.5)
    log(f"корабль потоплен - жму «Починить и вернуться» ({target[0]}, {target[1]})")
    if not dry:
        io.click(*target)
    return True


def _death_button(crop):
    """Зелёная круглая кнопка «Починить и вернуться» (справа от неё - красная «Выйти»).
    Ищет крупное почти круглое зелёное пятно. Возвращает (x, y) в координатах crop или None."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (35, 60, 50), (95, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, stats, cent = cv2.connectedComponentsWithStats(mask)
    side = max(crop.shape[:2])
    best = None
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (side * 0.05 <= w <= side * 0.25 and 0.75 <= w / max(h, 1) <= 1.33):
            continue
        if best is None or area > best[0]:
            best = (area, (int(x + w / 2), int(y + h / 2)))
    return best[1] if best else None


def _find_hammer(full):
    """Молоток: ((x, y), масштаб) или None."""
    global _repair_tpl
    if _repair_tpl is None:
        img = cv2.imread(REPAIR_TEMPLATE, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        _repair_tpl = img
    gray = cv2.cvtColor(full, cv2.COLOR_BGR2GRAY)
    top = int(gray.shape[0] * 0.7)      # панель умений - в самом низу экрана
    gray = gray[top:]
    base = _scale_guess(full)
    # в фоновом браузере панель рисуется почти в натуральную величину, хотя окно меньше
    # 2560 px - поэтому кроме масштаба от размера окна пробуем и обычные масштабы
    scales = sorted({round(base * kk, 2) for kk in (0.85, 0.92, 1.0, 1.08, 1.15)}
                    | {0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2})   # мелкие - панель сбоку в углу
    best = (0.0, None)
    for sc in scales:
        t = cv2.resize(_repair_tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        if t.shape[0] < 10 or t.shape[0] >= gray.shape[0]:
            continue
        _, score, _, p = cv2.minMaxLoc(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED))
        if score > best[0]:
            best = (score, (int(p[0] + t.shape[1] / 2), int(top + p[1] + t.shape[0] / 2)), sc)
    return (best[1], best[2]) if best[0] >= REPAIR_THRESHOLD else None


# Полоска HP на панели - левее и выше молотка «Ремонт» (молоток - ориентир).
# Цвет полоски меняется (зелёная при полном HP, красная при малом), поэтому читаем
# надпись "HP: 5.000 / 40.000" OCR-ом: белый текст -> чёрный на белом.
REPAIR_BELOW = 0.6         # HP меньше этой доли -> жмём «Ремонт»
HP_CHECK = 10.0            # как часто смотреть на HP, сек
REPAIR_COOLDOWN = 120.0    # после нажатия «Ремонт» столько сек не жмём снова (идёт ремонт)


def read_hp(full):
    """(текущее, максимум) HP с панели или None, если не прочиталось."""
    if not OCR_OK:
        return None
    found = _find_hammer(full)
    if not found:
        return None
    (hx, hy), sc = found
    crop = full[max(0, int(hy - 230 * sc)):max(0, int(hy - 60 * sc)),
                max(0, int(hx - 750 * sc)):max(0, int(hx - 150 * sc))]
    if crop.size == 0:
        return None
    big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)
    text_mask = ((hsv[..., 1] < 70) & (hsv[..., 2] > 150)).astype(np.uint8) * 255
    try:
        text = ocr_win.read_text(cv2.cvtColor(255 - text_mask, cv2.COLOR_GRAY2BGR))
    except Exception:
        return None
    # "НР: 43.255 / 40.000" (Н/Р могут прочитаться кириллицей); точки - разделители тысяч
    m = re.search(r"[HНhн][PРpр]\W*(\d[\d.,]*)\D{1,4}(\d[\d.,]*)", text)
    if not m:
        return None
    cur, mx = (int(re.sub(r"\D", "", g)) for g in m.groups())
    if mx <= 0 or cur > mx * 2:
        return None
    return cur, mx


def _handle_dont_show(io, full, words, to_full, dry):
    """Окно вида «Добро пожаловать на борт» с галочкой «Больше не показывать»:
    ставит галочку и жмёт «Нет, спасибо» (или крестик)."""
    like = _like
    box = None   # (x, y, h) начала надписи "Больше не показывать"
    for i, (text, x, y, w, h) in enumerate(words):
        if like(text, "показывать", 0.7):
            cy = y + h / 2
            # начало строки: самое левое слово на той же высоте, не дальше ~8 высот букв
            same = [(x2, h2) for t2, x2, y2, w2, h2 in words
                    if abs(y2 + h2 / 2 - cy) < h * 0.6 and x - h * 8 < x2 <= x]
            sx, sh = min(same) if same else (x, h)
            box = (sx, cy, h)
            break
    if box is None:
        return False

    # галочка - квадратик слева от надписи
    bx, by, bh = box
    cb = to_full(bx - bh * 1.2, by)
    log(f"окно «Больше не показывать» - ставлю галочку ({cb[0]}, {cb[1]})")
    if not dry:
        io.click(*cb)
        time.sleep(0.4)

    no_btn = None
    for i, (text, x, y, w, h) in enumerate(words):
        if like(text, "спасибо", 0.6) or (like(text, "нет", 0.66) and i + 1 < len(words)
                                            and like(words[i + 1][0], "спасибо", 0.5)):
            no_btn = to_full(x + w / 2, y + h / 2)
            break
    if no_btn:
        log(f"закрываю окно кнопкой «Нет, спасибо» ({no_btn[0]}, {no_btn[1]})")
        if not dry:
            io.click(*no_btn)
        return True
    close = find_close_button(full)
    if close:
        log(f"закрываю окно крестиком ({close[0]}, {close[1]})")
        if not dry:
            io.click(*close)
    else:
        log("закрываю окно клавишей Esc")
        if not dry:
            io.key("Escape")
    return True


CLAIM_REPEAT = 15.0   # та же кнопка «Получить» в течение N сек после нажатия -> закрыть окно
_last_claim = (0.0, (0, 0))


def handle_popups(io, full, dry=False):
    """Море закрыто окном: забрать подарок, иначе закрыть окно крестиком, иначе Esc."""
    found = handle_dialogs(io, full, dry)
    if found:
        return found
    global _last_claim
    claim = find_claim_button(full)
    if claim:
        t, pos = _last_claim
        # кнопка на том же месте сразу после нажатия - подарок уже забран, окно надо закрыть
        if time.time() - t < CLAIM_REPEAT and dist2(pos, claim[:2]) < 40 ** 2:
            log("подарок забран - закрываю окно")
            claim = None
    if claim:
        log(f"вижу кнопку «{claim[2]}» - забираю подарок")
        if not dry:
            io.click(claim[0], claim[1])
        _last_claim = (time.time(), claim[:2])
        return "claim"
    close = find_close_button(full)
    if close:
        log(f"закрываю окно крестиком ({close[0]}, {close[1]})")
        if not dry:
            io.click(*close)
        return "close"
    log("закрываю окно клавишей Esc")
    if not dry:
        io.key("Escape")
    return "esc"


def thumb(frame):
    """Уменьшенный серый кадр для оценки, сдвинулась ли камера."""
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (g.shape[1] // 4, g.shape[0] // 4)).astype(np.float32)


def view_shift(a, b):
    """Насколько (в пикселях экрана) сдвинулся обзор между двумя кадрами."""
    (dx, dy), _ = cv2.phaseCorrelate(a, b)
    return 4 * (dx * dx + dy * dy) ** 0.5


def new_heading(old=None):
    names = list(DIRECTIONS)
    if old is not None:
        names.remove(old)
    return random.choice(names)


COMPASS = ["север", "северо-восток", "восток", "юго-восток", "юг", "юго-запад", "запад", "северо-запад"]
BLOCKED_FOR = 60.0   # направление, где корабль упёрся, избегаем столько сек


def away_heading(blocked, now):
    """Курс подальше от направлений, где корабль недавно упирался (угол/край карты):
    в углу на северо-востоке -> юго-запад, а не случайная сторона, которая снова в стену."""
    recent = [COMPASS.index(n) for n, t in blocked.items() if now - t < BLOCKED_FOR]

    def gap(i):   # сколько шагов компаса до ближайшего "тупика"
        return min(min(abs(i - j), 8 - abs(i - j)) for j in recent)
    free = [i for i in range(8) if i not in recent]
    if not recent or not free:
        return new_heading()
    best = max(gap(i) for i in free)
    return COMPASS[random.choice([i for i in free if gap(i) == best])]


class DesktopIO:
    """Работа с вашим настоящим экраном и мышью (mss + pyautogui)."""
    wait_for_game = False   # поле моря не найдено -> берём SEA_REGION из настроек

    def __init__(self):
        set_dpi_aware()
        self.sct = mss.mss()

    def grab_full(self):
        mon = self.sct.monitors[1]   # основной монитор
        full = cv2.cvtColor(np.array(self.sct.grab(mon)), cv2.COLOR_BGRA2BGR)
        return full, (mon["left"], mon["top"])

    def grab_region(self):
        return grab_region(self.sct)

    def move(self, x, y, duration=0.0):
        pyautogui.moveTo(x, y, duration=duration)

    def click(self, x, y):
        pyautogui.moveTo(x, y, duration=0)
        pyautogui.click()

    def key(self, name):
        pyautogui.press(name.lower())

    def pump(self):
        pass

    def close(self):
        self.sct.close()


class NullControl:
    """Управление по умолчанию (консоль): без паузы, остановка - Ctrl+C."""
    def stopped(self):
        return False

    def paused(self):
        return False


def run_bot(io, ctl=None, dry=False):
    """Основной цикл сбора блёсток. io - откуда брать картинку и куда кликать
    (DesktopIO - ваш экран, BrowserIO - фоновый браузер), ctl - пауза/стоп."""
    global STATS
    ctl = ctl or NullControl()

    def nap(sec):
        """Пауза, во время которой продолжаем обслуживать браузер/интерфейс."""
        end = time.time() + sec
        while not ctl.stopped():
            io.pump()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 0.1))

    t_hp = 0.0         # последняя проверка HP
    t_repaired = 0.0   # когда последний раз жали «Ремонт»
    hp_ok = None       # читалось ли HP при прошлой проверке (для лога)
    repair_due = 0.0   # когда нажать молоток "Ремонт" (после потопления), 0 - не нужно

    # --- ищем поле моря ---
    t_wait_log = 0.0
    while not ctl.stopped():
        full, origin = io.grab_full()
        if auto_calibrate(full, offset=origin, quiet=io.wait_for_game):
            break
        if not io.wait_for_game:
            break
        if ctl.paused():
            nap(0.5)
            continue
        if time.time() - t_wait_log > 15:
            log("жду, пока откроется море (игра грузится или открыто окно)...")
            t_wait_log = time.time()
        # море видно кусками - значит, игра загружена, но поверх открыто окно (подарок и т.п.)
        if sea_fraction(full) > 0.03:
            if handle_popups(io, full, dry) == "death":
                repair_due = time.time() + 3.0
            nap(POPUP_RETRY)
        else:
            nap(2)
    if ctl.stopped():
        return

    tpl = load_popup_template()
    heading = new_heading()
    t_patrol = 0.0
    t_heading = time.time()
    patrolling = False         # последний клик был "плыть по курсу"
    t_activity = time.time()   # когда последний раз видели/собирали блёстку
    if dry:
        log("Режим --dry: клики отключены, только лог.")

    target = None        # текущая блёстка (x, y)
    t_click = t_first = t_seen = 0.0
    target_clicks = 0
    last_pos = None      # где собрали прошлую - корабль сейчас там
    ignored = []         # [(x, y, until)]

    # Сторож неподвижности: сравниваем кадры раз в STILL_CHECK сек.
    still_thumb = None
    t_still_check = 0.0
    t_moved = time.time()      # когда последний раз видели, что обзор едет (или кликнули)

    def do_click(p):
        nonlocal t_moved
        if not dry:
            io.move(p[0] + random.randint(-1, 1), p[1] + random.randint(-1, 1),
                    duration=random.uniform(0.12, 0.3))
            io.click(p[0], p[1])
        t_moved = time.time()  # даём кораблю время тронуться

    def locate_near(p, radius):
        """Свежий снимок -> ближайшая к p блёстка (или None) и время снимка."""
        t = time.time()
        found = [q for q in find_sparkles(io.grab_region(), offset=SEA_REGION[:2])
                 if dist2(q, p) <= radius ** 2]
        return (min(found, key=lambda q: dist2(q, p)) if found else None), t

    def click_sparkle(p, jitter=0):
        """Клик по блёстке с поправкой на движение карты: подводим курсор, делаем два
        свежих снимка, считаем скорость блёстки на экране и бьём с упреждением."""
        nonlocal t_moved
        if dry:
            t_moved = time.time()
            return p
        io.move(p[0], p[1], duration=random.uniform(0.08, 0.18))
        p1, t1 = locate_near(p, TRACK_RADIUS * 2)
        if p1 is None:
            aim = p
        else:
            time.sleep(AIM_SAMPLE)
            p2, t2 = locate_near(p1, TRACK_RADIUS)
            if p2 is None:
                aim = p1
            else:
                dt = max(t2 - t1, 1e-3)
                vx, vy = (p2[0] - p1[0]) / dt, (p2[1] - p1[1]) / dt
                lead = time.time() - t2 + AIM_LATENCY
                aim = (int(p2[0] + vx * lead), int(p2[1] + vy * lead))
        if jitter:
            aim = (aim[0] + random.randint(-jitter, jitter), aim[1] + random.randint(-jitter, jitter))
        io.click(aim[0], aim[1])
        t_moved = time.time()
        return aim

    def finish(reason):
        nonlocal last_pos, target, t_activity
        if tpl is None:
            stats.sparkles += 1   # без образца надписи считаем по исчезновению
        log(f"собрано ({reason})")
        # пару секунд не трогаем это место - блёстка может ещё догорать на экране
        ignored.append((target[0], target[1], time.time() + 3.0))
        last_pos = target
        target = None
        t_activity = time.time()
        nap(random.uniform(*PAUSE_AFTER_COLLECT))

    STATS = stats = Stats()
    if tpl is not None:
        if OCR_OK:
            log("Подсчёт наград: включён (OCR Windows)")
        else:
            log("!!! OCR НЕ РАБОТАЕТ - бот не видит названия ресурсов, НЕ нажмёт «Починить и "
                "вернуться» после потопления, не заберёт подарки и не закроет окна. "
                "Причина: " + OCR_PROBLEM)
    reward_tries = 0           # сколько ещё кадров пытаться прочитать текущую награду
    popup_was = False
    was_paused = False
    t_popup = 0.0              # последняя попытка закрыть окно поверх моря
    t_tutorial = 0.0           # последняя проверка окна "Больше не показывать"
    n_frames, t_fps = 0, time.time()   # скорость цикла (кадров/с)
    t_diag = 0.0               # последняя запись диагностики "вижу, но не беру"
    t_blocked_log = 0.0        # чтобы не спамить "во все стороны монстры"
    stuck_n = 0                # сколько раз подряд "корабль стоит"
    blocked = {}               # направление -> когда в той стороне корабль упёрся
    log("Старт сбора блёсток.")

    while not ctl.stopped():
        if ctl.paused():
            if not was_paused:
                log("пауза")
                was_paused = True
            target = None
            nap(0.2)
            continue
        if was_paused:
            log("продолжаю")
            was_paused = False
            t_moved = t_activity = time.time()
            t_patrol = 0.0

        io.pump()
        now = time.time()
        # горячая клавиша R - ремонт сейчас (младший бит: была нажата с прошлой проверки)
        if ctypes.windll.user32.GetAsyncKeyState(0x52) & 1 and not repair_due:
            log("нажата R - ремонт")
            repair_due = now
        ignored = [g for g in ignored if g[2] > now]
        frame = io.grab_region()

        n_frames += 1
        if now - stats.t_report > STATS_EVERY:
            for line in stats.summary().splitlines():
                log(line)
            fps = n_frames / max(1.0, now - t_fps)
            log(f"  скорость: {fps:.1f} кадров/с" + (" - МЕДЛЕННО, компьютер не успевает "
                "(норма от 2)" if fps < 1.5 else ""))
            n_frames, t_fps = 0, now
            stats.t_report = now

        # --- надпись "Вы собрали блёстку. Награда: N ..." ---
        popup = find_popup(frame, tpl)
        if popup is not None:
            if not popup_was:
                stats.sparkles += 1
                reward_tries = 3 if OCR_OK else 0
            # читаем награду (до 3 кадров - надпись может ещё проявляться)
            if reward_tries > 0:
                reward, line = read_reward(frame, popup)
                if reward:
                    stats.add_reward(*reward)
                    log(f"+{reward[0]} {reward[1]}  (за сессию: {stats.rewards[reward[1]]})")
                    reward_tries = 0
                else:
                    reward_tries -= 1
                    if reward_tries == 0:
                        stats.unread += 1
                        log("награду прочитать не удалось")
                        save_ocr_fail(line)
            if not popup_was:
                if target is not None:
                    finish("надпись о сборе")
                else:
                    log("надпись о сборе")
                if POPUP_CLOSE_CLICK and not dry:
                    io.click(*popup)
        # надпись висит пару секунд - засчитываем один раз и сразу ищем следующую
        popup_was = popup is not None

        # --- ремонт: клавиша R в игре (молоток искать не нужно) ---
        if repair_due and now >= repair_due:
            log("жму R - ремонт")
            if not dry:
                io.key("r")
                ctypes.windll.user32.GetAsyncKeyState(0x52)   # своё нажатие - не горячая клавиша
            repair_due = 0.0
            t_repaired = now

        # --- корабль побит (без потопления) - тоже ремонт ---
        if not repair_due and now - t_hp > HP_CHECK and now - t_repaired > REPAIR_COOLDOWN:
            t_hp = now
            hp = read_hp(io.grab_full()[0])
            # в лог - только когда меняется "читается / не читается" (проверка идёт каждые HP_CHECK сек)
            if hp is None and hp_ok is not False:
                log(f"HP на панели не читается (закрыто окном?) - проверю снова через {HP_CHECK:.0f} сек")
            elif hp and hp_ok is not True:
                log(f"HP на панели читается: {hp[0]} / {hp[1]}")
            hp_ok = hp is not None
            if hp and hp[0] < hp[1] * REPAIR_BELOW:
                log(f"HP {hp[0]} / {hp[1]} - нужен ремонт")
                repair_due = now

        # --- маленькие окна: потопление, "Больше не показывать" ---
        if now - t_tutorial > TUTORIAL_RETRY:
            t_tutorial = now
            full, _ = io.grab_full()
            found = handle_dialogs(io, full, dry)
            if found:
                if found == "death":
                    repair_due = now + 3.0
                    t_tutorial = now - TUTORIAL_RETRY + 2.0   # быстро проверить, закрылось ли окно
                target = None
                t_moved = t_activity = now
                nap(1.0)
                continue

        # --- море закрыто окном (подарок, рынок...) - сначала разбираемся с окном ---
        if sea_fraction(frame) < COVERED_LIMIT:
            if now - t_popup > POPUP_RETRY:
                full, _ = io.grab_full()
                if handle_popups(io, full, dry) == "death":
                    repair_due = now + 3.0
                t_popup = now
            target = None
            t_moved = t_activity = now   # пока окно открыто, "стоит" и "нет блёсток" не считаем
            nap(0.5)
            continue

        pts = find_sparkles(frame, offset=SEA_REGION[:2])
        monsters = find_monsters(frame, offset=SEA_REGION[:2], sparkles=pts)

        # --- корабль едет или стоит? ---
        if now - t_still_check >= STILL_CHECK:
            cur = thumb(frame)
            if still_thumb is not None and view_shift(still_thumb, cur) >= STUCK_SHIFT:
                t_moved = now
            still_thumb, t_still_check = cur, now
        standing = now - t_moved > STILL_TIMEOUT

        if target is None:
            raw = pts
            pts = [p for p in pts if all(dist2(p, g) > TRACK_RADIUS ** 2 for g in ignored)]
            n_ign = len(raw) - len(pts)
            # зелёные части монстров, текст, полоски HP - не блёстки
            pts = real_sparkles(frame, pts, SEA_REGION[:2])
            n_mon = len(raw) - n_ign - len(pts)
            if raw and not pts and now - t_diag > DIAG_EVERY:
                t_diag = now
                log(f"вижу похожих на блёстки: {len(raw)}, но не беру: в пропущенных {n_ign}, "
                    f"часть монстра/корабля/текста {n_mon}")
                save_diag(frame, raw, monsters, ignored, "skip")
            elif not raw and now - t_diag > DIAG_EVERY:
                lim = SPARKLE_BOX + 2 * OBSTACLE_PAD   # мелкие "препятствия" - это сами пятна
                big = [z for z in monsters if z[2] - z[0] > lim or z[3] - z[1] > lim]
                loose = find_sparkles_loose(frame, SEA_REGION[:2], big)
                if loose:
                    t_diag = now
                    log(f"похожих на блёстку пятен: {loose}, но по цвету/размеру не подходят "
                        f"- сохранил кадр в {DIAG_DIR}")
                    save_diag(frame, [], monsters, ignored, "color")
            if pts:
                stuck_n = 0
                # ближайшая к кораблю (к месту прошлого сбора)
                ref = last_pos or pts[0]
                target = min(pts, key=lambda p: dist2(p, ref))
                click_sparkle(target)
                patrolling = False
                target_clicks = 1
                t_click = t_first = t_seen = now
                t_activity = now
                log(f"цель ({target[0]}, {target[1]}), клик; блёсток на экране: {len(pts)}")
            elif now - t_activity > PATROL_AFTER and (now - t_patrol > PATROL_EVERY or standing):
                # блёсток нет - плывём дальше в выбранном направлении
                if standing and patrolling:
                    blocked[heading] = now
                    heading = away_heading(blocked, now)
                    t_heading = now
                    log(f"корабль стоит (край карты/остров?) - пробую другую сторону: {heading}")
                    stuck_n += 1
                    if stuck_n == 4:
                        # долго не можем сдвинуться - вдруг окно, которое бот не узнаёт
                        full, _ = io.grab_full()
                        os.makedirs(DIAG_DIR, exist_ok=True)
                        name = os.path.join(DIAG_DIR, time.strftime("%Y%m%d-%H%M%S") + "-stuck.png")
                        cv2.imwrite(name, full)
                        txt = ""
                        if OCR_OK:
                            try:
                                hh, ww = full.shape[:2]
                                txt = " ".join(w[0] for w in ocr_win.read_words(
                                    full[int(hh * 0.2):int(hh * 0.85), int(ww * 0.26):int(ww * 0.74)]))
                            except Exception:
                                pass
                        log(f"корабль давно стоит - сохранил кадр {name}; текст на экране: {txt[:200]}")
                elif now - t_heading > PATROL_TURN_EVERY:
                    heading = new_heading(heading)
                    t_heading = now
                    log(f"меняю курс на {heading}")
                p = direction_point(heading, monsters)
                if p is None:
                    # пробуем все остальные направления по очереди
                    for name in random.sample(list(DIRECTIONS), len(DIRECTIONS)):
                        p = direction_point(name, monsters)
                        if p is not None:
                            log(f"в той стороне препятствия - поворачиваю на {name}")
                            heading, t_heading = name, now
                            break
                if p is not None:
                    do_click(p)
                    patrolling = True
                else:
                    if now - t_blocked_log > 15:
                        log("со всех сторон препятствия - жду")
                        t_blocked_log = now
                    t_moved = now   # не считать это "кораблём, который стоит"
                if t_patrol == 0 or now - t_patrol >= PATROL_EVERY * 2:
                    log(f"блёсток нет - плыву на {heading}")
                t_patrol = now
                last_pos = None
        else:
            near = [p for p in pts if dist2(p, target) <= TRACK_RADIUS ** 2]
            if near:
                target = min(near, key=lambda p: dist2(p, target))  # следим за смещением
                t_seen = now
                if now - t_first > GIVE_UP_AFTER:
                    log(f"не собирается за {GIVE_UP_AFTER:.0f} с - пропускаю")
                    ignored.append((target[0], target[1], now + IGNORE_FOR))
                    target = None
                elif (standing or now - t_click > RECLICK_AFTER) and \
                        real_sparkles(frame, [target], SEA_REGION[:2]):
                    if target_clicks < MAX_CLICKS_PER_TARGET:
                        # корабль стоит / не доплыл - клик рядом с блёсткой, а не в ту же точку
                        click_sparkle(target, jitter=6 * (target_clicks - 1) if standing else 0)
                        target_clicks += 1
                        t_click = now
                        why = "корабль стоит" if standing else "долго не доплывает"
                        log(f"{why} - кликаю по блёстке ещё раз, попытка #{target_clicks}")
                    elif standing:
                        # не получается - бросаем её и уплываем в другую сторону
                        log("до блёстки не доплыть - пропускаю, плыву в другую сторону")
                        ignored.append((target[0], target[1], now + IGNORE_FOR))
                        target = None
                        heading = new_heading(heading)
                        t_heading = now
                        t_activity = t_patrol = 0.0
            elif now - t_seen > GONE_AFTER:
                # Корабль мог просто накрыть блёстку. Смотрим на это место ещё
                # CONFIRM_GONE сек: если она снова видна - продолжаем ждать её.
                back = None
                t_end = time.time() + CONFIRM_GONE
                popup_hit = False
                while time.time() < t_end and back is None and not ctl.stopped():
                    nap(SCAN_INTERVAL)
                    f2 = io.grab_region()
                    if find_popup(f2, tpl) is not None:
                        popup_hit = True   # обработается в основном цикле
                        break
                    again = find_sparkles(f2, offset=SEA_REGION[:2])
                    near = [p for p in again if dist2(p, target) <= TRACK_RADIUS ** 2]
                    if near:
                        back = min(near, key=lambda p: dist2(p, target))
                if popup_hit:
                    continue
                if back is not None:
                    target, t_seen = back, time.time()
                    log("блёстка снова видна (была под кораблём) - жду дальше")
                    continue
                finish("исчезла")
                continue

        nap(SCAN_INTERVAL)


def finish_session():
    """Печатает итог и дописывает его в sessions_log.csv."""
    if STATS is None:
        return
    for line in STATS.summary().splitlines():
        log(line)
    try:
        STATS.save()
        log(f"Итог сессии дописан в {SESSIONS_LOG}")
    except OSError as e:
        log(f"Не удалось записать {SESSIONS_LOG}: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="не кликать, только показывать находки")
    ap.add_argument("--test", metavar="IMG", help="проверить поиск на скриншоте")
    a = ap.parse_args()
    if a.test:
        run_test(a.test)
        sys.exit()
    io = DesktopIO()
    try:
        print("Старт через 3 сек. Ctrl+C или мышь в левый верхний угол - стоп.")
        time.sleep(3)
        run_bot(io, dry=a.dry)
    except (KeyboardInterrupt, pyautogui.FailSafeException):
        print("\nОстановлено.")
    finally:
        io.close()
        finish_session()

"""Распознавание текста встроенным OCR Windows (локально, без интернета)."""
import asyncio

import cv2
import numpy as np
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap, BitmapAlphaMode
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

_engine = None

# OCR Windows асинхронный. В потоке бота уже крутится цикл asyncio от Playwright,
# и второй asyncio.run() там падает. Поэтому все вызовы OCR идут через свой поток.
from concurrent.futures import ThreadPoolExecutor
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")


def _run(coro_fn, *args):
    return _executor.submit(lambda: asyncio.run(coro_fn(*args))).result()


def available():
    try:
        return OcrEngine.is_language_supported(Language("ru"))
    except Exception:
        return False


def _get_engine():
    global _engine
    if _engine is None:
        _engine = OcrEngine.try_create_from_language(Language("ru"))
    return _engine


async def _recognize(bgr):
    bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    h, w = bgra.shape[:2]
    writer = DataWriter()
    writer.write_bytes(bytes(np.ascontiguousarray(bgra).tobytes()))
    bmp = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    return await _get_engine().recognize_async(bmp)


def read_text(bgr):
    """Возвращает распознанный текст картинки (BGR)."""
    return _run(_recognize, bgr).text


async def _recognize_result(bgr):
    bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    h, w = bgra.shape[:2]
    writer = DataWriter()
    writer.write_bytes(bytes(np.ascontiguousarray(bgra).tobytes()))
    bmp = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    return await _get_engine().recognize_async(bmp)


def read_words(bgr):
    """Слова с координатами: [(текст, x, y, w, h), ...] в пикселях картинки."""
    result = _run(_recognize_result, bgr)
    words = []
    for line in result.lines:
        for wd in line.words:
            r = wd.bounding_rect
            words.append((wd.text, r.x, r.y, r.width, r.height))
    return words

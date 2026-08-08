import logging
import os
import tempfile
from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.id3 import ID3, POPM, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

logger = logging.getLogger(__name__)

# --- КОНСТАНТЫ И КАРТЫ POPM ---
_PRIMARY_MP3_RATING_MAP = {0: 0, 1: 13, 2: 1, 3: 54, 4: 64, 5: 118, 6: 128, 7: 186, 8: 196, 9: 242, 10: 255}
_ALTERNATIVE_MP3_RATING_MAP = {0: 0, 2: 1, 4: 64, 6: 128, 8: 196, 10: 255}
_PICARD_MP3_RATING_MAP = {0: 0, 2: 51, 4: 102, 6: 153, 8: 204, 10: 255}
_KNOWN_PRIMARY_RATING_PLAYERS = ["MusicBee", "no@email"]
_RATING_EMAIL = "no@email"
# --- Теги лайка в формате MusicBee (бинарный like: "L"=love, "0"=нет лайка) ---
_LIKE_TAG_ID3  = "LOVE RATING"                            # TXXX:LOVE RATING (MP3/AIFF)
_LIKE_TAG_XIPH = "LOVE RATING"                            # Vorbis Comment (FLAC/OGG/OPUS)
_LIKE_TAG_MP4  = "----:com.apple.iTunes:LOVERATING"      # MPEG-4 atom (M4A)
_LIKE_VALUE_ON = "L"                                      # MusicBee пишет "L" для Love

# --- БАЗОВЫЙ КЛАСС СТРАТЕГИИ ---
class RatingHandler:
    def read_rating(self, file_path: str) -> int | None: raise NotImplementedError
    def read_starred(self, file_path: str) -> int: raise NotImplementedError
    def read_all(self, file_path: str): raise NotImplementedError
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None) -> None: raise NotImplementedError
    def write_all(self, file_path: str, rating: int, starred: bool) -> None: raise NotImplementedError
    def _load(self, file_path: str): raise NotImplementedError

    def _safe_save(self, audio, file_path: str):
        """Атомарная запись файла для 100% защиты от бинарной порчи."""
        dir_name = os.path.dirname(file_path)
        # Создаем временный файл в той же директории (это важно для атомарности os.replace)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".ha_sync_tmp_")
        try:
            os.close(fd)
            # 1. Сохраняем теги во временный файл (mutagen пишет весь файл целиком)
            audio.save(tmp_path)
            # 2. Атомарно заменяем оригинальный файл временным (занимает доли миллисекунды)
            os.replace(tmp_path, file_path)
        except Exception:
            # Если на этапе записи упала ошибка (например, нет места на диске) - удаляем мусор
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise # Пробрасываем ошибку дальше, чтобы сработал try-except в вызывающем методе

# --- КОНВЕРСИИ POPM ---
def _popm_rating_to_internal(popm_rating, email=None):
    if popm_rating == 0 or popm_rating is None: return None
    if email in _KNOWN_PRIMARY_RATING_PLAYERS:
        for internal_rating, popm_value in _PRIMARY_MP3_RATING_MAP.items():
            if popm_rating == popm_value: return internal_rating
    for map_to_try in [_ALTERNATIVE_MP3_RATING_MAP, _PICARD_MP3_RATING_MAP]:
        for internal_rating, popm_value in map_to_try.items():
            if popm_rating == popm_value: return internal_rating
    return min(10, max(1, round((popm_rating / 255) * 9 + 1)))

def _internal_rating_to_popm(internal_rating):
    if internal_rating == 0 or internal_rating is None: return 0
    return _PRIMARY_MP3_RATING_MAP.get(internal_rating, 0)

# --- СТРАТЕГИИ ID3 (MP3 / AIFF) ---
class ID3Handler(RatingHandler):
    def read_rating(self, file_path: str) -> int | None:
        try:
            audio = self._load(file_path)
            if audio and audio.tags:
                popm_frames = audio.tags.getall("POPM")
                if popm_frames:
                    # При чтении приоритет отдаем своему тегу, потом любому другому
                    nav_popm = next((f for f in popm_frames if f.email == _RATING_EMAIL), None)
                    if nav_popm: return _popm_rating_to_internal(nav_popm.rating, _RATING_EMAIL)
                    return _popm_rating_to_internal(popm_frames[0].rating, popm_frames[0].email)
        except Exception as e: logger.error(f"ID3 read rating err ({file_path}): {e}")
        return None

    def read_starred(self, file_path: str) -> int:
        try:
            audio = self._load(file_path)
            if audio and audio.tags:
                like_frames = audio.tags.getall(f"TXXX:{_LIKE_TAG_ID3}")
                if like_frames: return 1 if str(like_frames[0].text[0]) == _LIKE_VALUE_ON else 0
        except Exception: pass
        return 0

    # ОПТИМИЗАЦИЯ: Чтение рейтинга и лайка за один раз
    def read_all(self, file_path: str):
        try:
            audio = self._load(file_path)
            if audio and audio.tags:
                rating = None
                starred = 0
                
                popm_frames = audio.tags.getall("POPM")
                if popm_frames:
                    nav_popm = next((f for f in popm_frames if f.email == _RATING_EMAIL), None)
                    if nav_popm: rating = _popm_rating_to_internal(nav_popm.rating, _RATING_EMAIL)
                    else: rating = _popm_rating_to_internal(popm_frames[0].rating, popm_frames[0].email)
                
                like_frames = audio.tags.getall(f"TXXX:{_LIKE_TAG_ID3}")
                if like_frames: starred = 1 if str(like_frames[0].text[0]) == _LIKE_VALUE_ON else 0
                
                return rating, starred
        except Exception as e: logger.error(f"ID3 read all err ({file_path}): {e}")
        return None, 0

    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None) -> None:
        try:
            audio = self._load(file_path)
            if audio is None: return
            if audio.tags is None: audio.tags = ID3()

            # --- Условие 1: Если передан рейтинг ---
            if rating is not None:
                popm_frames = audio.tags.getall("POPM")
                if rating == 0:
                    if popm_frames: audio.tags.delall("POPM")
                else:
                    popm_rating = _internal_rating_to_popm(rating)
                    if popm_frames:
                        for frame in popm_frames:
                            frame.rating = popm_rating
                            frame.count = 0
                    else:
                        audio.tags.add(POPM(email=_RATING_EMAIL, rating=popm_rating, count=0))

            # --- Условие 2: Если передан лайк ---
            if starred is not None:
                audio.tags.delall(f"TXXX:{_LIKE_TAG_ID3}")
                value = _LIKE_VALUE_ON if starred else "0"
                audio.tags.add(TXXX(encoding=3, desc=_LIKE_TAG_ID3, text=value))
            
            # --- Единая атомарная запись ---
            self._safe_save(audio, file_path)
        except Exception as e: logger.error(f"ID3 write tags err ({file_path}): {e}")

class MP3Handler(ID3Handler):
    def _load(self, file_path): return MP3(file_path, ID3=ID3)

class AIFFHandler(ID3Handler):
    def _load(self, file_path): return AIFF(file_path)

# --- СТРАТЕГИЯ XIPH (FLAC, OGG, OPUS) ---
class XiphHandler(RatingHandler):
    def read_rating(self, file_path: str) -> int | None:
        try:
            audio = self._load(file_path)
            if audio:
                rating_raw = audio.get("RATING")
                if rating_raw:
                    xiph_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                    if xiph_rating == 0: return None
                    return max(1, min(10, round(xiph_rating / 10)))
        except Exception as e: logger.error(f"Xiph read rating err ({file_path}): {e}")
        return None

    def read_starred(self, file_path: str) -> int:
        try:
            audio = self._load(file_path)
            if audio and _LIKE_TAG_XIPH in audio:
                return 1 if str(audio[_LIKE_TAG_XIPH][0]) == _LIKE_VALUE_ON else 0
        except Exception: pass
        return 0

    # ОПТИМИЗАЦИЯ: Чтение рейтинга и лайка за один раз
    def read_all(self, file_path: str):
        try:
            audio = self._load(file_path)
            if audio:
                rating = None
                starred = 0
                rating_raw = audio.get("RATING")
                if rating_raw:
                    xiph_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                    if xiph_rating == 0: rating = None
                    else: rating = max(1, min(10, round(xiph_rating / 10)))
                if _LIKE_TAG_XIPH in audio:
                    starred = 1 if str(audio[_LIKE_TAG_XIPH][0]) == _LIKE_VALUE_ON else 0
                return rating, starred
        except Exception as e: logger.error(f"Xiph read all err ({file_path}): {e}")
        return None, 0

    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None) -> None:
        try:
            audio = self._load(file_path)
            if audio:
                # --- Условие 1: Если передан рейтинг ---
                if rating is not None:
                    if "RATING" in audio:
                        del audio["RATING"]
                    if rating > 0:
                        audio["RATING"] = str(max(10, min(100, rating * 10)))
                
                # --- Условие 2: Если передан лайк ---
                if starred is not None:
                    audio[_LIKE_TAG_XIPH] = _LIKE_VALUE_ON if starred else "0"

                # --- Единая атомарная запись ---
                self._safe_save(audio, file_path)
        except Exception as e: logger.error(f"Xiph write tags err ({file_path}): {e}")

    def _load(self, file_path): return MutagenFile(file_path)

# --- СТРАТЕГИЯ MP4 (M4A / AAC) ---
class MP4Handler(RatingHandler):
    _RATE_TAG = "----:com.apple.iTunes:RATE"

    def read_rating(self, file_path: str) -> int | None:
        try:
            audio = self._load(file_path)
            rating_raw = audio.tags.get(self._RATE_TAG) if audio.tags else None
            if rating_raw:
                m4a_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                if m4a_rating == 0: return None
                return max(1, min(10, round(m4a_rating / 10)))
        except Exception as e: logger.error(f"MP4 read rating err ({file_path}): {e}")
        return None

    def read_starred(self, file_path: str) -> int:
        try:
            audio = self._load(file_path)
            if audio.tags and _LIKE_TAG_MP4 in audio.tags:
                return 1 if audio.tags[_LIKE_TAG_MP4][0].decode('utf-8') == _LIKE_VALUE_ON else 0
        except Exception: pass
        return 0

    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None) -> None:
        try:
            audio = self._load(file_path)
            if audio.tags is None: audio.add_tags()
            
            # --- Условие 1: Если передан рейтинг ---
            if rating is not None:
                if self._RATE_TAG in audio.tags:
                    del audio.tags[self._RATE_TAG]
                if rating > 0:
                    m4a_rating = str(max(10, min(100, rating * 10)))
                    audio[self._RATE_TAG] = [m4a_rating.encode("utf-8")]
                
            # --- Условие 2: Если передан лайк ---
            if starred is not None:
                value = _LIKE_VALUE_ON if starred else "0"
                audio[_LIKE_TAG_MP4] = [bytes(value, 'utf-8')]

            # --- Единая атомарная запись ---
            self._safe_save(audio, file_path)
        except Exception as e: logger.error(f"MP4 write tags err ({file_path}): {e}")

    def _load(self, file_path): return MP4(file_path)

    # ОПТИМИЗАЦИЯ: Чтение рейтинга и лайка за один раз
    def read_all(self, file_path: str):
        try:
            audio = self._load(file_path)
            rating = None
            starred = 0
            if audio.tags:
                rating_raw = audio.tags.get(self._RATE_TAG)
                if rating_raw:
                    m4a_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                    if m4a_rating == 0: rating = None
                    else: rating = max(1, min(10, round(m4a_rating / 10)))
                if _LIKE_TAG_MP4 in audio.tags:
                    starred = 1 if audio.tags[_LIKE_TAG_MP4][0].decode('utf-8') == _LIKE_VALUE_ON else 0
            return rating, starred
        except Exception as e: logger.error(f"MP4 read all err ({file_path}): {e}")
        return None, 0

# --- РЕЕСТР И ФАСАД ---
HANDLER_REGISTRY = {
    ".mp3": MP3Handler(), ".aif": AIFFHandler(), ".aiff": AIFFHandler(),
    ".flac": XiphHandler(), ".ogg": XiphHandler(), ".opus": XiphHandler(),
    ".m4a": MP4Handler(),
}

def get_handler(file_path: str) -> RatingHandler | None:
    ext = os.path.splitext(file_path)[1].lower()
    return HANDLER_REGISTRY.get(ext)

def get_rating_from_file(file_path: str) -> int | None:
    handler = get_handler(file_path)
    return handler.read_rating(file_path) if handler else None

def get_starred_from_file(file_path: str) -> int:
    handler = get_handler(file_path)
    return handler.read_starred(file_path) if handler else 0

def set_tags_to_file(file_path: str, rating: int | None = None, starred: bool | None = None) -> None:
    handler = get_handler(file_path)
    if handler: handler.write_tags(file_path, rating, starred)

# НОВАЯ ФУНКЦИЯ ФАСАДА
def get_all_ratings_from_file(file_path: str):
    handler = get_handler(file_path)
    return handler.read_all(file_path) if handler else (None, 0)

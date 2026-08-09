import logging
import os
import shutil
import tempfile
from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.id3 import ID3, POPM, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.wave import WAVE
from mutagen.asf import ASF, ASFDWordAttribute, ASFUnicodeAttribute

logger = logging.getLogger(__name__)

# --- КОНСТАНТЫ И КАРТЫ POPM ---
_PRIMARY_MP3_RATING_MAP = {0: 0, 1: 13, 2: 1, 3: 54, 4: 64, 5: 118, 6: 128, 7: 186, 8: 196, 9: 242, 10: 255}
_ALTERNATIVE_MP3_RATING_MAP = {0: 0, 2: 1, 4: 64, 6: 128, 8: 196, 10: 255}
_PICARD_MP3_RATING_MAP = {0: 0, 2: 51, 4: 102, 6: 153, 8: 204, 10: 255}
_WMA_RATING_WRITE_MAP = {0: 0, 1: 1, 2: 1, 3: 25, 4: 25, 5: 50, 6: 50, 7: 75, 8: 75, 9: 99, 10: 99}
_WMA_RATING_READ_MAP = {0: 0, 1: 2, 25: 4, 50: 6, 75: 8, 99: 10}
_KNOWN_PRIMARY_RATING_PLAYERS = ["MusicBee", "no@email"]
_RATING_EMAIL = "no@email"
# --- Теги лайка в формате MusicBee (бинарный like: "L"=love, отсутствие тега или значение "0"=нет лайка) ---
_LIKE_TAG = "LOVE RATING"                            # Универсальный текстовый тег (MP3/AIFF/WAV, FLAC/OGG/OPUS/APE/WV)
_LIKE_TAG_ASF = "MUSICBEE/LOVE RATING"                   # WMA (ASF) атрибут MusicBee
_LIKE_TAG_MP4 = "----:com.apple.iTunes:LOVERATING"      # MPEG-4 atom (M4A)
_LIKE_VALUE_ON = "L"

# --- БАЗОВЫЙ КЛАСС СТРАТЕГИИ ---
class RatingHandler:
    def read_all(self, file_path: str): raise NotImplementedError
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None) -> None: raise NotImplementedError
    def _load(self, file_path: str): raise NotImplementedError

    def _safe_save(self, audio, file_path: str):
        """Атомарная запись файла для 100% защиты от бинарной порчи."""
        dir_name = os.path.dirname(file_path)
        # Создаем временный файл в той же директории
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".ha_sync_tmp_")
        try:
            os.close(fd)
            # 1. Копируем оригинальный файл целиком во временный (чтобы перенести аудиоданные!)
            # Используем copy (без '2'), чтобы избежать ошибок доступа на сетевых дисках (SMB/NFS).
            # Дата изменения файла все равно обновится на актуальную при вызове audio.save() ниже.
            shutil.copy(file_path, tmp_path)

            # 2. Сохраняем измененные теги во временный файл (mutagen перепишет теги в копии, не трогая аудио)
            audio.save(tmp_path)

            # --- ЗАЩИТА ОТ ПОТЕРИ ПИТАНИЯ ---
            # Принудительно сбрасываем буферы ОС на физический диск
            with open(tmp_path, 'rb') as f:
                os.fsync(f.fileno())

            # 3. Атомарно заменяем оригинальный файл временным
            os.replace(tmp_path, file_path)
        except Exception:
            # Если на этапе записи упала ошибка - удаляем мусор
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
                
                # ИСПРАВЛЕНИЕ: Железобетонное чтение TXXX лайков для AIFF/MP3
                # Ищем по всем TXXX фреймам, игнорируя регистр описания и лишние пробелы
                for frame in audio.tags.getall("TXXX"):
                    if frame.desc and frame.desc.strip().upper() == _LIKE_TAG.upper():
                        try:
                            val_str = str(frame.text[0]).strip().upper()
                            if val_str == _LIKE_VALUE_ON:
                                starred = 1
                            else:
                                starred = 0
                            break # Нашли наш фрейм, выходим из цикла
                        except Exception:
                            pass
                
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
                audio.tags.delall(f"TXXX:{_LIKE_TAG}")
                value = _LIKE_VALUE_ON if starred else "0"
                audio.tags.add(TXXX(encoding=3, desc=_LIKE_TAG, text=value))
            
            # --- Единая атомарная запись ---
            self._safe_save(audio, file_path)
        except Exception as e: 
            logger.error(f"ID3 write tags err ({file_path}): {e}")
            raise

class MP3Handler(ID3Handler):
    def _load(self, file_path): return MP3(file_path, ID3=ID3)

class AIFFHandler(ID3Handler):
    def _load(self, file_path): return AIFF(file_path)

class WAVHandler(ID3Handler):
    def _load(self, file_path): return WAVE(file_path)

# --- СТРАТЕГИЯ XIPH (FLAC, OGG, OPUS, APE) ---
class XiphHandler(RatingHandler):
    # ОПТИМИЗАЦИЯ: Чтение рейтинга и лайка за один раз
    def read_all(self, file_path: str):
        try:
            audio = self._load(file_path)
            if audio:
                rating = None
                starred = 0
                rating_raw = audio.get("RATING")
                if rating_raw:
                    # ИСПРАВЛЕНО: Добавлено str() для совместимости с APETextValue
                    xiph_rating = int(str(rating_raw[0] if isinstance(rating_raw, list) else rating_raw))
                    if xiph_rating == 0: rating = None
                    else: rating = max(1, min(10, round(xiph_rating / 10)))
                if _LIKE_TAG in audio:
                    # ИСПРАВЛЕНИЕ: Железобетонное чтение для Vorbis Comments / APE
                    try:
                        val_str = str(audio[_LIKE_TAG][0]).strip().upper()
                        starred = 1 if val_str == _LIKE_VALUE_ON else 0
                    except Exception:
                        starred = 0
                return rating, starred
        except Exception as e: logger.error(f"Xiph read all err ({file_path}): {e}")
        return None, 0

    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None) -> None:
        try:
            audio = self._load(file_path)
            if audio:
                # ЗАЩИТА: Если файл совсем без тегов, инициализируем пустой словарь
                if audio.tags is None:
                    audio.add_tags()
                # --- Условие 1: Если передан рейтинг ---
                if rating is not None:
                    if "RATING" in audio:
                        del audio["RATING"]
                    if rating > 0:
                        audio["RATING"] = str(max(10, min(100, rating * 10)))
                
                # --- Условие 2: Если передан лайк ---
                if starred is not None:
                    audio[_LIKE_TAG] = _LIKE_VALUE_ON if starred else "0"

                # --- Единая атомарная запись ---
                self._safe_save(audio, file_path)
        except Exception as e: 
            logger.error(f"Xiph write tags err ({file_path}): {e}")
            raise

    def _load(self, file_path): return MutagenFile(file_path)

# --- СТРАТЕГИЯ MP4 (M4A / AAC) ---
class MP4Handler(RatingHandler):
    _RATE_TAG = "----:com.apple.iTunes:RATE"
    
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
                    # ИСПРАВЛЕНИЕ: Железобетонное чтение для M4A атомов
                    try:
                        val_str = audio.tags[_LIKE_TAG_MP4][0].decode('utf-8').strip().upper()
                        starred = 1 if val_str == _LIKE_VALUE_ON else 0
                    except Exception:
                        starred = 0
            return rating, starred
        except Exception as e: logger.error(f"MP4 read all err ({file_path}): {e}")
        return None, 0
    
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
        except Exception as e: 
            logger.error(f"MP4 write tags err ({file_path}): {e}")
            raise

    def _load(self, file_path): return MP4(file_path)

# --- СТРАТЕГИЯ WMA (Windows Media Audio / ASF) ---
class ASFHandler(RatingHandler):
    _RATE_TAG = "WM/SharedUserRating"
    
    def read_all(self, file_path: str):
        try:
            audio = self._load(file_path)
            if audio:
                rating = None
                starred = 0
                
                # ФАКТ ИЗ ИСХОДНИКОВ mutagen 1.48.1: audio.get(key) проксируется в self.tags.get(key)
                rating_raw = audio.get(self._RATE_TAG)
                if rating_raw:
                    try:
                        raw_val = rating_raw[0].value
                        # Защита от байтов
                        if isinstance(raw_val, bytes):
                            wma_rating = int(raw_val.decode('utf-8', errors='ignore').strip())
                        else:
                            wma_rating = int(str(raw_val).strip())
                        
                        rating = _WMA_RATING_READ_MAP.get(wma_rating)
                        if rating is None: 
                            rating = max(1, min(10, round(wma_rating / 10)))
                        if rating == 0: rating = None
                    except Exception as e:
                        # БОЛЬШЕ НИКАКИХ СКРЫТЫХ ОШИБОК! Пишем в лог точную причину.
                        logger.error(f"ASF read rating err ({file_path}): {e}")
                        rating = None
                
                if _LIKE_TAG_ASF in audio:
                    try:
                        raw_val = audio[_LIKE_TAG_ASF][0].value
                        if isinstance(raw_val, bytes):
                            val_str = raw_val.decode('utf-8', errors='ignore').strip().upper()
                        else:
                            val_str = str(raw_val).strip().upper()
                        starred = 1 if val_str == _LIKE_VALUE_ON else 0
                    except Exception as e:
                        logger.error(f"ASF read like err ({file_path}): {e}")
                        starred = 0
                return rating, starred
        except Exception as e: 
            logger.error(f"ASF read all err ({file_path}): {e}")
        return None, 0
    
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None) -> None:
        try:
            audio = self._load(file_path)
            if audio.tags is None: audio.add_tags()
            
            if rating is not None:
                # ФАКТ ИЗ ИСХОДНИКОВ: del audio[key] проксируется в del self.tags[key]
                if self._RATE_TAG in audio:
                    del audio[self._RATE_TAG]
                if rating > 0:
                    wma_rating = _WMA_RATING_WRITE_MAP.get(rating, 0)
                    audio[self._RATE_TAG] = [ASFDWordAttribute(wma_rating)] 
                
            if starred is not None:
                value = _LIKE_VALUE_ON if starred else "0"
                audio[_LIKE_TAG_ASF] = [ASFUnicodeAttribute(value)]

            self._safe_save(audio, file_path)
        except Exception as e: 
            logger.error(f"ASF write tags err ({file_path}): {e}")
            raise

    def _load(self, file_path): return ASF(file_path)

    def _load(self, file_path): return ASF(file_path)

# --- РЕЕСТР И ФАСАД ---
HANDLER_REGISTRY = {
    ".flac": XiphHandler(), ".ogg": XiphHandler(), ".opus": XiphHandler(), ".ape": XiphHandler(), ".wv": XiphHandler(),
    ".aif": AIFFHandler(), ".aiff": AIFFHandler(),
    ".mp3": MP3Handler(),
    ".m4a": MP4Handler(),
    ".wav": WAVHandler(),
    ".wma": ASFHandler(),
}

def get_handler(file_path: str) -> RatingHandler | None:
    ext = os.path.splitext(file_path)[1].lower()
    return HANDLER_REGISTRY.get(ext)

def set_tags_to_file(file_path: str, rating: int | None = None, starred: bool | None = None) -> None:
    handler = get_handler(file_path)
    if handler: handler.write_tags(file_path, rating, starred)

def get_all_ratings_from_file(file_path: str):
    handler = get_handler(file_path)
    return handler.read_all(file_path) if handler else (None, 0)

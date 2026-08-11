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

_PRIMARY_MP3_RATING_MAP = {0: 0, 1: 13, 2: 1, 3: 54, 4: 64, 5: 118, 6: 128, 7: 186, 8: 196, 9: 242, 10: 255}
_ALTERNATIVE_MP3_RATING_MAP = {0: 0, 2: 1, 4: 64, 6: 128, 8: 196, 10: 255}
_PICARD_MP3_RATING_MAP = {0: 0, 2: 51, 4: 102, 6: 153, 8: 204, 10: 255}
_WMA_RATING_WRITE_MAP = {0: 0, 1: 1, 2: 1, 3: 25, 4: 25, 5: 50, 6: 50, 7: 75, 8: 75, 9: 99, 10: 99}
_WMA_RATING_READ_MAP = {0: 0, 1: 2, 25: 4, 50: 6, 75: 8, 99: 10}
_KNOWN_PRIMARY_RATING_PLAYERS = ["MusicBee", "no@email"]
_RATING_EMAIL = "no@email"
# --- Теги лайка в формате MusicBee ---
_LIKE_TAG = "LOVE RATING"
_LIKE_TAG_ASF = "musicbee/LOVE RATING"
_LIKE_TAG_MP4 = "----:com.apple.iTunes:LOVERATING"
_LIKE_VALUE_ON = "L"
_LIKE_VALUE_OFF = "0"
_LIKE_VALUE_BAN = "B"

# --- БАЗОВЫЙ КЛАСС СТРАТЕГИИ ---
class RatingHandler:
    def read_all(self, file_path: str): raise NotImplementedError
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> None: raise NotImplementedError
    def _load(self, file_path: str): raise NotImplementedError

    def _safe_save(self, audio, file_path: str, atomic_save: bool = False):
        if atomic_save:
            # --- АТОМАРНЫЙ РЕЖИМ (Copy-Save-Replace) ---
            # 100% защита от бинарной порчи при гонках и сбоях питания (для SMB/сети).
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
                with open(tmp_path, 'r+b') as f:
                    os.fsync(f.fileno())
    
                # 3. Атомарно заменяем оригинальный файл временным
                os.replace(tmp_path, file_path)
            except Exception:
                # Если на этапе записи упала ошибка - удаляем мусор
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise # Пробрасываем ошибку дальше, чтобы сработал try-except в вызывающем методе

        else:
            # --- ШТАТНЫЙ РЕЖИМ (In-place) ---
            # Прямая запись тегов в файл.
            audio.save(file_path)
            
            # --- ЗАЩИТА ОТ ПОТЕРИ ПИТАНИЯ ---
            # Принудительно сбрасываем буферы ОС на физический диск
            with open(file_path, 'r+b') as f:
                os.fsync(f.fileno())

# --- КОНВЕРСИИ POPM ---
def _popm_rating_to_internal(popm_rating, email=None):
    if popm_rating == 0 or popm_rating is None: return None
    if email in _KNOWN_PRIMARY_RATING_PLAYERS:
        for internal_rating, popm_value in _PRIMARY_MP3_RATING_MAP.items():
            if popm_rating == popm_value: return internal_rating
    for map_to_try in [_ALTERNATIVE_MP3_RATING_MAP, _PICARD_MP3_RATING_MAP]:
        for internal_rating, popm_value in map_to_try.items():
            if popm_rating == popm_value: return internal_rating
    # Если рейтинг <= 100, это шкала 0-100 (WAV/AIFF из MusicBee)
    if popm_rating <= 100:
        return max(1, min(10, round(popm_rating / 10)))
    # В остальных случаях — стандартная шкала 0-255
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
            if not audio or not audio.tags:
                return None, 0
            
            rating = None
            starred = 0
            
            # 1. Чтение рейтинга (POPM)
            popm_frames = audio.tags.getall("POPM")
            if popm_frames:
                selected_popm = None
                # Ищем по списку приоритетных плееров
                for email in _KNOWN_PRIMARY_RATING_PLAYERS:
                    selected_popm = next((f for f in popm_frames if f.email == email), None)
                    if selected_popm:
                        break
                
                # Если ничего из приоритетного не нашли - берем первый попавшийся
                if not selected_popm:
                    selected_popm = popm_frames[0]
                
                rating = _popm_rating_to_internal(selected_popm.rating, selected_popm.email)
            
            # --- ЛАЙК ---
            like_frames = audio.tags.getall(f"TXXX:{_LIKE_TAG}")
            if like_frames:
                try:
                    val_raw = like_frames[0].text[0]
                    starred = 1 if val_raw == _LIKE_VALUE_ON else 0
                except Exception:
                    starred = 0
            
            return rating, starred
        except Exception as e: logger.error(f"ID3 read all err ({file_path}): {e}")
        return None, 0

    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> None:
        try:
            audio = self._load(file_path)
            if audio is None: return
            if audio.tags is None: audio.tags = ID3()

            # --- РЕЙТИНГ ---
            if rating is not None:
                # Конвертируем рейтинг (даже если это 0) в шкалу POPM
                popm_rating = _internal_rating_to_popm(rating)
                # Получаем все существующие фреймы POPM за один вызов
                popm_frames = audio.tags.getall("POPM")
                
                if popm_frames:
                    # Фреймы есть: обновляем рейтинг в каждом из них
                    for frame in popm_frames:
                        frame.rating = popm_rating
                        # НЕ трогаем frame.count! Если счетчик там был, он сохранится. Если нет — останется отсутствовать.
                        # Это закладывает фундамент для будущей работы со счетчиком без изменения логики сейчас.
                else:
                    # Фреймов нет: создаем один стандартный с нужным рейтингом
                    # count не указываем, так как он опционален в спецификации ID3
                    audio.tags.add(POPM(email=_RATING_EMAIL, rating=popm_rating))
            
            # --- ЛАЙК ---
            if starred is not None:
                value = _LIKE_VALUE_ON if starred else _LIKE_VALUE_OFF
                audio.tags.add(TXXX(encoding=3, desc=_LIKE_TAG, text=value))
            
            # --- Единая атомарная запись ---
            self._safe_save(audio, file_path, atomic_save)
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
            if not audio or not audio.tags:
                return None, 0
            
            rating = None
            starred = 0
            
            # 1. Чтение рейтинга
            rating_raw = audio.get("RATING")
            if rating_raw:
                xiph_rating = int(rating_raw[0])
                if xiph_rating > 0:
                    rating = max(1, min(10, round(xiph_rating / 10)))
            
            # --- ЛАЙК ---
            like_raw = audio.get(_LIKE_TAG)
            if like_raw:
                try:
                    starred = 1 if like_raw[0] == _LIKE_VALUE_ON else 0
                except Exception:
                    starred = 0
            
            return rating, starred
        except Exception as e: logger.error(f"Xiph read all err ({file_path}): {e}")
        return None, 0

    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> None:
        try:
            audio = self._load(file_path)
            if audio:
                # ЗАЩИТА: Если файл совсем без тегов, инициализируем пустой словарь
                if audio.tags is None:
                    audio.add_tags()

                # --- РЕЙТИНГ ---
                if rating is not None:
                    if rating > 0:
                        # Перезаписывает тег, если он есть, или создает новый
                        audio["RATING"] = str(max(10, min(100, rating * 10)))
                    else:
                        # Рейтинг 0 — удаляем тег, если он физически существует
                        if "RATING" in audio:
                            del audio["RATING"]
                
                # --- ЛАЙК ---
                if starred is not None:
                    audio[_LIKE_TAG] = _LIKE_VALUE_ON if starred else _LIKE_VALUE_OFF

                # --- Единая атомарная запись ---
                self._safe_save(audio, file_path, atomic_save)
        except Exception as e: 
            logger.error(f"Xiph write tags err ({file_path}): {e}")
            raise

    def _load(self, file_path): return MutagenFile(file_path)

# --- СТРАТЕГИЯ MP4 (M4A / AAC) ---
class MP4Handler(RatingHandler):
    # ОПТИМИЗАЦИЯ: Чтение рейтинга и лайка за один раз
    def read_all(self, file_path: str):
        try:
            audio = self._load(file_path)
            if not audio or not audio.tags:
                return None, 0
            
            rating = None
            starred = 0
            
            # 1. Чтение рейтинга
            rating_raw = audio.tags.get("----:com.apple.iTunes:RATE")
            if rating_raw:
                m4a_rating = int(rating_raw[0])
                if m4a_rating > 0:
                    rating = max(1, min(10, round(m4a_rating / 10)))
            
            # --- ЛАЙК ---
            like_raw = audio.tags.get(_LIKE_TAG_MP4)
            if like_raw:
                try:
                    starred = 1 if like_raw[0].decode('utf-8') == _LIKE_VALUE_ON else 0
                except Exception:
                    starred = 0
            
            return rating, starred
        except Exception as e: logger.error(f"MP4 read all err ({file_path}): {e}")
        return None, 0
    
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> None:
        try:
            audio = self._load(file_path)
            if audio.tags is None: audio.add_tags()
            
            # --- РЕЙТИНГ ---
            if rating is not None:
                if rating > 0:
                    m4a_rating = str(max(10, min(100, rating * 10)))
                    # Перезаписывает атом, если он есть, или создает новый
                    audio["----:com.apple.iTunes:RATE"] = [m4a_rating.encode("utf-8")]
                else:
                    # Рейтинг 0 — удаляем атом, если он существует
                    if "----:com.apple.iTunes:RATE" in audio.tags:
                        del audio.tags["----:com.apple.iTunes:RATE"]
                
            # --- ЛАЙК ---
            if starred is not None:
                value = _LIKE_VALUE_ON if starred else _LIKE_VALUE_OFF
                audio[_LIKE_TAG_MP4] = [bytes(value, 'utf-8')]

            # --- Единая атомарная запись ---
            self._safe_save(audio, file_path, atomic_save)
        except Exception as e: 
            logger.error(f"MP4 write tags err ({file_path}): {e}")
            raise

    def _load(self, file_path): return MP4(file_path)

# --- СТРАТЕГИЯ WMA (Windows Media Audio / ASF) ---
class ASFHandler(RatingHandler):
    def read_all(self, file_path: str):
        try:
            audio = self._load(file_path)
            if not audio or not audio.tags:
                return None, 0
            
            rating = None
            starred = 0
            
            # 1. Чтение рейтинга
            rating_raw = audio.tags.get("WM/SharedUserRating")
            if rating_raw:
                wma_rating = rating_raw[0].value
                if wma_rating > 0:
                    rating = _WMA_RATING_READ_MAP.get(wma_rating)
                    # Если значение не стандартное (нет в карте) - rating останется None
            
            # --- ЛАЙК ---
            like_raw = audio.tags.get(_LIKE_TAG_ASF)
            if like_raw:
                try:
                    starred = 1 if like_raw[0].value == _LIKE_VALUE_ON else 0
                except Exception:
                    starred = 0
            
            return rating, starred
        except Exception as e: logger.error(f"ASF read all err ({file_path}): {e}")
        return None, 0
    
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> None:
        try:
            audio = self._load(file_path)
            if audio.tags is None: 
                audio.add_tags()
            
            # --- РЕЙТИНГ ---
            if rating is not None:
                if rating > 0:
                    wma_rating = _WMA_RATING_WRITE_MAP.get(rating, 0)
                    # Перезаписывает атрибут, если он есть, или создает новый
                    audio.tags["WM/SharedUserRating"] = ASFDWordAttribute(wma_rating)
                else:
                    # Рейтинг 0 — удаляем атрибут по точному ключу, если он существует
                    if "WM/SharedUserRating" in audio.tags:
                        del audio.tags["WM/SharedUserRating"]
                
            # --- ЛАЙК ---
            if starred is not None:
                value = _LIKE_VALUE_ON if starred else _LIKE_VALUE_OFF
                audio.tags[_LIKE_TAG_ASF] = ASFUnicodeAttribute(value)

            self._safe_save(audio, file_path, atomic_save)
        except Exception as e: 
            logger.error(f"ASF write tags err ({file_path}): {e}")
            raise

    def _load(self, file_path): return ASF(file_path)

HANDLER_REGISTRY = {
    ".flac": XiphHandler(), ".ogg": XiphHandler(), ".opus": XiphHandler(), ".ape": XiphHandler(), ".wv": XiphHandler(),
    ".mp3": MP3Handler(), ".aif": AIFFHandler(), ".aiff": AIFFHandler(), ".wav": WAVHandler(),
    ".m4a": MP4Handler(),
    ".wma": ASFHandler(),
}

def get_handler(file_path: str) -> RatingHandler | None:
    ext = os.path.splitext(file_path)[1].lower()
    return HANDLER_REGISTRY.get(ext)

def set_tags_to_file(file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> None:
    handler = get_handler(file_path)
    if handler: handler.write_tags(file_path, rating, starred, atomic_save)

def get_all_ratings_from_file(file_path: str):
    handler = get_handler(file_path)
    return handler.read_all(file_path) if handler else (None, 0)

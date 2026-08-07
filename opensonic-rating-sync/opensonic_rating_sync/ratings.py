import logging
import os
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
_KNOWN_PRIMARY_RATING_PLAYERS = ["MusicBee", "no@email", "Navidrome"]
_RATING_EMAIL = "Navidrome" 

# Заглушка для будущей реализации мультиплеерности (Развилка 5)
#_active_players = []

# --- ПРОФИЛИ ПЛЕЕРОВ ---
#_PLAYER_PROFILES = {
#    'musicbee': {
#        'popm_emails': ['musicbee@no.email', 'no@email'],
#        'like_mp3_desc': 'LOVE RATING',                 # ИСПРАВЛЕНО: С пробелом
#        'like_vorbis': 'LOVE RATING',                   # ИСПРАВЛЕНО: С пробелом
#        'like_mp4': '----:com.apple.iTunes:LOVERATING'  # ИСПРАВЛЕНО: Без пробела
#    }

def set_active_players(players_list):
    global _active_players
    _active_players = players_list or []
    logger.debug(f"Активные плееры установлены: {_active_players}")

# --- БАЗОВЫЙ КЛАСС СТРАТЕГИИ ---
class RatingHandler:
    def read_rating(self, file_path: str) -> int | None: raise NotImplementedError
    def write_rating(self, file_path: str, rating: int) -> None: raise NotImplementedError
    def read_starred(self, file_path: str) -> int: raise NotImplementedError
    def write_starred(self, file_path: str, starred: bool) -> None: raise NotImplementedError

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
                    nav_popm = next((f for f in popm_frames if f.email == _RATING_EMAIL), None)
                    if nav_popm: return _popm_rating_to_internal(nav_popm.rating, _RATING_EMAIL)
                    return _popm_rating_to_internal(popm_frames[0].rating, popm_frames[0].email)
        except Exception as e: logger.error(f"ID3 read rating err ({file_path}): {e}")
        return None

    def write_rating(self, file_path: str, rating: int) -> None:
        try:
            audio = self._load(file_path)
            if audio is None: return
            if audio.tags is None: audio.tags = ID3()
            
            # Ищем и удаляем все наши фреймы POPM с email Navidrome
            popm_frames = audio.tags.getall("POPM")
            for f in [f for f in popm_frames if f.email == _RATING_EMAIL]:
                audio.tags.remove(f)
                
            # Добавляем фрейм только если рейтинг валиден (не None и > 0)
            if rating is not None and rating > 0:
                popm_rating = _internal_rating_to_popm(rating)
                audio.tags.add(POPM(email=_RATING_EMAIL, rating=popm_rating, count=0))
            audio.save()
        except Exception as e: logger.error(f"ID3 write rating err ({file_path}): {e}")

    def read_starred(self, file_path: str) -> int:
        try:
            audio = self._load(file_path)
            if audio and audio.tags:
                fav_frames = audio.tags.getall("TXXX:FAVORITE")
                if fav_frames: return 1 if str(fav_frames[0].text[0]) == "1" else 0
        except Exception: pass
        return 0

    def write_starred(self, file_path: str, starred: bool) -> None:
        try:
            audio = self._load(file_path)
            if audio is None: return
            if audio.tags is None: audio.tags = ID3()
            audio.tags.delall("TXXX:FAVORITE")
            audio.tags.add(TXXX(encoding=3, desc="FAVORITE", text="1" if starred else "0"))
            audio.save()
        except Exception as e: logger.error(f"ID3 write star err ({file_path}): {e}")

class MP3Handler(ID3Handler):
    def _load(self, file_path): return MP3(file_path, ID3=ID3)

class AIFFHandler(ID3Handler):
    def _load(self, file_path): return AIFF(file_path)

# --- СТРАТЕГИЯ XIPH (FLAC, OGG, OPUS) ---
class XiphHandler(RatingHandler):
    def read_rating(self, file_path: str) -> int | None:
        try:
            audio = MutagenFile(file_path)
            if audio:
                rating_raw = audio.get("RATING")
                if rating_raw:
                    xiph_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                    if xiph_rating == 0: return None
                    return max(1, min(10, round(xiph_rating / 10)))
        except Exception as e: logger.error(f"Xiph read rating err ({file_path}): {e}")
        return None

    def write_rating(self, file_path: str, rating: int) -> None:
        try:
            audio = MutagenFile(file_path)
            if audio:
                # Удаляем тег RATING, если он есть
                if "RATING" in audio:
                    del audio["RATING"]
                    
                # Пишем новый тег только если рейтинг валиден
                if rating is not None and rating > 0:
                    xiph_rating = str(max(10, min(100, rating * 10)))
                    audio["RATING"] = xiph_rating
                audio.save()
        except Exception as e: logger.error(f"Xiph write rating err ({file_path}): {e}")

    def read_starred(self, file_path: str) -> int:
        try:
            audio = MutagenFile(file_path)
            if audio and "FAVORITE" in audio: return 1 if str(audio["FAVORITE"][0]) == "1" else 0
        except Exception: pass
        return 0

    def write_starred(self, file_path: str, starred: bool) -> None:
        try:
            audio = MutagenFile(file_path)
            if audio:
                audio["FAVORITE"] = "1" if starred else "0"
                audio.save()
        except Exception as e: logger.error(f"Xiph write star err ({file_path}): {e}")

# --- СТРАТЕГИЯ MP4 (M4A / AAC) ---
class MP4Handler(RatingHandler):
    _RATE_TAG = "----:com.apple.iTunes:RATE"
    _FAV_TAG = "----:com.apple.iTunes:FAVORITE"

    def read_rating(self, file_path: str) -> int | None:
        try:
            audio = MP4(file_path)
            rating_raw = audio.tags.get(self._RATE_TAG) if audio.tags else None
            if rating_raw:
                m4a_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                if m4a_rating == 0: return None
                return max(1, min(10, round(m4a_rating / 10)))
        except Exception as e: logger.error(f"MP4 read rating err ({file_path}): {e}")
        return None

    def write_rating(self, file_path: str, rating: int) -> None:
        try:
            audio = MP4(file_path)
            if audio.tags is None: audio.add_tags()
            
            # Удаляем тег RATE, если он есть
            if self._RATE_TAG in audio.tags:
                del audio.tags[self._RATE_TAG]
                
            # Пишем новый тег только если рейтинг валиден
            if rating is not None and rating > 0:
                m4a_rating = str(max(10, min(100, rating * 10)))
                audio[self._RATE_TAG] = [m4a_rating.encode("utf-8")]
            audio.save()
        except Exception as e: logger.error(f"MP4 write rating err ({file_path}): {e}")

    def read_starred(self, file_path: str) -> int:
        try:
            audio = MP4(file_path)
            if audio.tags and self._FAV_TAG in audio.tags:
                return 1 if audio.tags[self._FAV_TAG][0].decode('utf-8') == "1" else 0
        except Exception: pass
        return 0

    def write_starred(self, file_path: str, starred: bool) -> None:
        try:
            audio = MP4(file_path)
            if audio.tags is None: audio.add_tags()
            audio[self._FAV_TAG] = [bytes("1" if starred else "0", 'utf-8')]
            audio.save()
        except Exception as e: logger.error(f"MP4 write star err ({file_path}): {e}")

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

def set_rating_to_file(file_path: str, rating: int) -> None:
    handler = get_handler(file_path)
    if handler: handler.write_rating(file_path, rating)

def get_starred_from_file(file_path: str) -> int:
    handler = get_handler(file_path)
    return handler.read_starred(file_path) if handler else 0

def set_starred_to_file(file_path: str, starred: bool) -> None:
    handler = get_handler(file_path)
    if handler: handler.write_starred(file_path, starred)
        

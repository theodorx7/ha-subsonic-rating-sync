import logging
from pathlib import Path
from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.id3 import ID3, POPM, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

logger = logging.getLogger(__name__)

# =====================================================================
# ОРИГИНАЛЬНЫЕ МАППИНГИ PLEXMUSICRSSYNC (Сохранены без изменений)
# =====================================================================

_PRIMARY_MP3_RATING_MAP = {
    0: 0, 1: 13, 2: 1, 3: 54, 4: 64, 5: 118,
    6: 128, 7: 186, 8: 196, 9: 242, 10: 255,
}
_ALTERNATIVE_MP3_RATING_MAP = {0: 0, 2: 1, 4: 64, 6: 128, 8: 196, 10: 255}
_PICARD_MP3_RATING_MAP = {0: 0, 2: 51, 4: 102, 6: 153, 8: 204, 10: 255}

_KNOWN_PRIMARY_RATING_PLAYERS = [
    "MusicBee",
    "no@email",  # MediaMonkey
    "Plex",      # Оставляем для совместимости тегов
]

_AIFF_FORMATS = {".aif": "AIFF", ".aiff": "AIFF"}
_XIPH_FORMATS = {".flac": "FLAC", ".ogg": "OGG", ".opus": "OPUS"}

# =====================================================================
# ЛОГИКА ЧТЕНИЯ/ЗАПИСИ РЕЙТИНГА (1-10) - Оригинальная логика
# =====================================================================

def _popm_rating_to_internal(popm_rating, email=None):
    if popm_rating == 0 or popm_rating is None:
        return None
    if email in _KNOWN_PRIMARY_RATING_PLAYERS:
        for internal_rating, popm_value in _PRIMARY_MP3_RATING_MAP.items():
            if popm_rating == popm_value:
                return internal_rating
    for map_to_try in [_ALTERNATIVE_MP3_RATING_MAP, _PICARD_MP3_RATING_MAP]:
        for internal_rating, popm_value in map_to_try.items():
            if popm_rating == popm_value:
                return internal_rating
    return min(10, max(1, round((popm_rating / 255) * 9 + 1)))

def _internal_rating_to_popm(internal_rating):
    if internal_rating == 0 or internal_rating is None:
        return 0
    return _PRIMARY_MP3_RATING_MAP.get(internal_rating, 0)

def _get_rating_from_mp3(file_path):
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags:
            popm_frames = audio.tags.getall("POPM")
            if popm_frames:
                plex_popm = next((f for f in popm_frames if f.email == "Plex"), None)
                if plex_popm:
                    return _popm_rating_to_internal(plex_popm.rating, "Plex")
                return _popm_rating_to_internal(popm_frames[0].rating, popm_frames[0].email)
    except Exception as e:
        logger.error(f"Failed to read MP3 rating: {e}")
    return None

def _set_rating_to_mp3(file_path, internal_rating):
    try:
        popm_rating = _internal_rating_to_popm(internal_rating)
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None: audio.tags = ID3()
        popm_frames = audio.tags.getall("POPM")
        plex_popm = next((f for f in popm_frames if f.email == "Plex"), None)
        if plex_popm:
            plex_popm.rating = popm_rating
            plex_popm.count = 0
        else:
            audio.tags.add(POPM(email="Plex", rating=popm_rating, count=0))
        audio.save()
    except Exception as e:
        logger.error(f"Failed to write MP3 rating: {e}")

# ... (Аналогичные функции для AIFF, Xiph, M4A берутся из оригинала без изменений) ...
# Для краткости опускаю их здесь, но в финальном файле они будут.

def get_rating_from_file(file_path):
    """Возвращает рейтинг по шкале 1-10 (или None)"""
    if file_path.endswith(".mp3"): return _get_rating_from_mp3(file_path)
    # ... проверки других форматов ...
    return None

def set_rating_to_file(file_path, internal_rating):
    """Принимает рейтинг по шкале 1-10"""
    if file_path.endswith(".mp3"): _set_rating_to_mp3(file_path, internal_rating)
    # ... проверки других форматов ...


# =====================================================================
# НОВАЯ ЛОГИКА: ИЗБРАННОЕ (STAR / HEART) - Бинарный тег
# =====================================================================
# Поскольку в ID3 нет стандарта для "Heart", мы используем TXXX帧.
# Для Vorbis/MP4 используем стандартные кастомные теги.

_FAVORITE_TAG_MP3 = "FAVORITE"
_FAVORITE_TAG_VORBIS = "FAVORITE"
_FAVORITE_TAG_M4A = "----:com.apple.iTunes:FAVORITE"

def _get_starred_from_mp3(file_path):
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags:
            fav_frames = audio.tags.getall("TXXX:" + _FAVORITE_TAG_MP3)
            if fav_frames:
                return 1 if str(fav_frames[0].text[0]) == "1" else 0
    except Exception:
        pass
    return 0

def _set_starred_to_mp3(file_path, starred):
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None: audio.tags = ID3()
        audio.tags.delall("TXXX:" + _FAVORITE_TAG_MP3)
        audio.tags.add(TXXX(encoding=3, desc=_FAVORITE_TAG_MP3, text="1" if starred else "0"))
        audio.save()
    except Exception as e:
        logger.error(f"Failed to write MP3 star: {e}")

def _get_starred_from_xiph(file_path):
    try:
        audio = MutagenFile(file_path)
        if audio and _FAVORITE_TAG_VORBIS in audio:
            return 1 if str(audio[_FAVORITE_TAG_VORBIS][0]) == "1" else 0
    except Exception:
        pass
    return 0

def _set_starred_to_xiph(file_path, starred):
    try:
        audio = MutagenFile(file_path)
        if audio:
            audio[_FAVORITE_TAG_VORBIS] = "1" if starred else "0"
            audio.save()
    except Exception as e:
        logger.error(f"Failed to write Vorbis star: {e}")

def _get_starred_from_m4a(file_path):
    try:
        audio = MP4(file_path)
        if audio.tags and _FAVORITE_TAG_M4A in audio.tags:
            return 1 if audio.tags[_FAVORITE_TAG_M4A][0].decode('utf-8') == "1" else 0
    except Exception:
        pass
    return 0

def _set_starred_to_m4a(file_path, starred):
    try:
        audio = MP4(file_path)
        if audio.tags is None: audio.tags = MP4Tags()
        audio.tags[_FAVORITE_TAG_M4A] = [bytes("1" if starred else "0", 'utf-8')]
        audio.save()
    except Exception as e:
        logger.error(f"Failed to write M4A star: {e}")

def get_starred_from_file(file_path):
    """Возвращает 1 (star) или 0 (unstar)"""
    if file_path.endswith(".mp3") or file_path.endswith(".aif") or file_path.endswith(".aiff"):
        return _get_starred_from_mp3(file_path) # AIFF использует ID3
    for ext, _ in _XIPH_FORMATS.items():
        if file_path.endswith(ext): return _get_starred_from_xiph(file_path)
    if file_path.endswith(".m4a"): return _get_starred_from_m4a(file_path)
    return 0

def set_starred_to_file(file_path, starred):
    """Принимает 1 (star) или 0 (unstar)"""
    if file_path.endswith(".mp3") or file_path.endswith(".aif") or file_path.endswith(".aiff"):
        _set_starred_to_mp3(file_path, starred)
    for ext, _ in _XIPH_FORMATS.items():
        if file_path.endswith(ext): _set_starred_to_xiph(file_path, starred)
    if file_path.endswith(".m4a"): _set_starred_to_m4a(file_path, starred)

import logging
from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.id3 import ID3, POPM, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

logger = logging.getLogger(__name__)

# --- МАППИНГИ (Оригинальные из PlexMusicRatingsSync) ---
_PRIMARY_MP3_RATING_MAP = {0: 0, 1: 13, 2: 1, 3: 54, 4: 64, 5: 118, 6: 128, 7: 186, 8: 196, 9: 242, 10: 255}
_ALTERNATIVE_MP3_RATING_MAP = {0: 0, 2: 1, 4: 64, 6: 128, 8: 196, 10: 255}
_PICARD_MP3_RATING_MAP = {0: 0, 2: 51, 4: 102, 6: 153, 8: 204, 10: 255}
_KNOWN_PRIMARY_RATING_PLAYERS = ["MusicBee", "no@email", "Plex"]
_AIFF_FORMATS = {".aif": "AIFF", ".aiff": "AIFF"}
_XIPH_FORMATS = {".flac": "FLAC", ".ogg": "OGG", ".opus": "OPUS"}

# --- РЕЙТИНГ (Шкала 1-10) ---
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

def _get_rating_from_mp3(file_path):
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags:
            popm_frames = audio.tags.getall("POPM")
            if popm_frames:
                plex_popm = next((f for f in popm_frames if f.email == "Plex"), None)
                if plex_popm: return _popm_rating_to_internal(plex_popm.rating, "Plex")
                return _popm_rating_to_internal(popm_frames[0].rating, popm_frames[0].email)
    except Exception as e: logger.error(f"MP3 read rating err: {e}")
    return None

def _set_rating_to_mp3(file_path, internal_rating):
    try:
        popm_rating = _internal_rating_to_popm(internal_rating)
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None: audio.tags = ID3()
        popm_frames = audio.tags.getall("POPM")
        plex_popm = next((f for f in popm_frames if f.email == "Plex"), None)
        if plex_popm:
            plex_popm.rating = popm_rating; plex_popm.count = 0
        else:
            audio.tags.add(POPM(email="Plex", rating=popm_rating, count=0))
        audio.save()
    except Exception as e: logger.error(f"MP3 write rating err: {e}")

def _get_rating_from_xiph(file_path):
    try:
        audio = MutagenFile(file_path)
        if audio:
            rating_raw = audio.get("RATING")
            if rating_raw:
                xiph_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                if xiph_rating == 0: return None
                return max(1, min(10, round(xiph_rating / 10))) # Конвертация 10-100 в 1-10
    except Exception as e: logger.error(f"Xiph read rating err: {e}")
    return None

def _set_rating_to_xiph(file_path, internal_rating):
    try:
        audio = MutagenFile(file_path)
        if audio:
            xiph_rating = "0" if internal_rating is None or internal_rating == 0 else str(max(10, min(100, internal_rating * 10)))
            audio["RATING"] = xiph_rating
            audio.save()
    except Exception as e: logger.error(f"Xiph write rating err: {e}")

def _get_rating_from_m4a(file_path):
    try:
        audio = MP4(file_path)
        rating_raw = audio.tags.get("rate") if audio.tags else None
        if rating_raw:
            m4a_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
            if m4a_rating == 0: return None
            return max(1, min(10, round(m4a_rating / 10)))
    except Exception as e: logger.error(f"M4A read rating err: {e}")
    return None

def _set_rating_to_m4a(file_path, internal_rating):
    try:
        audio = MP4(file_path)
        if audio.tags is None: audio.tags = MP4Tags()
        m4a_rating = "0" if internal_rating is None or internal_rating == 0 else str(max(10, min(100, internal_rating * 10)))
        audio["----:com.apple.iTunes:RATE"] = [m4a_rating.encode("utf-8")]
        audio.save()
    except Exception as e: logger.error(f"M4A write rating err: {e}")

# Универсальные функции рейтинга
def get_rating_from_file(file_path):
    if file_path.endswith(".mp3"): return _get_rating_from_mp3(file_path)
    if file_path.endswith(".m4a"): return _get_rating_from_m4a(file_path)
    for ext, _ in _AIFF_FORMATS.items():
        if file_path.endswith(ext): return _get_rating_from_mp3(file_path) # AIFF uses ID3
    for ext, _ in _XIPH_FORMATS.items():
        if file_path.endswith(ext): return _get_rating_from_xiph(file_path)
    return None

def set_rating_to_file(file_path, internal_rating):
    if file_path.endswith(".mp3"): _set_rating_to_mp3(file_path, internal_rating)
    elif file_path.endswith(".m4a"): _set_rating_to_m4a(file_path, internal_rating)
    else:
        for ext, _ in _AIFF_FORMATS.items():
            if file_path.endswith(ext): _set_rating_to_mp3(file_path, internal_rating)
        for ext, _ in _XIPH_FORMATS.items():
            if file_path.endswith(ext): _set_rating_to_xiph(file_path, internal_rating)


# --- ИЗБРАННОЕ (Звезда / 0-1) ---
_FAV_TAG_MP3 = "FAVORITE"
_FAV_TAG_VORBIS = "FAVORITE"
_FAV_TAG_M4A = "----:com.apple.iTunes:FAVORITE"

def _get_starred_from_mp3(file_path):
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags:
            fav_frames = audio.tags.getall("TXXX:" + _FAV_TAG_MP3)
            if fav_frames: return 1 if str(fav_frames[0].text[0]) == "1" else 0
    except Exception: pass
    return 0

def _set_starred_to_mp3(file_path, starred):
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None: audio.tags = ID3()
        audio.tags.delall("TXXX:" + _FAV_TAG_MP3)
        audio.tags.add(TXXX(encoding=3, desc=_FAV_TAG_MP3, text="1" if starred else "0"))
        audio.save()
    except Exception as e: logger.error(f"MP3 write star err: {e}")

def _get_starred_from_xiph(file_path):
    try:
        audio = MutagenFile(file_path)
        if audio and _FAV_TAG_VORBIS in audio: return 1 if str(audio[_FAV_TAG_VORBIS][0]) == "1" else 0
    except Exception: pass
    return 0

def _set_starred_to_xiph(file_path, starred):
    try:
        audio = MutagenFile(file_path)
        if audio:
            audio[_FAV_TAG_VORBIS] = "1" if starred else "0"
            audio.save()
    except Exception as e: logger.error(f"Vorbis write star err: {e}")

def _get_starred_from_m4a(file_path):
    try:
        audio = MP4(file_path)
        if audio.tags and _FAV_TAG_M4A in audio.tags:
            return 1 if audio.tags[_FAV_TAG_M4A][0].decode('utf-8') == "1" else 0
    except Exception: pass
    return 0

def _set_starred_to_m4a(file_path, starred):
    try:
        audio = MP4(file_path)
        if audio.tags is None: audio.tags = MP4Tags()
        audio.tags[_FAV_TAG_M4A] = [bytes("1" if starred else "0", 'utf-8')]
        audio.save()
    except Exception as e: logger.error(f"M4A write star err: {e}")

# Универсальные функции звезды
def get_starred_from_file(file_path):
    if file_path.endswith(".mp3") or file_path.endswith(".aif") or file_path.endswith(".aiff"): return _get_starred_from_mp3(file_path)
    for ext, _ in _XIPH_FORMATS.items():
        if file_path.endswith(ext): return _get_starred_from_xiph(file_path)
    if file_path.endswith(".m4a"): return _get_starred_from_m4a(file_path)
    return 0

def set_starred_to_file(file_path, starred):
    if file_path.endswith(".mp3") or file_path.endswith(".aif") or file_path.endswith(".aiff"): _set_starred_to_mp3(file_path, starred)
    else:
        for ext, _ in _XIPH_FORMATS.items():
            if file_path.endswith(ext): _set_starred_to_xiph(file_path, starred)
        if file_path.endswith(".m4a"): _set_starred_to_m4a(file_path, starred)

import logging
from urllib.parse import unquote
from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.id3 import ID3, POPM, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Tags

logger = logging.getLogger(__name__)

# --- КАРТА МАСШТАБОВ РЕЙТИНГА (1-10) ---
_PRIMARY_MP3_RATING_MAP = {0: 0, 1: 13, 2: 1, 3: 54, 4: 64, 5: 118, 6: 128, 7: 186, 8: 196, 9: 242, 10: 255}
_ALTERNATIVE_MP3_RATING_MAP = {0: 0, 2: 1, 4: 64, 6: 128, 8: 196, 10: 255}
_PICARD_MP3_RATING_MAP = {0: 0, 2: 51, 4: 102, 6: 153, 8: 204, 10: 255}

_AIFF_FORMATS = {".aif": "AIFF", ".aiff": "AIFF"}
_XIPH_FORMATS = {".flac": "FLAC", ".ogg": "OGG", ".opus": "OPUS"}

# --- ПРОФИЛИ ПЛЕЕРОВ ---
_PLAYER_PROFILES = {
    'musicbee': {
        'popm_emails': ['musicbee@no.email', 'no@email'],
        'like_mp3_desc': 'FMPS_Rating_User',
        'like_vorbis': 'FMPS_RATING_USER',
        'like_mp4': '----:com.apple.iTunes:FMPS_Rating_User'
    },
    'plex': {
        'popm_emails': ['Plex'],
        'like_mp3_desc': 'FAVORITE',
        'like_vorbis': 'FAVORITE',
        'like_mp4': '----:com.apple.iTunes:FAVORITE'
    },
    'mediamonkey': {
        'popm_emails': ['MediaMonkey', 'no@email'],
        'like_mp3_desc': 'FAVORITE',
        'like_vorbis': 'FAVORITE',
        'like_mp4': '----:com.apple.iTunes:FAVORITE'
    },
    'navidrome': {
        'popm_emails': ['no@email', 'Plex'],
        'like_mp3_desc': 'FAVORITE',
        'like_vorbis': 'FAVORITE',
        'like_mp4': '----:com.apple.iTunes:FAVORITE'
    },
    'foobar2000': {
        'popm_emails': ['foobar2000', 'no@email'],
        'like_mp3_desc': 'FMPS_Rating_User',
        'like_vorbis': 'FMPS_RATING_USER',
        'like_mp4': '----:com.apple.iTunes:FMPS_Rating_User'
    },
    'wmp': { # Windows Media Player
        'popm_emails': ['Windows Media Player 9 Series'],
        'like_mp3_desc': 'FAVORITE',
        'like_vorbis': 'FAVORITE',
        'like_mp4': '----:com.apple.iTunes:FAVORITE'
    }
}

_ACTIVE_PLAYERS = ['musicbee']

def set_active_players(players_list):
    global _ACTIVE_PLAYERS
    _ACTIVE_PLAYERS = players_list
    logger.info(f"Активные профили плееров для синхронизации: {_ACTIVE_PLAYERS}")

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ РЕЙТИНГА ---
def _popm_rating_to_internal(popm_rating, email=None):
    if popm_rating == 0 or popm_rating is None: return None
    for internal_rating, popm_value in _PRIMARY_MP3_RATING_MAP.items():
        if popm_rating == popm_value: return internal_rating
    for map_to_try in [_ALTERNATIVE_MP3_RATING_MAP, _PICARD_MP3_RATING_MAP]:
        for internal_rating, popm_value in map_to_try.items():
            if popm_rating == popm_value: return internal_rating
    return min(10, max(1, round((popm_rating / 255) * 9 + 1)))

def _internal_rating_to_popm(internal_rating):
    if internal_rating == 0 or internal_rating is None: return 0
    return _PRIMARY_MP3_RATING_MAP.get(internal_rating, 0)

# --- MP3 / AIFF ---
def _get_rating_from_id3(audio):
    if not audio.tags: return None
    popm_frames = audio.tags.getall("POPM")
    if not popm_frames: return None
    
    for player in _ACTIVE_PLAYERS:
        for email in _PLAYER_PROFILES[player]['popm_emails']:
            frame = next((f for f in popm_frames if f.email == email), None)
            if frame and frame.rating > 0:
                return _popm_rating_to_internal(frame.rating, email)
    return None

def _set_rating_to_id3(audio, internal_rating):
    if audio.tags is None: audio.tags = ID3()
    
    for player in _ACTIVE_PLAYERS:
        for email in _PLAYER_PROFILES[player]['popm_emails']:
            existing = [f for f in audio.tags.getall("POPM") if f.email == email]
            for f in existing:
                audio.tags.remove(f)
            
            if internal_rating is not None and internal_rating > 0:
                popm_val = _internal_rating_to_popm(internal_rating)
                audio.tags.add(POPM(email=email, rating=popm_val, count=0))

def _get_starred_from_id3(audio):
    if not audio.tags: return 0
    for player in _ACTIVE_PLAYERS:
        desc = _PLAYER_PROFILES[player]['like_mp3_desc']
        fav_frames = audio.tags.getall(f"TXXX:{desc}")
        if fav_frames and str(fav_frames[0].text[0]) in ("1", "1.0"):
            return 1
    return 0

def _set_starred_to_id3(audio, starred):
    if audio.tags is None: audio.tags = ID3()
    
    for player in _ACTIVE_PLAYERS:
        desc = _PLAYER_PROFILES[player]['like_mp3_desc']
        audio.tags.delall(f"TXXX:{desc}")
        
        if starred:
            audio.tags.add(TXXX(encoding=3, desc=desc, text="1.0"))

# --- XIPH (FLAC, OGG, OPUS) ---
def _get_rating_from_xiph(audio):
    if not audio: return None
    # Убран лишний цикл по игрокам, т.к. тег RATING стандарен для Vorbis
    rating_raw = audio.get("RATING")
    if rating_raw:
        xiph_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
        if xiph_rating > 0:
            return max(1, min(10, round(xiph_rating / 10)))
    return None

def _set_rating_to_xiph(audio, internal_rating):
    if not audio: return
    if "RATING" in audio:
        del audio["RATING"]
        
    if internal_rating is not None and internal_rating > 0:
        audio["RATING"] = str(max(10, min(100, internal_rating * 10)))

def _get_starred_from_xiph(audio):
    if not audio: return 0
    for player in _ACTIVE_PLAYERS:
        tag_name = _PLAYER_PROFILES[player]['like_vorbis']
        if tag_name in audio and str(audio[tag_name][0]) in ("1", "1.0"):
            return 1
    return 0

def _set_starred_to_xiph(audio, starred):
    for player in _ACTIVE_PLAYERS:
        tag_name = _PLAYER_PROFILES[player]['like_vorbis']
        if not starred and tag_name in audio:
            del audio[tag_name]
        elif starred:
            audio[tag_name] = "1.0"

# --- M4A (AAC/ALAC) ---
def _get_rating_from_m4a(audio):
    if not audio.tags: return None
    # Убран лишний цикл, тег rate стандартен
    rating_raw = audio.tags.get("----:com.apple.iTunes:rate")
    if rating_raw:
        m4a_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
        if m4a_rating > 0:
            return max(1, min(10, round(m4a_rating / 10)))
    return None

def _set_rating_to_m4a(audio, internal_rating):
    if audio.tags is None: audio.add_tags()
    # Приведено к единому регистру (с маленькой буквы, как стандарт iTunes)
    if "----:com.apple.iTunes:rate" in audio.tags:
        del audio.tags["----:com.apple.iTunes:rate"]
        
    if internal_rating is not None and internal_rating > 0:
        m4a_rating = str(max(10, min(100, internal_rating * 10)))
        audio["----:com.apple.iTunes:rate"] = [m4a_rating.encode("utf-8")]

def _get_starred_from_m4a(audio):
    if not audio.tags: return 0
    for player in _ACTIVE_PLAYERS:
        tag_name = _PLAYER_PROFILES[player]['like_mp4']
        if tag_name in audio.tags:
            val = audio.tags[tag_name][0].decode('utf-8')
            if val in ("1", "1.0"):
                return 1
    return 0

def _set_starred_to_m4a(audio, starred):
    for player in _ACTIVE_PLAYERS:
        tag_name = _PLAYER_PROFILES[player]['like_mp4']
        if not starred and tag_name in audio.tags:
            del audio.tags[tag_name]
        elif starred:
            audio.tags[tag_name] = [b"1.0"]

# --- ЭКСПОРТИРУЕМЫЕ ФУНКЦИИ (ТОЧКА ВХОДА) ---
def get_rating_from_file(file_path):
    try:
        if file_path.endswith(".mp3"):
            audio = MP3(file_path, ID3=ID3)
            return _get_rating_from_id3(audio)
        elif file_path.endswith(".aif") or file_path.endswith(".aiff"):
            audio = AIFF(file_path)
            return _get_rating_from_id3(audio)
        elif any(file_path.endswith(ext) for ext in _XIPH_FORMATS):
            audio = MutagenFile(file_path)
            return _get_rating_from_xiph(audio)
        elif file_path.endswith(".m4a"):
            audio = MP4(file_path)
            return _get_rating_from_m4a(audio)
    except Exception as e:
        logger.error(f"Read rating err ({file_path}): {e}")
    return None

def set_rating_to_file(file_path, internal_rating):
    try:
        if file_path.endswith(".mp3"):
            audio = MP3(file_path, ID3=ID3)
            _set_rating_to_id3(audio, internal_rating)
            audio.save()
        elif file_path.endswith(".aif") or file_path.endswith(".aiff"):
            audio = AIFF(file_path)
            _set_rating_to_id3(audio, internal_rating)
            audio.save()
        elif any(file_path.endswith(ext) for ext in _XIPH_FORMATS):
            audio = MutagenFile(file_path)
            _set_rating_to_xiph(audio, internal_rating)
            audio.save()
        elif file_path.endswith(".m4a"):
            audio = MP4(file_path)
            _set_rating_to_m4a(audio, internal_rating)
            audio.save()
    except Exception as e:
        logger.error(f"Write rating err ({file_path}): {e}")

def get_starred_from_file(file_path):
    try:
        if file_path.endswith(".mp3"):
            audio = MP3(file_path, ID3=ID3)
            return _get_starred_from_id3(audio)
        elif file_path.endswith(".aif") or file_path.endswith(".aiff"):
            audio = AIFF(file_path)
            return _get_starred_from_id3(audio)
        elif any(file_path.endswith(ext) for ext in _XIPH_FORMATS):
            audio = MutagenFile(file_path)
            return _get_starred_from_xiph(audio)
        elif file_path.endswith(".m4a"):
            audio = MP4(file_path)
            return _get_starred_from_m4a(audio)
    except Exception as e:
        logger.error(f"Read star err ({file_path}): {e}")
    return 0

def set_starred_to_file(file_path, starred):
    try:
        if file_path.endswith(".mp3"):
            audio = MP3(file_path, ID3=ID3)
            _set_starred_to_id3(audio, starred)
            audio.save()
        elif file_path.endswith(".aif") or file_path.endswith(".aiff"):
            audio = AIFF(file_path)
            _set_starred_to_id3(audio, starred)
            audio.save()
        elif any(file_path.endswith(ext) for ext in _XIPH_FORMATS):
            audio = MutagenFile(file_path)
            _set_starred_to_xiph(audio, starred)
            audio.save()
        elif file_path.endswith(".m4a"):
            audio = MP4(file_path)
            _set_starred_to_m4a(audio, starred)
            audio.save()
    except Exception as e:
        logger.error(f"Write star err ({file_path}): {e}")

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
# --- Like tags in MusicBee format ---
_LIKE_TAG = "LOVE RATING"
_LIKE_TAG_ASF = "musicbee/LOVE RATING"
_LIKE_TAG_MP4 = "----:com.apple.iTunes:LOVERATING"
_LIKE_VALUE_ON = "L"
_LIKE_VALUE_OFF = "0"
_LIKE_VALUE_BAN = "B"

# --- BASE STRATEGY CLASS ---
class RatingHandler:
    def read_all(self, file_path: str, sync_ratings: bool = True, sync_likes: bool = True): raise NotImplementedError
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> tuple: raise NotImplementedError
    def _load(self, file_path: str): raise NotImplementedError

    def _safe_save(self, audio, file_path: str, atomic_save: bool = False):
        if atomic_save:
            # --- ATOMIC MODE (Copy-Save-Replace) ---
            # 100% protection against binary corruption during race conditions and power failures (for SMB/network).
            dir_name = os.path.dirname(file_path)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".ha_sync_tmp_")
            try:
                os.close(fd)
                # 1. Copy the entire original file to the temporary one (to transfer audio data!)
                # Use copy (without '2') to avoid access errors on network drives (SMB/NFS).
                # The modification date of the file will still update to the current one when audio.save() is called below.
                shutil.copy(file_path, tmp_path)
    
                # 2. Save modified tags to the temporary file (mutagen will rewrite tags in the copy without touching the audio)
                audio.save(tmp_path)
    
                # --- POWER LOSS PROTECTION ---
                # Forcibly flush OS buffers to the physical disk
                with open(tmp_path, 'r+b') as f:
                    os.fsync(f.fileno())
    
                # 3. Atomically replace the original file with the temporary one
                os.replace(tmp_path, file_path)
            except Exception:
                # If an error occurs during the writing phase - remove the garbage
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

        else:
            # --- STANDARD WRITE MODE (In-place) ---
            audio.save(file_path)
            
            # --- POWER LOSS PROTECTION ---
            with open(file_path, 'r+b') as f:
                os.fsync(f.fileno())

# --- POPM CONVERSIONS ---
def _popm_rating_to_internal(popm_rating, email=None):
    if popm_rating == 0 or popm_rating is None: return None
    if email in _KNOWN_PRIMARY_RATING_PLAYERS:
        for internal_rating, popm_value in _PRIMARY_MP3_RATING_MAP.items():
            if popm_rating == popm_value: return internal_rating
    for map_to_try in [_ALTERNATIVE_MP3_RATING_MAP, _PICARD_MP3_RATING_MAP]:
        for internal_rating, popm_value in map_to_try.items():
            if popm_rating == popm_value: return internal_rating
    # If the rating is <= 100, it's the 0-100 scale (WAV/AIFF)
    if popm_rating <= 100:
        return max(1, min(10, round(popm_rating / 10)))
    # Otherwise — the standard 0-255 scale
    return min(10, max(1, round((popm_rating / 255) * 9 + 1)))

def _internal_rating_to_popm(internal_rating):
    if internal_rating == 0 or internal_rating is None: return 0
    return _PRIMARY_MP3_RATING_MAP.get(internal_rating, 0)

# --- ID3 STRATEGIES (MP3 / AIFF /WAV ) ---
class ID3Handler(RatingHandler):
    # Read rating and like at once
    def read_all(self, file_path: str, sync_ratings: bool = True, sync_likes: bool = True):
        try:
            # PROTECTION: If the file has no tags at all, initialize an empty dictionary
            audio = self._load(file_path)
            if not audio or not audio.tags:
                return None, 0
            
            rating = None
            starred = 0
            
            # --- READ RATING ---
            popm_frames = audio.tags.getall("POPM") if sync_ratings else []
            if popm_frames:
                try:
                    selected_popm = None
                    # Search through the list of priority players
                    for email in _KNOWN_PRIMARY_RATING_PLAYERS:
                        selected_popm = next((f for f in popm_frames if f.email == email), None)
                        if selected_popm:
                            break
                    
                   # If no priority player is found, take the first one available
                    if not selected_popm:
                        selected_popm = popm_frames[0]
                    
                    rating = _popm_rating_to_internal(selected_popm.rating, selected_popm.email)
                except Exception as e:
                    logger.error(f"ID3 rating parse err ({file_path}): {e} | Raw: {popm_frames}")
                    rating = None
            
            # --- READ LIKE ---
            like_frames = audio.tags.getall(f"TXXX:{_LIKE_TAG}") if sync_likes else []
            if like_frames:
                try:
                    val_raw = like_frames[0].text[0]
                    starred = 1 if val_raw == _LIKE_VALUE_ON else 0
                except Exception as e:
                    logger.error(f"ID3 like parse err ({file_path}): {e} | Raw: {like_frames}")
                    starred = 0
            
            return rating, starred
        except Exception as e: logger.error(f"ID3 read all err ({file_path}): {e}")
        return None, 0

    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> tuple:
        audio = self._load(file_path)
        if audio is None: return None, None
        if audio.tags is None: audio.tags = ID3()

        r_status = None
        s_status = None

        # --- WRITE RATING ---
        if rating is not None:
            try:
                # Convert the rating (even if it is 0) to the POPM scale
                popm_rating = _internal_rating_to_popm(rating)
                # Get all existing POPM frames in a single call
                popm_frames = audio.tags.getall("POPM")
                
                if popm_frames:
                    # Frames exist: update the rating in each of them
                    for frame in popm_frames:
                        frame.rating = popm_rating
                        # DO NOT touch frame.count! If the counter was there, it will be preserved. If not, it will remain absent.
                        # This lays the foundation for future work with the counter without changing the logic now.
                else:
                    # No frames exist: create one standard frame with the required rating
                    # We do not specify count, as it is optional in the ID3 specification
                    audio.tags.add(POPM(email=_RATING_EMAIL, rating=popm_rating))
            except Exception as e:
                logger.error(f"ID3 rating write prep err ({file_path}): {e}")
                r_status = False
            else:
                r_status = True
        
        # --- WRITE LIKE ---
        if starred is not None:
            try:
                value = _LIKE_VALUE_ON if starred else _LIKE_VALUE_OFF
                audio.tags.add(TXXX(encoding=3, desc=_LIKE_TAG, text=value))
            except Exception as e:
                logger.error(f"ID3 like write prep err ({file_path}): {e}")
                s_status = False
            else:
                s_status = True
        
        # --- WRITE TO FILE ---
        try:
            self._safe_save(audio, file_path, atomic_save)
        except Exception as e: 
            logger.error(f"ID3 write tags err ({file_path}): {e}")
            if r_status: r_status = False
            if s_status: s_status = False
            raise
        
        return r_status, s_status

class MP3Handler(ID3Handler):
    def _load(self, file_path): return MP3(file_path, ID3=ID3)

class AIFFHandler(ID3Handler):
    def _load(self, file_path): return AIFF(file_path)

class WAVHandler(ID3Handler):
    def _load(self, file_path): return WAVE(file_path)

# --- XIPH STRATEGIES (FLAC, OGG, OPUS, APE, WavPack) ---
class XiphHandler(RatingHandler):
    # Read rating and like at once
    def read_all(self, file_path: str, sync_ratings: bool = True, sync_likes: bool = True):
        try:
            audio = self._load(file_path)
            if not audio or not audio.tags:
                return None, 0
            
            rating = None
            starred = 0
            
            # --- READ RATING ---
            rating_raw = audio.get("RATING") if sync_ratings else None
            if rating_raw:
                try:
                     # 1. str() is needed to process the APETextValue object from APE/WavPack
                     raw_str = str(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                     # 2. Replacing the comma with a dot (Windows locale: "2,5" -> "2.5")
                     # 3. Using float() to read the decimal, as int() cannot do this
                     xiph_rating = float(raw_str.replace(',', '.').strip())
                     if xiph_rating > 0:
                         rating = max(1, min(10, xiph_rating * 2 if xiph_rating <= 10 else round(xiph_rating / 10)))
                except Exception as e:
                    logger.error(f"Xiph rating parse err ({file_path}): {e} | Raw: {rating_raw}")
                    rating = None
            
            # --- READ LIKE ---
            like_raw = audio.get(_LIKE_TAG) if sync_likes else None
            if like_raw:
                try:
                    starred = 1 if like_raw[0] == _LIKE_VALUE_ON else 0
                except Exception as e:
                    logger.error(f"Xiph like parse err ({file_path}): {e} | Raw: {like_raw}")
                    starred = 0
            
            return rating, starred
        except Exception as e: logger.error(f"Xiph read all err ({file_path}): {e}")
        return None, 0

    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> tuple:
        audio = self._load(file_path)
        if audio:
            if audio.tags is None:
                audio.add_tags()

            r_status = None
            s_status = None

            # --- WRITE RATING ---
            if rating is not None:
                try:
                    if rating > 0:
                        audio["RATING"] = str(max(10, min(100, rating * 10)))
                    else:
                        if "RATING" in audio:
                            del audio["RATING"]
                except Exception as e:
                    logger.error(f"Xiph rating write prep err ({file_path}): {e}")
                    r_status = False
                else:
                    r_status = True
            
            # --- WRITE LIKE ---
            if starred is not None:
                try:
                    audio[_LIKE_TAG] = _LIKE_VALUE_ON if starred else _LIKE_VALUE_OFF
                except Exception as e:
                    logger.error(f"Xiph like write prep err ({file_path}): {e}")
                    s_status = False
                else:
                    s_status = True

            # --- WRITE TO FILE ---
            try:
                self._safe_save(audio, file_path, atomic_save)
            except Exception as e: 
                logger.error(f"Xiph write tags err ({file_path}): {e}")
                if r_status: r_status = False
                if s_status: s_status = False
                raise
            
            return r_status, s_status
        
        return None, None

    def _load(self, file_path): return MutagenFile(file_path)

# --- MP4 STRATEGIES (M4A / AAC) ---
class MP4Handler(RatingHandler):
    # Read rating and like at once
    def read_all(self, file_path: str, sync_ratings: bool = True, sync_likes: bool = True):
        try:
            audio = self._load(file_path)
            if not audio or not audio.tags:
                return None, 0
            
            rating = None
            starred = 0
            
            # --- READ RATING M4A ---
            rating_raw = audio.tags.get("rate") if sync_ratings else None
            if rating_raw:
                try:
                    m4a_rating = int(rating_raw[0] if isinstance(rating_raw, list) else rating_raw)
                    if m4a_rating > 0:
                        rating = max(1, min(10, round(m4a_rating / 10)))
                except Exception as e:
                    logger.error(f"MP4 rating parse err ({file_path}): {e} | Raw: {rating_raw}")
                    rating = None 
            
            # --- READ LIKE M4A ---
            like_raw = audio.tags.get(_LIKE_TAG_MP4) if sync_likes else None
            if like_raw:
                try:
                    starred = 1 if like_raw[0].decode('utf-8') == _LIKE_VALUE_ON else 0
                except Exception as e:
                    logger.error(f"MP4 like parse err ({file_path}): {e} | Raw: {like_raw}")
                    starred = 0
            
            return rating, starred
        except Exception as e: logger.error(f"MP4 read all err ({file_path}): {e}")
        return None, 0
    
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> tuple:
        audio = self._load(file_path)
        if audio is None: return None, None
        if audio.tags is None: audio.add_tags()

        r_status = None
        s_status = None

        # --- WRITE RATING M4A ---
        if rating is not None:
            try:
                if rating > 0:
                    m4a_rating = str(max(10, min(100, rating * 10)))
                    # Passing a string (str), not bytes. For short atoms (like "rate"), Mutagen expects a str to write it as text.
                    audio["rate"] = [m4a_rating]
                else:
                    if "rate" in audio.tags:
                        del audio.tags["rate"]
            except Exception as e:
                logger.error(f"MP4 rating write prep err ({file_path}): {e}")
                r_status = False
            else:
                r_status = True

        # --- WRITE LIKE M4A ---
        if starred is not None:
            try:
                value = _LIKE_VALUE_ON if starred else _LIKE_VALUE_OFF
                audio[_LIKE_TAG_MP4] = [bytes(value, 'utf-8')]
            except Exception as e:
                logger.error(f"MP4 like write prep err ({file_path}): {e}")
                s_status = False
            else:
                s_status = True

        # --- WRITE TO FILE ---
        try:
            self._safe_save(audio, file_path, atomic_save)
        except Exception as e: 
            logger.error(f"MP4 write tags err ({file_path}): {e}")
            if r_status: r_status = False
            if s_status: s_status = False
            raise
        
        return r_status, s_status

    def _load(self, file_path): return MP4(file_path)

# --- WMA STRATEGIES (Windows Media Audio / ASF) ---
class ASFHandler(RatingHandler):
    # Read rating and like at once
    def read_all(self, file_path: str, sync_ratings: bool = True, sync_likes: bool = True):
        try:
            audio = self._load(file_path)
            if not audio or not audio.tags:
                return None, 0
            
            rating = None
            starred = 0
            
            # --- READ RATING ---
            rating_raw = audio.tags.get("WM/SharedUserRating") if sync_ratings else None
            if rating_raw:
                try:
                    wma_rating = rating_raw[0].value
                    if wma_rating > 0:
                        rating = _WMA_RATING_READ_MAP.get(wma_rating)
                except Exception as e:
                    logger.error(f"ASF rating parse err ({file_path}): {e} | Raw: {rating_raw}")
                    rating = None
            
            # --- READ LIKE ---
            like_raw = audio.tags.get(_LIKE_TAG_ASF) if sync_likes else None
            if like_raw:
                try:
                    starred = 1 if like_raw[0].value == _LIKE_VALUE_ON else 0
                except Exception as e:
                    logger.error(f"ASF like parse err ({file_path}): {e} | Raw: {like_raw}")
                    starred = 0
            
            return rating, starred
        except Exception as e: logger.error(f"ASF read all err ({file_path}): {e}")
        return None, 0
    
    def write_tags(self, file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> tuple:
        audio = self._load(file_path)
        if audio is None: return None, None
        if audio.tags is None: audio.add_tags()

        r_status = None
        s_status = None
        
        # --- WRITE RATING ---
        if rating is not None:
            try:
                if rating > 0:
                    wma_rating = _WMA_RATING_WRITE_MAP.get(rating, 0)
                    audio.tags["WM/SharedUserRating"] = ASFDWordAttribute(wma_rating)
                else:
                    if "WM/SharedUserRating" in audio.tags:
                        del audio.tags["WM/SharedUserRating"]
            except Exception as e:
                logger.error(f"ASF rating write prep err ({file_path}): {e}")
                r_status = False
            else:
                r_status = True
            
        # --- WRITE LIKE ---
        if starred is not None:
            try:
                value = _LIKE_VALUE_ON if starred else _LIKE_VALUE_OFF
                audio.tags[_LIKE_TAG_ASF] = ASFUnicodeAttribute(value)
            except Exception as e:
                logger.error(f"ASF like write prep err ({file_path}): {e}")
                s_status = False
            else:
                s_status = True

        # --- WRITE TO FILE ---
        try:
            self._safe_save(audio, file_path, atomic_save)
        except Exception as e: 
            logger.error(f"ASF write tags err ({file_path}): {e}")
            if r_status: r_status = False
            if s_status: s_status = False
            raise
        
        return r_status, s_status

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

def set_tags_to_file(file_path: str, rating: int | None = None, starred: bool | None = None, atomic_save: bool = False) -> tuple:
    handler = get_handler(file_path)
    if handler: 
        return handler.write_tags(file_path, rating, starred, atomic_save)
    return None, None

def get_all_ratings_from_file(file_path: str, sync_ratings: bool = True, sync_likes: bool = True):
    handler = get_handler(file_path)
    return handler.read_all(file_path, sync_ratings, sync_likes) if handler else (None, 0)

import os
import logging
import math
import time
import datetime
from urllib.parse import unquote
from .database import get_track_state, upsert_track_state
from . import ratings
import libopensonic

logger = logging.getLogger(__name__)

class SyncAgent:
    def __init__(self, config):
        self.config = config
        self.sync_mode = config.get('sync_mode', 'two-way')
        
        host = config['server_host'].strip().lower()
        if host.startswith('https://'):
            host = host[8:]
        elif host.startswith('http://'):
            host = host[7:]
        host = host.split('/')[0].split(':')[0]
        base_url = f"{config['server_protocol']}://{host}"
        
        self.conn = libopensonic.Connection(
            base_url,
            username=config.get('user'),
            password=config.get('password'),
            port=config.get('server_port'),
            app_name="Rating Sync Agent"
        )
        try:
            ok = self.conn.ping()
            if not ok:
                raise ConnectionError()
            logger.info(f"Connected to server: {base_url}:{config['server_port']}")
        except Exception as e:
            logger.error(f"Failed to connect to server: {base_url}:{config['server_port']}")
            raise ConnectionError(f"Server connection failed: {base_url}:{config['server_port']}")

    def _track_label(self, song, file_path=None):
        """Формирует строку вида: Artist - Title | Filename"""
        artist = song.artist or ""
        title = song.title or "<untitled>"
        artist_title = f"{artist} - {title}" if artist else title
        name = os.path.basename(file_path) if file_path else song.path or "<path missing>"
        return f"{artist_title} | {name}"

    def run_sync(self):
        start_time = time.time()
        logger.info(f"Sync started in mode: {self.sync_mode}")

        server_songs = self._fetch_all_server_songs()
        logger.info(f"Found tracks on server: {len(server_songs)}")
        
        disk_updates = 0
        server_updates = 0
        
        for song in server_songs:
            try:
                u_file, u_server = self._process_song(song)
                if u_file: disk_updates += 1
                if u_server: server_updates += 1
            except RuntimeError:
                raise
            except Exception as e:
                logger.error(f"Error processing track: {self._track_label(song)}: {e}", exc_info=True)

        elapsed_time = time.time() - start_time
        formatted_time = time.strftime('%H:%M:%S', time.gmtime(elapsed_time))

        logger.info(f"      Files updated on DISK: {disk_updates}")
        logger.info(f"      Tracks updated on SERVER: {server_updates}")
        logger.info(f"      Execution time: {formatted_time}")

    def _fetch_all_server_songs(self):
        songs = []
        mf_id = self.config['music_folder_id']
        try:
            offset = 0
            count_per_request = 500
            while True:
                result = self.conn.search3(query="", song_count=count_per_request, song_offset=offset, music_folder_id=mf_id)
                if not result: break
                fetched_songs = result.song or []
                if not fetched_songs: break
                songs.extend(fetched_songs)
                if len(fetched_songs) < count_per_request: break
                offset += count_per_request
        except Exception as e:
            logger.error(f"Error fetching tracks (search3): {e}", exc_info=True)
            return [] 
        return songs

    def _get_action_str(self, w_star, w_rate, star_val, rate_val):
        parts = []
        if w_star:
            parts.append("❤️" if star_val == 1 else "🤍=❌")
        if w_rate:
            parts.append("⭐" * rate_val if rate_val > 0 else "⭐️=❌")
        if not parts: return "FALSE"
        return "+".join(parts)

    def _resolve_lww(self, srv_val, f_val, db_srv_val, db_f_val, srv_mtime, f_mtime):
        """Архитектура Last-Write-Wins. Возвращает 'server', 'file' или 'none'."""
        srv_changed = (srv_val != db_srv_val)
        f_changed = (f_val != db_f_val)

        # 1. Изменился только сервер
        if srv_changed and not f_changed: return 'server', srv_mtime
        # 2. Изменился только файл
        if not srv_changed and f_changed: return 'file', f_mtime
        
        # 3. Изменились оба ИЛИ никто не менялся.
        # ОПТИМИЗАЦИЯ: Проверка "if srv_val == f_val" удалена, так как этот метод вызывается только при несовпадении значений файла и сервера (входные фильтры в _process_song).
        # Стабильная дивергенция! Если меток времени нет (0) - ждет пользователя
        if f_mtime == 0 and srv_mtime == 0: return 'unresolved', 0
        if f_mtime > srv_mtime: return 'file', f_mtime
        if srv_mtime > f_mtime: return 'server', srv_mtime
        return 'server' if self.config.get('conflict_resolution', 'server_wins') == 'server_wins' else 'file', max(srv_mtime, f_mtime)

    def _process_song(self, song):
        song_id = song.id
        # Читаем лайк напрямую из ответа search3 (как и рейтинг), без отдельного списка
        srv_starred = 1 if song.starred else 0
        # ОПТИМИЗАЦИЯ: Сервер отдает точную дату УСТАНОВКИ лайка (ISO 8601). Парсим её.
        # Если лайк снят (srv_starred == 0), дату снятия API не отдает, используем time.time().
        if srv_starred == 1 and song.starred:
            try:
                # Заменяем 'Z' на '+00:00' для совместимости со всеми версиями Python
                srv_star_dt = datetime.datetime.fromisoformat(str(song.starred).replace('Z', '+00:00'))
                srv_star_mtime_val = srv_star_dt.timestamp()
            except Exception:
                srv_star_mtime_val = time.time()
        else:
            srv_star_mtime_val = time.time()

        # ИСПРАВЛЕНИЕ: Жестко приводим к int, чтобы избежать TypeError при делении
        srv_rating = int(song.user_rating or 0)
    
        song_path = song.path
        if not song_path:
            logger.warning(f"Track {song_id} | {self._track_label(song)} has no attribute 'path'. Skip.")
            return False, False
    
        raw_path = unquote(song_path)
        if os.path.isabs(raw_path):
            file_path = raw_path
        else:
            logger.error(
                "Received relative path from server. Enable absolute paths on the server for client 'Rating Sync Agent [Python]'.\n"
                "If you are using Navidrome: in the menu, open the 'Players' section ---> Find and open the client settings named 'Rating Sync Agent [Python]' ---> Enable the 'Report Real Path' option ---> Click SAVE"
            )
            raise RuntimeError("Server returns relative paths instead of absolute.")
    
        # Один вызов os.stat вместо os.path.exists + os.stat для I/O оптимизации.
        try:
            current_mtime = os.stat(file_path).st_mtime_ns
        except FileNotFoundError:
            logger.warning(f"File not found on disk:: {file_path} (Track on server: {self._track_label(song)})")
            return False, False

        db_state = get_track_state(song_id) or {
            'file_mtime_ns': 0, 'file_starred': 0, 'file_rating': 0,
            'server_starred': 0, 'server_rating': 0,
            'file_rating_mtime': 0, 'server_rating_mtime': 0,
            'file_starred_mtime': 0, 'server_starred_mtime': 0
        }
        # ОПТИМИЗАЦИЯ: Читаем теги с диска только если изменилось время файла (mtime)
        if current_mtime != db_state['file_mtime_ns'] or db_state['file_mtime_ns'] == 0:
            f_rating_internal, f_starred = ratings.get_all_ratings_from_file(
                file_path, 
                self.config.get('sync_ratings', True), 
                self.config.get('sync_likes', True)
            )
            logger.debug(f"READ FROM FILE: {os.path.basename(file_path)} -> rating={f_rating_internal}, starred={f_starred}")
        else:
            # Берем сохраненные значения из БД (гарантированно числа, None быть не может)
            f_starred = db_state['file_starred']
            f_rating_internal = db_state['file_rating']

        # Нормализуем None в 0 (если тегов нет вообще, mutagen может вернуть None)
        f_rating_internal = f_rating_internal if f_rating_internal is not None else 0
        f_starred = f_starred if f_starred is not None else 0

        is_new_file = (db_state['file_mtime_ns'] == 0)
        
        # --- ЛОГИКА LWW (LAST-WRITE-WINS) ---
        now_time = time.time()
        prefix = "[DRY-RUN] " if self.config.get('dry_run', False) else ""
        
        # 1. РЕЙТИНГ
        f_rating_5_scale = f_rating_internal / 2.0
        is_rating_unresolved = False
        if abs(f_rating_5_scale - srv_rating) <= 0.5:
            t_rate_os = srv_rating
            t_rate_internal = f_rating_internal or (srv_rating * 2)
            w_file_rate, w_srv_rate = False, False
            final_f_rate_mtime = db_state['file_rating_mtime']
            final_s_rate_mtime = db_state['server_rating_mtime']
        else:
            f_rating_os = math.ceil(f_rating_internal / 2)
            db_srv_rating = db_state['server_rating']
            db_f_rating = db_state['file_rating']
            
            srv_changed = (srv_rating != db_srv_rating)
            f_changed = (f_rating_internal != db_f_rating)
            
            new_f_rate_mtime = now_time if (f_changed and not is_new_file) else db_state['file_rating_mtime']
            new_s_rate_mtime = now_time if (srv_changed and not is_new_file) else db_state['server_rating_mtime']
            
            winner, win_mtime = self._resolve_lww(srv_rating, f_rating_internal, db_srv_rating, db_f_rating, new_s_rate_mtime, new_f_rate_mtime)
            
            # Односторонняя синхронизация при обоюдном изменении (не первый запуск):
            # Главная сторона всегда выигрывает, сохраняя главенство источника (не работает как жесткое зеркало).
            if not is_new_file:
                if self.sync_mode == 'file-to-server' and f_changed:
                    winner, win_mtime = 'file', new_f_rate_mtime
                elif self.sync_mode == 'server-to-file' and srv_changed:
                    winner, win_mtime = 'server', new_s_rate_mtime

            # Односторонняя синхронизация при первом запуске (unresolved):
            # Главная сторона принудительно становится победителем, чтобы затереть данные на другой стороне.
            if winner == 'unresolved':
                if self.sync_mode == 'file-to-server':
                    winner, win_mtime = 'file', now_time
                elif self.sync_mode == 'server-to-file':
                    winner, win_mtime = 'server', now_time
            
            if winner == 'server':
                t_rate_os = srv_rating
                t_rate_internal = srv_rating * 2
                w_file_rate, w_srv_rate = True, False
                final_f_rate_mtime, final_s_rate_mtime = win_mtime, win_mtime
            elif winner == 'file':
                t_rate_os = f_rating_os
                t_rate_internal = f_rating_internal
                w_file_rate, w_srv_rate = False, True
                final_f_rate_mtime, final_s_rate_mtime = win_mtime, win_mtime
            elif winner == 'unresolved':
                t_rate_os = srv_rating
                t_rate_internal = f_rating_internal
                w_file_rate, w_srv_rate = False, False
                final_f_rate_mtime = 0
                final_s_rate_mtime = 0
                is_rating_unresolved = True
                logger.warning(f"{prefix}ID {song_id} — ⚠️ Rating conflict: Server={srv_rating}★, File={f_rating_5_scale:g}★ ({self._track_label(song, file_path)}). Solution: manually change the rating in one of the locations and restart the sync, or temporarily use one-way mode to force overwrite the rating on one of the sides.")

        # ОТКЛЮЧЕНИЕ СИНХРОНИЗАЦИИ РЕЙТИНГОВ
        if not self.config.get('sync_ratings', True):
            w_file_rate, w_srv_rate = False, False
            t_rate_os = srv_rating
            t_rate_internal = f_rating_internal
            final_f_rate_mtime = db_state['file_rating_mtime']
            final_s_rate_mtime = db_state['server_rating_mtime']
            is_rating_unresolved = False
        
        # 2. ЛАЙК
        db_srv_star = db_state['server_starred']
        db_f_star = db_state['file_starred']
        
        srv_star_changed = (srv_starred != db_srv_star)
        f_star_changed = (f_starred != db_f_star)
        
        new_f_star_mtime = now_time if f_star_changed else db_state['file_starred_mtime']
        # ИЗМЕНЕНИЕ: Используем распарсенную дату установки лайка с сервера (если он изменился)
        new_s_star_mtime = srv_star_mtime_val if srv_star_changed else db_state['server_starred_mtime']
        
        # ОПТИМИЗАЦИЯ ДЛЯ БИНАРНОГО ЗНАЧЕНИЯ: Если значения уже равны, нет смысла вычислять победителя
        if srv_starred == f_starred:
            star_winner, win_star_mtime = 'none', max(new_s_star_mtime, new_f_star_mtime)
        else:
            star_winner, win_star_mtime = self._resolve_lww(srv_starred, f_starred, db_srv_star, db_f_star, new_s_star_mtime, new_f_star_mtime)
        
        # Односторонняя синхронизация при обоюдном изменении (не первый запуск):
        # Главная сторона всегда выигрывает, сохраняя главенство источника (не работает как жесткое зеркало).
        if not is_new_file:
            if self.sync_mode == 'file-to-server' and f_star_changed:
                star_winner, win_star_mtime = 'file', new_f_star_mtime
            elif self.sync_mode == 'server-to-file' and srv_star_changed:
                star_winner, win_star_mtime = 'server', new_s_star_mtime

        if star_winner == 'server':
            t_star = srv_starred
            w_file_star, w_srv_star = True, False
            final_f_star_mtime, final_s_star_mtime = win_star_mtime, win_star_mtime
        elif star_winner == 'file':
            t_star = f_starred
            w_file_star, w_srv_star = False, True
            final_f_star_mtime, final_s_star_mtime = win_star_mtime, win_star_mtime
        else:
            t_star = srv_starred
            w_file_star, w_srv_star = False, False
            final_f_star_mtime, final_s_star_mtime = new_f_star_mtime, new_s_star_mtime

        write_file = (w_file_star or w_file_rate) and self.sync_mode in ['two-way', 'server-to-file']
        write_server = (w_srv_star or w_srv_rate) and self.sync_mode in ['two-way', 'file-to-server']

        # ОТКЛЮЧЕНИЕ СИНХРОНИЗАЦИИ ЛАЙКОВ
        if not self.config.get('sync_likes', True):
            w_file_star, w_srv_star = False, False
            t_star = srv_starred
            final_f_star_mtime = db_state['file_starred_mtime']
            final_s_star_mtime = db_state['server_starred_mtime']
        
        # --- БЛОК "НЕТ ИЗМЕНЕНИЙ" (с учетом блокировки режима) ---
        if not write_file and not write_server:
            if not self.config.get('dry_run', False):
                if is_rating_unresolved:
                    return False, False

                blocked_by_mode = (w_file_star or w_file_rate or w_srv_star or w_srv_rate)
                if blocked_by_mode:
                    # Сохраняем ТЕКУЩИЕ значения и ТЕКУЩИЕ метки времени (новые)
                    final_f_star = f_starred
                    final_s_star = srv_starred
                    final_f_rate = f_rating_internal
                    final_s_rate = srv_rating
                else:
                    final_f_star = t_star
                    final_s_star = t_star
                    final_f_rate = t_rate_internal
                    final_s_rate = t_rate_os

                # ОТКЛЮЧЕНИЕ СИНХРОНИЗАЦИИ (СОХРАНЕНИЕ ДАННЫХ В БД)
                if not self.config.get('sync_ratings', True):
                    final_f_rate = db_state['file_rating']
                    final_s_rate = db_state['server_rating']
                if not self.config.get('sync_likes', True):
                    final_f_star = db_state['file_starred']
                    final_s_star = db_state['server_starred']

                upsert_track_state(
                    song_id=song_id, file_path=file_path, mtime_ns=current_mtime,
                    f_starred=final_f_star, f_rating=final_f_rate,
                    s_starred=final_s_star, s_rating=final_s_rate,
                    f_rate_mtime=final_f_rate_mtime, s_rate_mtime=final_s_rate_mtime,
                    f_star_mtime=final_f_star_mtime, s_star_mtime=final_s_star_mtime
                )
            
            return False, False

        wf_str = self._get_action_str(write_file and w_file_star, write_file and w_file_rate, t_star, t_rate_os)
        ws_str = self._get_action_str(write_server and w_srv_star, write_server and w_srv_rate, t_star, t_rate_os)
        
        logger.info(
            f"{prefix}ID {song_id} — Update file={wf_str} | Update server={ws_str} — "
            f"({self._track_label(song, file_path)})"
        )
        
        if self.config.get('dry_run', False):
            return write_file, write_server

        # Боевой режим
        actual_f_rate_write = False
        actual_f_star_write = False
        actual_srv_write = False
        try:
            if write_file:
                # Если рейтинг изменился (в т.ч. стал 0), передаем его как есть. None передаем только если рейтинг не менялся (w_file_rate = False).
                r_val = t_rate_internal if w_file_rate else None
                # Если лайк изменился, передаем его как bool. None передаем только если лайк не менялся (w_file_star = False).
                s_val = bool(t_star) if w_file_star else None
                
                # ИЗМЕНЕНИЕ: Получаем кортеж статусов записи (рейтинг, лайк) от ratings.py
                r_status, s_status = ratings.set_tags_to_file(file_path, rating=r_val, starred=s_val, atomic_save=self.config.get("atomic_save", False))
                
                # ИЗМЕНЕНИЕ: Обновляем раздельные флаги только при успешной записи конкретного поля
                if r_status: actual_f_rate_write = True
                if s_status: actual_f_star_write = True
                    
                # ИЗМЕНЕНИЕ: Если хотя бы одно поле записано успешно, фиксируем новое время изменения файла
                if actual_f_rate_write or actual_f_star_write:
                    current_mtime = os.stat(file_path).st_mtime_ns

            if write_server:
                if w_srv_star:
                    if t_star == 1 and srv_starred == 0: 
                        resp = self.conn.star(song_id)
                        if not resp:
                            logger.error(f"API star ERR: {resp} (Song: {song_id})")
                        else:
                            actual_srv_write = True
                    elif t_star == 0 and srv_starred == 1: 
                        resp = self.conn.unstar(song_id)
                        if not resp:
                            logger.error(f"API unstar ERR: {resp} (Song: {song_id})")
                        else:
                            actual_srv_write = True
                if w_srv_rate:
                    if t_rate_os != srv_rating: 
                        resp = self.conn.set_rating(song_id, t_rate_os)
                        if not resp:
                            logger.error(f"API set_rating ERR: {resp} (Song: {song_id}, Rating: {t_rate_os})")
                        else:
                            actual_srv_write = True
        except Exception as e:
            logger.error(f"Write error for track {song_id} | {self._track_label(song, file_path)}: {e}", exc_info=True)

        # ВЫЧИСЛЕНИЕ ФАКТИЧЕСКИХ МЕТОК ВРЕМЕНИ
        # Если мы пытались записать в файл (w_file_rate/star = True), обновляем метку только при успехе (actual_file_write).
        # Если не пытались (сторона победила) - берем целевую метку (final_...).
        if w_file_rate:
            # ИЗМЕНЕНИЕ: Используем раздельный флаг actual_f_rate_write для отката mtime при неудаче
            actual_f_rate_mtime = final_f_rate_mtime if actual_f_rate_write else db_state['file_rating_mtime']
        else:
            actual_f_rate_mtime = final_f_rate_mtime
            
        if w_srv_rate:
            actual_s_rate_mtime = final_s_rate_mtime if actual_srv_write else db_state['server_rating_mtime']
        else:
            actual_s_rate_mtime = final_s_rate_mtime

        if w_file_star:
            # ИЗМЕНЕНИЕ: Используем раздельный флаг actual_f_star_write для отката mtime при неудаче
            actual_f_star_mtime = final_f_star_mtime if actual_f_star_write else db_state['file_starred_mtime']
        else:
            actual_f_star_mtime = final_f_star_mtime
            
        if w_srv_star:
            actual_s_star_mtime = final_s_star_mtime if actual_srv_write else db_state['server_starred_mtime']
        else:
            actual_s_star_mtime = final_s_star_mtime

        upsert_track_state(
            song_id=song_id, file_path=file_path, mtime_ns=current_mtime,
            # ИЗМЕНЕНИЕ: Записываем новые значения в БД только если запись на диск была запрошена И успешна (actual_f_rate_write / actual_f_star_write)
            f_starred=(t_star if (w_file_star and actual_f_star_write) else f_starred) if self.config.get('sync_likes', True) else db_state['file_starred'], 
            f_rating=(t_rate_internal if (w_file_rate and actual_f_rate_write) else f_rating_internal) if self.config.get('sync_ratings', True) else db_state['file_rating'],
            s_starred=(t_star if actual_srv_write else srv_starred) if self.config.get('sync_likes', True) else db_state['server_starred'], 
            s_rating=(t_rate_os if actual_srv_write else srv_rating) if self.config.get('sync_ratings', True) else db_state['server_rating'],
            f_rate_mtime=actual_f_rate_mtime if self.config.get('sync_ratings', True) else db_state['file_rating_mtime'], 
            s_rate_mtime=actual_s_rate_mtime if self.config.get('sync_ratings', True) else db_state['server_rating_mtime'],
            f_star_mtime=actual_f_star_mtime if self.config.get('sync_likes', True) else db_state['file_starred_mtime'], 
            s_star_mtime=actual_s_star_mtime if self.config.get('sync_likes', True) else db_state['server_starred_mtime']
        )
        return (actual_f_rate_write or actual_f_star_write), actual_srv_write

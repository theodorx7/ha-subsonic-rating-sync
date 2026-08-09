import os
import logging
import math
import time
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
        
        conn_kwargs = {
            'base_url': base_url,
            'port': config['server_port'],
            'app_name': "Rating Sync Agent",
            'username': config.get('user') or None,
            'password': config.get('password') or None
        }
            
        self.conn = libopensonic.Connection(**conn_kwargs)
        try:
            ok = self.conn.ping()
            if not ok:
                raise ConnectionError("ping() вернул False")
            logger.info(f"Подключение к Navidrome установлено: {base_url}:{config['server_port']} (Username/Password, ping OK)")
        except Exception as e:
            logger.error(f"Ошибка подключения к Navidrome ({base_url}:{config['server_port']}): {e}")
            raise

    def _track_label(self, song, file_path=None):
        """Формирует строку вида: Artist - Title | Filename"""
        artist = getattr(song, 'artist', None) or ""
        title = getattr(song, 'title', None) or "<без названия>"
        artist_title = f"{artist} - {title}" if artist else title
        name = os.path.basename(file_path) if file_path else getattr(song, 'path', None) or "<нет пути>"
        return f"{artist_title} | {name}"

    def run_sync(self):
        start_time = time.time()
        logger.info(f"Начало цикла синхронизации (режим: {self.sync_mode})...")
        
        starred_ids = self._fetch_starred_ids()
        logger.info(f"Найдено избранных (starred/liked) треков на сервере: {len(starred_ids)}")
        
        server_songs = self._fetch_all_server_songs()
        logger.info(f"Найдено треков на сервере: {len(server_songs)}")
        
        disk_updates = 0
        server_updates = 0
        
        for song in server_songs:
            try:
                u_file, u_server = self._process_song(song, starred_ids)
                if u_file: disk_updates += 1
                if u_server: server_updates += 1
            except Exception as e:
                logger.error(f"Ошибка при обработке трека {self._track_label(song)}: {e}", exc_info=True)

        elapsed_time = time.time() - start_time
        formatted_time = time.strftime('%H:%M:%S', time.gmtime(elapsed_time))

        logger.info("Цикл синхронизации завершен:")
        logger.info(f"      Обновлено файлов на ДИСКЕ: {disk_updates}")
        logger.info(f"      Обновлено элементов на СЕРВЕРЕ: {server_updates}")
        logger.info(f"      Время выполнения: {formatted_time}")

    def _fetch_starred_ids(self):
        starred_ids = set()
        try:
            mf_id = self.config.get('music_folder_id') or None
            result = self.conn.get_starred2(music_folder_id=mf_id) if mf_id else self.conn.get_starred2()
            if result and result.song:
                for s in result.song: starred_ids.add(s.id)
        except Exception as e:
            logger.error(f"Ошибка при получении избранных треков (get_starred2): {e}", exc_info=True)
        return starred_ids

    def _fetch_all_server_songs(self):
        songs = []
        mf_id = self.config.get('music_folder_id') or None
        if not mf_id:
            logger.error("Не указан music_folder_id в настройках аддона!")
            return songs
        try:
            offset = 0
            count_per_request = 500
            while True:
                result = self.conn.search3(query="", song_count=count_per_request, song_offset=offset, music_folder_id=mf_id)
                if not result or not result.song: break
                songs.extend(result.song)
                if len(result.song) < count_per_request: break
                offset += count_per_request
        except Exception as e:
            logger.error(f"Ошибка при получении треков (search3): {e}", exc_info=True)
        return songs

    def _get_action_str(self, w_star, w_rate, star_val, rate_val):
        parts = []
        if w_star:
            parts.append("❤️" if star_val == 1 else "❌(Лайк)")
        if w_rate:
            parts.append("⭐" * rate_val if rate_val > 0 else "❌(Рейтинг)")
        if not parts: return "FALSE"
        return "+".join(parts)

    def _resolve_lww(self, srv_val, f_val, db_srv_val, db_f_val, srv_mtime, f_mtime):
        """Архитектура Last-Write-Wins. Возвращает 'server', 'file' или 'none'."""
        srv_val = 0 if srv_val is None else srv_val
        f_val = 0 if f_val is None else f_val
        db_srv_val = 0 if db_srv_val is None else db_srv_val
        db_f_val = 0 if db_f_val is None else db_f_val

        srv_changed = (srv_val != db_srv_val)
        f_changed = (f_val != db_f_val)

        # 1. Никто не менялся
        if not srv_changed and not f_changed:
            if srv_val == f_val: return 'none', max(srv_mtime, f_mtime)
            # Стабильная дивергенция! Если меток времени нет (0) - ждет пользователя
            if f_mtime == 0 and srv_mtime == 0: return 'unresolved', 0
            if f_mtime > srv_mtime: return 'file', f_mtime
            if srv_mtime > f_mtime: return 'server', srv_mtime
            return 'server' if self.config.get('conflict_resolution', 'server_wins') == 'server_wins' else 'file', max(srv_mtime, f_mtime)

        # 2. Изменился только сервер
        if srv_changed and not f_changed: return 'server', srv_mtime
        # 3. Изменился только файл
        if not srv_changed and f_changed: return 'file', f_mtime
        
        # 4. Изменились оба (одновременно)
        if srv_val == f_val: return 'none', max(srv_mtime, f_mtime)
        if f_mtime == 0 and srv_mtime == 0: return 'unresolved', 0
        if f_mtime > srv_mtime: return 'file', f_mtime
        if srv_mtime > f_mtime: return 'server', srv_mtime
        return 'server' if self.config.get('conflict_resolution', 'server_wins') == 'server_wins' else 'file', max(srv_mtime, f_mtime)
    
    def _process_song(self, song, starred_ids):
        srv_starred = 1 if song.id in starred_ids else 0
        # ИСПРАВЛЕНИЕ: Жестко приводим к int, чтобы избежать TypeError при делении
        srv_rating = int(getattr(song, 'userRating', 0) or getattr(song, 'user_rating', 0) or 0)
    
        if not getattr(song, 'path', None):
            logger.warning(f"Трек {song.id} | {self._track_label(song)} не имеет атрибута path. Пропуск.")
            return False, False
    
        raw_path = unquote(song.path)
        if os.path.isabs(raw_path):
            file_path = os.path.normpath(raw_path)
        else:
            base_folder = self.config.get('music_folder', '').strip()
            if not base_folder:
                logger.warning(f"Трек {self._track_label(song)}: Сервер вернул относительный путь, но опция 'music_folder' не настроена. Пропуск.")
                return False, False
            file_path = os.path.normpath(os.path.join(base_folder, raw_path.lstrip('/')))
    
        if not os.path.exists(file_path):
            logger.warning(f"Файл не найден на диске: {file_path} (Трек: {self._track_label(song)})")
            return False, False

        current_mtime = os.stat(file_path).st_mtime_ns
        db_state = get_track_state(song.id) or {
            'file_mtime_ns': 0, 'file_starred': None, 'file_rating': None,
            'server_starred': None, 'server_rating': None,
            'file_rating_mtime': 0, 'server_rating_mtime': 0,
            'file_starred_mtime': 0, 'server_starred_mtime': 0
        }

        if current_mtime != db_state['file_mtime_ns'] or db_state['file_mtime_ns'] == 0:
            # ОПТИМИЗАЦИЯ: Однократное чтение тегов
            f_rating_internal, f_starred = ratings.get_all_ratings_from_file(file_path)
        else:
            f_starred = db_state['file_starred'] if db_state['file_starred'] is not None else 0
            f_rating_internal = db_state['file_rating']

        # ИСПРАВЛЕНИЕ: Нормализуем None в 0
        f_rating_internal = f_rating_internal if f_rating_internal is not None else 0

        is_new_file = (db_state['file_mtime_ns'] == 0)
        
        # --- ЛОГИКА LWW (LAST-WRITE-WINS) ---
        now_time = time.time()
        prefix = "[DRY-RUN] " if self.config.get('dry_run', False) else ""
        
        # 1. РЕЙТИНГ
        f_rating_5_scale = f_rating_internal / 2.0
        if abs(f_rating_5_scale - srv_rating) <= 0.5:
            t_rate_os = srv_rating
            t_rate_internal = f_rating_internal or (srv_rating * 2)
            w_file_rate, w_srv_rate = False, False
            final_f_rate_mtime = db_state['file_rating_mtime'] if db_state['file_rating_mtime'] else 0
            final_s_rate_mtime = db_state['server_rating_mtime'] if db_state['server_rating_mtime'] else 0
        else:
            f_rating_os = math.ceil(f_rating_internal / 2)
            db_srv_rating = db_state['server_rating'] or 0
            db_f_rating = db_state['file_rating'] or 0
            
            srv_changed = (srv_rating != db_srv_rating)
            f_changed = (f_rating_internal != db_f_rating)
            
            new_f_rate_mtime = now_time if f_changed else (db_state['file_rating_mtime'] or 0)
            new_s_rate_mtime = now_time if srv_changed else (db_state['server_rating_mtime'] or 0)
            
            winner, win_mtime = self._resolve_lww(srv_rating, f_rating_internal, db_srv_rating, db_f_rating, new_s_rate_mtime, new_f_rate_mtime)
            
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
                if is_new_file:
                    logger.info(f"{prefix}ID {song.id} — ⚠️ КОНФЛИКТ РЕЙТИНГА (Нет данных о времени): Сервер={srv_rating}★, Файл={f_rating_5_scale:g}★. Измените оценку в одном из мест. ({self._track_label(song, file_path)})")
            else:
                t_rate_os = srv_rating
                t_rate_internal = f_rating_internal or (srv_rating * 2)
                w_file_rate, w_srv_rate = False, False
                final_f_rate_mtime, final_s_rate_mtime = new_f_rate_mtime, new_s_rate_mtime

        # 2. ЛАЙК
        db_srv_star = db_state['server_starred']
        db_f_star = db_state['file_starred']
        
        srv_star_changed = (srv_starred != db_srv_star) if db_srv_star is not None else False
        f_star_changed = (f_starred != db_f_star) if db_f_star is not None else False
        
        new_f_star_mtime = now_time if f_star_changed else (db_state['file_starred_mtime'] or 0)
        new_s_star_mtime = now_time if srv_star_changed else (db_state['server_starred_mtime'] or 0)
        
        star_winner, win_star_mtime = self._resolve_lww(srv_starred, f_starred, db_srv_star or 0, db_f_star or 0, new_s_star_mtime, new_f_star_mtime)
        
        if star_winner == 'server':
            t_star = srv_starred
            w_file_star, w_srv_star = True, False
            final_f_star_mtime, final_s_star_mtime = win_star_mtime, win_star_mtime
        elif star_winner == 'file':
            t_star = f_starred
            w_file_star, w_srv_star = False, True
            final_f_star_mtime, final_s_star_mtime = win_star_mtime, win_star_mtime
        elif star_winner == 'unresolved':
            t_star = srv_starred
            w_file_star, w_srv_star = False, False
            final_f_star_mtime = 0
            final_s_star_mtime = 0
            if is_new_file:
                logger.info(f"{prefix}ID {song.id} — ⚠️ КОНФЛИКТ ЛАЙКОВ (Нет данных о времени): Сервер={srv_starred}, Файл={f_starred}. Измените оценку в одном из мест. ({self._track_label(song, file_path)})")
        else:
            t_star = srv_starred
            w_file_star, w_srv_star = False, False
            final_f_star_mtime, final_s_star_mtime = new_f_star_mtime, new_s_star_mtime

        write_file = (w_file_star or w_file_rate) and self.sync_mode in ['two-way', 'server-to-file']
        write_server = (w_srv_star or w_srv_rate) and self.sync_mode in ['two-way', 'file-to-server']

        # --- БЛОК "НЕТ ИЗМЕНЕНИЙ" (С учетом блокировки режима) ---
        if not write_file and not write_server:
            if not self.config.get('dry_run', False):
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
                
                upsert_track_state(
                    song_id=song.id, file_path=file_path, mtime_ns=current_mtime,
                    f_starred=final_f_star, f_rating=final_f_rate,
                    s_starred=final_s_star, s_rating=final_s_rate,
                    f_rate_mtime=final_f_rate_mtime, s_rate_mtime=final_s_rate_mtime,
                    f_star_mtime=final_f_star_mtime, s_star_mtime=final_s_star_mtime
                )
            
            return False, False

        wf_str = self._get_action_str(write_file and w_file_star, write_file and w_file_rate, t_star, t_rate_os)
        ws_str = self._get_action_str(write_server and w_srv_star, write_server and w_srv_rate, t_star, t_rate_os)
        
        logger.info(
            f"{prefix}ID {song.id} — Обновляем файл={wf_str} | Обновляем сервер={ws_str} — "
            f"({self._track_label(song, file_path)})"
        )
        
        if self.config.get('dry_run', False):
            return write_file, write_server

        # Боевой режим
        actual_file_write = False
        actual_srv_write = False
        try:
            if write_file:
                # Если рейтинг изменился (в т.ч. стал 0), передаем его как есть. None передаем только если рейтинг не менялся (w_file_rate = False).
                r_val = t_rate_internal if w_file_rate else None
                # Если лайк изменился, передаем его как bool. None передаем только если лайк не менялся (w_file_star = False).
                s_val = bool(t_star) if w_file_star else None
                
                ratings.set_tags_to_file(file_path, rating=r_val, starred=s_val)
                actual_file_write = True  # Флаг означает, что файл физически обновлен
                if actual_file_write:
                    current_mtime = os.stat(file_path).st_mtime_ns

            if write_server:
                if w_srv_star:
                    if t_star == 1 and srv_starred == 0: 
                        self.conn.star(sids=[song.id])
                        actual_srv_write = True
                    elif t_star == 0 and srv_starred == 1: 
                        self.conn.unstar(sids=[song.id])
                        actual_srv_write = True
                if w_srv_rate:
                    if t_rate_os != srv_rating: 
                        self.conn.set_rating(song.id, t_rate_os)
                        actual_srv_write = True
        except Exception as e:
            logger.error(f"Ошибка записи для трека {song.id} | {self._track_label(song, file_path)}: {e}", exc_info=True)
            return False, False

        upsert_track_state(
            song_id=song.id, file_path=file_path, mtime_ns=current_mtime,
            f_starred=t_star if write_file else f_starred, 
            f_rating=t_rate_internal if write_file else f_rating_internal,
            s_starred=t_star if write_server else srv_starred, 
            s_rating=t_rate_os if write_server else srv_rating,
            f_rate_mtime=final_f_rate_mtime, s_rate_mtime=final_s_rate_mtime,
            f_star_mtime=final_f_star_mtime, s_star_mtime=final_s_star_mtime
        )
        return actual_file_write, actual_srv_write

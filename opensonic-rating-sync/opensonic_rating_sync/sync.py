import os
import logging
import math
from urllib.parse import unquote
from .locker import get_file_lock
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
        logger.info(f"Начало цикла синхронизации (режим: {self.sync_mode})...")
        
        starred_ids = self._fetch_starred_ids()
        logger.info(f"Найдено избранных (starred) треков на сервере: {len(starred_ids)}")
        
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

        logger.info("Цикл синхронизации завершен:")
        logger.info(f"   -  Обновлено файлов на ДИСКЕ: {disk_updates}")
        logger.info(f"   -  Обновлено файлов на СЕРВЕРЕ: {server_updates}")

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
        if not parts: return "Ничего"
        return " + ".join(parts)

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
            'server_starred': None, 'server_rating': None
        }

        if current_mtime != db_state['file_mtime_ns'] or db_state['file_mtime_ns'] == 0:
            # Теперь get_starred_from_file читает тег LOVE RATING
            f_starred = ratings.get_starred_from_file(file_path)
            f_rating_internal = ratings.get_rating_from_file(file_path)
        else:
            f_starred = db_state['file_starred'] if db_state['file_starred'] is not None else 0
            f_rating_internal = db_state['file_rating']

        # ИСПРАВЛЕНИЕ: Нормализуем None в 0
        f_rating_internal = f_rating_internal if f_rating_internal is not None else 0

        is_new_file = (db_state['file_mtime_ns'] == 0)
        
        # --- ЛОГИКА ДОПУСКА (TOLERANCE) 0.5 ---
        f_rating_5_scale = f_rating_internal / 2.0
        
        if abs(f_rating_5_scale - srv_rating) <= 0.5:
            t_rate_os = srv_rating
            t_rate_internal = f_rating_internal or (srv_rating * 2)
            w_file_rate, w_srv_rate = False, False
        else:
            # ИСПРАВЛЕНИЕ: Убрано избыточное условие, т.к. math.ceil(0/2) = 0
            f_rating_os = math.ceil(f_rating_internal / 2)
            db_srv_rating = db_state['server_rating'] or 0
            db_f_rating = db_state['file_rating'] or 0
            
            srv_changed = (srv_rating != db_srv_rating)
            f_changed = (f_rating_internal != db_f_rating)
            
            if srv_changed and not f_changed:
                t_rate_os = srv_rating
                t_rate_internal = srv_rating * 2
                w_file_rate, w_srv_rate = True, False
            elif not srv_changed and f_changed:
                t_rate_os = f_rating_os
                t_rate_internal = f_rating_internal
                w_file_rate, w_srv_rate = False, True
            else:
                # ИСПРАВЛЕНИЕ XAVIER v4 (ЭТАЛОННАЯ ЛОГИКА):
                # Мы попадаем сюда, если обе стороны изменились одновременно, 
                # ИЛИ если ни одна не изменилась, но значения расходятся (стабильная дивергенция от прошлого заблокированного режима).
                
                if not srv_changed and not f_changed:
                    # Стабильная дивергенция. Данные не менялись с прошлого цикла.
                    # Если они расходятся, значит так и должно быть (заблокировано режимом). Ничего не делаем!
                    t_rate_os = srv_rating
                    t_rate_internal = f_rating_internal
                    w_file_rate, w_srv_rate = False, False
                else:
                    # Реальный конфликт: обе стороны изменились одновременно с момента последней синхронизации.
                    conflict_res = self.config.get('conflict_resolution', 'server_wins')
                    if conflict_res == 'server_wins':
                        t_rate_os = srv_rating
                        t_rate_internal = srv_rating * 2
                        w_file_rate, w_srv_rate = True, False
                    else: # file_wins
                        t_rate_os = f_rating_os
                        t_rate_internal = f_rating_internal
                        w_file_rate, w_srv_rate = False, True

        t_star, w_file_star, w_srv_star = self._resolve_conflict(
            srv_starred, f_starred, db_state['server_starred'], db_state['file_starred']
        )

        write_file = (w_file_star or w_file_rate) and self.sync_mode in ['two-way', 'server-to-file']
        write_server = (w_srv_star or w_srv_rate) and self.sync_mode in ['two-way', 'file-to-server']

        # --- ИСПРАВЛЕНИЕ: Единый блок обработки "Нет изменений" ---
        if not write_file and not write_server:
            if not self.config.get('dry_run', False):
                # ИСПРАВЛЕНИЕ XAVIER v3: Финальная логика предотвращения зацикливания
                blocked_by_mode = (w_file_star or w_file_rate or w_srv_star or w_srv_rate)
                
                if blocked_by_mode:
                    # Сохраняем ТЕКУЩИЕ прочитанные значения (чтобы не потерять изменения),
                    # но НЕ обновляем консенсус, так как синхронизация не произошла.
                    final_f_star = f_starred
                    final_s_star = srv_starred
                    final_f_rate = f_rating_internal
                    final_s_rate = srv_rating
                else:
                    # Изменений не было вообще (или сработал допуск 0.5). 
                    # Сохраняем текущий консенсус.
                    final_f_star = t_star
                    final_s_star = t_star
                    final_f_rate = t_rate_internal
                    final_s_rate = t_rate_os
                
                upsert_track_state(
                    song_id=song.id, file_path=file_path, mtime_ns=current_mtime,
                    f_starred=final_f_star, f_rating=final_f_rate,
                    s_starred=final_s_star, s_rating=final_s_rate
                )
            
            # Лог конфликта выводим только при первом запуске и если расхождение больше 0.5
            if is_new_file and abs(f_rating_5_scale - srv_rating) > 0.5:
                prefix = "[DRY-RUN] " if self.config.get('dry_run', False) else ""
                logger.info(
                    f"{prefix}ID {song.id} — ⚠️ КОНФЛИКТ ПРИ ПЕРВОМ ЗАПУСКЕ: Сервер={srv_rating}★, Файл={f_rating_5_scale:g}★. "
                    f"Данные оставлены без изменений. Измените оценку в одном из мест для синхронизации. "
                    f"({self._track_label(song, file_path)})"
                )
            return False, False

        prefix = "[DRY-RUN] " if self.config.get('dry_run', False) else ""
        wf_str = self._get_action_str(w_file_star, w_file_rate, t_star, t_rate_os)
        ws_str = self._get_action_str(w_srv_star, w_srv_rate, t_star, t_rate_os)
        
        logger.info(
            f"{prefix}ID {song.id} — Обновляем файл={wf_str} | Обновляем сервер={ws_str} — "
            f"({self._track_label(song, file_path)})"
        )
        
        if self.config.get('dry_run', False):
            return write_file, write_server

        # Боевой режим
        lock = get_file_lock(song.id)
        actual_file_write = False
        actual_srv_write = False
        try:
            with lock:
                if write_file:
                    if w_file_star:
                        ratings.set_starred_to_file(file_path, bool(t_star))
                        actual_file_write = True
                    if w_file_rate:
                        t_rate_internal_none = t_rate_internal if t_rate_internal > 0 else None
                        ratings.set_rating_to_file(file_path, t_rate_internal_none)
                        actual_file_write = True
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
            logger.error(f"Ошибка блокировки/записи для трека {song.id} | {self._track_label(song, file_path)}: {e}", exc_info=True)
            return False, False

        # ИСПРАВЛЕНИЕ XAVIER: Сохраняем фактические состояния после боевой записи
        final_f_star = t_star if write_file else f_starred
        final_s_star = t_star if write_server else srv_starred
        final_f_rate = t_rate_internal if write_file else f_rating_internal
        final_s_rate = t_rate_os if write_server else srv_rating

        upsert_track_state(
            song_id=song.id, file_path=file_path, mtime_ns=current_mtime,
            f_starred=final_f_star, f_rating=final_f_rate,
            s_starred=final_s_star, s_rating=final_s_rate
        )
        return actual_file_write, actual_srv_write

    def _resolve_conflict(self, srv_val, f_val, db_srv_val, db_f_val):
        srv_val = 0 if srv_val is None else srv_val
        f_val = 0 if f_val is None else f_val
        db_srv_val = 0 if db_srv_val is None else db_srv_val
        db_f_val = 0 if db_f_val is None else db_f_val

        srv_changed = (srv_val != db_srv_val)
        f_changed = (f_val != db_f_val)

        if not srv_changed and not f_changed: return srv_val, False, False
        if srv_changed and not f_changed: return srv_val, True, False
        if not srv_changed and f_changed: return f_val, False, True
        if srv_changed and f_changed:
            if srv_val == f_val: return srv_val, False, False
            conflict_res = self.config.get('conflict_resolution', 'server_wins')
            if conflict_res == 'server_wins': return srv_val, True, False
            elif conflict_res == 'file_wins': return f_val, False, True
        return srv_val, False, False

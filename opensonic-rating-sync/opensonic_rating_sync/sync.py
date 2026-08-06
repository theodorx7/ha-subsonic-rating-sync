import os
import logging
from urllib.parse import unquote
from .locker import get_file_lock
from .database import get_track_state, upsert_track_state
from . import ratings
import libopensonic

logger = logging.getLogger(__name__)

class SyncAgent:
    def __init__(self, config):
        self.config = config
        # --- НОВОЕ: Передаем выбранные плееры в модуль рейтингов ---
        active_players = config.get('players', ['musicbee'])
        ratings.set_active_players(active_players)
        # -----------------------------------------------------------
        
        host = config['server_host'].strip().lower()
        if host.startswith('https://'):
            host = host[8:]
        elif host.startswith('http://'):
            host = host[7:]
        host = host.split('/')[0].split(':')[0]
        base_url = f"{config['server_protocol']}://{host}"
        
        api_key = config.get('api_key') or None
        username = config.get('user') or None
        password = config.get('password') or None
        
        conn_kwargs = {
            'base_url': base_url,
            'port': config['server_port'],
            'app_name': "Rating Sync Agent"
        }
        if api_key:
            conn_kwargs['api_key'] = api_key
            auth_method = "API Key"
        else:
            conn_kwargs['username'] = username
            conn_kwargs['password'] = password
            auth_method = "Username/Password"
            
        self.conn = libopensonic.Connection(**conn_kwargs)
        try:
            ok = self.conn.ping()
            if not ok:
                raise ConnectionError("ping() вернул False")
            logger.info(f"Подключение к Navidrome установлено: {base_url}:{config['server_port']} ({auth_method}, ping OK)")
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
        logger.info("Начало цикла синхронизации...")
        
        # 1. Сначала получаем надежный список ID треков, у которых стоит лайк (starred)
        starred_ids = self._fetch_starred_ids()
        logger.info(f"Найдено избранных (starred) треков на сервере: {len(starred_ids)}")
        
        # 2. Получаем всю библиотеку для проверки рейтингов и путей
        server_songs = self._fetch_all_server_songs()
        logger.info(f"Найдено треков на сервере: {len(server_songs)}")
        
        disk_updates = 0
        server_updates = 0
        
        for song in server_songs:
            try:
                # Передаем множество starred_ids для точного определения лайка
                u_file, u_server = self._process_song(song, starred_ids)
                if u_file: disk_updates += 1
                if u_server: server_updates += 1
            except Exception as e:
                logger.error(f"Ошибка при обработке трека {self._track_label(song)}: {e}", exc_info=True)

        logger.info(f"Цикл синхронизации завершен:")
        logger.info(f"   -  Обновлено файлов на ДИСКЕ {disk_updates}")
        logger.info(f"   -  Обновлено файлов на СЕРВЕРЕ {server_updates}")

    def _fetch_starred_ids(self):
        """
        Надежный способ получения избранных треков через getStarred2.
        Согласно OpenSubsonic API, возвращает только залайканные элементы.
        """
        starred_ids = set()
        try:
            mf_id = self.config.get('music_folder_id') or None
            
            result = self.conn.get_starred2(music_folder_id=mf_id) if mf_id else self.conn.get_starred2()
            
            if result and result.song:
                for s in result.song:
                    starred_ids.add(s.id)
        except Exception as e:
            logger.error(f"Ошибка при получении избранных треков (get_starred2): {e}", exc_info=True)
            
        return starred_ids

    def _fetch_all_server_songs(self):
        """
        Получаем все треки из библиотеки через search3.
        """
        songs = []
        mf_id = self.config.get('music_folder_id') or None
        
        if not mf_id:
            logger.error("Не указан music_folder_id в настройках аддона!")
            return songs
            
        try:
            logger.info(f"Запрос треков (search3) для библиотеки ID={mf_id}...")
            
            offset = 0
            count_per_request = 500  # Безопасный лимит для одного запроса
            
            while True:
                result = self.conn.search3(
                    query="",
                    song_count=count_per_request,
                    song_offset=offset,
                    music_folder_id=mf_id
                )
                
                if not result or not result.song:
                    break
                    
                songs.extend(result.song)
                logger.debug(f"Получено треков: {len(result.song)} (всего: {len(songs)})")
                
                if len(result.song) < count_per_request:
                    break
                    
                offset += count_per_request
                
        except Exception as e:
            logger.error(f"Ошибка при получении треков (search3): {e}", exc_info=True)
            
        return songs

    def _get_action_str(self, do_write, star_val, rate_val):
        """Форматирует строку действий для логов DRY-RUN"""
        if not do_write:
            return "FALSE"
        
        heart = "❤️" if star_val == 1 else ""
        stars = "⭐" * rate_val if rate_val > 0 else ""
        
        # Если мы очищаем и лайк, и рейтинг
        if not heart and not stars:
            return "❌ (Сброс)"
            
        # Склеиваем сердце и звезды. Если сердца нет, slash удалится автоматически
        return f"{heart}/{stars}".strip("/")
    
    def _process_song(self, song, starred_ids):
        srv_starred = 1 if song.id in starred_ids else 0
        # ВОЗВРАЩАЕМ ваш надежный парсинг рейтинга
        srv_rating = getattr(song, 'userRating', getattr(song, 'user_rating', 0)) or 0
    
        if not getattr(song, 'path', None):
            logger.warning(f"Трек {song.id} | {self._track_label(song)} не имеет атрибута path. Пропуск.")
            return False, False
    
        raw_path = unquote(song.path)
        if os.path.isabs(raw_path):
            file_path = os.path.normpath(raw_path)
        else:
            base_folder = self.config.get('music_folder', '').strip()
            if not base_folder:
                logger.warning(f"Трек {self._track_label(song)}: Сервер вернул относительный путь ('{raw_path}'), но опция 'music_folder' не настроена в аддоне. Синхронизация этого файла невозможна. Пропуск.")
                return False, False
            file_path = os.path.normpath(os.path.join(base_folder, raw_path.lstrip('/')))
    
        if not os.path.exists(file_path):
            logger.warning(f"Файл не найден на диске: {file_path} (Трек: {self._track_label(song)})")
            return False, False

        current_mtime = os.stat(file_path).st_mtime_ns
        db_state = get_track_state(song.id)
        
        if not db_state:
            db_state = {
                'file_mtime_ns': 0, 'file_starred': None, 'file_rating': None,
                'server_starred': None, 'server_rating': None
            }

        if current_mtime != db_state['file_mtime_ns'] or db_state['file_mtime_ns'] == 0:
            f_starred = ratings.get_starred_from_file(file_path)
            f_rating_internal = ratings.get_rating_from_file(file_path)
        else:
            f_starred = db_state['file_starred'] if db_state['file_starred'] is not None else 0
            f_rating_internal = db_state['file_rating']

        f_rating_os = 0
        if f_rating_internal is not None and f_rating_internal > 0:
            f_rating_os = round(f_rating_internal / 2)

        t_star, w_file_star, w_srv_star = self._resolve_conflict(
            srv_starred, f_starred, db_state['server_starred'], db_state['file_starred']
        )
        
        db_f_rating_os = round(db_state['file_rating'] / 2) if db_state['file_rating'] else 0
        t_rate_os, w_file_rate, w_srv_rate = self._resolve_conflict(
            srv_rating, f_rating_os, db_state['server_rating'], db_f_rating_os
        )

        write_file = w_file_star or w_file_rate
        write_server = w_srv_star or w_srv_rate

        # ВОЗВРАЩАЕМ ранний выход, чтобы не спамить логами об unchanged треках
        if not write_file and not write_server:
            return False, False

        # ВОЗВРАЩАЕМ единое красивое логирование
        prefix = "[DRY-RUN] " if self.config.get('dry_run', False) else ""
        wf_str = self._get_action_str(write_file, t_star, t_rate_os)
        ws_str = self._get_action_str(write_server, t_star, t_rate_os)
        
        logger.info(
            f"{prefix}ID {song.id} — Обновляем файл={wf_str} | Обновляем сервер={ws_str} — "
            f"{self._track_label(song, file_path)}"
        )

        # Если Dry-Run — на этом заканчиваем
        if self.config.get('dry_run', False):
            return write_file, write_server

        # Боевой режим
        lock = get_file_lock(song.id)
        try:
            with lock:
                if write_file:
                    # ВАЖНО: Передаем None вместо 0, чтобы ratings.py удалил тег рейтинга
                    t_rate_internal = t_rate_os * 2 if t_rate_os > 0 else None
                    ratings.set_starred_to_file(file_path, t_star)
                    ratings.set_rating_to_file(file_path, t_rate_internal)
                    current_mtime = os.stat(file_path).st_mtime_ns
                
                if write_server:
                    if t_star == 1 and srv_starred == 0: 
                        self.conn.star(sids=[song.id])
                    elif t_star == 0 and srv_starred == 1: 
                        self.conn.unstar(sids=[song.id])
                    if t_rate_os != srv_rating: 
                        self.conn.set_rating(song.id, t_rate_os)
        except Exception as e:
            logger.error(f"Ошибка блокировки/записи для трека {song.id} | {self._track_label(song, file_path)}: {e}", exc_info=True)
            return False, False

        # Обновляем БД только при реальной записи!
        final_f_rating = t_rate_os * 2 if t_rate_os > 0 else 0
        upsert_track_state(
            song_id=song.id, file_path=file_path, mtime_ns=current_mtime,
            f_starred=t_star, f_rating=final_f_rating,
            s_starred=t_star, s_rating=t_rate_os
        )
        
        return write_file, write_server

    def _resolve_conflict(self, srv_val, f_val, db_srv_val, db_f_val):
        srv_val = 0 if srv_val is None else srv_val
        f_val = 0 if f_val is None else f_val
        db_srv_val = 0 if db_srv_val is None else db_srv_val
        db_f_val = 0 if db_f_val is None else db_f_val

        srv_changed = (srv_val != db_srv_val)
        f_changed = (f_val != db_f_val)

        if not srv_changed and not f_changed:
            return srv_val, False, False
        if srv_changed and not f_changed:
            return srv_val, True, False
        if not srv_changed and f_changed:
            return f_val, False, True
        if srv_changed and f_changed:
            if srv_val == f_val: 
                return srv_val, False, False
            conflict_res = self.config.get('conflict_resolution', 'server_wins')
            if conflict_res == 'server_wins':
                return srv_val, True, False
            elif conflict_res == 'file_wins':
                return f_val, False, True
        return srv_val, False, False

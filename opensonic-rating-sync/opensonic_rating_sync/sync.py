import os
from urllib.parse import unquote
from .logger import setup_logger
from .locker import get_file_lock
from .database import get_track_state, upsert_track_state
from . import ratings
import libopensonic

logger = setup_logger()

class SyncAgent:
    def __init__(self, config):
        self.config = config

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
            'app_name': "ha-subsonic-rating-sync"
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

    def run_sync(self):
        logger.info("Начало цикла синхронизации...")
        server_songs = self._fetch_all_server_songs()
        logger.info(f"Найдено треков на сервере: {len(server_songs)}")
        
        for song in server_songs:
            try:
                self._process_song(song)
            except Exception as e:
                logger.error(f"Ошибка при обработке трека {getattr(song, 'id', 'Unknown')}: {e}", exc_info=True)

        logger.info("Цикл синхронизации завершен.")

    def _fetch_all_server_songs(self):
            """
            Сканирует медиатеку по файловой структуре через get_indexes и get_music_directory 
            с защитой от NoneType элементов.
            """
            songs = []
            mf_id = self.config.get('music_folder_id') or None
            
            try:
                indexes = self.conn.get_indexes(music_folder_id=mf_id)
            except Exception as e:
                logger.error(f"Ошибка получения корневых индексов (папок): {e}")
                return songs
    
            if not indexes:
                return songs
    
            folders_to_scan = []
    
            # 1. Безопасно проверяем алфавитные индексы артистов
            if hasattr(indexes, 'index') and indexes.index:
                for idx in indexes.index:
                    if hasattr(idx, 'artist') and idx.artist:
                        for artist in idx.artist:
                            if hasattr(artist, 'id'):
                                folders_to_scan.append(artist.id)
            
            # 2. Безопасно проверяем прямые дочерние элементы в корне (если они есть и не None)
            if hasattr(indexes, 'child') and indexes.child:
                for child in indexes.child:
                    if child is not None:
                        if getattr(child, 'is_dir', getattr(child, 'isDir', False)):
                            folders_to_scan.append(child.id)
                        else:
                            songs.append(child)
    
            # 3. Рекурсивный обход дерева папок
            while folders_to_scan:
                current_folder_id = folders_to_scan.pop(0)
                try:
                    directory = self.conn.get_music_directory(current_folder_id)
                    if directory and hasattr(directory, 'child') and directory.child:
                        for child in directory.child:
                            if child is not None:
                                if getattr(child, 'is_dir', getattr(child, 'isDir', False)):
                                    folders_to_scan.append(child.id)
                                else:
                                    songs.append(child)
                except Exception as e:
                    logger.error(f"Ошибка при сканировании директории {current_folder_id}: {e}")
    
            return songs

    def _process_song(self, song):
        srv_starred = 1 if getattr(song, 'starred', None) else 0
        srv_rating = getattr(song, 'user_rating', 0) or 0

        if not hasattr(song, 'path') or not song.path:
            logger.warning(f"Трек {song.id} не имеет атрибута path. Пропуск.")
            return
        # 1. Декодируем URL (если Navidrome передал %20 и т.д.) и убираем начальный слеш
        relative_path = unquote(song.path).lstrip('/')
        
        # 2. Склеиваем базовую папку и относительный путь, нормализуем слеши
        base_folder = self.config['music_folder'].rstrip('/')
        file_path = os.path.normpath(os.path.join(base_folder, relative_path))
        
        # 3. КРИТИЧЕСКАЯ ПРОВЕРКА существования файла
        if not os.path.exists(file_path):
            logger.warning(f"Файл не найден на диске: {file_path}")
            return

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

        if self.config.get('dry_run', False):
            logger.info(f"[DRY-RUN] Трек {song.id}: Пишем файл={write_file}, Пишем сервер={write_server}")
        else:
            lock = get_file_lock(song.id)
            try:
                with lock:
                    if write_file:
                        t_rate_internal = t_rate_os * 2 if t_rate_os > 0 else 0
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
                logger.error(f"Ошибка блокировки/записи для {song.id}: {e}", exc_info=True)
                return

        final_f_rating = t_rate_os * 2 if t_rate_os > 0 else 0
        upsert_track_state(
            song_id=song.id, file_path=file_path, mtime_ns=current_mtime,
            f_starred=t_star, f_rating=final_f_rating,
            s_starred=t_star, s_rating=t_rate_os
        )

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

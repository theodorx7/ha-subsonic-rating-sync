import os
from .logger import setup_logger
from .locker import get_file_lock
from .database import get_track_state, upsert_track_state
from . import ratings
import libopensonic

logger = setup_logger()

class SyncAgent:
    def __init__(self, config):
        self.config = config
        # Извлекаем порт из URL, если он там есть, иначе по умолчанию
        port = 443
        if ":" in config['navidrome_url'].split("//")[-1]:
            port = int(config['navidrome_url'].split(":")[-1])
            
        self.conn = libopensonic.Connection(
            baseUrl=config['navidrome_url'],
            username=config['navidrome_user'],
            password=config['navidrome_password'],
            appName="HA-StarSync",
            port=port
        )
        logger.info("Подключение к Navidrome установлено.")

    def run_sync(self):
        logger.info("Начало цикла синхронизации...")
        server_songs = self._fetch_all_server_songs()
        logger.info(f"Найдено треков на сервере: {len(server_songs)}")
        
        for song in server_songs:
            try:
                self._process_song(song)
            except Exception as e:
                logger.error(f"Ошибка при обработке трека {getattr(song, 'id', 'Unknown')}: {e}")

        logger.info("Цикл синхронизации завершен.")

    def _fetch_all_server_songs(self):
        """Обход библиотеки Navidrome через OpenSubsonic API."""
        songs = []
        offset = 0
        size = 500 # Пагинация
        
        while True:
            try:
                # get_album_list2 возвращает объект AlbumList2
                res = self.conn.get_album_list2(ltype="alphabeticalByName", size=size, offset=offset)
                if not res or not hasattr(res, 'album') or not res.album:
                    break
                
                for album in res.album:
                    # get_album возвращает AlbumWithSongsID3
                    album_data = self.conn.get_album(album.id)
                    if album_data and hasattr(album_data, 'song') and album_data.song:
                        songs.extend(album_data.song)
                
                if len(res.album) < size:
                    break
                offset += size
            except Exception as e:
                logger.error(f"Ошибка при получении списка альбомов: {e}")
                break
                
        return songs

    def _process_song(self, song):
        # 1. Текущие данные с сервера (0-5)
        srv_starred = 1 if song.starred else 0
        srv_rating = song.user_rating if song.user_rating else 0

        # 2. Формируем путь
        if not hasattr(song, 'path') or not song.path:
            logger.warning(f"Трек {song.id} не имеет атрибута path. Пропуск.")
            return

        file_path = os.path.join(self.config['music_folder'], song.path)
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

        # 3. Читаем теги файла только если mtime изменилось
        if current_mtime != db_state['file_mtime_ns'] or db_state['file_mtime_ns'] == 0:
            f_starred = ratings.get_starred_from_file(file_path)
            f_rating_internal = ratings.get_rating_from_file(file_path)
        else:
            f_starred = db_state['file_starred'] if db_state['file_starred'] is not None else 0
            f_rating_internal = db_state['file_rating']

        # Конвертация шкалы файла (1-10) в шкалу сервера (0-5)
        f_rating_os = 0
        if f_rating_internal is not None and f_rating_internal > 0:
            f_rating_os = round(f_rating_internal / 2)

        # 4. МАТРИЦА РЕШЕНИЙ
        t_star, w_file_star, w_srv_star = self._resolve_conflict(
            srv_starred, f_starred, db_state['server_starred'], db_state['file_starred']
        )
        
        # Для рейтинга передаем значения, нормализованные к 0
        db_f_rating_os = round(db_state['file_rating'] / 2) if db_state['file_rating'] else 0
        t_rate_os, w_file_rate, w_srv_rate = self._resolve_conflict(
            srv_rating, f_rating_os, db_state['server_rating'], db_f_rating_os
        )

        write_file = w_file_star or w_file_rate
        write_server = w_srv_star or w_srv_rate

        # 5. Применение изменений
        if self.config.get('dry_run', False):
            logger.info(f"[DRY-RUN] Трек {song.id}: Пишем файл={write_file}, Пишем сервер={write_server}")
        else:
            lock = get_file_lock(song.id)
            try:
                with lock:
                    if write_file:
                        # Конвертация шкалы сервера (0-5) в шкалу файла (1-10)
                        t_rate_internal = t_rate_os * 2 if t_rate_os > 0 else 0
                        ratings.set_starred_to_file(file_path, t_star)
                        ratings.set_rating_to_file(file_path, t_rate_internal)
                        current_mtime = os.stat(file_path).st_mtime_ns
                    
                    if write_server:
                        if t_star == 1 and srv_starred == 0: 
                            self.conn.star(id=song.id)
                        elif t_star == 0 and srv_starred == 1: 
                            self.conn.unstar(id=song.id)
                        if t_rate_os != srv_rating: 
                            self.conn.set_rating(song.id, t_rate_os)
            except Exception as e:
                logger.error(f"Ошибка блокировки/записи для {song.id}: {e}")
                return

        # 6. Сохраняем финальное состояние в БД
        final_f_rating = t_rate_os * 2 if t_rate_os > 0 else 0
        upsert_track_state(
            song_id=song.id, file_path=file_path, mtime_ns=current_mtime,
            f_starred=t_star, f_rating=final_f_rating,
            s_starred=t_star, s_rating=t_rate_os
        )

    def _resolve_conflict(self, srv_val, f_val, db_srv_val, db_f_val):
        """Универсальная матрица решений. Возвращает (target_value, write_file, write_server)"""
        # Нормализация None
        srv_val = 0 if srv_val is None else srv_val
        f_val = 0 if f_val is None else f_val
        db_srv_val = 0 if db_srv_val is None else db_srv_val
        db_f_val = 0 if db_f_val is None else db_f_val

        srv_changed = (srv_val != db_srv_val)
        f_changed = (f_val != db_f_val)

        # 1. Ничего не изменилось
        if not srv_changed and not f_changed:
            return srv_val, False, False
        
        # 2. Изменился только сервер
        if srv_changed and not f_changed:
            return srv_val, True, False
        
        # 3. Изменился только файл
        if not srv_changed and f_changed:
            return f_val, False, True
        
        # 4. Изменились обе стороны (Конфликт)
        if srv_changed and f_changed:
            if srv_val == f_val: 
                return srv_val, False, False # Разрешено само собой
            
            if self.config['conflict_resolution'] == 'server_wins':
                return srv_val, True, False
            elif self.config['conflict_resolution'] == 'file_wins':
                return f_val, False, True
        
        return srv_val, False, False

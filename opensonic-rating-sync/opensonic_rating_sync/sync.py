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
        server_songs = self._fetch_all_server_songs()
        logger.info(f"Найдено треков на сервере: {len(server_songs)}")
        
        for song in server_songs:
            try:
                self._process_song(song)
            except Exception as e:
                # Заменил getattr(song, 'id', 'Unknown') на вызов хэлпера
                logger.error(f"Ошибка при обработке трека {self._track_label(song)}: {e}", exc_info=True)

        logger.info("Цикл синхронизации завершен.")

    def _fetch_all_server_songs(self):
        """
        Получаем все треки из библиотеки через search3.
        Navidrome не поддерживает browse-by-folder, поэтому getIndexes/getMusicDirectory
        возвращают симулированное дерево по ID3-тегам. Прямой запрос search3 надёжнее.
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
                
                # Если получили меньше запрошенного — это последняя страница
                if len(result.song) < count_per_request:
                    break
                    
                offset += count_per_request
                
        except Exception as e:
            logger.error(f"Ошибка при получении треков (search3): {e}", exc_info=True)
            
        return songs

    def _process_song(self, song):
        srv_starred = 1 if getattr(song, 'starred', None) else 0
        srv_rating = getattr(song, 'user_rating', 0) or 0
    
        if not getattr(song, 'path', None):
            logger.warning(f"Трек {song.id} | {self._track_label(song)} не имеет атрибута path. Пропуск.")
            return
    
        # Navidrome с "Report Full Path" отдаёт АБСОЛЮТНЫЙ путь.
        # Без этой опции (или для Airsonic) — относительный вида Artist/Album/Track.
        raw_path = unquote(song.path)
        if os.path.isabs(raw_path):
            # Navidrome с включенной опцией работает здесь идеально
            file_path = os.path.normpath(raw_path)
        else:
            # Сюда попадут Airsonic/Gonic (отдают реальный, но относительный путь)
            # И Navidrome с выключенной опцией (отдает путь из тегов)
            base_folder = self.config.get('music_folder', '').strip()
            
            if not base_folder:
                logger.warning(f"Трек {self._track_label(song)}: Сервер вернул относительный путь ('{raw_path}'), но опция 'music_folder' не настроена в аддоне. Синхронизация этого файла невозможна. Пропуск.")
                return
            
            file_path = os.path.normpath(os.path.join(base_folder, raw_path.lstrip('/')))
    
        if not os.path.exists(file_path):
            logger.warning(f"Файл не найден на диске: {file_path} (Трек: {self._track_label(song)})")
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
            # Используем эмодзи, так как веб-интерфейс HA не поддерживает ANSI-цвета
            wf_str = "🟢 TRUE" if write_file else "⚪ FALSE"
            ws_str = "🟢 TRUE" if write_server else "⚪ FALSE"
            
            logger.info(
                f"[DRY-RUN] Трек {song.id} — Обновляем файл={wf_str}, Обновляем сервер={ws_str} — "
                f"{self._track_label(song, file_path)}"
            )
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
                # Заменено на вызов хэлпера
                logger.error(f"Ошибка блокировки/записи для трека {song.id} | {self._track_label(song, file_path)}: {e}", exc_info=True)
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

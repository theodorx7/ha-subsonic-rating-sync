import os
import logging
from filelock import FileLock, Timeout
import libopensonic
from .database import get_track_state, upsert_track_state
from .ratings import get_tags_from_file, set_tags_to_file # Предполагается, что ratings.py будет реализован

logger = logging.getLogger(__name__)

class SyncAgent:
    def __init__(self, config):
        self.config = config
        # Инициализация подключения к Navidrome через py-opensonic
        self.conn = libopensonic.Connection(
            baseUrl=config['navidrome_url'],
            username=config['navidrome_user'],
            password=config['navidrome_password'],
            appName=config.get('navidrome_client_name', 'HA-StarSync'),
            port=config.get('navidrome_port', 443)
        )
        # Папка для временных lock-файлов
        self.lock_dir = "/data/locks"
        os.makedirs(self.lock_dir, exist_ok=True)

    def run_sync(self):
        """Главный цикл синхронизации."""
        logger.info("Начало цикла синхронизации...")
        
        # 1. Получаем весь список песен с сервера (через search3 или обход по артистам)
        # В реальном коде здесь будет пагинация
        server_songs = self._fetch_all_server_songs()
        
        for song in server_songs:
            try:
                self._process_song(song)
            except Exception as e:
                logger.error(f"Ошибка при обработке трека {song.id}: {e}")

        logger.info("Цикл синхронизации завершен.")

    def _process_song(self, song):
        """Обработка одного трека."""
        # 1. Определяем текущее состояние на сервере
        srv_starred = 1 if song.starred else 0
        srv_rating = song.user_rating if song.user_rating else 0

        # 2. Формируем абсолютный путь к файлу
        # song.path обычно относительный, например "Artist/Album/track.flac"
        file_path = os.path.join(self.config['music_folder'], song.path)
        if not os.path.exists(file_path):
            logger.warning(f"Файл не найден на диске: {file_path}")
            return

        # 3. Читаем текущее состояние файла и его mtime
        current_mtime = os.stat(file_path).st_mtime_ns
        db_state = get_track_state(song.id)
        
        # Если файла нет в БД (новый трек), инициализируем пустое состояние
        if not db_state:
            db_state = {
                'file_mtime_ns': 0, 'file_starred': None, 'file_rating': None,
                'server_starred': None, 'server_rating': None
            }

        # Читаем теги файла только если его mtime изменилось с прошлого цикла
        # ИЛИ если это первичная синхронизация (в БД mtime = 0)
        if current_mtime != db_state['file_mtime_ns'] or db_state['file_mtime_ns'] == 0:
            f_starred, f_rating = get_tags_from_file(file_path)
            file_changed = True
        else:
            # Файл не менялся, берем данные из БД
            f_starred = db_state['file_starred']
            f_rating = db_state['file_rating']
            file_changed = False

        # 4. Матрица решений (Двусторонняя синхронизация)
        # Результат: (target_starred, target_rating, need_write_to_file, need_write_to_server)
        t_star, t_rate, write_file, write_server = self._resolve_conflict(
            srv_starred, srv_rating,
            f_starred, f_rating,
            db_state['server_starred'], db_state['server_rating'],
            db_state['file_starred'], db_state['file_rating']
        )

        # 5. Применение изменений
        if self.config.get('dry_run', False):
            logger.info(f"[DRY-RUN] Трек {song.id}: Пишем в файл={write_file}, Пишем на сервер={write_server}")
        else:
            # Блокировка файла перед записью
            lock_path = os.path.join(self.lock_dir, f"{song.id}.lock")
            lock = FileLock(lock_path, timeout=10)
            
            try:
                with lock:
                    if write_file:
                        set_tags_to_file(file_path, t_star, t_rate)
                        # Перечитываем mtime после модификации тегов
                        current_mtime = os.stat(file_path).st_mtime_ns
                    
                    if write_server:
                        if t_star == 1 and srv_starred == 0:
                            self.conn.star(id=song.id)
                        elif t_star == 0 and srv_starred == 1:
                            self.conn.unstar(id=song.id)
                        
                        if t_rate != srv_rating:
                            self.conn.set_rating(song.id, t_rate)
            except Timeout:
                logger.error(f"Не удалось получить блокировку файла для {song.id}")
                return

        # 6. Сохраняем финальное состояние в БД
        upsert_track_state(
            song_id=song.id,
            file_path=file_path,
            mtime_ns=current_mtime,
            f_starred=t_star,
            f_rating=t_rate,
            s_starred=t_star, # После успешной синхронизации состояния равны
            s_rating=t_rate
        )

    def _resolve_conflict(self, srv_val, f_val, db_srv_val, db_f_val):
        """
        Универсальная матрица решений для одного параметра (звезда или рейтинг).
        Возвращает: (target_value, need_write_file, need_write_server)
        """
        # Определяем, изменилась ли сторона с прошлого цикла (сравнение с БД)
        srv_changed = (srv_val != db_srv_val)
        f_changed = (f_val != db_f_val)

        # Сценарий 1: Ничего не изменилось, но данные разошлись (например, БД была сброшена)
        if not srv_changed and not f_changed and srv_val != f_val:
            # Применяем политику по умолчанию (сервер всегда прав)
            return srv_val, True, False

        # Сценарий 2: Изменился только сервер
        if srv_changed and not f_changed:
            return srv_val, True, False # Пишем в файл

        # Сценарий 3: Изменился только файл
        if not srv_changed and f_changed:
            return f_val, False, True # Пишем на сервер

        # Сценарий 4: Изменились обе стороны (Конфликт)
        if srv_changed and f_changed:
            if srv_val == f_val:
                # Изменились одинаково - конфликта нет
                return srv_val, False, False
            
            # Реальный конфликт. Разрешаем через конфиг
            if self.config['conflict_resolution'] == 'server_wins':
                return srv_val, True, False
            elif self.config['conflict_resolution'] == 'file_wins':
                return f_val, False, True
            else:
                # Fallback
                return srv_val, True, False

        # Сценарий 5: Первичная синхронизация (в БД значения NULL)
        if db_srv_val is None or db_f_val is None:
            if srv_val != f_val:
                # По умолчанию при первичном импорте сервер выигрывает
                return srv_val, True, False
            return srv_val, False, False

        # Сценарий 6: Всё синхронизировано
        return srv_val, False, False

    def _resolve_conflict_wrapper(self, srv_star, srv_rate, f_star, f_rate, db_srv_star, db_srv_rate, db_f_star, db_f_rate):
        """Обертка для вызова матрицы решений отдельно для звезды и для рейтинга."""
        t_star, w_file_star, w_srv_star = self._resolve_conflict(srv_star, f_star, db_srv_star, db_f_star)
        t_rate, w_file_rate, w_srv_rate = self._resolve_conflict(srv_rate, f_rate, db_srv_rate, db_f_rate)
        
        # Агрегируем флаги записи
        write_file = w_file_star or w_file_rate
        write_server = w_srv_star or w_srv_rate
        
        return t_star, t_rate, write_file, write_server

    def _fetch_all_server_songs(self):
        """Получение всех треков из Navidrome через OpenSubsonic API."""
        # Здесь будет реализован обход search3 или getArtists -> getAlbum -> getSong
        # Для экономии трафика запрашиваем только поля: id, path, starred, userRating
        # Пока заглушка
        return []

    def _convert_os_to_internal(os_rating):
        """0-5 из Navidrome -> 1-10 для mutagen"""
        if os_rating is None or os_rating == 0: return 0
        return os_rating * 2

    def _convert_internal_to_os(internal_rating):
        """1-10 из mutagen -> 0-5 для Navidrome"""
        if internal_rating is None or internal_rating == 0: return 0
        return round(internal_rating / 2)
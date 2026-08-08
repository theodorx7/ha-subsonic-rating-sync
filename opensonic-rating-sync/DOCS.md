![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg

# Home Assistant App: Navidrome Rating Sync
## The app is under active development and currently non-functional. Keep an eye out for updates on the full public release.

Syncing 5-star ratings and favorites (LOVE RATING) between audio files and Navidrome (Subsonic/Opensonic API). Supported sync modes: two-way, one-way (Files → Navidrome), or (Navidrome → Files).

  
    Важно по настройке опции music_folder:

         Для Navidrome: Убедитесь, что в настройках вашего плеера/агента в Navidrome включена опция "Показать реальный путь" (Report Full Path). При включенной опции поле music_folder в настройках аддона можно оставить пустым.
         С серверами Airsonic, Gonic и других Subsonic-серверов: Эти серверы отдают путь к файлам относительно корня библиотеки. Вам обязательно нужно указать в поле music_folder путь, по которому ваша музыка примонтирована в Home Assistant (например, /media/music).

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg

# Home Assistant App: Navidrome/Subsonic Rating Sync
## The app is under active development and currently non-functional. Keep an eye out for updates on the full public release.

Syncing rating/favorite/like tags between audio files and Navidrome (Subsonic/Opensonic API). Supported sync modes: two-way, one-way (Files → Navidrome), or (Navidrome → Files).





  
    Важно по настройке music_folder:

         Для Navidrome: Убедитесь, что в настройках вашего плеера/агента в Navidrome включена опция "Показать реальный путь" (Report Full Path). При включенной опции поле music_folder в настройках аддона можно оставить пустым.
         Для Airsonic, Gonic и других Subsonic-серверов: Эти серверы отдают путь к файлам относительно корня библиотеки. Вам обязательно нужно указать в поле music_folder путь, по которому ваша музыка примонтирована в Home Assistant (например, /media/music).
Код останется абсолютно универсальным. Пользователи Navidrome смогут оставить поле пустым (если включили опцию на сервере), а пользователи других серверов смогут продолжить пользоваться аддоном, указав свою папку.

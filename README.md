# GoPro Media Graber

**RU:** Скрипт для автоматического скачивания и организации медиафайлов с камеры GoPro.
<br>
**EN:** A script for automatically downloading and organizing media files from a GoPro camera.

---

<details>
<summary>🇷🇺 **Русская версия**</summary>

Скрипт для автоматического скачивания и организации медиафайлов с камеры GoPro.

Он подключается к камере по Bluetooth, активирует Wi-Fi, скачивает новые файлы, обрабатывает их (склеивает и переименовывает по дате съемки), а затем возвращает исходные настройки сети.

### 🚀 Возможности

*   Автоматическое обнаружение и подключение к камере по **Bluetooth LE**.
*   Автоматическое включение Wi-Fi на камере.
*   Автоматическое **переключение Wi-Fi** на ПК (только для Windows).
*   Скачивание только **новых файлов**, которых еще нет на диске.
*   Информативные **прогресс-бары** для каждого файла и общего процесса скачивания.
*   Гибкая постобработка файлов:
    *   Группировка файлов в съемочные сессии по временному интервалу.
    *   Автоматическая **склейка "глав"** (chapters) видео с помощью `ffmpeg`.
    *   **Переименование файлов** на основе даты и времени съемки.
    *   Несколько режимов обработки (только скачивание, только переименование и т.д.).
*   Настройка через удобный файл `config.ini`.
*   Автоматический **возврат к домашней сети Wi-Fi** после завершения работы (только для Windows).
*   **Предотвращение засыпания системы** во время работы скрипта.
*   Скрипт полагается на **автоматическое выключение камеры** для экономии заряда.

### 📋 Требования

*   **Python 3.8+**
*   **Внешние библиотеки Python**: `bleak`, `httpx`, `tqdm`
*   **ffmpeg**: Необходим для обработки видео.
    *   На Windows, если `ffmpeg` не найден, скрипт **предложит скачать его автоматически** при первом запуске.
    *   На других ОС `ffmpeg` должен быть установлен и доступен в системной переменной `PATH`, либо путь к нему должен быть указан в `config.ini`.
*   **ОС**: Скрипт полностью функционален на **Windows**. На других ОС автоматическое переключение Wi-Fi не будет работать (потребуется ручное подключение).
*   **Bluetooth-адаптер** на компьютере.

### 🛠️ Установка и настройка

1.  Клонируйте репозиторий или скачайте файлы.

2.  Установите необходимые Python-библиотеки:
    ```bash
    pip install "bleak" "httpx" "tqdm"
    ```

3.  **Установите ffmpeg (если не используете автозагрузку на Windows):**
    Самый простой способ для Windows — скачать сборку с [gyan.dev](https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip) и распаковать `ffmpeg.exe` и `ffprobe.exe` в папку `bin` рядом со скриптом, либо в любую папку, добавленную в системную переменную `PATH`.

4.  **Выполните первое сопряжение с камерой (важный шаг!):**
    *   Включите камеру GoPro.
    *   На камере перейдите в меню: **Настройки -> Подключения -> Подключить устройство -> GoPro Quik App**.
    *   Пока камера находится в режиме поиска, запустите скрипт. Windows "запомнит" устройство, и в дальнейшем этот шаг не потребуется.

### ⚙️ Конфигурация (`config.ini`)

При первом запуске скрипт автоматически создаст файл `config.ini`. Вот описание его параметров:

#### `[General]`
*   `identifier`: Последние 4 символа серийного номера камеры. Используется для поиска BLE-устройства и сети Wi-Fi. Если оставить пустым, скрипт попытается найти камеру автоматически.
*   `output_folder`: Папка для сохранения скачанных медиафайлов.
*   `home_wifi`: Имя вашей домашней Wi-Fi сети. Скрипт вернется к ней после завершения работы.

#### `[Processing]`
*   `mode`: Режим обработки файлов.
    *   `full`: Скачать, склеить сессии и переименовать по дате (по умолчанию).
    *   `rename_only`: Скачать и переименовать КАЖДЫЙ файл по дате, не склеивая.
    *   `download_only`: Только скачать файлы с оригинальными именами.
    *   `touch_only`: Скачать и установить дату модификации файла равной дате съемки.
    *   `process_only`: Не скачивать, а только обработать уже скачанные файлы в папке `output_folder`.
*   `session_gap_hours`: Временной разрыв в часах, который считается новой съемочной сессией.
*   `filename_format`: Формат имени для переименованных файлов. Использует стандартные директивы Python (`%Y`, `%m`, `%d`, `%H`, `%M`).
*   `ffmpeg_path`: Путь к исполняемому файлу `ffmpeg.exe`. По умолчанию `ffmpeg`, что требует его наличия в `PATH`.

#### `[Advanced]`
*   `wifi_wait`: Время ожидания (в секундах) при попытке подключения к Wi-Fi сети.
*   `auto_close_window`: Автоматически закрывать окно консоли после завершения работы (только для `.exe` версии). `yes` / `no`.

#### `[Deletion]`
*   `delete_after_download`: Определяет, нужно ли удалять файлы с камеры после успешного скачивания.
    *   `no`: Никогда не удалять.
    *   `ask`: Спрашивать каждый раз (по умолчанию).
    *   `yes`: Всегда удалять без запроса.

### ▶️ Использование

1.  Убедитесь, что камера включена и находится рядом с компьютером.
2.  Запустите скрипт из командной строки:
    ```bash
    python GP_graber.py
    ```
3.  Для получения справки по всем параметрам конфигурации, запустите:
    ```bash
    python GP_graber.py --help
    ```
4.  Следите за логами в консоли. Если потребуется ручное действие (например, подключение к Wi-Fi), скрипт выведет соответствующее сообщение.

### 📦 Сборка в .exe (для Windows)

Вы можете собрать скрипт в один исполняемый `.exe` файл.

1.  **Установите PyInstaller:**
    ```bash
    pip install pyinstaller
    ```

2.  **Запустите сборку:**
    ```bash
    pyinstaller --onefile --name GoProGraber --icon=icon.ico GP_graber.py
    ```

3.  **Подготовьте файлы для запуска:**
    *   Ваш `GoProGraber.exe` будет находиться в папке `dist`.
    *   **ffmpeg**: Если `ffmpeg.exe` и `ffprobe.exe` не встроены, поместите их рядом с `.exe` файлом или в подпапку `bin`. Если скрипт их не найдет, он предложит скачать их.
    *   **config.ini**: Файл конфигурации будет создан автоматически при первом запуске.

###  troubleshooting Решение проблем

*   **Камера GoPro не найдена:**
    *   Убедитесь, что Bluetooth на вашем ПК включен.
    *   Убедитесь, что камера включена и находится близко к ПК.
    *   Если это первый запуск, убедитесь, что камера находится в режиме сопряжения (см. раздел "Установка").

*   **Не удалось подключиться к Wi-Fi автоматически:**
    *   **Пароль от Wi-Fi:** Пароль можно найти на экране камеры в меню **Подключения -> Информация о камере**.
    *   Windows хранит профили сетей. Подключитесь к Wi-Fi сети вашей камеры вручную хотя бы один раз. Убедитесь, что стоит галочка "Подключаться автоматически".

*   **Ошибка "ffmpeg не найден":**
    *   Позвольте скрипту скачать утилиты автоматически (только для Windows).
    *   Либо укажите полный путь к `ffmpeg.exe` в параметре `ffmpeg_path` в файле `config.ini`.

</details>

<details>
<summary>🇬🇧 **English version**</summary>

A script to automatically download and organize media files from a GoPro camera.

It connects to the camera via Bluetooth, activates Wi-Fi, downloads new files, processes them (merges and renames by shooting date), and then restores the original network settings.

### 🚀 Features

*   Automatic discovery and connection to the camera via **Bluetooth LE**.
*   Automatic activation of Wi-Fi on the camera.
*   Automatic **Wi-Fi switching** on the PC (Windows only).
*   Downloads only **new files** that are not yet on the disk.
*   Informative **progress bars** for each file and the overall download process.
*   Flexible post-processing of files:
    *   Grouping files into shooting sessions based on a time interval.
    *   Automatic **merging of video chapters** using `ffmpeg`.
    *   **Renaming files** based on the shooting date and time.
    *   Multiple processing modes (download only, rename only, etc.).
*   Configuration via a convenient `config.ini` file.
*   Automatic **return to the home Wi-Fi network** after completion (Windows only).
*   **Prevents the system from sleeping** while the script is running.
*   The script relies on the camera's **auto-power-off** feature to save battery.

### 📋 Requirements

*   **Python 3.8+**
*   **External Python libraries**: `bleak`, `httpx`, `tqdm`
*   **ffmpeg**: Required for video processing.
    *   On Windows, if `ffmpeg` is not found, the script will **offer to download it automatically** on the first run.
    *   On other OS, `ffmpeg` must be installed and available in the system's `PATH`, or the path to it must be specified in `config.ini`.
*   **OS**: The script is fully functional on **Windows**. On other operating systems, automatic Wi-Fi switching will not work (manual connection will be required).
*   A **Bluetooth adapter** on the computer.

### 🛠️ Installation and Setup

1.  Clone the repository or download the files.

2.  Install the required Python libraries:
    ```bash
    pip install "bleak" "httpx" "tqdm"
    ```

3.  **Install ffmpeg (if not using auto-download on Windows):**
    The easiest way for Windows is to download a build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip) and extract `ffmpeg.exe` and `ffprobe.exe` into a `bin` folder next to the script, or into any folder included in the system's `PATH`.

4.  **Perform the initial pairing with the camera (important step!):**
    *   Turn on your GoPro camera.
    *   On the camera, navigate to: **Preferences -> Connections -> Connect Device -> GoPro Quik App**.
    *   While the camera is in pairing mode, run the script for the first time. Windows will "remember" the device, and this step will not be required in the future.

### ⚙️ Configuration (`config.ini`)

On the first run, the script will automatically create a `config.ini` file. Here is a description of its parameters:

#### `[General]`
*   `identifier`: The last 4 characters of the camera's serial number. Used to find the BLE device and Wi-Fi network. If left empty, the script will try to find the camera automatically.
*   `output_folder`: The folder for saving downloaded media files.
*   `home_wifi`: The name of your home Wi-Fi network. The script will switch back to it after finishing.

#### `[Processing]`
*   `mode`: File processing mode.
    *   `full`: Download, merge sessions, and rename by date (default).
    *   `rename_only`: Download and rename EACH file by date, without merging.
    *   `download_only`: Only download files with their original names.
    *   `touch_only`: Download and set the file's modification date to the shooting date.
    *   `process_only`: Do not download, only process already downloaded files in the `output_folder`.
*   `session_gap_hours`: The time gap in hours that is considered a new shooting session.
*   `filename_format`: The name format for renamed files. Uses standard Python directives (`%Y`, `%m`, `%d`, `%H`, `%M`).
*   `ffmpeg_path`: The path to the `ffmpeg.exe` executable. Defaults to `ffmpeg`, which requires it to be in the `PATH`.

#### `[Advanced]`
*   `wifi_wait`: The time to wait (in seconds) when trying to connect to a Wi-Fi network.
*   `auto_close_window`: Automatically close the console window after completion (for the `.exe` version only). `yes` / `no`.

#### `[Deletion]`
*   `delete_after_download`: Defines whether to delete files from the camera after a successful download.
    *   `no`: Never delete.
    *   `ask`: Ask every time (default).
    *   `yes`: Always delete without prompting.

### ▶️ Usage

1.  Make sure the camera is turned on and near the computer.
2.  Run the script from the command line:
    ```bash
    python GP_graber.py
    ```
3.  To get help with all configuration parameters, run:
    ```bash
    python GP_graber.py --help
    ```
4.  Follow the logs in the console. If manual action is required (e.g., connecting to Wi-Fi), the script will display a corresponding message.

### 📦 Building into .exe (for Windows)

You can build the script into a single executable `.exe` file.

1.  **Install PyInstaller:**
    ```bash
    pip install pyinstaller
    ```

2.  **Run the build:**
    ```bash
    pyinstaller --onefile --name GoProGraber --icon=icon.ico GP_graber.py
    ```

3.  **Prepare the files for running:**
    *   Your `GoProGraber.exe` will be in the `dist` folder.
    *   **ffmpeg**: If `ffmpeg.exe` and `ffprobe.exe` are not bundled, place them next to the `.exe` file or in a `bin` subfolder. If the script doesn't find them, it will offer to download them.
    *   **config.ini**: The configuration file will be created automatically on the first run.

### troubleshooting Troubleshooting

*   **GoPro camera not found:**
    *   Make sure Bluetooth is enabled on your PC.
    *   Make sure the camera is turned on and close to the PC.
    *   If this is the first run, make sure the camera is in pairing mode (see the "Installation" section).

*   **Failed to connect to Wi-Fi automatically:**
    *   **Wi-Fi Password:** The password can be found on the camera's screen in the **Connections -> Camera Info** menu.
    *   Windows stores network profiles. Connect to your camera's Wi-Fi network manually at least once. Make sure the "Connect automatically" box is checked.

*   **Error "ffmpeg not found":**
    *   Allow the script to download the utilities automatically (Windows only).
    *   Or, specify the full path to `ffmpeg.exe` in the `ffmpeg_path` parameter in `config.ini`.

</details>
import re
import sys
import asyncio
import os
import json
import logging
import platform
import subprocess
import configparser
import zipfile
import io
import locale
import tempfile
from pathlib import Path
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# Проверка и импорт зависимостей
try:
    import httpx
    from tqdm import tqdm
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
    from bleak.backends.device import BLEDevice as BleakDevice
    from bleak.backends.characteristic import BleakGATTCharacteristic
    
except ImportError as e:
    logging.error(f"Необходимая библиотека не найдена: {e.name}. Установите ее: pip install {e.name}")
    logging.error('Полный список зависимостей: pip install "bleak" "httpx" "tqdm"')
    sys.exit(1)

def normalize_uuid(uuid_val: str | Any) -> str:
    """Нормализует UUID к каноническому виду (строчные буквы)."""
    return str(uuid_val).lower() if uuid_val else ""

# Базовый шаблон для GoPro UUID (GP-XXXX -> b5f9XXXX-aa8d-11e3-9046-0002a5d5c51b)
GOPRO_BASE_UUID = "b5f9{}-aa8d-11e3-9046-0002a5d5c51b"

# --- Настройка ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING) # Убираем из лога успешные keep-alive запросы
noti_handler_T = Callable[..., Any]

def get_script_dir() -> Path:
    """Возвращает путь к директории скрипта, корректно работая в .exe."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Если запущено из .exe (PyInstaller)
        return Path(sys.executable).parent
    else:
        # Если запущено как .py скрипт
        return Path(__file__).parent

def _get_subprocess_startupinfo():
    """Creates and returns a subprocess.STARTUPINFO object to hide the console window on Windows."""
    if platform.system() == "Windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return startupinfo
    return None


# --- Константы GoPro ---
# Ссылки на официальную документацию OpenGoPro:
# - UUIDs: https://gopro.github.io/OpenGoPro/ble/protocol/uuids
# - Команды: https://gopro.github.io/OpenGoPro/ble/commands/commands

# -- BLE UUIDs --
# Сервис управления и запросов (Control & Query)
OG_COMMAND_REQUEST_UUID = GOPRO_BASE_UUID.format("0072")  # GP-0072
OG_COMMAND_RESPONSE_UUID = GOPRO_BASE_UUID.format("0073") # GP-0073
OG_SETTINGS_RESPONSE_UUID = GOPRO_BASE_UUID.format("0075") # GP-0075
# Сервис точки доступа Wi-Fi (Wifi Access Point)
OG_WAP_SSID_UUID = GOPRO_BASE_UUID.format("0002")     # GP-0002
OG_WAP_PASSWORD_UUID = GOPRO_BASE_UUID.format("0003")  # GP-0003

# -- BLE Command IDs --
OG_CMD_SET_WIFI_AP = 0x17              # GP-0072 Set Wi-Fi AP state
OG_CMD_SLEEP = 0x05                    # GP-0005 Put camera to sleep
OG_CMD_SET_CLIENT_INFO = 0x56          # GP-0056 Register third party client
OG_CMD_KEEP_ALIVE = 0x5B               # Keep-alive command ID

# -- BLE Команды (Payloads) --
# Включение/выключение Wi-Fi AP (https://gopro.github.io/OpenGoPro/ble/features/control.html#set-ap-control)
OG_WIFI_AP_ON = bytearray([0x03, OG_CMD_SET_WIFI_AP, 0x01, 0x01])  # [len=3, cmd=0x17, len=1, enable=1]
OG_WIFI_AP_OFF = bytearray([0x03, OG_CMD_SET_WIFI_AP, 0x01, 0x00])  # [len=3, cmd=0x17, len=1, enable=0]

# Перевод камеры в режим сна (https://gopro.github.io/OpenGoPro/ble/features/control.html#sleep)
OG_SLEEP = bytearray([0x01, OG_CMD_SLEEP])  # [len=1, cmd=0x05]

# Регистрация клиента (https://gopro.github.io/OpenGoPro/ble/features/control.html#set-analytics)
OG_SET_THIRD_PARTY_CLIENT_INFO = bytearray([0x02, OG_CMD_SET_CLIENT_INFO, 0x00])  # [len=2, cmd=0x56, param=0]

# Поддержание BLE-соединения (https://gopro.github.io/OpenGoPro/ble/features/control.html#keep-alive)
OG_KEEP_ALIVE = bytearray([0x02, OG_CMD_KEEP_ALIVE, 0x42])  # [len=2, cmd=0x5B, data=0x42]

# -- HTTP Endpoints --
GOPRO_BASE_URL = "http://10.5.5.9"
GOPRO_MEDIA_PORT = 8080


HELP_RU = """
GoPro Graber - Автоматический загрузчик медиа с камер GoPro.

Использование:
  GP_graber.py [аргументы]

Аргументы:
  --help, -h    Показать это справочное сообщение и выйти.

Описание:
  Скрипт автоматически находит GoPro, включает Wi-Fi, подключается к камере,
  скачивает новые медиафайлы, обрабатывает их (склеивает и переименовывает),
  а затем возвращает ПК к исходной сети Wi-Fi.

  Поведение скрипта настраивается через файл 'config.ini', который создается
  при первом запуске.

Настройки (config.ini):

  [General]
  - identifier:      Последние 4 символа серийного номера GoPro (часть имени сети).
                     Закомментируйте эту строку (поставьте # в начале) для автоматического определения.
  - output_folder:   Папка для сохранения медиа.
  - home_wifi:       Имя домашней Wi-Fi сети для возврата ПК после скачивания.
                     Закомментируйте эту строку (поставьте # в начале), если не хотите автоматически переключаться.

  [Processing]
  - mode:            Режим обработки файлов после скачивания:
      - full:          Скачать, склеить сессии и переименовать по дате (по умолчанию).
      - rename_only:   Скачать и переименовать КАЖДЫЙ файл по дате, не склеивая.
      - download_only: Только скачать файлы с оригинальными именами.
      - touch_only:    Скачать и установить дату файла равной дате съемки.
      - process_only:  Не скачивать, только обработать уже скачанные файлы в папке.
  - session_gap_hours: Временной разрыв в часах, который считается новой съемочной сессией.
  - filename_format: Формат имени файла (директивы strftime, например, %Y-%m-%d_%H-%M).
  - ffmpeg_path:     Путь к исполняемому файлу ffmpeg.

  [Advanced]
  - wifi_wait:       Секунд ожидания для подключения к Wi-Fi.
  - auto_close_window: Автоматически закрывать окно после завершения (yes/no).

  [Deletion]
  - delete_after_download: Удалять ли файлы с камеры после скачивания (yes/ask/no).

  [Power]
  - shutdown_after_complete: Выключать ли камеру после завершения (yes/no).
"""

HELP_EN = """
GoPro Graber - Automatic media downloader for GoPro cameras.

Usage:
  GP_graber.py [arguments]

Arguments:
  --help, -h    Show this help message and exit.

Description:
  The script automatically finds a GoPro, enables Wi-Fi, connects to the camera,
  downloads new media files, processes them (merges and renames),
  and then switches the PC back to the original Wi-Fi network.

  The script's behavior is configured via the 'config.ini' file, which is
  created on the first run.

Settings (config.ini):

  [General]
  - identifier:      The last 4 characters of the GoPro serial number (part of the network name).
                     Comment out this line (add # at the beginning) for automatic detection.
  - output_folder:   The folder where media will be saved.
  - home_wifi:       The name of your home Wi-Fi network to switch back to after downloading.
                     Comment out this line (add # at the beginning) to disable automatic switching.

  [Processing]
  - mode:            File processing mode after download:
      - full:          Download, merge sessions, and rename by date (default).
      - rename_only:   Download and rename EACH file by date, without merging.
      - download_only: Only download files with their original names.
      - touch_only:    Download and set the file's date to the shooting date.
      - process_only:  Do not download, only process already downloaded files in the folder.
  - session_gap_hours: The time gap in hours that defines a new shooting session.
  - filename_format: The file name format (strftime directives, e.g., %Y-%m-%d_%H-%M).
  - ffmpeg_path:     The path to the ffmpeg executable.

  [Advanced]
  - wifi_wait:       Seconds to wait for a Wi-Fi connection.
  - auto_close_window: Automatically close the window upon completion (yes/no).

  [Deletion]
  - delete_after_download: Whether to delete files from the camera after downloading (yes/ask/no).

  [Power]
  - shutdown_after_complete: Whether to turn off the camera after completion (yes/no).
"""

def show_help():
    """Определяет язык системы и показывает соответствующее справочное сообщение."""
    try:
        # Определяем язык системы
        lang, _ = locale.getdefaultlocale()
        is_russian = lang and lang.lower().startswith('ru')
    except Exception:
        is_russian = False

    if is_russian:
        print(HELP_RU)
    else:
        print(HELP_EN)


class GoProState:
    """Хранит состояние и обрабатывает уведомления от камеры."""
    COMMAND_ID_SET_WIFI_AP = 0x17 # ID команды "Set Wi-Fi AP"
    COMMAND_ID_SLEEP = 0x05       # ID команды "Sleep"

    def __init__(self) -> None:
        self.command_status: dict[int, asyncio.Future[int]] = {}

    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        # Normalize UUID comparison to lowercase string to handle library UUID object representations
        sender_uuid = normalize_uuid(sender.uuid)
        logging.debug(f"Received notification from {sender_uuid}: {data.hex()}")
        if sender_uuid == OG_COMMAND_RESPONSE_UUID and len(data) > 2:
            command_id, status = data[1], data[2]
            fut = self.command_status.get(command_id)
            if fut and not fut.done():
                fut.set_result(status)
                if status != 0: # 0 means SUCCESS
                    # Команды, для которых ошибка "не поддерживается" (2) является ожидаемой на некоторых моделях
                    ignorable_commands = {
                        0x56,  # Set Third Party Client Info
                        0x3C,  # Get Hardware Info (новый keep-alive)
                    }
                    if status == 2 and command_id in ignorable_commands:
                        logging.debug(f"GoPro ответила на команду {hex(command_id)} с ошибкой {status} (NOT_SUPPORTED). Это нормально для некоторых камер.")
                    else:
                        # Логируем ошибку только для команд, которые мы ожидаем
                        logging.error(f"GoPro ответила на команду {hex(command_id)} с ошибкой {status}")

def exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Обработчик исключений в асинхронном цикле."""
    msg = context.get("exception", context["message"])
    logging.error(f"Перехвачено исключение в event loop: {msg}")

async def connect_ble(notification_handler: noti_handler_T, identifier: str | None = None) -> tuple[BleakClient, BleakDevice, dict[str, str | None]] | tuple[None, None, None]:
    """Ищет, подключается, сопрягается и включает уведомления на камере GoPro."""
    logging.info("Поиск камеры GoPro...")
    search_pattern = identifier or r"GoPro [A-Z0-9]{4}"
    token = re.compile(search_pattern)
    matched_device = None
    logging.debug(f"Используется паттерн для поиска BLE-устройства: '{search_pattern}'")

    # Ищем устройство с заданным паттерном имени
    for i in range(3): # Уменьшено до 3 попыток
        logging.debug(f"Попытка сканирования BLE #{i+1}/3...")
        devices = []
        try:
            devices = await BleakScanner.discover(timeout=5)
        except BleakError as e:
            logging.error(f"Ошибка при сканировании Bluetooth: {e}. Проверьте, включен ли Bluetooth на компьютере.")
            await asyncio.sleep(2) # Пауза перед следующей попыткой
            continue

        all_found_names = [d.name for d in devices if d.name]

        for device in devices:
            if device.name and token.search(device.name):
                matched_device = device
                logging.info(f"Найдена камера: {device.name} ({device.address})")
                break
        if matched_device:
            break

        logging.debug("Камера не найдена, повторный поиск...")
        if all_found_names:
            logging.debug(f"  (Найденные устройства: {', '.join(all_found_names)})")
        else:
            logging.debug("  (Не найдено ни одного устройства с именем. Убедитесь, что Bluetooth на ПК включен.)")

        await asyncio.sleep(1)

    if not matched_device:
        logging.error("Камера GoPro не найдена. Убедитесь, что камера включена и сопряжена с компьютером.")
        return None, None, None

    client = BleakClient(matched_device,pair=True)
    try:
        logging.debug(f"Установка BLE-соединения с {matched_device.name}...")
        await client.connect(timeout=12)
        logging.debug("BLE-соединение установлено!")

        # Даем стеку Bluetooth время на стабилизацию.
        await asyncio.sleep(1.0)

        # Пауза и явное обнаружение сервисов для надежности.
        # Некоторые камеры медленно инициализируют свои BLE сервисы после подключения/сопряжения.
        logging.debug("Обнаружение сервисов...")
        
        # Активно пытаемся обнаружить сервисы в течение нескольких секунд.
        # Это решает проблему, когда камера засыпает или сервисы появляются с задержкой.
        services_found = False
        for attempt in range(4): # Попытки в течение ~6 секунд
            if not client.is_connected:
                logging.error("Соединение с камерой было потеряно во время ожидания сервисов.")
                return None, None, None

            logging.debug(f"Попытка обнаружения сервисов #{attempt + 1}/4...")
            # Принудительно запускаем обнаружение сервисов, обращаясь к свойству
            _ = client.services

            # Проверяем, что нужная характеристика найдена
            if client.services.get_characteristic(OG_COMMAND_RESPONSE_UUID):
                logging.debug("Необходимые сервисы и характеристики найдены.")
                services_found = True
                break  # Успех

            logging.debug("Ключевые характеристики еще не найдены, ожидание...")
            await asyncio.sleep(1.0)

        if not services_found:
            logging.error("Не удалось обнаружить сервисы GoPro. Возможно, камера уснула или используется несовместимая модель.")
            return None, None, None
            
        # Включаем уведомления. Этот шаг требует аутентификации и должен автоматически
        # инициировать сопряжение в ОС, если оно еще не было выполнено.
        try:
            logging.debug("Включение уведомлений от камеры (это может вызвать запрос на сопряжение в Windows)...")
            await client.start_notify(OG_COMMAND_RESPONSE_UUID, notification_handler)
            await client.start_notify(OG_SETTINGS_RESPONSE_UUID, notification_handler)
            logging.debug("Уведомления успешно включены.")
        except BleakError as e:
            if "Insufficient Authentication" in str(e) or "Protocol Error 0x05" in str(e):
                logging.error("="*80)
                logging.error("  ОШИБКА: РАССИНХРОНИЗАЦИЯ СОПРЯЖЕНИЯ С КАМЕРОЙ.")
                logging.error("  Это часто происходит, если сопряжение было удалено на ПК, но не на камере.")
                logging.error("")
                logging.error("  ЧТО ДЕЛАТЬ:")
                logging.error("  1. Удалите камеру GoPro из списка Bluetooth-устройств в Windows.")
                logging.error("  2. На самой камере выполните сброс соединений:")
                logging.error("     Настройки -> Подключения -> Сброс подключений (Reset Connections).")
                logging.error("  3. Перезапустите этот скрипт. Он должен будет создать новое, чистое сопряжение.")
                logging.error("="*80)
                return None, None, None
            else:
                # Другая, неожиданная ошибка Bleak
                logging.error(f"Неожиданная ошибка Bleak при включении уведомлений: {e}")
                # Перевызываем исключение, так как оно не связано с сопряжением
                raise RuntimeError("Критическая ошибка при включении уведомлений BLE.") from e

        logging.debug("Регистрация клиента на камере (Third Party Client Info)...")
        await client.write_gatt_char(OG_COMMAND_REQUEST_UUID, OG_SET_THIRD_PARTY_CLIENT_INFO)

        # Читаем учетные данные Wi-Fi
        wifi_creds: dict[str, str | None] = {"ssid": None, "password": None}
        try:
            logging.debug("Чтение учетных данных Wi-Fi с камеры...")
            raw_ssid = await client.read_gatt_char(OG_WAP_SSID_UUID)
            wifi_creds["ssid"] = raw_ssid.decode("utf-8", errors="ignore").rstrip("\x00")
            
            raw_pwd = await client.read_gatt_char(OG_WAP_PASSWORD_UUID)
            wifi_creds["password"] = raw_pwd.decode("utf-8", errors="ignore").rstrip("\x00")
            
            if wifi_creds["ssid"] and wifi_creds["password"]:
                logging.debug(f"Успешно получены учетные данные Wi-Fi: SSID='{wifi_creds['ssid']}'")
        except Exception as e:
            logging.warning(f"Ошибка при чтении учетных данных Wi-Fi: {e}. Скрипт продолжит работу, полагаясь на сохраненные профили.")

        logging.debug("Уведомления включены. BLE готов к работе.")
        return client, matched_device, wifi_creds
    except Exception as e:
        logging.error(f"Не удалось установить BLE-соединение: {e}")
        raise RuntimeError("Критическая ошибка при подключении по BLE.") from e


async def ensure_client_connected(client: BleakClient | None, matched_device: BleakDevice, notification_handler: noti_handler_T) -> BleakClient:
    """Проверяет, что клиент подключен; если нет — переподключается и включает уведомления.

    Возвращает новый или существующий подключённый клиент.
    """
    if client and getattr(client, "is_connected", False):
        return client

    logging.debug("Переподключение к устройству по BLE...")
    new_client = BleakClient(matched_device)
    try:
        await new_client.connect(timeout=15)
        # Явное обнаружение сервисов и включение уведомлений
        await asyncio.sleep(0.2) # Небольшая пауза после подключения
        await new_client.start_notify(OG_COMMAND_RESPONSE_UUID, notification_handler)
        await new_client.start_notify(OG_SETTINGS_RESPONSE_UUID, notification_handler)
        logging.debug("Переподключение по BLE выполнено.")
        return new_client
    except Exception as e:
        logging.error(f"Не удалось переподключиться по BLE: {e}")
        raise

async def control_wifi_ap(client: BleakClient, matched_device: BleakDevice, state: GoProState, enable: bool) -> BleakClient:
    """Включает или выключает Wi-Fi на камере."""
    action = "ON" if enable else "OFF"
    logging.debug(f"Отправка команды на включение Wi-Fi AP ({action})...")
    command = OG_WIFI_AP_ON if enable else OG_WIFI_AP_OFF
    future = asyncio.Future()
    state.command_status[GoProState.COMMAND_ID_SET_WIFI_AP] = future
    try:
        # Убедимся, что клиент подключен и, при необходимости, переподключимся
        client = await ensure_client_connected(client, matched_device, state.notification_handler)
        await client.write_gatt_char(OG_COMMAND_REQUEST_UUID, command)
    except BleakError as e:
        logging.warning(f"BLE write failed: {e}. Попытка переподключения и повторной отправки...")
        client = await ensure_client_connected(None, matched_device, state.notification_handler)
        await client.write_gatt_char(OG_COMMAND_REQUEST_UUID, command)
    try:
        status = await asyncio.wait_for(future, timeout=10)
        if status == 0:
            logging.debug(f"Команда Wi-Fi AP ({action}) успешно выполнена.")
        else:
            raise RuntimeError(f"Ошибка при установке состояния Wi-Fi AP. Статус: {status}")
    except asyncio.TimeoutError:
        raise RuntimeError("Тайм-аут ожидания ответа на команду Wi-Fi AP.")
    return client

async def sleep_camera(client: BleakClient | None, matched_device: BleakDevice, state: GoProState) -> BleakClient:
    """Отправляет команду на выключение камеры."""
    logging.info("Отправка команды на выключение камеры...")
    future = asyncio.Future()
    state.command_status[GoProState.COMMAND_ID_SLEEP] = future
    try:
        # Убедимся, что клиент подключен и, при необходимости, переподключимся
        client = await ensure_client_connected(client, matched_device, state.notification_handler)
        await client.write_gatt_char(OG_COMMAND_REQUEST_UUID, OG_SLEEP)
    except BleakError as e:
        logging.warning(f"BLE write failed: {e}. Попытка переподключения и повторной отправки...")
        client = await ensure_client_connected(None, matched_device, state.notification_handler)
        await client.write_gatt_char(OG_COMMAND_REQUEST_UUID, OG_SLEEP)
    try:
        status = await asyncio.wait_for(future, timeout=10)
        if status == 0:
            logging.debug("Команда на выключение успешно отправлена.")
        else:
            logging.error(f"Ошибка при отправке команды на выключение. Статус: {status}")
    except asyncio.TimeoutError:
        logging.error("Тайм-аут ожидания ответа на команду выключения.")
    return client

async def download_files(output_path: Path) -> tuple[int, bool, list[dict[str, Any]], list[dict[str, Any]]]:
    """Скачивает все медиафайлы с камеры с отображением прогресса.

    Возвращает: (количество скачанных файлов, флаг полного завершения, список скачанных файлов с метаданными).
    """
    output_path.mkdir(exist_ok=True)
    media_list_url = f"{GOPRO_BASE_URL}/gopro/media/list"
    downloaded_count = 0
    all_files_on_camera_meta: list[dict[str, Any]] = []
    downloaded_files_meta: list[dict[str, Any]] = []
    
    # Устанавливаем таймаут на чтение, чтобы избежать зависаний при потере Wi-Fi
    client_timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)

    async with httpx.AsyncClient(timeout=client_timeout, transport=httpx.AsyncHTTPTransport(retries=3)) as client:
        logging.info("Получение списка файлов с камеры...")
        try:
            response = await client.get(media_list_url)
            response.raise_for_status()
            media_data = response.json()

            all_files_on_camera_meta = [
                {
                    "d": media_item["d"],
                    "n": file_info["n"],
                    "s": int(file_info.get("s", 0)),
                    "mod": int(file_info.get("mod", "0")) # Время модификации (Unix timestamp)
                }
                for media_item in media_data.get("media", [])
                for file_info in media_item.get("fs", [])
            ]

            # Фильтруем файлы, которые уже существуют
            files_to_download = [
                f for f in all_files_on_camera_meta if not (output_path / f["n"]).exists()
            ]

            if not files_to_download:
                logging.info("Все медиафайлы с камеры уже скачаны.")
                return 0, True, [], all_files_on_camera_meta
        except httpx.RequestError as e:
            logging.error(f"Не удалось получить список файлов с камеры: {e}")
            logging.error("Проверьте Wi-Fi соединение с камерой.")
            return 0, False, [], []

        total_size_to_download = sum(f['s'] for f in files_to_download)
        total_downloaded_size = 0
        logging.info(f"Найдено {len(files_to_download)} новых файлов для скачивания (общий размер: {total_size_to_download / 1024**2:.2f} МБ).")
        
        download_start_time = asyncio.get_event_loop().time()

        for i, file_info in enumerate(files_to_download):
            directory, filename, file_size = file_info["d"], file_info["n"], file_info["s"]
            local_path = output_path / filename
            
            file_url = f"{GOPRO_BASE_URL}/videos/DCIM/{directory}/{filename}"
            try:
                # Общий таймаут на скачивание файла оставляем большим, но read_timeout спасет от зависаний
                async with client.stream("GET", file_url) as r:
                    r.raise_for_status()
                    total_size_from_header = int(r.headers.get("Content-Length", file_size))
                    with open(local_path, "wb") as f, tqdm(
                        total=total_size_from_header, unit='B', unit_scale=True, desc=filename, ncols=100
                    ) as pbar:
                        last_postfix_update_time = 0
                        downloaded_in_file = 0
                        async for chunk in r.aiter_bytes(): # Read timeout сработает здесь
                            f.write(chunk)
                            chunk_len = len(chunk)
                            pbar.update(chunk_len)
                            downloaded_in_file += chunk_len
                            
                            current_time = asyncio.get_event_loop().time()
                            if current_time - last_postfix_update_time > 1.0: # Обновляем постфикс не чаще раза в секунду
                                last_postfix_update_time = current_time
                                
                                current_total_downloaded = total_downloaded_size + downloaded_in_file
                                elapsed_time = current_time - download_start_time
                                
                                if elapsed_time > 1: # Избегаем деления на ноль и нестабильных начальных скоростей
                                    overall_speed = current_total_downloaded / elapsed_time
                                    if overall_speed > 0:
                                        remaining_bytes = total_size_to_download - current_total_downloaded
                                        remaining_time_sec = remaining_bytes / overall_speed
                                        
                                        m, s = divmod(remaining_time_sec, 60)
                                        h, m = divmod(m, 60)
                                        
                                        remaining_str = f"{int(h):02d}:{int(m):02d}:{int(s):02d}" if h > 0 else f"{int(m):02d}:{int(s):02d}"
                                        pbar.set_postfix_str(f"общее {remaining_str}", refresh=False)
                
                total_downloaded_size += file_size # Добавляем размер файла к общему скачанному объему
                downloaded_count += 1
                downloaded_files_meta.append(file_info)
            except (httpx.ReadTimeout, httpx.RequestError, IOError, KeyboardInterrupt, asyncio.CancelledError) as e:
                # Общая логика очистки для всех прерываний скачивания
                if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
                    # Не выводим полный стектрейс, просто информируем
                    logging.warning(f"\nОперация прервана. Скачивание '{filename}' остановлено.")
                elif isinstance(e, httpx.ReadTimeout):
                    logging.error(f"\nСоединение с камерой потеряно во время скачивания '{filename}'.")
                else: # RequestError, IOError
                    logging.error(f"\nОшибка при скачивании {filename}: {e}")

                if local_path.exists():
                    logging.warning(f"Удаление неполного файла '{filename}'...")
                    try:
                        local_path.unlink()
                    except OSError as unlink_e:
                        logging.error(f"Не удалось удалить неполный файл: {unlink_e}")

                if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
                    raise  # Передаем исключение выше для корректного завершения

                logging.error("Прерывание скачивания. Запустите скрипт снова для возобновления.")
                return downloaded_count, False, downloaded_files_meta, all_files_on_camera_meta
    return downloaded_count, True, downloaded_files_meta, all_files_on_camera_meta

async def delete_files_from_camera(files_to_delete: list[dict[str, Any]]):
    """Удаляет указанные файлы с камеры по HTTP."""
    if not files_to_delete:
        return

    logging.info(f"Удаление {len(files_to_delete)} файлов с камеры...")
    
    async with httpx.AsyncClient(timeout=10) as client:
        with tqdm(total=len(files_to_delete), desc="Удаление файлов", ncols=100) as pbar:
            for file_info in files_to_delete:
                directory, filename = file_info["d"], file_info["n"]
                # URL согласно спецификации OpenGoPro
                delete_url = f"{GOPRO_BASE_URL}/gopro/media/delete/file?path={directory}/{filename}"
                
                pbar.set_description_str(f"Удаление {filename}")
                try:
                    response = await client.get(delete_url)
                    response.raise_for_status()
                    logging.debug(f"Файл '{filename}' успешно удален.")
                except (httpx.RequestError, httpx.HTTPStatusError) as e:
                    logging.warning(f"\nНе удалось удалить файл '{filename}': {e}")
                pbar.update(1)
    logging.info("Удаление завершено.")

def get_video_creation_time(file_path: Path, ffmpeg_path: str) -> datetime | None:
    """
    Извлекает дату создания видео (время съемки) из метаданных с помощью ffprobe.
    В случае ошибки чтения (поврежденный файл) возвращает None.
    Если ffprobe не найден, возвращает время последней модификации файла.
    """
    # Предполагаем, что ffprobe находится там же, где и ffmpeg
    ffprobe_cmd = ffmpeg_path.replace("ffmpeg", "ffprobe")
    
    cmd = [
        ffprobe_cmd,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(file_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8', startupinfo=_get_subprocess_startupinfo())
        metadata = json.loads(result.stdout)
        # GoPro stores creation time in UTC format with a 'Z' at the end.
        creation_time_str = metadata.get("format", {}).get("tags", {}).get("creation_time")
        
        if creation_time_str:
            # Преобразуем строку '2024-05-21T15:30:00.000000Z' в объект datetime
            # GoPro пишет время в UTC. Сразу делаем aware-объект.
            dt_utc = datetime.fromisoformat(creation_time_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
            return dt_utc # dt_utc уже является timezone-aware
    
    except subprocess.CalledProcessError as e:
        # Эта ошибка чаще всего возникает при попытке прочитать поврежденный/неполный файл.
        logging.error(f"Ошибка при чтении метаданных '{file_path.name}' (возможно, файл поврежден). Он будет пропущен.")
        logging.debug(f"ffprobe stderr: {e.stderr}")
        return None
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logging.warning(f"Не удалось получить дату съемки из метаданных для '{file_path.name}'. "
                        f"Причина: {type(e).__name__}. Будет использовано время создания файла на диске.")
    
    # Резервный вариант: если ffprobe не найден или вернул странный JSON, но файл скорее всего целый.
    # Возвращаем время модификации файла как aware-объект в UTC
    return datetime.fromtimestamp(file_path.stat().st_mtime, timezone.utc)

def process_media(output_folder: Path, downloaded_files: list[dict[str, Any]], session_gap_hours: int = 2, ffmpeg_path: str = "ffmpeg", mode: str = "full", filename_format: str = "%y_%m_%d_%H-%M"):
    """Группирует, переименовывает и склеивает/обрабатывает скачанные видео."""
    logging.info("Обработка скачанных медиафайлов...")
    try:
        ffprobe_cmd = ffmpeg_path.replace("ffmpeg", "ffprobe")
        # Скрываем окно консоли в Windows
        startupinfo = _get_subprocess_startupinfo()
        
        subprocess.run([ffmpeg_path, "-version"], capture_output=True, check=True, text=True, startupinfo=startupinfo)
        subprocess.run([ffprobe_cmd, "-version"], capture_output=True, check=True, text=True, startupinfo=startupinfo)

    except (FileNotFoundError, subprocess.CalledProcessError):
        logging.warning(f"ffmpeg ('{ffmpeg_path}') или ffprobe не найдены. Эти утилиты необходимы для режимов обработки 'full', 'rename_only' и 'process_only'.")
        logging.warning("Пропуск этапа обработки файлов.")
        return

    # --- Новая логика: всегда сканируем папку на наличие ВСЕХ необработанных файлов ---
    # Это решает проблему, когда скачивание было прервано и возобновлено.
    logging.debug("Сканирование папки на наличие необработанных файлов...")
    raw_files_on_disk = [f for f in output_folder.glob("*.MP4") if re.match(r"G[HX]\d{2}\d{4}\.MP4", f.name, re.IGNORECASE)]
    
    if not raw_files_on_disk:
        logging.info("Необработанные файлы (Gxxxxxxx.MP4) не найдены.")
        return

    # Создаем словарь с метаданными из API для быстрого доступа
    api_meta_map = {file_meta['n']: file_meta for file_meta in downloaded_files}

    # Формируем единый список файлов для обработки, используя метаданные из API, если они есть.
    files_to_process_meta = [
        api_meta_map.get(f.name, {'n': f.name}) for f in raw_files_on_disk
    ]

    logging.debug("Определение дат съемки для скачанных файлов...")
    file_datetimes = []
    for file_meta in tqdm(files_to_process_meta, desc="Подготовка файлов", ncols=100):
        file_path = output_folder / file_meta["n"]
        if not file_path.exists():
            logging.warning(f"Файл '{file_meta['n']}' был в списке скачанных, но не найден на диске. Пропуск.")
            continue
        
        # Временная метка 'mod' из API GoPro ведет себя непредсказуемо в разных режимах.
        # Чтобы гарантировать корректное время, всегда используем ffprobe для его получения.
        # Это может быть немного медленнее, но значительно надежнее.
        creation_time = get_video_creation_time(file_path, ffmpeg_path)
        
        if creation_time:
            # Конвертируем в локальное время для корректного именования
            local_creation_time = creation_time.astimezone()
            file_datetimes.append((file_path, local_creation_time))
            # Если режим process_only, то mode будет 'full' по умолчанию
            if mode == 'process_only':
                mode = 'full'

        else:
            # Если время получить не удалось ни из API, ни из ffprobe, пропускаем файл,
            # чтобы избежать неверного переименования/склейки.
            logging.warning(f"Не удалось определить время съемки для '{file_path.name}'. Файл будет пропущен при обработке.")

    # Сортируем файлы по дате съемки
    files_sorted = sorted(file_datetimes, key=lambda item: item[1])

    if mode == 'rename_only':
        logging.info("Режим 'rename_only': переименование каждого файла индивидуально.")
        for file_path, creation_time in files_sorted:
            # Добавляем часть оригинального имени, чтобы избежать коллизий
            sequence_match = re.search(r"G[HX](\\d{2})(\\d{4})", file_path.stem, re.IGNORECASE)
            sequence_str = f"_{sequence_match.group(1)}{sequence_match.group(2)}" if sequence_match else ""
            
            # Для уникальности в режиме rename_only добавляем секунды и оригинальный номер к формату из конфига
            base_name = creation_time.strftime(f"{filename_format}_%S{sequence_str}")
            out_name = f"{base_name}.mp4"
            out_path = output_folder / out_name
            
            counter = 1
            while out_path.exists():
                out_name = f"{base_name}_{counter}.mp4"
                out_path = output_folder / out_name
                counter += 1

            logging.info(f"Переименование '{file_path.name}' -> '{out_name}'")
            try:
                file_path.rename(out_path)
            except OSError as e:
                logging.error(f"Не удалось переименовать файл '{file_path.name}': {e}")
        return

    # --- Логика для режима 'full' (группировка и склейка) ---
    # Новая логика: группируем по имени файла, а не по времени.
    # Файлы вида GH01xxxx.MP4, GH02xxxx.MP4 и т.д. - это одна сессия. (ПРЕДЫДУЩАЯ ЛОГИКА)
    # НОВАЯ ЛОГИКА (по запросу): Группируем строго по времени.
    sessions, current_session = [], []
    if files_sorted:
        current_session.append(files_sorted[0])
        for i in range(1, len(files_sorted)):
            gap = timedelta(hours=session_gap_hours)
            _prev_path, prev_dt = files_sorted[i-1]
            _curr_path, curr_dt = files_sorted[i]
            
            if curr_dt - prev_dt > gap:
                sessions.append(current_session)
                current_session = []
            current_session.append(files_sorted[i])
        sessions.append(current_session)

    for session in sessions:
        if not session: continue
        
        # Сортируем файлы внутри сессии по имени, чтобы главы шли по порядку
        session.sort(key=lambda item: item[0].name)
        
        session_files = [item[0] for item in session]
        _first_file_path, first_file_dt = session[0]
        
        base_name = first_file_dt.strftime(filename_format)
        out_name = f"{base_name}.mp4"
        out_path = output_folder / out_name

        # Проверка на конфликт имен, чтобы не перезаписать существующий файл
        counter = 1
        while out_path.exists():
            out_name = f"{base_name}_{counter}.mp4"
            out_path = output_folder / out_name
            counter += 1
        
        if len(session_files) == 1:
            logging.info(f"Переименование '{session_files[0].name}' -> '{out_name}'")
            # out_path уже уникален
            try:
                session_files[0].rename(out_path)
            except OSError as e:
                logging.error(f"Не удалось переименовать '{session_files[0].name}': {e}")
        else:
            logging.info(f"Склейка {len(session_files)} файлов в '{out_name}'...")
            concat_list_path = output_folder / "concat.txt"
            try:
                with open(concat_list_path, "w", encoding="utf-8") as f:
                    for file_path in session_files:
                        f.write(f"file '{file_path.resolve()}\n")
                
                cmd = [ffmpeg_path, "-f", "concat", "-safe", "0", "-i", str(concat_list_path), "-c", "copy", "-y", str(out_path)]
                res = subprocess.run(cmd, capture_output=True, text=True, check=True)
                
                logging.info("Склейка успешна. Удаление исходных файлов...")
                for file_path in session_files:
                    try:
                        file_path.unlink()
                    except OSError as e:
                        logging.error(f"Не удалось удалить исходный файл '{file_path.name}': {e}")
            except (Exception, subprocess.CalledProcessError) as e:
                logging.error(f"Ошибка при склейке: {e}\n{getattr(e, 'stderr', '')}")
            finally:
                if concat_list_path.exists(): concat_list_path.unlink()

def touch_files(output_folder: Path, downloaded_files: list[dict[str, Any]]):
    """Устанавливает дату модификации файлов равной дате съемки из API."""
    logging.info("Режим 'touch_only': обновление временных меток файлов...")
    
    touched_count = 0
    for file_meta in tqdm(downloaded_files, desc="Обновление дат", ncols=100):
        file_path = output_folder / file_meta["n"]
        timestamp = file_meta.get("mod")
        
        if timestamp and timestamp > 0 and file_path.exists():
            try:
                os.utime(file_path, (timestamp, timestamp))
                touched_count += 1
            except Exception as e:
                logging.warning(f"Не удалось обновить дату для '{file_path.name}': {e}")
    logging.info(f"Обновлены временные метки для {touched_count} файлов.")

async def wifi_keep_alive_task(stop_event: asyncio.Event) -> None:
    """Периодически пингует камеру по Wi-Fi, чтобы предотвратить ее засыпание."""
    logging.debug("Задача Wi-Fi keep-alive запущена.")
    # Используем новый эндпоинт Open GoPro, т.к. старый /gp/command/wireless/ping не работает на всех моделях.
    ping_url = f"{GOPRO_BASE_URL}/gopro/camera/keep_alive"
    async with httpx.AsyncClient(timeout=10) as client:
        while not stop_event.is_set():
            try:
                # Ждем 15 секунд. Если stop_event будет установлен, выйдем раньше.
                await asyncio.wait_for(stop_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                # Таймаут означает, что пора отправить пинг
                try:
                    logging.debug("Отправка Wi-Fi keep-alive ping...")
                    await client.get(ping_url)
                except httpx.RequestError as e:
                    logging.warning(f"Не удалось отправить Wi-Fi keep-alive (соединение может быть потеряно): {e}")
    logging.debug("Задача Wi-Fi keep-alive остановлена.")

async def disk_keep_alive_task(output_path: Path, stop_event: asyncio.Event) -> None:
    """Периодически выполняет небольшую операцию записи в папку назначения, чтобы предотвратить засыпание жесткого диска."""
    if not output_path.is_dir():
        logging.warning(f"Disk keep-alive: Папка назначения '{output_path}' не существует. Задача не будет запущена.")
        return

    keep_alive_file = output_path / ".disk_keep_alive"
    logging.debug(f"Задача disk keep-alive запущена для папки '{output_path}'.")
    
    try:
        while not stop_event.is_set():
            try:
                # Ждем 60 секунд, но выходим раньше, если stop_event будет установлен.
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                # Таймаут означает, что пора "коснуться" диска.
                logging.debug("Disk keep-alive: 'касание' диска для предотвращения засыпания...")
                # Выполняем реальную запись, а не просто touch, чтобы гарантированно
                # разбудить диск и обойти кэширование ОС.
                with open(keep_alive_file, "w") as f:
                    f.write(datetime.now().isoformat())
    finally:
        if keep_alive_file.exists():
            keep_alive_file.unlink()
        logging.debug("Задача disk keep-alive остановлена.")

# --- Native Windows Wi-Fi Scanning (ctypes) ---
# Этот раздел реализует сканирование Wi-Fi через WinAPI, что быстрее и надежнее,
# чем парсинг вывода 'netsh'.
is_windows = platform.system() == "Windows"

if is_windows:
    from xml.sax.saxutils import escape
    wlanapi = ctypes.WinDLL("wlanapi.dll")
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
 
    # Определяем кодировку вывода консоли для корректного декодирования вывода netsh.
    try:
        CONSOLE_OUTPUT_CP = f'cp{kernel32.GetConsoleOutputCP()}'
    except Exception:
        # Резервный вариант для русского Windows, если GetConsoleOutputCP не удался.
        CONSOLE_OUTPUT_CP = 'cp866'

    # Определяем кодировку ввода консоли для корректного декодирования пользовательского ввода.
    try:
        CONSOLE_INPUT_CP = f'cp{kernel32.GetConsoleCP()}'
    except Exception:
        CONSOLE_INPUT_CP = 'cp866' # Fallback for Russian Windows if GetConsoleCP fails
 
    # Constants for SetThreadExecutionState to prevent sleep
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
 
    kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    kernel32.SetThreadExecutionState.restype = wintypes.DWORD
 
    class DOT11_SSID(ctypes.Structure):
        _fields_ = [
            ("uSSIDLength", wintypes.DWORD),
            ("ucSSID", ctypes.c_char * 32),
        ]
 
    class WLAN_RATE_SET(ctypes.Structure):
        _fields_ = [
            ("uRateSetLength", wintypes.DWORD),
            ("usRateSet", wintypes.WORD * 126)
        ]
 
    class WLAN_BSS_ENTRY(ctypes.Structure):
        _fields_ = [
            ("dot11Ssid", DOT11_SSID),
            ("uPhyId", wintypes.DWORD),
            ("dot11Bssid", ctypes.c_ubyte * 6),
            ("dot11BssType", wintypes.DWORD),
            ("dot11BssPhyType", wintypes.DWORD),
            ("lRssi", ctypes.c_long),
            ("uLinkQuality", wintypes.DWORD),
            ("bInRegDomain", ctypes.c_bool),
            ("usBeaconPeriod", wintypes.WORD),
            ("ullTimestamp", ctypes.c_ulonglong),
            ("ullHostTimestamp", ctypes.c_ulonglong),
            ("usCapabilityInformation", wintypes.WORD),
            ("ulChCenterFrequency", wintypes.DWORD),
            ("wlanRateSet", WLAN_RATE_SET),
            ("ulIeOffset", wintypes.DWORD),
            ("ulIeSize", wintypes.DWORD),
        ]
 
    class WLAN_BSS_LIST(ctypes.Structure):
        _fields_ = [
            ("dwTotalSize", wintypes.DWORD),
            ("dwNumberOfItems", wintypes.DWORD),
            ("wlanBssEntries", WLAN_BSS_ENTRY * 1),
        ]
 
    class WLAN_INTERFACE_INFO(ctypes.Structure):
        _fields_ = [
            ("InterfaceGuid", ctypes.c_byte * 16),
            ("strInterfaceDescription", wintypes.WCHAR * 256),
            ("isState", wintypes.DWORD),
        ]
 
    class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
        _fields_ = [
            ("dwNumberOfItems", wintypes.DWORD),
            ("dwIndex", wintypes.DWORD),
            ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
        ]
 
    # Константы и структуры для WlanConnect
    wlan_connection_mode_profile = 0
    dot11_BSS_type_infrastructure = 1

    class WLAN_CONNECTION_PARAMETERS(ctypes.Structure):
        _fields_ = [
            ('wlanConnectionMode', wintypes.DWORD), # WLAN_CONNECTION_MODE enum
            ('strProfile', wintypes.LPWSTR),
            ('pDot11Ssid', ctypes.POINTER(DOT11_SSID)),
            ('pDesiredBssidList', ctypes.c_void_p), # Pointer to DOT11_BSSID_LIST
            ('dot11BssType', wintypes.DWORD),       # DOT11_BSS_TYPE enum
            ('dwFlags', wintypes.DWORD)
        ]

    wlanapi.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    wlanapi.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    wlanapi.WlanConnect.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_byte * 16), ctypes.POINTER(WLAN_CONNECTION_PARAMETERS), ctypes.c_void_p]
    wlanapi.WlanConnect.restype = wintypes.DWORD
    wlanapi.WlanSetProfile.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_byte * 16), wintypes.DWORD, wintypes.LPWSTR, wintypes.LPWSTR, wintypes.BOOL, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    wlanapi.WlanSetProfile.restype = wintypes.DWORD
    wlanapi.WlanDeleteProfile.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_byte * 16), wintypes.LPWSTR, ctypes.c_void_p]
    wlanapi.WlanDeleteProfile.restype = wintypes.DWORD

    def create_wifi_profile_xml(ssid: str, password: str) -> str:
        """Создает XML-строку для профиля Wi-Fi WPA2-PSK."""
        safe_ssid = escape(ssid)
        safe_password = escape(password)
        
        return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{safe_ssid}</name>
    <SSIDConfig>
        <SSID>
            <name>{safe_ssid}</name>
        </SSID>
    </SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM>
        <security>
            <authEncryption>
                <authentication>WPA2PSK</authentication>
                <encryption>AES</encryption>
                <useOneX>false</useOneX>
            </authEncryption>
            <sharedKey>
                <keyType>passPhrase</keyType>
                <protected>false</protected>
                <keyMaterial>{safe_password}</keyMaterial>
            </sharedKey>
        </security>
    </MSM>
</WLANProfile>"""

    async def get_wifi_interface_windows() -> str | None:
        """Возвращает имя (описание) первого активного Wi-Fi интерфейса."""
        hClient = wintypes.HANDLE()
        pIfList = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
        try:
            if wlanapi.WlanOpenHandle(2, None, ctypes.byref(wintypes.DWORD()), ctypes.byref(hClient)) != 0:
                return None
            if wlanapi.WlanEnumInterfaces(hClient, None, ctypes.byref(pIfList)) != 0:
                return None
            
            iface_list = pIfList.contents
            if iface_list.dwNumberOfItems > 0:
                return iface_list.InterfaceInfo[0].strInterfaceDescription
            return None
        finally:
            if pIfList:
                wlanapi.WlanFreeMemory(pIfList)
            if hClient:
                wlanapi.WlanCloseHandle(hClient, None)

    async def get_current_wifi_ssid_windows() -> str | None:
        """Возвращает SSID текущей Wi-Fi сети в Windows."""
        try:
            startupinfo = _get_subprocess_startupinfo()

            proc = await asyncio.create_subprocess_exec(
                "netsh", "wlan", "show", "interfaces",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                err_msg = stderr.decode(CONSOLE_OUTPUT_CP, errors='ignore').strip()
                logging.warning(f"Не удалось получить текущий SSID через netsh: {err_msg}")
                return None

            for line in stdout.decode(CONSOLE_OUTPUT_CP, errors='ignore').splitlines():
                if "SSID" in line and ":" in line:
                    ssid = line.split(":", 1)[1].strip()
                    if ssid:
                        return ssid
            return None
        except FileNotFoundError as e:
            logging.warning(f"Не удалось получить текущий SSID через netsh: {e}")
            return None

    async def find_wifi_ssid_windows_native(identifier: str, timeout: int = 15) -> str | None:
        """
        Сканирует Wi-Fi сети с помощью WinAPI и ищет SSID, содержащий 'identifier'.
        Возвращает полное имя SSID в случае успеха.
        """
        hClient = wintypes.HANDLE()
        pIfList = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
        pBssList = ctypes.c_void_p()
        
        try:
            if wlanapi.WlanOpenHandle(2, None, ctypes.byref(wintypes.DWORD()), ctypes.byref(hClient)) != 0:
                raise RuntimeError("WlanOpenHandle failed.")
            
            if wlanapi.WlanEnumInterfaces(hClient, None, ctypes.byref(pIfList)) != 0:
                raise RuntimeError("WlanEnumInterfaces failed.")
                
            iface_list = pIfList.contents
            if iface_list.dwNumberOfItems == 0:
                logging.warning("Wi-Fi интерфейс не найден.")
                return None
            iface_guid = iface_list.InterfaceInfo[0].InterfaceGuid

            logging.info(f"Поиск Wi-Fi сети камеры (до {timeout} секунд)...")
            end_time = asyncio.get_event_loop().time() + timeout
            
            while asyncio.get_event_loop().time() < end_time:
                logging.debug("Запуск нового сканирования Wi-Fi...")
                if wlanapi.WlanScan(hClient, ctypes.byref(iface_guid), None, None, None) != 0:
                    logging.warning("Не удалось запустить сканирование Wi-Fi.")
                
                # Ждем несколько секунд, пока сканирование завершится и результаты обновятся
                for _ in range(5): # Проверяем в течение 5 секунд после каждого сканирования
                    if asyncio.get_event_loop().time() > end_time: break
                    await asyncio.sleep(1)
                    if wlanapi.WlanGetNetworkBssList(hClient, ctypes.byref(iface_guid), None, 2, False, None, ctypes.byref(pBssList)) == 0:
                        bss_list = ctypes.cast(pBssList, ctypes.POINTER(WLAN_BSS_LIST)).contents
                        
                        for i in range(bss_list.dwNumberOfItems):
                            entry_ptr = ctypes.cast(
                                ctypes.addressof(bss_list.wlanBssEntries) + i * ctypes.sizeof(WLAN_BSS_ENTRY),
                                ctypes.POINTER(WLAN_BSS_ENTRY)
                            )
                            entry = entry_ptr.contents
                            ssid_len = entry.dot11Ssid.uSSIDLength
                            ssid = entry.dot11Ssid.ucSSID[:ssid_len].decode('utf-8', errors='ignore')
                            
                            if identifier in ssid:
                                logging.info(f"Найдена сеть GoPro по идентификатору: '{ssid}'")
                                return ssid
                        
                        if pBssList:
                            wlanapi.WlanFreeMemory(pBssList)
                            pBssList = ctypes.c_void_p()

            logging.warning(f"Сеть Wi-Fi, содержащая '{identifier}', не найдена после сканирования.")
            return None

        except Exception as e:
            logging.error(f"Ошибка при сканировании Wi-Fi сетей: {e}")
            return None
        finally:
            if pBssList: wlanapi.WlanFreeMemory(pBssList)
            if pIfList: wlanapi.WlanFreeMemory(pIfList)
            if hClient: wlanapi.WlanCloseHandle(hClient, None)

    async def switch_wifi_windows(ssid: str, password: str | None = None, timeout: int = 15, interface: str | None = None, verify_gopro: bool = True) -> bool:
        """
        Подключается к указанной Wi-Fi сети в Windows с логикой отката на 2.4 ГГц.
        """
        
        async def _connection_loop(profile_ssid: str, loop_timeout: int) -> bool:
            """Внутренняя функция, реализующая цикл подключения WlanConnect."""
            hClient = wintypes.HANDLE()
            pIfList = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
            try:
                if wlanapi.WlanOpenHandle(2, None, ctypes.byref(wintypes.DWORD()), ctypes.byref(hClient)) != 0:
                    raise RuntimeError("WlanOpenHandle failed.")
                if wlanapi.WlanEnumInterfaces(hClient, None, ctypes.byref(pIfList)) != 0:
                    raise RuntimeError("WlanEnumInterfaces failed.")
                iface_list = pIfList.contents
                if iface_list.dwNumberOfItems == 0:
                    logging.warning("Wi-Fi интерфейс не найден.")
                    return False
                iface_guid_bytes = iface_list.InterfaceInfo[0].InterfaceGuid
                iface_guid = ctypes.cast(ctypes.byref(iface_guid_bytes), ctypes.POINTER(ctypes.c_byte * 16))

                params = WLAN_CONNECTION_PARAMETERS()
                params.wlanConnectionMode = wlan_connection_mode_profile
                params.strProfile = profile_ssid
                params.dot11BssType = dot11_BSS_type_infrastructure

                end_time = asyncio.get_event_loop().time() + loop_timeout
                connect_spam_interval = 4
                last_spam_time = 0

                while asyncio.get_event_loop().time() < end_time:
                    current_time = asyncio.get_event_loop().time()
                    if current_time - last_spam_time >= connect_spam_interval:
                        last_spam_time = current_time
                        logging.debug(f"Отправка команды WlanConnect для '{profile_ssid}'...")
                        ret = wlanapi.WlanConnect(hClient, iface_guid, ctypes.byref(params), None)
                        if ret != 0:
                            logging.debug(f"WlanConnect вернул код ошибки: {ret}. Это может быть нормально, если сеть еще не видна.")

                    await asyncio.sleep(1)
                    current_ssid = await get_current_wifi_ssid_windows()
                    if current_ssid == profile_ssid:
                        if not verify_gopro:
                            logging.debug(f"Успешно подключено к Wi-Fi: {profile_ssid}")
                            return True
                        if await verify_gopro_connection():
                            logging.info(f"Успешно подключено к Wi-Fi '{profile_ssid}'.")
                            return True
                return False
            finally:
                if pIfList: wlanapi.WlanFreeMemory(pIfList)
                if hClient: wlanapi.WlanCloseHandle(hClient, None)

        async def _manage_profile_and_connect() -> bool:
            """Создает профиль и пытается подключиться."""
            # --- Управление профилем ---
            if password:
                logging.debug(f"Обновление профиля Wi-Fi для '{ssid}'...")
                profile_xml = create_wifi_profile_xml(ssid, password)
                
                hClient = wintypes.HANDLE()
                pIfList = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
                try: #
                    if wlanapi.WlanOpenHandle(2, None, ctypes.byref(wintypes.DWORD()), ctypes.byref(hClient)) != 0:
                        raise RuntimeError("WlanOpenHandle failed for profile management.")
                    if wlanapi.WlanEnumInterfaces(hClient, None, ctypes.byref(pIfList)) != 0:
                        raise RuntimeError("WlanEnumInterfaces failed for profile management.")
                    iface_list = pIfList.contents
                    if iface_list.dwNumberOfItems == 0:
                        logging.warning("Wi-Fi интерфейс не найден для управления профилем.")
                        return False
                    iface_guid_bytes = iface_list.InterfaceInfo[0].InterfaceGuid
                    iface_guid = ctypes.cast(ctypes.byref(iface_guid_bytes), ctypes.POINTER(ctypes.c_byte * 16))

                    # Удаляем старый профиль, если он существует, чтобы избежать конфликтов
                    wlanapi.WlanDeleteProfile(hClient, iface_guid, ssid, None)

                    # Пытаемся установить профиль для текущего пользователя (не требует прав администратора)
                    dwFlags_user = 0  # WLAN_PROFILE_USER
                    pdwReasonCode = wintypes.DWORD()
                    ret = wlanapi.WlanSetProfile(hClient, iface_guid, dwFlags_user, profile_xml, None, True, None, ctypes.byref(pdwReasonCode))

                    if ret != 0:
                        logging.warning(f"Не удалось создать профиль для текущего пользователя (код {ret}). Пробуем создать для всех пользователей (может требовать прав администратора)...")
                        # Резервный вариант: пытаемся установить профиль для всех пользователей
                        dwFlags_all_users = 0x4  # WLAN_PROFILE_GROUP_POLICY
                        ret = wlanapi.WlanSetProfile(hClient, iface_guid, dwFlags_all_users, profile_xml, None, True, None, ctypes.byref(pdwReasonCode))
                        if ret != 0: # Если и это не удалось, просто возвращаем False без лога ошибки
                            return False

                    logging.debug(f"Профиль Wi-Fi для '{ssid}' успешно создан/обновлен.")
                except Exception as e:
                    logging.error(f"Ошибка при управлении профилем Wi-Fi через WinAPI: {e}")
                    return False
                finally:
                    if pIfList: wlanapi.WlanFreeMemory(pIfList)
                    if hClient: wlanapi.WlanCloseHandle(hClient, None)

            else: # Для домашней сети
                profile_exists = False
                startupinfo = _get_subprocess_startupinfo()
                check_profile_cmd = ["netsh", "wlan", "show", "profile", f"name={ssid}"]
                try:
                    proc = await asyncio.create_subprocess_exec(*check_profile_cmd, startupinfo=startupinfo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    await proc.wait()
                    if proc.returncode == 0:
                        profile_exists = True
                except Exception as e:
                    logging.warning(f"Ошибка при проверке профиля Wi-Fi для '{ssid}': {e}")

                if not profile_exists:
                    logging.error(f"Профиль для домашней сети Wi-Fi '{ssid}' не найден в Windows.")
                    logging.error("Не удалось вернуться к исходной сети. Пожалуйста, подключитесь вручную.")
                    return False
            
            # --- Подключение ---
            log_func = logging.info if verify_gopro else logging.debug
            log_func(f"Подключаемся к Wi-Fi '{ssid}'...")
            if await _connection_loop(ssid, timeout):
                return True

            logging.error(f"Тайм-аут: не удалось подключиться и проверить соединение с '{ssid}' за {timeout} секунд.")
            return False

        # --- Упрощенная логика switch_wifi_windows ---
        return await _manage_profile_and_connect()


    async def verify_gopro_connection() -> bool:
        """Проверяет доступность GoPro по HTTP после подключения к Wi-Fi."""
        try: #
            async with httpx.AsyncClient(timeout=5.0) as client: #
                response = await client.get(f"{GOPRO_BASE_URL}/gopro/camera/state", follow_redirects=True) #
                response.raise_for_status()
            logging.debug("Проверка соединения с камерой по Wi-Fi успешна.")
            return True
        except (httpx.RequestError, httpx.HTTPStatusError):
            logging.debug(f"Не удалось связаться с камерой по адресу {GOPRO_BASE_URL}.")
            return False

else:
    # Заглушки для не-Windows систем, чтобы избежать NameError
    async def get_wifi_interface_windows() -> str | None: return None
    async def get_current_wifi_ssid_windows() -> str | None: return None
    async def find_wifi_ssid_windows_native(identifier: str, timeout: int = 30) -> str | None: return None
    async def switch_wifi_windows(ssid: str, password: str | None = None, timeout: int = 15, interface: str | None = None, verify_gopro: bool = True) -> bool: return False
    async def verify_gopro_connection() -> bool: return True

if is_windows:
    async def get_y_n_with_timeout_windows(prompt: str, timeout: int, input_queue: asyncio.Queue) -> bool:
        """
        Asks a y/n question with a timeout on Windows, using a shared input queue.
        Returns True for 'y'/'д', False for 'n'/'н' or timeout.
        """
        print(f"\n{prompt} (y/n, {timeout} сек на ответ): ", end="", flush=True)

        try:
            # Очищаем очередь от случайных нажатий перед началом
            while not input_queue.empty():
                input_queue.get_nowait()

            char_byte = await asyncio.wait_for(input_queue.get(), timeout=timeout)
            
            # Echo character and add a newline for clean output
            sys.stdout.buffer.write(char_byte)
            sys.stdout.buffer.write(b'\r\n')
            sys.stdout.flush()

            # Декодируем байт в строку, используя кодовую страницу консоли для ввода
            try:
                char_str = char_byte.decode(CONSOLE_INPUT_CP, errors='ignore').lower()
            except Exception:
                # Резервный вариант, если CONSOLE_INPUT_CP не сработал
                char_str = char_byte.decode('latin-1', errors='ignore').lower()

            # 'y' (английская) или 'н' (русская, на той же клавише, что и 'Y') для "да"
            if char_str in ('y', 'н'):
                return True
            # 'n' (английская) или 'т' (русская, на той же клавише, что и 'N') для "нет"
            elif char_str in ('n', 'т'):
                return False
            return False # Любая другая клавиша считается "нет" по умолчанию
        except asyncio.TimeoutError:
            print("\nВремя ожидания истекло. Ответ по умолчанию: 'нет'.")
            return False

def is_ffmpeg_available(ffmpeg_path: str) -> bool:
    """Проверяет доступность ffmpeg и ffprobe."""
    try:
        ffmpeg_p = Path(ffmpeg_path)
        # ffprobe должен находиться в той же директории, что и ffmpeg
        ffprobe_p = ffmpeg_p.with_name(ffmpeg_p.name.replace("ffmpeg", "ffprobe"))

        startupinfo = _get_subprocess_startupinfo()
        
        # Проверяем ffmpeg
        subprocess.run([str(ffmpeg_p), "-version"], capture_output=True, check=True, text=True, startupinfo=startupinfo)
        
        # Проверяем ffprobe
        try:
            subprocess.run([str(ffprobe_p), "-version"], capture_output=True, check=True, text=True, startupinfo=startupinfo)
        except (FileNotFoundError, subprocess.CalledProcessError):
            logging.debug(f"ffmpeg найден ('{ffmpeg_p}'), но ffprobe не найден по ожидаемому пути ('{ffprobe_p}').")
            logging.debug("ffprobe необходим для обработки видео. Убедитесь, что он находится в той же папке, что и ffmpeg.")
            return False

        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

async def download_ffmpeg_windows(target_dir: Path, input_queue: asyncio.Queue) -> str | None:
    """Скачивает и распаковывает ffmpeg для Windows."""
    FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    
    print("\n---")
    print("Утилита ffmpeg не найдена. Она необходима для склейки и переименования видео.")
    print(f"Скачать последнюю версию для Windows с gyan.dev? (архив ~100 МБ)")
    print("Нажмите 'y' для скачивания. Любая другая клавиша - пропустить и продолжить.")
    print("Ваш выбор: ", end="", flush=True)
    
    # Очищаем очередь от случайных нажатий
    while not input_queue.empty():
        input_queue.get_nowait()

    # Ждем одно нажатие клавиши
    char_byte = await input_queue.get()

    # Эхо-вывод и перевод строки
    sys.stdout.buffer.write(char_byte)
    sys.stdout.buffer.write(b'\r\n')
    sys.stdout.flush()

    # Декодируем байт в строку, используя кодовую страницу консоли для ввода
    try:
        answer_char = char_byte.decode(CONSOLE_INPUT_CP, errors='ignore').lower()
    except Exception:
        answer_char = char_byte.decode('latin-1', errors='ignore').lower()

    # 'y' (английская) или 'н' (русская, на той же клавише, что и 'Y') для "да"
    if answer_char not in ('y', 'н'):
        logging.warning("Скачивание ffmpeg отменено пользователем.")
        return None

    target_dir.mkdir(exist_ok=True, parents=True)
    
    logging.info(f"Скачивание ffmpeg с {FFMPEG_URL}...")
    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            async with client.stream("GET", FFMPEG_URL) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("Content-Length", 0))
                zip_data = io.BytesIO()
                
                with tqdm(total=total_size, unit='B', unit_scale=True, desc="Скачивание ffmpeg", ncols=100) as pbar:
                    async for chunk in response.aiter_bytes():
                        zip_data.write(chunk)
                        pbar.update(len(chunk))
        
        logging.info("Распаковка архива...")
        zip_data.seek(0)
        with zipfile.ZipFile(zip_data) as zf:
            ffmpeg_path_in_zip = next((name for name in zf.namelist() if name.endswith("bin/ffmpeg.exe")), None)
            ffprobe_path_in_zip = next((name for name in zf.namelist() if name.endswith("bin/ffprobe.exe")), None)
            
            if not ffmpeg_path_in_zip or not ffprobe_path_in_zip:
                raise FileNotFoundError("Не удалось найти ffmpeg.exe/ffprobe.exe в скачанном архиве.")
            
            ffmpeg_exe_path = target_dir / "ffmpeg.exe"
            ffprobe_exe_path = target_dir / "ffprobe.exe"
            
            with open(ffmpeg_exe_path, "wb") as f:
                f.write(zf.read(ffmpeg_path_in_zip))
            
            with open(ffprobe_exe_path, "wb") as f:
                f.write(zf.read(ffprobe_path_in_zip))
                
            logging.info(f"ffmpeg и ffprobe успешно установлены в '{target_dir}'")
            return str(ffmpeg_exe_path.resolve())
    except Exception as e:
        logging.error(f"Ошибка при скачивании или распаковке ffmpeg: {e}")
        return None

DEFAULT_CONFIG = r"""[General]
# Identifier: последние 4 символа серийного номера GoPro (часть имени сети).
# Закомментируйте эту строку (поставьте # в начале) для автоматического определения.
#identifier = 

# OutputFolder: папка для сохранения медиа.
output_folder = GoPro_Media

# HomeWifi: имя домашней Wi-Fi сети для возврата ПК после скачивания.
# Закомментируйте эту строку (поставьте # в начале), если не хотите автоматически переключаться обратно.
#home_wifi = 

[Processing]
# Mode: режим обработки файлов после скачивания.
# full:         Скачать, склеить сессии и переименовать по дате (по умолчанию).
# rename_only:  Скачать и переименовать КАЖДЫЙ файл по дате, не склеивая.
# download_only:Только скачать файлы с оригинальными именами.
# touch_only:   Скачать и установить дату файла равной дате съемки (для сторонних программ).
# process_only: Не скачивать, только обработать уже скачанные файлы в папке.
mode = full

# SessionGapHours: временной разрыв в часах, который считается новой съемочной сессией.
session_gap_hours = 2

# FileNameFormat: формат имени файла. Использует стандартные директивы strftime.
# Недопустимые для имен файлов символы (например, \ / : * ? " < > |) будут заменены на '_'.
# %y=год(2), %Y=год(4), %m=месяц, %d=день, %H=час, %M=минута, %S=секунда
# Пример для '2025-09-09_10-02.mp4': %Y-%m-%d_%H-%M
filename_format = %Y-%m-%d_%H_%M

# FfmpegPath: путь к исполняемому файлу ffmpeg.
ffmpeg_path = ffmpeg

[Advanced]
# WifiWait: секунд ожидания для одной попытки автоматического подключения к Wi-Fi.
wifi_wait = 30

# AutoCloseWindow: автоматически закрывать окно консоли после завершения работы (только для .exe).
# yes: Закрывать автоматически.
# no:  Ожидать нажатия Enter перед закрытием (по умолчанию).
auto_close_window = no

# MediaPort: порт для скачивания медиафайлов.
# Для старых камер (до HERO9) используйте 8080.
# Для новых камер (HERO9 и новее) можно оставить пустым для использования стандартного порта 80.
media_port = 8080

[Deletion]
# DeleteAfterDownload: удалять ли файлы с камеры после успешного скачивания.
# no:  Никогда не удалять.
# ask: Спрашивать каждый раз (по умолчанию).
# yes: Всегда удалять без запроса.
delete_after_download = ask

[Power]
# ShutdownAfterComplete: выключать ли камеру после завершения всех операций.
# yes: Выключать.
# no:  Оставить включенной (она выключится сама по таймеру).
shutdown_after_complete = yes
"""

def load_config(config_path: Path) -> tuple[dict[str, Any], configparser.ConfigParser]:
    """Загружает конфигурацию из config.ini. Если файл не существует, создает его с настройками по умолчанию."""
    config = configparser.ConfigParser(comment_prefixes=('#', ';'), allow_no_value=True, interpolation=None)
    
    if not config_path.exists():
        logging.info(f"Создан файл конфигурации '{config_path}'. Ознакомьтесь с его содержимым и настройте при необходимости.")
        try:
            with config_path.open('w', encoding='utf-8') as configfile:
                configfile.write(DEFAULT_CONFIG)
        except IOError as e:
            logging.error(f"Не удалось создать файл конфигурации: {e}")
            raise

    config.read(config_path, encoding='utf-8')

    settings = {
        'identifier': config.get('General', 'identifier', fallback=''),
        'output_folder': config.get('General', 'output_folder', fallback='GoPro_Media'),
        'home_wifi': config.get('General', 'home_wifi', fallback=''),
        'mode': config.get('Processing', 'mode', fallback='full').lower(),
        'session_gap_hours': config.getint('Processing', 'session_gap_hours', fallback=2),
        'filename_format': config.get('Processing', 'filename_format', fallback='(%Y-%m-%d_%H_%M)'),
        'ffmpeg_path': config.get('Processing', 'ffmpeg_path', fallback='ffmpeg'),
        'wifi_wait': config.getint('Advanced', 'wifi_wait', fallback=30),
        'media_port': config.get('Advanced', 'media_port', fallback='8080'),
        'auto_close_window': config.get('Advanced', 'auto_close_window', fallback='no').lower(),
        'delete_after_download': config.get('Deletion', 'delete_after_download', fallback='ask').lower(),
        'shutdown_after_complete': config.get('Power', 'shutdown_after_complete', fallback='yes').lower()
    }
    
    # Обновляем глобальные переменные на основе конфига
    global GOPRO_BASE_URL
    media_port_str = settings['media_port']
    if media_port_str and media_port_str.isdigit():
        GOPRO_BASE_URL = f"http://10.5.5.9:{media_port_str}"
    else:
        GOPRO_BASE_URL = "http://10.5.5.9"

    if not settings['identifier'].strip():
        settings['identifier'] = None
        
    return settings, config

def save_config_updates(config_path: Path, updates: dict[tuple[str, str], str]):
    """
    Обновляет файл конфигурации, сохраняя комментарии.
    `updates` - это словарь, где ключ - кортеж (секция, ключ), а значение - новое значение.
    """
    if not updates:
        return

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        current_section = None
        new_lines = []
        
        pending_updates = updates.copy()

        for line in lines:
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith('#') or stripped_line.startswith(';'):
                new_lines.append(line)
                continue

            section_match = re.match(r'\[(.+?)\]', stripped_line)
            if section_match:
                current_section = section_match.group(1).strip()
                new_lines.append(line)
                continue

            if current_section and '=' in line:
                key_part = line.split('=', 1)[0].strip()
                update_key = (current_section, key_part)
                
                if update_key in pending_updates:
                    new_value = pending_updates[update_key]
                    indent = line[:len(line) - len(line.lstrip())]
                    new_line = f"{indent}{key_part} = {new_value}\n"
                    new_lines.append(new_line)
                    del pending_updates[update_key]
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        with open(config_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        logging.debug(f"Файл конфигурации '{config_path}' обновлен для ускорения будущих запусков.")

    except (IOError, FileNotFoundError) as e:
        logging.warning(f"Не удалось обновить файл конфигурации: {e}")

async def input_and_cancel_handler_windows(main_task: asyncio.Task, input_queue: asyncio.Queue) -> None:
    """
    В фоновом режиме ожидает нажатия клавиш.
    - При нажатии Escape отменяет основную задачу.
    - Другие нажатые клавиши помещает в очередь `input_queue`.
    """
    import msvcrt
    loop = asyncio.get_event_loop()
    
    while not main_task.done():
        try:
            if await loop.run_in_executor(None, msvcrt.kbhit):
                key = await loop.run_in_executor(None, msvcrt.getch)
                if key == b'\x1b':  # Код клавиши Escape
                    if not main_task.done():
                        logging.warning("\nНажата клавиша Escape. Инициируется отмена операции...")
                        main_task.cancel()
                    break
                else:
                    # Помещаем в очередь, чтобы другие части программы могли обработать ввод
                    await input_queue.put(key)
            
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
    logging.debug("Задача-слушатель ввода и отмены завершена.")

async def main() -> None:
    """Основная логика скрипта."""
    if '--help' in sys.argv or '-h' in sys.argv:
        show_help()
        return

    # Сообщение о возможности отмены выводится в самом начале
    if platform.system() == "Windows":
        print("\n" + "="*80)
        print("    (Для отмены операции в любой момент нажмите клавишу 'Escape')")
        print("="*80)

    asyncio.get_event_loop().set_exception_handler(exception_handler)
    wifi_keep_alive_hdl: asyncio.Task | None = None
    disk_keep_alive_hdl: asyncio.Task | None = None
    stop_wifi_keep_alive = asyncio.Event()
    stop_disk_keep_alive = asyncio.Event()
    matched_device: BleakDevice | None = None
    state = GoProState()
    original_ssid = None
    is_windows = platform.system() == "Windows"
    connected_to_gopro_wifi = False
    all_downloads_completed = False
    downloaded_count = 0
    downloaded_files_meta: list[dict[str, Any]] = []
    wifi_interface: str | None = None
    config_updates: dict[tuple[str, str], str] = {}
    input_queue: asyncio.Queue | None = None

    try:
        if is_windows:
            logging.debug("Предотвращение засыпания системы и дисплея на время работы скрипта.")
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)

            # Создаем очередь для ввода и запускаем задачу-слушатель
            input_queue = asyncio.Queue()
            main_task = asyncio.current_task()
            escape_listener_task = asyncio.create_task(input_and_cancel_handler_windows(main_task, input_queue))

        config_path = Path("config.ini")
        config, config_parser = load_config(config_path)

        identifier = config['identifier']
        output_folder = config['output_folder']
        home_wifi = config['home_wifi']
        mode = config['mode']
        session_gap_hours = config['session_gap_hours']
        filename_format = config['filename_format']
        ffmpeg_path = config['ffmpeg_path']
        wifi_wait = config['wifi_wait']
        delete_after_download = config['delete_after_download']
        shutdown_after_complete = config['shutdown_after_complete']
        
        valid_modes = ['full', 'rename_only', 'download_only', 'touch_only', 'process_only']
        if mode not in valid_modes:
            raise ValueError(f"Неверное значение 'mode' в config.ini: {mode}. Допустимые значения: {valid_modes}")

        if mode in ['full', 'rename_only', 'process_only']:
            script_dir = get_script_dir()
            ffmpeg_found = is_ffmpeg_available(ffmpeg_path)

            # Если ffmpeg не найден по пути из конфига, ищем его в стандартных местах
            # 1. В корневой папке скрипта
            if not ffmpeg_found:
                local_ffmpeg_path = script_dir / ("ffmpeg.exe" if is_windows else "ffmpeg")
                if local_ffmpeg_path.exists() and is_ffmpeg_available(str(local_ffmpeg_path)):
                    logging.debug(f"Найден ffmpeg в корневой папке: '{local_ffmpeg_path}'. Используем его.")
                    ffmpeg_path = str(local_ffmpeg_path)
                    ffmpeg_found = True

            # 2. В папке 'bin' рядом со скриптом
            if not ffmpeg_found:
                bin_ffmpeg_path = script_dir / "bin" / ("ffmpeg.exe" if is_windows else "ffmpeg")
                if bin_ffmpeg_path.exists() and is_ffmpeg_available(str(bin_ffmpeg_path)):
                    logging.debug(f"Найден ffmpeg в папке 'bin': '{bin_ffmpeg_path}'. Используем его.")
                    ffmpeg_path = str(bin_ffmpeg_path)
                    ffmpeg_found = True

            # 3. Если ничего не найдено, предлагаем скачать
            if not ffmpeg_found:
                if is_windows:
                    bin_dir = script_dir / "bin"
                    new_ffmpeg_path = await download_ffmpeg_windows(bin_dir, input_queue)
                    if new_ffmpeg_path:
                        ffmpeg_path = new_ffmpeg_path
                        config_updates[('Processing', 'ffmpeg_path')] = new_ffmpeg_path

        if is_windows:
            original_ssid = await get_current_wifi_ssid_windows()
            if original_ssid:
                logging.debug(f"Текущая сеть Wi-Fi: {original_ssid}")
            # Автоматически сохраняем домашнюю сеть, если она не задана
            if not home_wifi and original_ssid and not original_ssid.startswith("GP"):
                logging.debug(f"Сохранение текущей сети '{original_ssid}' как домашней в config.ini.")
                home_wifi = original_ssid # Обновляем локальную переменную
                config_updates[('General', 'home_wifi')] = original_ssid
            wifi_interface = await get_wifi_interface_windows()

        # 1. Подключение к камере по BLE
        if mode == 'process_only':
            logging.info("Режим 'process_only': пропуск этапа подключения и скачивания.")
            # Устанавливаем флаги, чтобы блок обработки запустился после finally
            all_downloads_completed = True
            downloaded_count = 1 # > 0, чтобы запустить обработку
        else:
            # Вся логика подключения и скачивания выполняется, если режим НЕ 'process_only'
            client, matched_device, wifi_creds = await connect_ble(state.notification_handler, identifier) # type: ignore
            if not client or not matched_device: # Если камера не найдена
                logging.info("Завершение работы, так как камера не была найдена.")
                return

            # Автоматически сохраняем идентификатор камеры, если он не был задан
            if not identifier and matched_device.name:
                match = re.search(r"([A-Z0-9]{4})$", matched_device.name)
                if match:
                    found_identifier = match.group(1)
                    logging.debug(f"Сохранение идентификатора камеры '{found_identifier}' в config.ini.")
                    identifier = found_identifier # Обновляем локальную переменную
                    config_updates[('General', 'identifier')] = found_identifier

            # Если были изменения в конфигурации, сохраняем их, сохраняя комментарии
            if config_updates:
                save_config_updates(config_path, config_updates)

            # 2. Включаем Wi-Fi Access Point на камере
            logging.info("Включаем Wi-Fi Access Point на камере.")
            client = await control_wifi_ap(client, matched_device, state, enable=True)

            # Отключаемся от BLE, так как он больше не требуется для скачивания по Wi-Fi
            logging.info("Wi-Fi включен. Завершение сеанса Bluetooth...")
            if client:
                await client.disconnect()
                await asyncio.sleep(1.0) # Пауза для корректного завершения
            client = None # Указываем, что клиент больше не подключен
            # Короткая пауза перед началом сканирования, чтобы Wi-Fi успел включиться и стать видимым
            logging.debug("Ожидание инициализации Wi-Fi на камере (1 секунда)...")
            await asyncio.sleep(1)

            # 3. Поиск и подключение к Wi-Fi камеры
            if is_windows:
                gopro_ssid_from_ble = wifi_creds.get("ssid")

                if gopro_ssid_from_ble:
                    logging.debug(f"Используем SSID '{gopro_ssid_from_ble}', полученный с камеры по BLE, для прямого подключения.")
                    if await switch_wifi_windows(gopro_ssid_from_ble, password=wifi_creds.get("password"), timeout=wifi_wait, interface=wifi_interface, verify_gopro=True):
                        connected_to_gopro_wifi = True
                else:
                    # Резервный вариант: если не удалось получить SSID по BLE, ищем сеть сканированием.
                    logging.warning("Не удалось получить SSID с камеры по BLE. Попытка найти сеть сканированием...")
                    ble_name_identifier = (matched_device.name or "").split(" ")[-1] if matched_device else ""
                    target_identifier = identifier or ble_name_identifier
                    
                    scanned_ssid = await find_wifi_ssid_windows_native(target_identifier, timeout=30)
                    if scanned_ssid and await switch_wifi_windows(scanned_ssid, password=wifi_creds.get("password"), timeout=wifi_wait, interface=wifi_interface, verify_gopro=True):
                        connected_to_gopro_wifi = True

            if not connected_to_gopro_wifi: # Если автоматически не вышло
                logging.info("="*80)
                logging.info("ДЕЙСТВИЕ: Не удалось подключиться к Wi-Fi автоматически.")
                logging.info("Пожалуйста, подключитесь к Wi-Fi сети камеры вручную.")
                logging.info("="*80)
                connection_verified = False
                loop = asyncio.get_event_loop()
                while not connection_verified:
                    print("Нажмите Enter, когда подключитесь, или введите 'x' и Enter для отмены.", end="", flush=True)
                    # Используем run_in_executor для неблокирующего и безопасного чтения из stdin
                    user_input = await loop.run_in_executor(None, sys.stdin.readline)
                    
                    if user_input.strip().lower() in ['x', 'х']:
                        logging.info("Отмена пользователем. Завершение работы.")
                        return

                    logging.info("Проверка соединения с камерой по Wi-Fi...")
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            # Используем простой эндпоинт для быстрой проверки
                            response = await client.get(f"{GOPRO_BASE_URL}/gopro/camera/state", follow_redirects=True)
                            response.raise_for_status()
                        logging.info("✅ Соединение с камерой установлено!")
                        connection_verified = True
                        connected_to_gopro_wifi = True # Отмечаем для finally блока
                    except (httpx.RequestError, httpx.HTTPStatusError) as e:
                        logging.error(f"❌ Не удалось связаться с камерой по адресу {GOPRO_BASE_URL}: {e}")
                        logging.info("   Убедитесь, что вы подключены к Wi-Fi сети GoPro, и попробуйте снова.")

            # Если мы подключились к Wi-Fi, запускаем Wi-Fi keep-alive
            if connected_to_gopro_wifi:
                # Добавляем небольшую паузу, чтобы дать сетевому стеку (особенно в Windows)
                # полностью стабилизироваться после подключения к Wi-Fi, даже после успешной проверки.
                # Это помогает избежать ошибок "connection failed" сразу после подключения.
                logging.debug("Соединение с Wi-Fi установлено. Ожидание стабилизации сети (3 сек)...")
                await asyncio.sleep(3) # Пауза для стабилизации IP

                wifi_keep_alive_hdl = asyncio.create_task(wifi_keep_alive_task(stop_wifi_keep_alive))

            # 4. Скачивание всех медиафайлов
            # Запускаем задачу для предотвращения засыпания диска
            output_path = Path(output_folder)
            disk_keep_alive_hdl = asyncio.create_task(disk_keep_alive_task(output_path, stop_disk_keep_alive))
            downloaded_count, all_downloads_completed, downloaded_files_meta, all_files_on_camera_meta = await download_files(output_path)
            # Останавливаем задачу disk keep-alive
            stop_disk_keep_alive.set()
            logging.info(f"Процесс скачивания завершен. Скачано новых файлов: {downloaded_count}.")
            if not all_downloads_completed:
                logging.warning("Скачивание было прервано. Обработка и удаление файлов будут пропущены.")
            
            # 5. Удаление файлов с камеры (если включено)
            if all_downloads_completed and all_files_on_camera_meta:
                # Определяем, какие из файлов на камере уже есть на диске
                files_on_disk_and_camera = [
                    f_meta for f_meta in all_files_on_camera_meta if (output_path / f_meta['n']).exists()
                ]

                if not files_on_disk_and_camera:
                    # Это может произойти, если скачивание было прервано и ни один файл не скачался полностью
                    logging.debug("Нет полностью скачанных файлов для удаления.")
                    should_delete = False # Already False, but explicit for clarity
                else:
                    should_delete = False
                should_delete = False
                if delete_after_download == 'yes':
                    should_delete = True
                elif delete_after_download == 'ask':
                    if is_windows:
                        should_delete = await get_y_n_with_timeout_windows(
                            f"Удалить {len(files_on_disk_and_camera)} файлов, уже имеющихся на диске, с камеры?",
                            15,
                            input_queue
                        )
                    else:
                        # Резервный вариант для не-Windows систем без таймаута
                        loop = asyncio.get_event_loop()
                        print(f"\nУдалить {len(files_on_disk_and_camera)} файлов, уже имеющихся на диске, с камеры? (y/n): ", end="", flush=True)
                        user_input = await loop.run_in_executor(None, sys.stdin.readline)
                        answer = user_input.strip().lower()
                        if answer in ['y', 'yes', 'д', 'да', 'н']:
                            should_delete = True
                        else:
                            should_delete = False
                
                if should_delete:
                    # Отправляем один пинг перед удалением на всякий случай
                    logging.debug("Отправка дополнительного keep-alive ping перед началом удаления...")
                    try:
                        async with httpx.AsyncClient(timeout=10) as client:
                            ping_url = f"{GOPRO_BASE_URL}/gopro/camera/keep_alive"
                            await client.get(ping_url)
                    except httpx.RequestError as e:
                        logging.warning(f"Не удалось отправить ping перед удалением (камера могла уснуть): {e}")

                    await delete_files_from_camera(files_on_disk_and_camera)
                else:
                    logging.info("Удаление файлов с камеры пропущено.")

            # 6. Выключение камеры (если включено)
            if shutdown_after_complete == 'yes' and matched_device and mode != 'process_only':
                try:
                    # Нам нужен новый клиент, т.к. старый был отключен
                    client = await sleep_camera(None, matched_device, state)
                    if client and client.is_connected:
                        await client.disconnect()
                        client = None # Убедимся, что он None для finally блока
                except Exception as e:
                    logging.warning(f"Не удалось корректно выключить камеру: {e}")

    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("\nОперация прервана пользователем. Запускается процедура очистки...")
    except Exception as e:
        logging.error(f"Произошла критическая ошибка в основном процессе: {e}", exc_info=True)
    finally:
        # Останавливаем задачу-слушатель Escape в первую очередь
        if 'escape_listener_task' in locals() and escape_listener_task:
            logging.debug("Остановка задачи-слушателя Escape...")
            if not escape_listener_task.done():
                escape_listener_task.cancel()
                try:
                    # Даем ей шанс завершиться чисто
                    await asyncio.wait_for(escape_listener_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass # Ожидаемое поведение

        logging.debug("Блок finally: начало выполнения.")
        if is_windows:
            logging.debug("Возврат настроек энергосбережения системы.")
            # Сбрасываем флаг, возвращая систему к нормальному управлению питанием
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)

        if disk_keep_alive_hdl:
            logging.debug("Остановка задачи disk keep-alive...")
            stop_disk_keep_alive.set() # Убеждаемся, что событие установлено
            try:
                await asyncio.wait_for(disk_keep_alive_hdl, timeout=2)
            except asyncio.TimeoutError:
                logging.warning("Задача disk keep-alive не остановилась вовремя.")

        if wifi_keep_alive_hdl:
            logging.debug("Остановка задачи Wi-Fi keep-alive...")
            stop_wifi_keep_alive.set()
            try:
                await asyncio.wait_for(wifi_keep_alive_hdl, timeout=3)
            except asyncio.TimeoutError:
                logging.warning("Задача Wi-Fi keep-alive не остановилась вовремя.")

        # 7. Обработка скачанных файлов (после всех операций с камерой)
        # Этот блок выполняется после скачивания и до возврата на домашний Wi-Fi.
        if all_downloads_completed and (downloaded_count > 0 or mode == 'process_only'):
            if mode in ['full', 'rename_only', 'process_only']:
                process_media(
                    output_folder=Path(output_folder),
                    downloaded_files=downloaded_files_meta,
                    session_gap_hours=session_gap_hours,
                    ffmpeg_path=ffmpeg_path,
                    mode=mode,
                    filename_format=filename_format
                )
            elif mode == 'touch_only':
                touch_files(Path(output_folder), downloaded_files_meta)
            elif mode == 'download_only':
                logging.info("Режим 'download_only': обработка файлов пропущена, как и было задано.")
        # Не выводим сообщение о пропуске, если скачивание было прервано, 
        # т.к. пользователь уже получил сообщение об отмене.
        elif mode != 'process_only' and downloaded_count == 0 and all_downloads_completed:
             logging.info("Пропуск обработки медиа (новые файлы не были скачаны).")

        # Возвращаемся на домашний Wi-Fi
        if is_windows and connected_to_gopro_wifi:
            target_ssid = home_wifi or original_ssid
            if target_ssid:
                logging.info(f"Возврат к исходной сети '{target_ssid}'...")
                await switch_wifi_windows(target_ssid, timeout=15, interface=wifi_interface, verify_gopro=False)
            else:
                logging.debug("Блок finally: Не найдено имя исходной сети для возврата.")
        else:
            logging.debug("Блок finally: Возврат к исходной сети Wi-Fi не требуется (переключения не было или не Windows).")

        logging.debug("Блок finally: завершение.")


if __name__ == "__main__":
    exit_code = 0
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() перехватывает KeyboardInterrupt, отменяет задачу и затем снова его вызывает.
        # Так как мы обрабатываем прерывание в main(), здесь нам нужно просто тихо выйти,
        # чтобы избежать вывода "необработанного прерывания".
        pass # exit_code остается 0
    except (RuntimeError, asyncio.TimeoutError, ValueError) as e:
        logging.error(f"Ошибка выполнения: {e}")
        exit_code = 1
    finally:
        # Если скрипт запущен как .exe, ждем нажатия клавиши или таймаута перед закрытием
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            import time
            import msvcrt

            # Загружаем настройку автозакрытия окна
            auto_close = False
            auto_close_delay = 5  # секунд для auto_close=yes
            manual_close_timeout = 60 # секунд для auto_close=no

            try:
                config_path = Path("config.ini")
                if config_path.exists():
                    config = configparser.ConfigParser()
                    config.read(config_path, encoding='utf-8')
                    auto_close_config = config.get('Advanced', 'auto_close_window', fallback='no').lower()
                    auto_close = auto_close_config == 'yes'
            except Exception:
                # В случае ошибки при чтении конфига, придерживаемся безопасного поведения
                pass

            if auto_close:
                print(f"\nОкно закроется через {auto_close_delay} секунд...")
                time.sleep(auto_close_delay)
            else:
                print(f"\nНажмите Enter для закрытия или подождите {manual_close_timeout} секунд...")
                start_time = time.time()
                while time.time() - start_time < manual_close_timeout:
                    if msvcrt.kbhit() and msvcrt.getch() in (b'\r', b'\n'):
                        break
                    time.sleep(0.2)  # Предотвращаем загрузку CPU
    
    sys.exit(exit_code)


import sys
import os
import logging
import time
import subprocess
from pathlib import Path
import concurrent.futures  # Для параллельного выполнения длительностей
import json  # For handling JSON operations

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QFileDialog,
    QVBoxLayout, QWidget, QTableWidget, QHBoxLayout, QTableWidgetItem,
    QMessageBox, QLabel, QProgressBar, QTreeView, QFileSystemModel,
    QSplitter, QMenu, QAction, QPlainTextEdit, QCheckBox, QSpinBox,
    QListWidget, QListWidgetItem, QDialog, QTabWidget
)
from PyQt5.QtGui import QColor, QBrush, QFont, QCursor
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QDir, QTimer, QSortFilterProxyModel
from PyQt5.QtChart import QChart, QChartView, QBarSet, QValueAxis, QBarSeries, QBarCategoryAxis  # Import QChart, QChartView, QBarSet, QValueAxis, QBarSeries, and QBarCategoryAxis

from send2trash import send2trash
from fuzzywuzzy import fuzz

try:
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

VIDEO_EXTS = [".flv", ".mp4", ".avi", ".mov", ".mkv", ".m4v", ".ts", ".webm", ".mpeg", ".mpg", ".wmv"]
AUDIO_EXTS = [".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".opus", ".wma", ".aiff"]
MKV_EXT = ".mkv"
SUBTITLE_EXTS = ['.srt', '.ass', '.ssa', '.sub']

def check_mkvmerge():
    try:
        subprocess.run(
            ["mkvmerge", "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

def run_no_console(command, check=False, text=False):
    """
    Запуск процесса в фоновом режиме (без консоли) с ожиданием завершения.
    """
    if os.name == 'nt':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
        return subprocess.run(
            command,
            check=check,
            text=text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=si,
            creationflags=creationflags
        )
    else:
        return subprocess.run(command, check=check, text=text,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def get_media_duration_ffprobe(file_path: Path) -> float:
    """
    Определяем длительность медиафайла через ffprobe.
    Возвращаем -1 при ошибке.
    """
    try:
        # Сначала пытаемся прочитать длительность видео-дорожки
        r = run_no_console([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ], check=False, text=True)
        duration_str = r.stdout.strip()

        # Если не нашли видео, пробуем аудио-дорожку
        if not duration_str:
            r = run_no_console([
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path)
            ], check=False, text=True)
            duration_str = r.stdout.strip()

        if duration_str:
            return float(duration_str)
        return -1.0
    except Exception:
        return -1.0

def format_duration(seconds: float) -> str:
    if seconds < 0:
        return "N/A"
    total_seconds = int(round(seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def get_video_codec(file_path: Path) -> str:
    """
    Определяем кодек видео через ffprobe.
    """
    try:
        result = run_no_console([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ], check=False, text=True)
        return result.stdout.strip().lower()  # для удобства приводим к нижнему регистру
    except Exception:
        return ""

def popen_no_console(command):
    """
    Запуск процесса в фоновом режиме (без консоли) без ожидания завершения.
    Возвращает объект Popen.
    """
    if os.name == 'nt':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=si,
            creationflags=creationflags
        )
    else:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def run_cancellable(command, cancel_check, log_callback=None):
    """
    Запускает внешний процесс с проверкой флага отмены.
    - cancel_check: функция без аргументов, возвращающая True/False.
    - log_callback: функция для логирования сообщений.
    Возвращает (returncode, stderr).
    Если отменено, возвращает (None, "Cancelled").
    """
    cmd_str = " ".join(command)
    if log_callback:
        log_callback(f"Запуск команды: {cmd_str}")

    try:
        if os.name == 'nt':
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                startupinfo=si,
                creationflags=creationflags
            )
        else:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True
            )
    except Exception as e:
        if log_callback:
            log_callback(f"Не удалось запустить команду: {cmd_str}. Ошибка: {e}")
        return None, str(e)

    # Проверяем отмену до завершения процесса
    while proc.poll() is None:
        if cancel_check():
            proc.terminate()
            if log_callback:
                log_callback("Процесс прерван пользователем.")
            return None, "Cancelled"
        time.sleep(0.1)

    # После завершения читаем stderr
    _, err = proc.communicate()
    return proc.returncode, err.strip() if err else ""

class StatsWindow(QDialog):
    def __init__(self, file_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Статистика медиафайлов")
        self.resize(800, 600)
        
        self.file_data = file_data  # Список словарей с метаданными файлов
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout()
        
        # Вкладки
        self.tab_widget = QTabWidget()
        
        # 1. График длительностей
        self.duration_chart = QChart()
        self.duration_view = QChartView(self.duration_chart)
        self.tab_widget.addTab(self.duration_view, "Длительности")
        
        # 2. Распределение кодеков
        self.codec_chart = QChart()
        self.codec_view = QChartView(self.codec_chart)
        self.tab_widget.addTab(self.codec_view, "Кодеки")
        
        # 3. Отчет по операциям
        self.report_text = QPlainTextEdit()
        self.report_text.setReadOnly(True)
        self.tab_widget.addTab(self.report_text, "Отчет")
        
        layout.addWidget(self.tab_widget)
        self.setLayout(layout)
        
        # Заполняем данные
        self.update_stats()
    
    def update_stats(self):
        self.show_duration_distribution()
        self.show_codec_distribution()
        self.generate_report()
    
    def show_duration_distribution(self):
        durations = [f['duration'] for f in self.file_data if 'duration' in f and f['duration'] > 0]
        if not durations:
            self.report_text.appendPlainText("Нет данных о длительностях")
            return
            
        series = QBarSeries()
        bar_set = QBarSet("Длительность (сек)")
        
        # Группируем по диапазонам
        max_dur = max(durations)
        step = max(1, int(max_dur / 10))
        bins = [0] * 10
        
        for dur in durations:
            idx = min(int(dur / step), 9)
            bins[idx] += 1
            
        bar_set.append(bins)
        series.append(bar_set)
        
        chart = QChart()
        chart.addSeries(series)
        chart.setTitle("Распределение длительностей")
        chart.setAnimationOptions(QChart.SeriesAnimations)
        
        axis_x = QBarCategoryAxis()
        axis_x.append([f"{i*step}-{(i+1)*step}" for i in range(10)])
        chart.addAxis(axis_x, Qt.AlignBottom)
        series.attachAxis(axis_x)
        
        axis_y = QValueAxis()
        chart.addAxis(axis_y, Qt.AlignLeft)
        series.attachAxis(axis_y)
        
        self.duration_chart = chart
        self.duration_view.setChart(chart)
    
    def show_codec_distribution(self):
        # Собираем данные о кодеках
        codecs = {}
        for f in self.file_data:
            if 'codec' in f:
                codec = f['codec'] or 'unknown'
                codecs[codec] = codecs.get(codec, 0) + 1
        
        if not codecs:
            self.report_text.appendPlainText("Нет данных о кодеках")
            return
            
        series = QBarSeries()
        bar_set = QBarSet("Кодеки")
        
        # Сортируем по количеству
        sorted_codecs = sorted(codecs.items(), key=lambda x: x[1], reverse=True)
        codec_names = [codec[0] for codec in sorted_codecs]
        codec_counts = [codec[1] for codec in sorted_codecs]
        
        bar_set.append(codec_counts)
        series.append(bar_set)
        
        chart = QChart()
        chart.addSeries(series)
        chart.setTitle("Распределение кодеков")
        chart.setAnimationOptions(QChart.SeriesAnimations)
        
        axis_x = QBarCategoryAxis()
        axis_x.append(codec_names)
        chart.addAxis(axis_x, Qt.AlignBottom)
        series.attachAxis(axis_x)
        
        axis_y = QValueAxis()
        chart.addAxis(axis_y, Qt.AlignLeft)
        series.attachAxis(axis_y)
        
        self.codec_chart = chart
        self.codec_view.setChart(chart)
    
    def generate_report(self):
        """Генерация текстового отчета"""
        report = []
        
        # Общая статистика
        total_files = len(self.file_data)
        video_files = sum(1 for f in self.file_data if f.get('codec') not in ['audio', None])
        audio_files = total_files - video_files
        
        report.append(f"Всего файлов: {total_files}")
        report.append(f"Видео файлов: {video_files}")
        report.append(f"Аудио файлов: {audio_files}")
        report.append("")
        
        # Статистика по длительностям
        durations = [f['duration'] for f in self.file_data if 'duration' in f and f['duration'] > 0]
        if durations:
            avg_duration = sum(durations) / len(durations)
            max_duration = max(durations)
            min_duration = min(durations)
            
            report.append("Статистика длительностей:")
            report.append(f"  Средняя: {format_duration(avg_duration)}")
            report.append(f"  Максимальная: {format_duration(max_duration)}")
            report.append(f"  Минимальная: {format_duration(min_duration)}")
            report.append("")
        
        # Статистика по кодекам
        codecs = {}
        for f in self.file_data:
            if 'codec' in f:
                codec = f['codec'] or 'unknown'
                codecs[codec] = codecs.get(codec, 0) + 1
        
        if codecs:
            report.append("Используемые кодеки:")
            for codec, count in sorted(codecs.items(), key=lambda x: x[1], reverse=True):
                report.append(f"  {codec}: {count}")
        
        self.report_text.setPlainText("\n".join(report))



class DeleteConfirmDialog(QDialog):
    def __init__(self, file_list_text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подтверждение удаления файлов")
        self.resize(600, 400)
        layout = QVBoxLayout(self)
        label = QLabel("Будут удалены следующие файлы:")
        layout.addWidget(label)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlainText(file_list_text)
        layout.addWidget(self.text_edit)
        button_layout = QHBoxLayout()
        self.btn_delete = QPushButton("Удалить")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_delete.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        button_layout.addWidget(self.btn_delete)
        button_layout.addWidget(self.btn_cancel)
        layout.addLayout(button_layout)

# Поток сканирования
class ScanThread(QThread):
    scanProgress = pyqtSignal(int)
    scanFinished = pyqtSignal(list, list, list, list, list, dict)

    def __init__(self, directory, check_duration, duration_tolerance, name_threshold):
        super().__init__()
        self.metadata_cache = MetadataCache()
        self.directory = Path(directory)
        self.check_duration = check_duration
        self.duration_tolerance = duration_tolerance
        self.name_threshold = name_threshold
        self._is_cancelled = False
        
    def get_cached_duration(self, file_path):
        """Пытаемся получить длительность из кэша"""
        duration = self.metadata_cache.get_duration(file_path)
        if duration > 0:
            return duration
        return get_media_duration_ffprobe(file_path)

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        if not self.directory.is_dir():
            self.scanFinished.emit([], [], [], [], [], {})
            return

        all_files = list(self.directory.rglob('*'))
        total = len(all_files)
        if total == 0:
            self.scanFinished.emit([], [], [], [], [], {})
            return

        video_files = []
        audio_files = []
        all_mkv_files = {}
        durations_cache = {}

        # Сканируем файлы
        for i, f in enumerate(all_files):
            if self._is_cancelled:
                logging.info("Сканирование отменено пользователем.")
                self.scanFinished.emit([], [], [], [], [], {})
                return

            if f.is_file():
                ext = f.suffix.lower()
                if ext in VIDEO_EXTS and ext != MKV_EXT:
                    video_files.append(f)
                elif ext in AUDIO_EXTS:
                    audio_files.append(f)
                elif ext == MKV_EXT:
                    # храним .mkv в словаре, чтобы не терять порядок
                    all_mkv_files[f] = True

            self.scanProgress.emit(int((i + 1) / total * 100))

        # Кэш длительностей (параллельно)
        if self.check_duration:
            files = video_files + audio_files
            total_files = len(files)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                future_to_file = {executor.submit(get_media_duration_ffprobe, f): f for f in files}
                for i, future in enumerate(concurrent.futures.as_completed(future_to_file)):
                    if self._is_cancelled:
                        logging.info("Сканирование отменено пользователем.")
                        self.scanFinished.emit([], [], [], [], [], {})
                        return
                    f = future_to_file[future]
                    try:
                        durations_cache[f] = future.result()
                    except Exception:
                        durations_cache[f] = -1.0
                    self.scanProgress.emit(int((i + 1) / total_files * 100))
        else:
            for f in video_files + audio_files:
                durations_cache[f] = -1.0

        matched_files = []
        converted_files = []

        # Сопоставляем видео и аудио в одной папке
        for video in video_files:
            if self._is_cancelled:
                logging.info("Сканирование отменено пользователем.")
                self.scanFinished.emit([], [], [], [], [], {})
                return

            best_match_audio = None
            best_ratio = -1
            vdur = durations_cache.get(video, -1)

            # Ищем аудио в той же папке
            for audio in audio_files:
                if video.parent != audio.parent:
                    continue

                ratio = fuzz.ratio(video.stem, audio.stem)
                adur = durations_cache.get(audio, -1)

                if self.check_duration and (vdur > 0 and adur > 0):
                    if abs(vdur - adur) > self.duration_tolerance:
                        continue

                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match_audio = audio

            if best_match_audio is not None and best_ratio >= self.name_threshold:
                matched_files.append((video, best_match_audio))
                out_mkv = video.with_name(video.stem + "_rus.mkv")
                if out_mkv.exists():
                    converted_files.append(out_mkv)

        # Превращаем словарь в список, чтобы сохранить совместимость
        all_mkv_list = list(all_mkv_files.keys())

        self.scanFinished.emit(
            video_files,
            audio_files,
            all_mkv_list,
            matched_files,
            converted_files,
            durations_cache
        )

class MetadataCache:
    def __init__(self):
        self.cache_file = Path.home() / ".video_merger_metadata_cache.json"
        self.cache = self.load_cache()
    
    def load_cache(self):
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка загрузки кэша: {e}")
        return {}
    
    def save_cache(self):
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка сохранения кэша: {e}")
    
    def get_duration(self, file_path):
        """Получаем длительность из кэша или вычисляем"""
        file_key = str(file_path)
        if file_key in self.cache:
            return self.cache[file_key].get('duration', -1)
        return -1
    
    def update_cache(self, file_path, metadata):
        """Обновляем кэш для файла"""
        self.cache[str(file_path)] = metadata
        self.save_cache()



# Поток конвертации
class ConvertThread(QThread):
    update_progress = pyqtSignal(int)       # прогресс общего числа файлов
    update_file_progress = pyqtSignal(int)  # прогресс текущего файла
    log_signal = pyqtSignal(str)            # вывод сообщений лога

    def __init__(self, matched_files, output_directory):
        super().__init__()
        self.matched_files = matched_files
        self.output_directory = output_directory
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def is_cancelled(self):
        return self._is_cancelled
    
    def process_existing_mkv(self, mkv_file, audio_file, output_file):
        """Добавляем аудиодорожку в существующий MKV"""
        mkvmerge_cmd = [
            "mkvmerge", "--ui-language", "ru",
            "--output", str(output_file),
            str(mkv_file),
            "--language", "0:rus", "--default-track", "0:yes",
            "(", str(audio_file), ")"
        ]
        ret, err = run_cancellable(mkvmerge_cmd, self.is_cancelled, self.log_signal.emit)
        return ret == 0
    

    def run(self):
        total_files = len(self.matched_files)
        self.log_signal.emit(f"ConvertThread запущен: всего пар = {total_files}")
        acceptable_codecs = {"h264", "mpeg4", "hevc", "av1", "av01"}

        for index, (video, audio) in enumerate(self.matched_files):
            if self._is_cancelled:
                self.log_signal.emit("Конвертация отменена пользователем.")
                break
            if video.suffix.lower() == ".mkv":
                output_file = video.parent / (video.stem + "_with_rus_audio.mkv")
                success = self.process_existing_mkv(video, audio, output_file)
                if success:
                    self.log_signal.emit(f"Аудио добавлено в существующий MKV: {output_file.name}")
                continue            

            if audio is None:
                self.log_signal.emit(f"Файл {video.name} без аудио - пропускаем.")
                continue

            # Определяем кодек
            codec = get_video_codec(video)
            msg_codec = f"Видео {video.name} имеет кодек: {codec}"
            self.log_signal.emit(msg_codec)

            # Определяем выходную папку
            out_dir = Path(self.output_directory) if self.output_directory else video.parent
            output_file = out_dir / (video.stem + "_rus.mkv")

            # 1) Если кодек не в acceptable_codecs, делаем перемультиплексирование (re-wrap) через ffmpeg
            if codec not in acceptable_codecs:
                self.log_signal.emit(
                    f"Кодек {codec} не в списке поддерживаемых ({acceptable_codecs}). Выполняем re-wrap через ffmpeg."
                )
                converted_video = video.with_suffix(".converted.mkv")
                ffmpeg_cmd = [
                    "ffmpeg", "-y",
                    "-i", str(video),
                    "-c", "copy",
                    "-f", "matroska",
                    str(converted_video)
                ]
                ret, err = run_cancellable(ffmpeg_cmd, self.is_cancelled, self.log_signal.emit)
                if ret is None:
                    # Прервано
                    break
                if ret != 0:
                    self.log_signal.emit(f"Ошибка re-wrap видео {video.name}: {err}")
                    continue
                # Обновляем файл на "перемультиплексированный"
                video = converted_video
                codec = get_video_codec(video)
                self.log_signal.emit(f"Re-wrap завершён, новый файл: {video.name} (кодек: {codec})")

            # 2) Если видео с кодеком AV1, делаем ffmpeg -c copy
            if codec in {"av1", "av01"}:
                self.log_signal.emit(f"Видео {video.name} (AV1) → мультиплексируем через ffmpeg без перекодирования.")
                ffmpeg_cmd = [
                    "ffmpeg", "-y",
                    "-i", str(video),
                    "-i", str(audio),
                    "-c", "copy",
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    str(output_file)
                ]
                ret, err = run_cancellable(ffmpeg_cmd, self.is_cancelled, self.log_signal.emit)
                if ret is None:
                    break
                if ret != 0:
                    self.log_signal.emit(f"Ошибка мультиплексирования AV1 видео {video.name}: {err}")
                else:
                    self.log_signal.emit(f"Успешно: {video.name} + {audio.name} -> {output_file.name}")
            else:
                # 3) Во всех остальных случаях — mkvmerge
                mkvmerge_cmd = [
                    "mkvmerge", "--ui-language", "ru",
                    "--output", str(output_file),
                    "--language", "0:und", "--language", "1:eng",
                    "(", str(video), ")",
                    "--language", "0:rus", "--default-track", "0:yes",
                    "(", str(audio), ")",
                    "--track-order", "0:0,0:1,1:0"
                ]
                self.log_signal.emit(f"Запуск mkvmerge для {video.name} + {audio.name}")
                ret, err = run_cancellable(mkvmerge_cmd, self.is_cancelled, self.log_signal.emit)
                if ret is None:
                    break
                if ret != 0:
                    # Если файл всё же создался и не пустой — выдадим предупреждение
                    if output_file.exists() and output_file.stat().st_size > 0:
                        self.log_signal.emit(
                            f"Предупреждение: mkvmerge вернул {ret}, но файл {output_file.name} создан. stderr: {err}"
                        )
                    else:
                        self.log_signal.emit(f"Ошибка mkvmerge для {video.name} и {audio.name}: {err}")
                else:
                    # Проверяем, что файл реально создался
                    if output_file.exists() and output_file.stat().st_size > 0:
                        self.log_signal.emit(f"Успешно: {video.name} + {audio.name} -> {output_file.name}")
                    else:
                        self.log_signal.emit(f"Файл {output_file.name} пуст или не создан!")

            # Обновляем прогресс
            self.update_file_progress.emit(100)
            self.update_progress.emit(int((index + 1) / total_files * 100))
            self.update_file_progress.emit(0)

        # Завершено
        if self._is_cancelled:
            self.log_signal.emit("Конвертация прервана пользователем.")
        else:
            self.log_signal.emit("Все файлы успешно обработаны.")
    def find_subtitles(self, video_path):
        """Ищем субтитры для видеофайла"""
        subs = []
        for ext in SUBTITLE_EXTS:
            sub_file = video_path.with_suffix(ext)
            if sub_file.exists():
                subs.append(sub_file)
                
            # Также ищем файлы с похожими именами
            for f in video_path.parent.glob(f"{video_path.stem}*{ext}"):
                if f not in subs:
                    subs.append(f)
        return subs
    
    def build_mkvmerge_command(self, video, audio, output_file):
        """Строим команду mkvmerge с учетом субтитров"""
        cmd = [
            "mkvmerge", "--ui-language", "ru",
            "--output", str(output_file),
            "--language", "0:und", "(", str(video), ")",
            "--language", "0:rus", "--default-track", "0:yes", "(", str(audio), ")"
        ]
        
        # Добавляем субтитры
        for i, sub in enumerate(self.find_subtitles(video)):
            lang = self.detect_language(sub.stem)
            cmd.extend([
                "--language", f"0:{lang}",
                "--default-track", "0:no",
                "(", str(sub), ")"
            ])
        
        cmd.append("--track-order")
        cmd.append("0:0,0:1,1:0")
        return cmd
    
    def detect_language(self, filename):
        """Определяем язык по имени файла"""
        filename = filename.lower()
        if 'rus' in filename or 'ru' in filename:
            return 'rus'
        elif 'eng' in filename or 'en' in filename:
            return 'eng'
        return 'und'            

# Логгер в QPlainTextEdit
class QTextEditLogger(logging.Handler):
    def __init__(self, text_edit):
        super().__init__()
        self.text_edit = text_edit

    def emit(self, record):
        msg = self.format(record)
        QTimer.singleShot(0, lambda: self.text_edit.appendPlainText(msg))

# Основное окно программы
class VideoAudioMerger(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video/Audio Merger — сопоставление внутри одной папки")
        self.setGeometry(100, 100, 1400, 800)

        # Проверка наличия mkvmerge
        self.mkvmerge_available = check_mkvmerge()
        if not self.mkvmerge_available:
            QMessageBox.warning(
                self, "MKVToolNix не найден",
                "Утилита mkvmerge не найдена!\nУстановите MKVToolNix: https://mkvtoolnix.download/"
            )

        # Инициализация переменных
        self.matched_files = []
        self.converted_files = []
        self.all_mkv_files = []
        self.file_durations = {}
        self.directory = None
        self.output_directory = None
        self.scan_thread = None
        self.convert_thread = None
        self.metadata_cache = MetadataCache()

        # Создание UI компонентов
        self.create_ui_components()
        
        # Настройка компоновки
        self.setup_layout()
        
        # Настройка DnD и других функций
        self.setup_drag_drop()
        
        # Логгер
        self.setup_logger()

        logging.info("Программа запущена — сопоставление видео/аудио только в одной папке.")

    def create_ui_components(self):
        """Создание всех компонентов UI"""
        # Кнопки
        self.select_output_button = QPushButton("Выбрать выходную директорию")
        self.convert_button = QPushButton("Сконвертировать в .mkv")
        self.delete_button = QPushButton("Удалить исходные файлы")
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setEnabled(False)
        self.stats_button = QPushButton("Показать статистику")

        # Виджеты настроек
        self.duration_check_box = QCheckBox("Проверять длительность (медленнее)")
        self.duration_check_box.setChecked(False)
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(0, 9999)
        self.duration_spin.setValue(10)
        self.duration_spin.setSuffix(" сек")
        self.name_threshold_spin = QSpinBox()
        self.name_threshold_spin.setRange(0, 100)
        self.name_threshold_spin.setValue(85)
        self.name_threshold_spin.setSuffix(" %")

        # Таблица сопоставлений
        self.match_table = QTableWidget()
        self.match_table.setColumnCount(5)
        self.match_table.setHorizontalHeaderLabels([
            "Видео", "Аудио", "Длительность видео", "Длительность аудио", "Статус"
        ])

        # Список конвертированных файлов
        self.converted_list = QListWidget()
        self.converted_list.setSelectionMode(QListWidget.ExtendedSelection)

        # Элементы статистики
        self.video_count_label = QLabel("Исходных видео файлов: 0")
        self.audio_count_label = QLabel("Исходных аудио файлов: 0")
        self.converted_count_label = QLabel("Сконвертированных файлов: 0")
        self.matched_count_label = QLabel("Сопоставленных пар: 0")
        self.converted_match_count_label = QLabel("Совпадений конвертированных файлов: 0")

        # Прогресс-бары
        self.file_progress_label = QLabel("Прогресс текущего файла:")
        self.file_progress_bar = QProgressBar()
        self.overall_progress_label = QLabel("Общий прогресс:")
        self.progress_bar = QProgressBar()

        # Логи
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont('Consolas', 9))

        # Дерево файлов
        self.directory_model = QFileSystemModel()
        self.directory_model.setRootPath(QDir.rootPath())
        self.directory_model.setFilter(QDir.NoDotAndDotDot | QDir.AllDirs | QDir.AllEntries)
        self.sort_filter_model = QSortFilterProxyModel()
        self.sort_filter_model.setSourceModel(self.directory_model)
        
        self.tree_view = QTreeView()
        self.tree_view.setModel(self.sort_filter_model)

    def setup_layout(self):
        """Настройка компоновки интерфейса"""
        # Верхняя панель с настройками
        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("Имя ≥"))
        top_layout.addWidget(self.name_threshold_spin)
        top_layout.addWidget(self.duration_check_box)
        top_layout.addWidget(QLabel("Допуск времени:"))
        top_layout.addWidget(self.duration_spin)
        top_layout.addStretch(1)
        
        top_widget = QWidget()
        top_widget.setLayout(top_layout)

        # Основные контейнеры
        match_container = self.create_match_container()
        converted_container = self.create_converted_container()
        stats_container = self.create_stats_container()
        progress_container = self.create_progress_container()
        logs_container = self.create_logs_container()

        # Центральный разделитель
        center_splitter = QSplitter(Qt.Vertical)
        center_splitter.addWidget(top_widget)
        center_splitter.addWidget(match_container)
        center_splitter.addWidget(converted_container)
        center_splitter.addWidget(stats_container)
        center_splitter.addWidget(progress_container)
        center_splitter.addWidget(logs_container)

        # Боковая панель с кнопками
        button_container = self.create_button_container()

        # Главный разделитель
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(self.tree_view)
        main_splitter.addWidget(center_splitter)
        main_splitter.addWidget(button_container)
        main_splitter.setStretchFactor(1, 1)

        # Главный контейнер
        main_container = QWidget()
        main_layout = QHBoxLayout()
        main_layout.addWidget(main_splitter)
        main_container.setLayout(main_layout)
        self.setCentralWidget(main_container)

    def create_match_container(self):
        """Контейнер для таблицы сопоставлений"""
        container = QWidget()
        layout = QVBoxLayout()
        self.match_table.horizontalHeader().setStretchLastSection(True)
        self.match_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.match_table.customContextMenuRequested.connect(self.open_match_context_menu)
        layout.addWidget(QLabel("Сопоставленные файлы (только в одной папке)"))
        layout.addWidget(self.match_table)
        container.setLayout(layout)
        return container

    def create_converted_container(self):
        """Контейнер для списка конвертированных файлов"""
        container = QWidget()
        layout = QVBoxLayout()
        self.converted_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.converted_list.customContextMenuRequested.connect(self.open_converted_context_menu)
        self.converted_list.itemDoubleClicked.connect(self.open_converted_file)
        layout.addWidget(QLabel("Конвертированные файлы"))
        layout.addWidget(self.converted_list)
        container.setLayout(layout)
        return container

    def create_stats_container(self):
        """Контейнер для статистики"""
        container = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self.video_count_label)
        layout.addWidget(self.audio_count_label)
        layout.addWidget(self.converted_count_label)
        layout.addWidget(self.matched_count_label)
        layout.addWidget(self.converted_match_count_label)
        container.setLayout(layout)
        return container

    def create_progress_container(self):
        """Контейнер для прогресс-баров"""
        container = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self.file_progress_label)
        layout.addWidget(self.file_progress_bar)
        layout.addWidget(self.overall_progress_label)
        layout.addWidget(self.progress_bar)
        container.setLayout(layout)
        return container

    def create_logs_container(self):
        """Контейнер для логов"""
        container = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(QLabel("Логи:"))
        layout.addWidget(self.log_view)
        container.setLayout(layout)
        return container

    def create_button_container(self):
        """Контейнер для кнопок управления"""
        container = QWidget()
        layout = QVBoxLayout()
        
        # Добавляем все кнопки
        layout.addWidget(self.select_output_button)
        layout.addWidget(self.convert_button)
        layout.addWidget(self.delete_button)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.stats_button)
        layout.addStretch(1)
        
        # Устанавливаем соединения
        self.select_output_button.clicked.connect(self.select_output_directory)
        self.convert_button.clicked.connect(self.convert_files)
        self.delete_button.clicked.connect(self.delete_source_files_button)
        self.cancel_button.clicked.connect(self.cancel_current_operation)
        self.stats_button.clicked.connect(self.show_stats)
        
        container.setLayout(layout)
        return container










    def setup_drag_drop(self):
        """Настройка системы Drag and Drop"""
        self.setAcceptDrops(True)
        self.match_table.setAcceptDrops(True)
        self.match_table.setDragEnabled(True)
        self.match_table.setDropIndicatorShown(True)
        self.tree_view.setRootIsDecorated(True)
        self.tree_view.setHeaderHidden(False)
        self.tree_view.setColumnWidth(0, 250)
        self.tree_view.setSortingEnabled(True)
        self.tree_view.sortByColumn(3, Qt.DescendingOrder)
        self.tree_view.setDragEnabled(True)
        self.tree_view.setAcceptDrops(True)
        self.tree_view.setDropIndicatorShown(True)
        self.tree_view.setDragDropMode(QTreeView.InternalMove)
        self.tree_view.clicked.connect(self.select_directory_from_tree)
        self.tree_view.doubleClicked.connect(self.on_tree_double_click)

    def setup_logger(self):
        """Настройка системы логирования"""
        self.logger_handler = QTextEditLogger(self.log_view)
        self.logger_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.logger_handler)

    def show_stats(self):
        """Показать окно статистики"""
        # Подготавливаем данные для статистики
        file_data = []
        for video, audio, *_ in self.matched_files:
            file_data.append({
                'path': str(video),
                'duration': self.file_durations.get(video, -1),
                'codec': get_video_codec(video)
            })
            if audio:
                file_data.append({
                    'path': str(audio),
                    'duration': self.file_durations.get(audio, -1),
                    'codec': "audio"
                })
        
        stats_window = StatsWindow(file_data, self)
        stats_window.exec_()
        
        
    # Добавим методы для DnD
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        files = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        self.manual_match_files(files)

    def manual_match_files(self, files):
        """Ручное сопоставление файлов"""
        videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]
        audios = [f for f in files if f.suffix.lower() in AUDIO_EXTS]
        
        if not videos or not audios:
            QMessageBox.warning(self, "Ошибка", "Не найдено подходящих видео/аудио файлов")
            return
            
        # Простое сопоставление по порядку (можно улучшить)
        for video, audio in zip(videos, audios):
            self.add_manual_match(video, audio)

    def add_manual_match(self, video, audio):
        """Добавляем ручное сопоставление в таблицу"""
        row = self.match_table.rowCount()
        self.match_table.insertRow(row)

        # Получаем длительности из кэша или вычисляем
        v_dur = self.metadata_cache.get_duration(video) or get_media_duration_ffprobe(video)
        a_dur = self.metadata_cache.get_duration(audio) or get_media_duration_ffprobe(audio)

        # Создаем элементы таблицы
        item_video = QTableWidgetItem(video.name)
        item_audio = QTableWidgetItem(audio.name)
        item_video_duration = QTableWidgetItem(format_duration(v_dur))
        item_audio_duration = QTableWidgetItem(format_duration(a_dur))

        # Сохраняем полные пути в UserRole
        item_video.setData(Qt.UserRole, video)
        item_audio.setData(Qt.UserRole, audio)

        # Определяем статус
        if v_dur > 0 and a_dur > 0:
            if abs(v_dur - a_dur) > 1:
                status = "Проверить (разница длительностей)"
                item_audio_duration.setBackground(QBrush(QColor('yellow')))
            else:
                status = "OK (ручное сопоставление)"
        else:
            status = "Нет данных о длительности"

        item_status = QTableWidgetItem(status)
        item_status.setBackground(QBrush(QColor(200, 255, 200)))  # Светло-зеленый для ручных

        # Заполняем строку таблицы
        self.match_table.setItem(row, 0, item_video)
        self.match_table.setItem(row, 1, item_audio)
        self.match_table.setItem(row, 2, item_video_duration)
        self.match_table.setItem(row, 3, item_audio_duration)
        self.match_table.setItem(row, 4, item_status)

        # Добавляем в список сопоставленных файлов с флагом manual=True
        self.matched_files.append((video, audio, True))  # True - manual match

        # Обновляем счетчики
        self.update_counters()

        logging.info(f"Добавлено ручное сопоставление: {video.name} + {audio.name}")
        
        
    def update_counters(self):
        """Обновляем счетчики в интерфейсе"""
        manual_count = sum(1 for m in self.matched_files if len(m) > 2 and m[2])
        auto_count = len(self.matched_files) - manual_count

        self.matched_count_label.setText(
            f"Сопоставленных пар: {len(self.matched_files)} "
            f"(авто: {auto_count}, ручн.: {manual_count})"
        )
        
        
    # Открыть файл/папку по двойному клику в дереве
    def on_tree_double_click(self, index):
        source_index = self.sort_filter_model.mapToSource(index)
        path = self.directory_model.filePath(source_index)
        if not Path(path).exists():
            return
        if os.name == 'nt':
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def select_directory_from_tree(self, index):
        source_index = self.sort_filter_model.mapToSource(index)
        directory = self.directory_model.filePath(source_index)
        if directory:
            self.directory = directory
            self.start_scan(directory)

    def select_output_directory(self):
        self.output_directory = QFileDialog.getExistingDirectory(self, "Выбрать выходную директорию")

    def start_scan(self, directory):
        # Сброс таблицы и списка
        self.match_table.setRowCount(0)
        self.converted_list.clear()
        self.video_count_label.setText("Исходных видео файлов: 0")
        self.audio_count_label.setText("Исходных аудио файлов: 0")
        self.converted_count_label.setText("Сконвертированных файлов: 0")
        self.matched_count_label.setText("Сопоставленных пар: 0")
        self.converted_match_count_label.setText("Совпадений конвертированных файлов: 0")
        self.progress_bar.setValue(0)

        # Если предыдущий поток сканирования ещё идёт — отменим
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel()
            self.scan_thread.wait()

        check_dur = self.duration_check_box.isChecked()
        duration_tol = float(self.duration_spin.value())
        name_thr = int(self.name_threshold_spin.value())

        self.scan_thread = ScanThread(
            directory=directory,
            check_duration=check_dur,
            duration_tolerance=duration_tol,
            name_threshold=name_thr
        )
        self.scan_thread.scanProgress.connect(self.on_scan_progress)
        self.scan_thread.scanFinished.connect(self.on_scan_finished)
        self.scan_thread.start()
        self.cancel_button.setEnabled(True)

        mode_str = (f"режим: {'с' if check_dur else 'без'} длительности, "
                    f"Tol={duration_tol}, Name≥{name_thr}")
        logging.info(f"Начато сканирование: {directory}, {mode_str}")

    def on_scan_progress(self, value: int):
        self.progress_bar.setValue(value)

    def on_scan_finished(self, video_files, audio_files,
                         all_mkv_files, auto_matched_files,
                         converted_files, durations_cache):
        self.cancel_button.setEnabled(False)
        self.file_durations = durations_cache
        self.converted_files = converted_files
        self.all_mkv_files = all_mkv_files

        # Формируем словарь {video: audio} из auto_matched_files
        matched_dict = {v: a for (v, a) in auto_matched_files}

        self.match_table.setRowCount(0)
        final_matched = []
        for video in video_files:
            row = self.match_table.rowCount()
            self.match_table.insertRow(row)

            audio = matched_dict.get(video, None)

            video_name = video.name
            audio_name = audio.name if audio else "Отсутствует"

            v_dur = durations_cache.get(video, -1)
            a_dur = durations_cache.get(audio, -1) if audio else -1

            item_video = QTableWidgetItem(video_name)
            item_audio = QTableWidgetItem(audio_name)
            item_video_duration = QTableWidgetItem(format_duration(v_dur))
            item_audio_duration = QTableWidgetItem(format_duration(a_dur))

            item_video.setData(Qt.UserRole, video)
            if audio:
                item_audio.setData(Qt.UserRole, audio)

            # Статус
            if audio is None:
                status = "Аудио отсутствует"
            else:
                if v_dur > 0 and a_dur > 0:
                    # Если сильно расходятся — выделим цветом
                    if abs(v_dur - a_dur) > 1:
                        status = "Проверить"
                        item_audio_duration.setBackground(QBrush(QColor('yellow')))
                    else:
                        status = "OK"
                else:
                    status = "Нет данных"

            # Если уже сконвертирован
            converted_file = video.with_name(video.stem + "_rus.mkv")
            if converted_file.exists():
                status += " / Сконвертировано"

            item_status = QTableWidgetItem(status)

            self.match_table.setItem(row, 0, item_video)
            self.match_table.setItem(row, 1, item_audio)
            self.match_table.setItem(row, 2, item_video_duration)
            self.match_table.setItem(row, 3, item_audio_duration)
            self.match_table.setItem(row, 4, item_status)

            final_matched.append((video, audio))

        self.matched_files = final_matched
        logging.info(f"Найдено {len(self.matched_files)} сопоставленных пар для конвертации.")
        for (v, a) in self.matched_files:
            logging.info(f"  {v.name} -> {(a.name if a else 'None')}")

        # Заполняем список .mkv
        self.converted_list.clear()
        for mkv in all_mkv_files:
            lw_item = QListWidgetItem(mkv.name)
            lw_item.setData(Qt.UserRole, mkv)
            if mkv in converted_files:
                lw_item.setBackground(QBrush(QColor('red')))
            self.converted_list.addItem(lw_item)

        # Обновляем счётчики
        self.video_count_label.setText(f"Исходных видео файлов: {len(video_files)}")
        self.audio_count_label.setText(f"Исходных аудио файлов: {len(audio_files)}")
        self.converted_count_label.setText(f"Сконвертированных файлов: {len(all_mkv_files)}")
        self.matched_count_label.setText(f"Сопоставленных пар: {len(auto_matched_files)}")
        self.converted_match_count_label.setText(f"Совпадений конвертированных файлов: {len(converted_files)}")

        self.progress_bar.setValue(100)
        logging.info("Сканирование завершено.")

    # Контекстное меню таблицы
    def open_match_context_menu(self, pos):
        index = self.match_table.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        menu = QMenu()
        preview_action = QAction("Предпросмотр", self)
        preview_action.triggered.connect(lambda: self.preview_pair(row))
        menu.addAction(preview_action)

        info_action = QAction("Показать информацию", self)
        info_action.triggered.connect(lambda: self.match_show_file_info(self.match_table.item(row, index.column())))
        menu.addAction(info_action)

        copy_action = QAction("Копировать имя", self)
        copy_action.triggered.connect(lambda: self.match_copy_item_name(self.match_table.item(row, index.column())))
        menu.addAction(copy_action)

        rename_action = QAction("Переименовать", self)
        rename_action.triggered.connect(lambda: self.match_paste_item_name(self.match_table.item(row, index.column())))
        menu.addAction(rename_action)

        menu.exec_(QCursor.pos())

    def match_show_file_info(self, item):
        file_obj = item.data(Qt.UserRole)
        if not file_obj or not Path(file_obj).exists():
            QMessageBox.information(self, "Информация", "Файл не найден.")
            return
        dur = self.file_durations.get(file_obj, -1)
        duration_str = format_duration(dur) if dur > 0 else "N/A"
        QMessageBox.information(self, "Информация",
                                f"Путь: {file_obj}\nДлительность: {duration_str}")

    def match_copy_item_name(self, item):
        file_obj = item.data(Qt.UserRole)
        if file_obj:
            QApplication.clipboard().setText(Path(file_obj).stem)

    def match_paste_item_name(self, item):
        file_obj = item.data(Qt.UserRole)
        if not file_obj or not Path(file_obj).exists():
            return
        new_name = QApplication.clipboard().text()
        if not new_name:
            return
        file_path = Path(file_obj)
        new_path = file_path.with_name(f"{new_name}{file_path.suffix}")
        try:
            file_path.rename(new_path)
            item.setText(new_path.name)
            item.setData(Qt.UserRole, new_path)
            logging.info(f"Переименован: {file_path} -> {new_path}")
        except Exception as e:
            logging.error(f"Ошибка переименования: {e}")
            QMessageBox.critical(self, "Ошибка", f"Не удалось переименовать: {e}")

    def preview_pair(self, row):
        video_item = self.match_table.item(row, 0)
        if not video_item:
            return
        video = video_item.data(Qt.UserRole)
        if video and Path(video).exists():
            if os.name == 'nt':
                os.startfile(str(video))
            else:
                subprocess.Popen(["xdg-open", str(video)])

    # Контекстное меню списка конвертированных
    def open_converted_context_menu(self, pos):
        selected_items = self.converted_list.selectedItems()
        if not selected_items:
            item = self.converted_list.itemAt(pos)
            if item:
                selected_items = [item]
        menu = QMenu()
        if selected_items:
            if len(selected_items) == 1:
                open_action = QAction("Открыть файл", self)
                open_action.triggered.connect(lambda: self.open_converted_file(selected_items[0]))
                menu.addAction(open_action)
            delete_action = QAction("Удалить выбранные файлы", self)
            delete_action.triggered.connect(lambda: self.delete_converted_files(selected_items))
            menu.addAction(delete_action)
        menu.exec_(self.converted_list.mapToGlobal(pos))

    def delete_converted_files(self, items):
        file_names = "\n".join([Path(item.data(Qt.UserRole)).name for item in items if item.data(Qt.UserRole)])
        reply = QMessageBox.question(
            self, "Подтверждение удаления", f"Удалить следующие файлы?\n{file_names}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            for item in items:
                file_path = item.data(Qt.UserRole)
                if file_path and Path(file_path).exists():
                    try:
                        send2trash(file_path)
                        row = self.converted_list.row(item)
                        self.converted_list.takeItem(row)
                        logging.info(f"Файл {file_path} удалён.")
                    except Exception as e:
                        QMessageBox.critical(self, "Ошибка", f"Ошибка при удалении файла {Path(file_path).name}: {e}")
                else:
                    QMessageBox.warning(self, "Ошибка", "Файл не найден или недоступен.")

    def open_converted_file(self, item):
        file_path = item.data(Qt.UserRole)
        if file_path and Path(file_path).exists():
            if os.name == 'nt':
                os.startfile(str(file_path))
            else:
                subprocess.Popen(["xdg-open", str(file_path)])
        else:
            QMessageBox.warning(self, "Ошибка", "Файл не найден или недоступен.")

    def delete_source_files_button(self):
        # Удаляем исходные файлы только у тех, у кого есть сконвертированный вариант
        if not self.matched_files:
            QMessageBox.warning(self, 'Удаление', 'Нет сопоставленных файлов.')
            return

        to_delete = []
        for (video, audio) in self.matched_files:
            converted_file = video.with_name(video.stem + "_rus.mkv")
            if converted_file.exists():
                to_delete.append((video, audio))

        if not to_delete:
            QMessageBox.information(self, 'Удаление', 'Нет видео, для которых есть сконвертированный файл.')
            return

        txt = "\n".join([
            f"Видео: {v.name}, Аудио: {a.name if a else 'None'}"
            for (v, a) in to_delete
        ])
        dlg = DeleteConfirmDialog(txt, self)
        result = dlg.exec_()
        if result == QDialog.Accepted:
            self.delete_source_files(to_delete)

    def delete_source_files(self, files):
        for video, audio in files:
            try:
                if video and Path(video).exists():
                    send2trash(video)
                if audio and Path(audio).exists():
                    send2trash(audio)
                logging.info(f"Удалены: {video}, {audio}")
            except Exception as e:
                logging.error(f"Ошибка удаления: {e}")
        if self.directory:
            self.start_scan(self.directory)

    def convert_files(self):
        logging.info(f"Запуск конвертации: {len(self.matched_files)} пар файлов.")
        if not self.matched_files:
            QMessageBox.warning(self, 'Конвертация', 'Нет файлов для конвертации.')
            return
        if not self.mkvmerge_available:
            QMessageBox.critical(
                self, "mkvmerge недоступен",
                "Невозможно запустить конвертацию, mkvmerge не найден.\nУстановите MKVToolNix."
            )
            return
        # Создаём поток конвертации
        self.convert_thread = ConvertThread(self.matched_files, self.output_directory)
        self.convert_thread.update_progress.connect(self.update_progress)
        self.convert_thread.update_file_progress.connect(self.update_file_progress)
        self.convert_thread.log_signal.connect(self.log_message)
        self.convert_thread.finished.connect(self.on_conversion_finished)
        self.convert_thread.start()
        self.cancel_button.setEnabled(True)

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def update_file_progress(self, value):
        self.file_progress_bar.setValue(value)

    def log_message(self, msg):
        logging.info(msg)

    def on_conversion_finished(self):
        self.cancel_button.setEnabled(False)
        # Перезапустим сканирование, чтобы обновить список уже сконвертированных
        if self.directory:
            self.start_scan(self.directory)
        QMessageBox.information(self, "Готово", "Конвертация завершена!")

    def cancel_current_operation(self):
        # Отмена активного потока (сканирования или конвертации)
        if self.convert_thread and self.convert_thread.isRunning():
            self.convert_thread.cancel()
            logging.info("Конвертация отменена пользователем.")
        elif self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel()
            logging.info("Сканирование отменено пользователем.")
        self.cancel_button.setEnabled(False)

def main():
    app = QApplication(sys.argv)
    window = VideoAudioMerger()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
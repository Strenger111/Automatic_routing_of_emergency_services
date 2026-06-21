import os
import os.path

# ----- НАСТРОЙКИ -----
TARGET_DIR = "C:\pygis\devel\olgis\city_monitor"  # Относительный путь к папке, которую нужно обработать (можно указать абсолютный)
OUTPUT_FILE = "project_dump.txt"  # Имя итогового файла
ENCODING = "utf-8"            # Кодировка (для русских букв в коде и тексте)

# Папки, которые нужно исключить (имена или начала путей)
IGNORE_DIRS = {".git", "export_project.py","__pycache__", "venv", ".venv", ".idea", ".pytest_cache", "node_modules", "dist", "build"}
# Расширения текстовых файлов (если нужно только определённые; None — все файлы)
TEXT_EXTENSIONS = {".txt", ".py", ".js", ".html", ".css", ".json", ".xml", ".yaml", ".yml", ".md", ".rst", ".ini", ".cfg", ".conf"}
# Максимальный размер файла для чтения (байт) — слишком большие пропускаем
MAX_FILE_SIZE = 10 * 1024 * 1024  # 5 МБ
# ----- КОНЕЦ НАСТРОЕК -----

def should_include_file(filepath, filename):
    """Проверяет, нужно ли включать файл."""
    # Исключаем бинарные/ненужные файлы по расширению
    if TEXT_EXTENSIONS is not None:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in TEXT_EXTENSIONS:
            return False
    # Проверяем размер
    try:
        size = os.path.getsize(filepath)
        if size > MAX_FILE_SIZE:
            return False
    except OSError:
        return False
    return True

def should_ignore_dir(dirpath, dirname):
    """Исключает системные и временные папки."""
    # Можно игнорировать по полному имени папки или по любому компоненту пути
    if dirname in IGNORE_DIRS:
        return True
    # Игнорируем также .git в любом месте пути (если нужно)
    if ".git" in dirpath.split(os.sep):
        return True
    return False

def walk_and_write(output_handle, start_dir):
    """Рекурсивно обходит директорию и записывает пути и содержимое файлов."""
    start_dir = os.path.abspath(start_dir)
    output_handle.write(f"Корневая папка: {start_dir}\n")
    output_handle.write("=" * 80 + "\n\n")

    for root, dirs, files in os.walk(start_dir):
        # Фильтруем папки на лету (удаляем из списка обхода)
        dirs[:] = [d for d in dirs if not should_ignore_dir(root, d)]

        relative_root = os.path.relpath(root, start_dir)
        if relative_root == ".":
            relative_root = ""

        # Выводим путь к текущей папке
        output_handle.write(f"\n📁 {relative_root if relative_root else '[корень]'}/\n")
        output_handle.write("-" * 60 + "\n")

        for filename in sorted(files):
            filepath = os.path.join(root, filename)
            if not should_include_file(filepath, filename):
                continue

            relative_path = os.path.join(relative_root, filename) if relative_root else filename
            output_handle.write(f"\n📄 {relative_path}\n")
            output_handle.write("~" * 40 + "\n")

            try:
                with open(filepath, "r", encoding=ENCODING, errors="replace") as f:
                    content = f.read()
                output_handle.write(content)
                if not content.endswith("\n"):
                    output_handle.write("\n")
            except Exception as e:
                output_handle.write(f"[Ошибка чтения файла: {e}]\n")
            output_handle.write("\n" + "=" * 40 + "\n")

def main():
    # Убедимся, что целевая директория существует
    abs_target = os.path.abspath(TARGET_DIR)
    if not os.path.isdir(abs_target):
        print(f"Ошибка: директория '{abs_target}' не найдена.")
        return

    # Записываем результат
    with open(OUTPUT_FILE, "w", encoding=ENCODING) as out:
        walk_and_write(out, abs_target)

    print(f"Готово! Результат сохранён в '{os.path.abspath(OUTPUT_FILE)}'")
    print(f"Размер файла: {os.path.getsize(OUTPUT_FILE) / 1024:.2f} КБ")

if __name__ == "__main__":
    main()
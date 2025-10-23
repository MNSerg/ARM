from cx_Freeze import setup, Executable

setup(
    name = "MultiTap",
    version = "0.1",
    description = "Приложение для контроля макро-кнопки",
    executables = [Executable("__main__.py", base="Win32GUI")]
)
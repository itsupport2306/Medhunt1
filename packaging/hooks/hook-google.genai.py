"""Bundle Google Gen AI runtime modules without its installed test suite."""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules


hiddenimports = collect_submodules(
    "google.genai",
    filter=lambda name: name != "google.genai.tests" and ".tests." not in name,
)
datas = collect_data_files(
    "google.genai", excludes=["tests/**", "**/tests/**"],
)

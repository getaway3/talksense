"""Создаёт demo/audio/*.ogg из сценариев demo/scripts/*.txt (для Postman и ручной загрузки).

Это не настоящий звук: файл содержит маркер TSDEMO и текст сценария — мок SpeechKit «распознаёт» его,
возвращая этот текст. С реальным SpeechKit нужно отправлять настоящие записи с устройства.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
(HERE / "audio").mkdir(exist_ok=True)
for src in sorted((HERE / "scripts").glob("*.txt")):
    dst = HERE / "audio" / (src.stem + ".ogg")
    dst.write_bytes(b"TSDEMO\n" + src.read_bytes())
    print("создан", dst.relative_to(HERE.parent))

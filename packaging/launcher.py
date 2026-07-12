"""PyInstaller entry point for the WhisperFlow app bundle."""

import multiprocessing

multiprocessing.freeze_support()

from flow.__main__ import main

main()

"""Compatibility entry point; the maintained implementation is telegram_bot.py."""

import telegram_bot as _implementation


def __getattr__(name):
    return getattr(_implementation, name)


if __name__ == "__main__":
    _implementation.main()

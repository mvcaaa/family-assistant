import argparse
import logging
import sys
from pathlib import Path

from family_assistant.config import Config, load_env_file
from family_assistant.db import Store
from family_assistant.loop import run
from family_assistant.prompts import RULES_SEED
from family_assistant.telegram import Telegram


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Family Telegram assistant gateway")
    parser.add_argument("--env", type=Path, help="KEY=VALUE file. Process environment wins.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.env:
        load_env_file(args.env)
    cfg = Config.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.rules_path.exists():
        cfg.rules_path.write_text(RULES_SEED, encoding="utf-8")
    store = Store(cfg.database)
    try:
        store.migrate()
        telegram = Telegram(cfg.bot_token)
        run(cfg, store, telegram)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

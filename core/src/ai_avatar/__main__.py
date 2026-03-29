"""python -m ai_avatar で起動するエントリーポイント（開発・テスト用）"""
import argparse
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Avatar Core")
    parser.add_argument(
        "--config-dir", "-c",
        default="./config",
        help="設定ファイルディレクトリ（デフォルト: ./config）",
    )
    parser.add_argument(
        "--data-dir", "-d",
        default="./data",
        help="データディレクトリ（デフォルト: ./data）",
    )
    args = parser.parse_args()

    from pathlib import Path
    from ai_avatar.engine import Engine

    # 相対パスは実行ディレクトリ基準で絶対パスに変換
    config_dir = Path(args.config_dir).resolve()
    data_dir = Path(args.data_dir).resolve()

    engine = Engine(config_dir=config_dir, data_dir=data_dir)
    asyncio.run(engine.run())


if __name__ == "__main__":
    main()

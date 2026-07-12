import argparse
import sys
from pathlib import Path

from modules.study_card_validator import validate_study_cards_file


def _print_section(title: str, items: list) -> None:
    print(f"\n{title} ({len(items)})")
    if not items:
        print("  - 없음")
        return
    for item in items:
        print(f"  - {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ClassFlowAI study_cards.json 구조와 품질을 검증합니다."
    )
    parser.add_argument("json_path", help="검증할 study_cards.json 경로")
    parser.add_argument(
        "--images-dir",
        help="source_images 파일 존재 여부를 확인할 images 폴더",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report = validate_study_cards_file(
        Path(args.json_path),
        images_dir=Path(args.images_dir) if args.images_dir else None,
    )

    print("ClassFlowAI 학습카드 검증 결과")
    print("=" * 34)
    print(f"파일: {Path(args.json_path)}")
    if args.images_dir:
        print(f"이미지 폴더: {Path(args.images_dir)}")
    print(f"판정: {'통과' if report['valid'] else '구조 오류'}")

    stats = report["stats"]
    print("\n통계")
    print(f"  - 전체 카드: {stats['total_cards']}")
    print(f"  - 근거 확인 카드: {stats['confirmed_cards']}")
    print(f"  - 검토 필요 카드: {stats['review_required_cards']}")
    print(f"  - 이미지 누락 카드: {stats['cards_with_missing_images']}")

    _print_section("오류", report["errors"])
    _print_section("경고", report["warnings"])
    _print_section("중복 카드 ID", report["duplicates"])
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())

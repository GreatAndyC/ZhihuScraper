#!/usr/bin/env python3
import argparse
import logging
import sys
import os

from scraper import QuestionScraper, UserScraper, FeedScraper
from renderers import render_question_html, render_user_html
from input_normalizer import normalize_question_input, normalize_user_input
from storage import (
    merge_question_batches,
    prepare_question_batch_dir,
    save_question,
    save_question_batch,
    save_user,
)
from config import OUTPUT_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def cmd_question(args):
    question_id = normalize_question_input(args.question_id)
    scraper = QuestionScraper(conservative_mode=args.conservative)
    batch_dir = prepare_question_batch_dir(question_id)
    print(f"分批保存目录: {batch_dir}")
    print(f"抓取节奏: {'保守模式（更慢、更稳）' if args.conservative else '标准模式'}")
    if question_id != args.question_id:
        print(f"已识别问题 ID: {question_id}")
    question = scraper.fetch_all(
        question_id,
        batch_callback=save_question_batch,
        content_mode=args.mode,
    )
    if not question:
        print("无法获取问题内容，可能需要完成知乎安全验证", file=sys.stderr)
        sys.exit(1)
    path = merge_question_batches(question_id) or save_question(question, question_id)
    html_path = render_question_html(question, conservative_mode=args.conservative, progress_callback=print)
    print(f"保存至: {path}")
    print(f"浏览页: {html_path}")
    print(f"问题: {question.title}")
    print(f"回答数: {len(question.answers)} / {question.answer_count}")


def cmd_merge_question(args):
    question_id = normalize_question_input(args.question_id)
    path = merge_question_batches(question_id)
    if not path:
        print(f"没有找到问题 {question_id} 的批次文件", file=sys.stderr)
        sys.exit(1)
    print(f"已合并至: {path}")


def cmd_user(args):
    user_id = normalize_user_input(args.user_id)
    scraper = UserScraper(conservative_mode=args.conservative)
    print(f"抓取节奏: {'保守模式（更慢、更稳）' if args.conservative else '标准模式'}")
    if user_id != args.user_id:
        print(f"已识别用户 ID: {user_id}")
    user = scraper.fetch_all(
        user_id,
        content_mode=args.mode,
        content_types=args.types,
    )
    if not user:
        print(f"无法获取用户 {user_id}", file=sys.stderr)
        sys.exit(1)
    path = save_user(user, user_id)
    html_path = render_user_html(user, conservative_mode=args.conservative, progress_callback=print)
    print(f"保存至: {path}")
    print(f"浏览页: {html_path}")
    print(f"用户: {user.name}")
    print(f"动态数: {len(user.activities)}")


def cmd_hot_list(args):
    scraper = FeedScraper()
    items = scraper.fetch_hot_list(limit=args.limit)
    print(f"热榜条目数: {len(items)}")
    for item in items:
        parsed = scraper.parse_feed_item(item)
        if parsed["title"]:
            print(f"  [{parsed['id']}] {parsed['title'][:50]} | 回答:{parsed['answer_count']} 关注:{parsed['follower_count']}")

    # 保存
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    import json
    path = os.path.join(OUTPUT_DIR, "hot-list.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n已保存至 {path}")


def cmd_recommend(args):
    scraper = FeedScraper()
    items = scraper.fetch_recommend(page=args.page, per_page=args.per_page)
    print(f"推荐条目数: {len(items)}")
    for item in items[:10]:
        parsed = scraper.parse_feed_item(item)
        if parsed["title"]:
            print(f"  [{parsed['id']}] {parsed['title'][:50]}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    import json
    path = os.path.join(OUTPUT_DIR, "recommend.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n已保存至 {path}")


def main():
    parser = argparse.ArgumentParser(description="知乎爬虫")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_question = subparsers.add_parser("question", help="爬取问题及全部回答")
    p_question.add_argument("question_id", help="问题 ID 或完整知乎问题链接")
    p_question.add_argument("--mode", choices=["full", "text"], default="full", help="保存模式")
    p_question.add_argument("--conservative", action="store_true", help="保守模式，更慢、更稳")

    p_merge_question = subparsers.add_parser("merge-question", help="合并已保存的问题回答批次文件")
    p_merge_question.add_argument("question_id", help="问题 ID 或完整知乎问题链接")

    p_user = subparsers.add_parser("user", help="爬取用户主页及动态")
    p_user.add_argument("user_id", help="用户 ID、url_token 或完整知乎用户链接")
    p_user.add_argument("--mode", choices=["full", "text"], default="full", help="保存模式")
    p_user.add_argument("--conservative", action="store_true", help="保守模式，更慢、更稳")
    p_user.add_argument(
        "--types",
        nargs="+",
        choices=["answer", "article", "pin"],
        default=["answer", "article", "pin"],
        help="抓取的用户内容类型",
    )

    p_hot = subparsers.add_parser("hot-list", help="爬取热榜")
    p_hot.add_argument("--limit", type=int, default=20, help="条数限制(默认20)")

    p_rec = subparsers.add_parser("recommend", help="爬取推荐流")
    p_rec.add_argument("--page", type=int, default=0, help="页码(默认0)")
    p_rec.add_argument("--per-page", type=int, default=10, help="每页条数(默认10)")

    args = parser.parse_args()

    if args.command == "question":
        cmd_question(args)
    elif args.command == "merge-question":
        cmd_merge_question(args)
    elif args.command == "user":
        cmd_user(args)
    elif args.command == "hot-list":
        cmd_hot_list(args)
    elif args.command == "recommend":
        cmd_recommend(args)


if __name__ == "__main__":
    main()

import argparse
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="HTML 转 EPUB：还原混淆字体的真实汉字，支持复制和搜索。")
    parser.add_argument("input", nargs="?", help="输入 HTML 路径")
    parser.add_argument("-o", "--output", help="输出 EPUB 路径，默认与输入同名")
    parser.add_argument("--mode", choices=["text", "visual"], default="text",
                        help="text：真实文字（默认，未验证字形即停止）；visual：保留原字体，仅保证显示")
    parser.add_argument("--reference-font", action="append", default=[], help="额外的正确参考字体，可多次使用")
    parser.add_argument("--restored-html", help="同时输出已还原汉字的独立 HTML，仅 text 模式")
    parser.add_argument("--title")
    parser.add_argument("--author")
    parser.add_argument("--language")
    parser.add_argument("--allow-remote", action="store_true", help="允许下载 HTML 引用的远程字体/图片")
    parser.add_argument("--force", action="store_true", help="允许覆盖指定输出文件")
    parser.add_argument("--gui", action="store_true", help="打开图形界面")
    args = parser.parse_args(argv)
    if args.gui or not args.input:
        from .gui import launch
        launch(args.input)
        return 0
    from .converter import convert
    try:
        report = convert(args.input, args.output, mode=args.mode, reference_fonts=args.reference_font,
                         title=args.title, author=args.author, language=args.language,
                         restored_html=args.restored_html, allow_remote=args.allow_remote, force=args.force)
        print(f"章节：{report['chapter_count']}；图片：{report['image_count']}；还原字符：{report['replaced_characters']}")
        if args.mode == "visual":
            print("此文件保留了错字编码和原字体，复制/搜索仍可能得到错字。")
        return 0
    except Exception as exc:
        print(f"转换失败：{exc}", file=sys.stderr)
        return 1

#!/usr/bin/env python3
"""
OCR 工具：从图片中提取文字
用法: python ocr_image.py <图片路径> [--lang chi_sim+eng] [--preprocess]
"""

import argparse
import sys
from PIL import Image, ImageFilter, ImageEnhance
import pytesseract


def preprocess(img: Image.Image) -> Image.Image:
    """对图片做预处理：灰度化 → 对比度增强 → 锐化，提升 OCR 准确率"""
    gray = img.convert("L")
    enhancer = ImageEnhance.Contrast(gray)
    gray = enhancer.enhance(1.8)
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1, percent=120))
    return gray


def ocr(image_path: str, lang: str = "chi_sim+eng", use_preprocess: bool = True) -> str:
    """返回识别出的文本"""
    img = Image.open(image_path)
    
    if use_preprocess and img.mode != "1":
        img = preprocess(img)
    
    text = pytesseract.image_to_string(img, lang=lang)
    return text


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OCR 图片文字提取")
    parser.add_argument("image", help="图片路径")
    parser.add_argument("--lang", default="chi_sim+eng", help="Tesseract 语言代码 (默认: chi_sim+eng)")
    parser.add_argument("--no-preprocess", action="store_true", help="跳过图片预处理")
    args = parser.parse_args()

    try:
        result = ocr(args.image, args.lang, not args.no_preprocess)
        print(result)
    except Exception as e:
        print(f"[OCR 错误] {e}", file=sys.stderr)
        sys.exit(1)

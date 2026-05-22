# AstrBot Workspace Tools

AstrBot 工作区实用工具脚本备份。

## 工具列表

### ocr_image.py
- **用途**：从图片中提取文字（基于 tesseract，支持中英文）
- **用法**：`python ocr_image.py <图片路径> [--lang chi_sim+eng] [--no-preprocess]`
- **依赖**：pytesseract + tesseract-ocr (chi_sim, chi_tra, eng)

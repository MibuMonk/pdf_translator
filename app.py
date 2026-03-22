#!/usr/bin/env python3
"""
PDF 翻译工具 - 网页界面（Gradio）
使用已登录的 Claude Code 账号，无需额外 API Key
运行: python app.py  →  浏览器打开 http://localhost:7860
"""

import gradio as gr
import os
import tempfile
from pathlib import Path
from pdf_translator import translate_pdf, SUPPORTED_LANGUAGES

LANG_CHOICES = [
    ("English", "en"),
    ("中文（简体）", "zh"),
    ("中文（繁體）", "zh-TW"),
    ("日本語", "ja"),
]

SOURCE_CHOICES = [("自动检测", "auto")] + LANG_CHOICES


def do_translate(input_file, source_lang, target_lang, progress=gr.Progress()):
    if input_file is None:
        return None, "❌ 请先上传 PDF 文件"
    if not target_lang:
        return None, "❌ 请选择目标语言"

    input_path = input_file.name
    output_dir = tempfile.mkdtemp()
    output_name = Path(input_path).stem + f"_translated_{target_lang}.pdf"
    output_path = os.path.join(output_dir, output_name)

    try:
        progress(0.1, desc="正在翻译，请稍候...")
        translate_pdf(
            input_path=input_path,
            output_path=output_path,
            source_lang=source_lang,
            target_lang=target_lang,
            verbose=False,
        )
        progress(1.0, desc="完成！")
        return output_path, "✅ 翻译完成，请点击下方下载结果文件。"
    except Exception as e:
        return None, f"❌ 错误: {e}"


with gr.Blocks(title="PDF 翻译工具", theme=gr.themes.Soft()) as demo:
    gr.Markdown("""
# 📄 PDF 翻译工具
**保留排版**翻译 PDF，特别适合 PPTX 导出的幻灯片。
使用你已登录的 **Claude Code 账号**，无需额外 API Key。
""")

    with gr.Row():
        with gr.Column(scale=1):
            input_file = gr.File(label="上传 PDF", file_types=[".pdf"])

            with gr.Row():
                source_lang = gr.Dropdown(
                    choices=SOURCE_CHOICES,
                    value="auto",
                    label="源语言",
                )
                target_lang = gr.Dropdown(
                    choices=LANG_CHOICES,
                    value="zh",
                    label="目标语言",
                )

            translate_btn = gr.Button("🚀 开始翻译", variant="primary", size="lg")

        with gr.Column(scale=1):
            output_file = gr.File(label="下载翻译结果")
            status = gr.Textbox(label="状态", lines=3, interactive=False)

    translate_btn.click(
        fn=do_translate,
        inputs=[input_file, source_lang, target_lang],
        outputs=[output_file, status],
    )

    gr.Markdown("""
---
**注意：** 扫描版 PDF（纯图片）无法翻译。背景图片、颜色、图形均自动保留。
""")


if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860, share=False)

# GeoPilot V2.0 报告构建

输入为 `docs/source/GeoPilot_产品需求规格与系统详细设计报告_V2.0.md`，图片使用相对于该文件的路径。图题读取 Markdown 替代文本，按一级章节分别编号。构建只处理报告，不运行 GeoPilot 产品或第三方 GIS Agent。

正文按目标与需求、系统设计、任务样例、测试与实施、开源依据排列。目录收录正文和附录的一、二级标题，文档说明使用独立样式；章标题加粗，节标题缩进，页码右对齐，三级标题保留在正文。正文采用 10.5 磅字、1.2 倍行距；图片按页面可用宽度等比放置，导读、图片、图题与紧随的说明保持衔接；表格和列表前的短导语与首项连排。需求、用例、接口和来源表分别设置列宽；短表尽量整表保留，长表重复表头、禁止拆分单行，并将末两行连排，减少孤行。数据对象表用 `<br>` 在字段组之间换行，避免英文标识被拆开。

需要 Python 的 `python-docx`、Pillow、PyMuPDF，以及 LibreOffice 和可导入 UNO 的系统 Python。先在临时目录构建并检查，再覆盖正式交付文件。

```bash
REPORT_BUILD_DIR=$(mktemp -d /tmp/geopilot-report-XXXXXX)
REPORT_STEM=GeoPilot_产品需求规格与系统详细设计报告_V2.0
python docs/tools/build_geopilot_report.py --output "$REPORT_BUILD_DIR/$REPORT_STEM.docx"
```

在独立终端启动 LibreOffice UNO 监听器，保持其运行到字段刷新结束：

```bash
libreoffice --headless --nologo --nodefault --nofirststartwizard --norestore \
  -env:UserInstallation=file:///tmp/geopilot-report-uno \
  '--accept=socket,host=127.0.0.1,port=2004;urp;StarOffice.ServiceManager'
```

回到构建终端，刷新字段并导出 PDF：

```bash
/usr/bin/python3 docs/tools/update_docx_fields.py "$REPORT_BUILD_DIR/$REPORT_STEM.docx" 2004
libreoffice -env:UserInstallation=file:///tmp/geopilot-report-pdf --headless \
  --convert-to pdf --outdir "$REPORT_BUILD_DIR" "$REPORT_BUILD_DIR/$REPORT_STEM.docx"
python docs/tools/restore_pdf_source_links.py \
  "$REPORT_BUILD_DIR/$REPORT_STEM.docx" "$REPORT_BUILD_DIR/$REPORT_STEM.pdf"
```

字段刷新会重新打开保存后的 DOCX，以处理首次导入造成的目录排版变化。部分 LibreOffice 版本会保留 Word 外部超链接，却在 PDF 中遗漏链接注释；最后一步按 DOCX 中的链接顺序恢复 PDF 来源链接，不改变正文、插图或分页。恢复时同时读取普通超链接和 HYPERLINK 字段，保留网页片段定位。标签无法定位时会报错，原 PDF 不会被覆盖；重复运行不会重复添加同一链接。

检查 DOCX 压缩包、27 张内嵌图片、目录页码与 PDF 实际页面、外部来源链接、表头重复、图题与图片同页，以及封面和末页。确认后再将临时目录中的同名 DOCX/PDF 复制到 `docs/`。本轮文档验收不等于 16 个产品固定用例已执行。

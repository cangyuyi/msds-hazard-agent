# MSDS Agent Demo（早期版本）

这是项目早期的 Streamlit 上传式演示版本，用于验证核心流程（CAS 提取 → 知识表匹配 → 页码证据 → 处理动作）。

## 运行

```bash
pip install -r requirements.txt
streamlit run app.py
```

## 说明

- 本 Demo 处理带可读文本层的 MSDS PDF；扫描件和乱码文件会提示转入本地 OCR / 人工确认
- 不保存上传文件，不会输出"无危害"
- 完整的本地 OCR 批处理版本（V1→V2）见仓库根目录的 `app/run_local_ocr_v2.py`

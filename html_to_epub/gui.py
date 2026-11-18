import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path


def launch(initial=None):
    from .converter import convert
    root = tk.Tk()
    root.title("HTML 转 EPUB · 真实汉字还原")
    root.geometry("800x520")
    root.minsize(640, 420)
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    source = tk.StringVar(value=initial or "")
    output = tk.StringVar(value=str(Path(initial).with_suffix(".epub")) if initial else "")
    mode = tk.StringVar(value="真实文字：支持正确复制与搜索")
    export_html = tk.BooleanVar(value=True)
    messages = queue.Queue()

    def choose_source():
        value = filedialog.askopenfilename(title="选择 HTML", filetypes=[("HTML", "*.html *.htm"), ("所有文件", "*.*")])
        if value:
            source.set(value)
            output.set(str(Path(value).with_suffix(".epub")))

    def choose_output():
        value = filedialog.asksaveasfilename(title="保存 EPUB", defaultextension=".epub", filetypes=[("EPUB", "*.epub")])
        if value:
            output.set(value)

    ttk.Label(frame, text="输入 HTML").grid(row=0, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=source).grid(row=0, column=1, sticky="ew", padx=8)
    ttk.Button(frame, text="选择文件", command=choose_source).grid(row=0, column=2)
    ttk.Label(frame, text="输出 EPUB").grid(row=1, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=output).grid(row=1, column=1, sticky="ew", padx=8)
    ttk.Button(frame, text="保存位置", command=choose_output).grid(row=1, column=2)
    ttk.Label(frame, text="转换模式").grid(row=2, column=0, sticky="w", pady=6)
    ttk.Combobox(frame, textvariable=mode, state="readonly", values=[
        "真实文字：支持正确复制与搜索", "保留字体：复制与搜索可能仍是错字"
    ]).grid(row=2, column=1, sticky="ew", padx=8)
    ttk.Checkbutton(frame, text="同时保存还原文字后的 HTML（真实文字模式）", variable=export_html).grid(row=3, column=1, sticky="w", pady=8)
    ttk.Label(frame, text="发现无法验证的字形时会停止转换，并生成报告，不会猜字。", wraplength=680).grid(row=4, column=0, columnspan=3, sticky="w", pady=5)
    logbox = tk.Text(frame, height=14, wrap="word", state="disabled")
    logbox.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=10)
    frame.rowconfigure(6, weight=1)

    def start():
        src, dst = source.get().strip().strip('"'), output.get().strip().strip('"')
        if not src or not dst:
            messagebox.showerror("缺少路径", "请选择输入 HTML 和输出 EPUB。")
            return
        text_mode = mode.get().startswith("真实")
        restored = Path(dst).with_suffix(".restored.html") if text_mode and export_html.get() else None
        existing = [str(p) for p in [Path(dst), restored] if p and p.exists()]
        if existing and not messagebox.askyesno("覆盖输出", "以下输出已存在，是否覆盖？\n" + "\n".join(existing)):
            return
        button.config(state="disabled")
        def work():
            try:
                result = convert(src, dst, mode="text" if text_mode else "visual", restored_html=restored,
                                 force=bool(existing), log=lambda s: messages.put(("log", s)))
                messages.put(("done", result))
            except Exception as exc:
                messages.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    button = ttk.Button(frame, text="开始转换", command=start)
    button.grid(row=5, column=1, sticky="ew", padx=8, pady=6)
    def poll():
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "done":
                    button.config(state="normal")
                    messagebox.showinfo("转换完成", f"已生成 EPUB。\n章节：{value['chapter_count']}\n还原字符：{value['replaced_characters']}\n{value['output']}")
                elif kind == "error":
                    button.config(state="normal")
                    messagebox.showerror("转换停止", value)
                else:
                    logbox.config(state="normal")
                    logbox.insert("end", value + "\n")
                    logbox.see("end")
                    logbox.config(state="disabled")
        except queue.Empty:
            pass
        root.after(120, poll)
    poll()
    root.mainloop()

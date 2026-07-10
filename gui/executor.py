import queue
import threading
import tkinter as tk
from tkinter import ttk

from orchestrator.agent import OrchestratorAgent
from runtime.runtime import Runtime


DEFAULT_QUERY = "用户张三昨天9点网速慢，请分析原因"


class GuiAgentExecutor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Wireless Agent Executor")
        self.geometry("980x680")
        self.minsize(760, 520)

        self.runtime = Runtime(OrchestratorAgent())
        self.events = queue.Queue()
        self.worker = None

        self._build_ui()
        self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        query_frame = ttk.Frame(self, padding=(16, 16, 16, 8))
        query_frame.grid(row=0, column=0, sticky="ew")
        query_frame.columnconfigure(0, weight=1)

        self.query_var = tk.StringVar(value=DEFAULT_QUERY)
        query_entry = ttk.Entry(query_frame, textvariable=self.query_var)
        query_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        query_entry.bind("<Return>", lambda _event: self.execute())

        self.run_button = ttk.Button(query_frame, text="运行分析", command=self.execute)
        self.run_button.grid(row=0, column=1)

        content = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        content.grid(row=1, column=0, sticky="nsew", padx=16, pady=(8, 16))

        result_frame = ttk.Labelframe(content, text="诊断结果", padding=8)
        trace_frame = ttk.Labelframe(content, text="执行轨迹", padding=8)
        content.add(result_frame, weight=3)
        content.add(trace_frame, weight=2)

        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)
        trace_frame.columnconfigure(0, weight=1)
        trace_frame.rowconfigure(0, weight=1)

        self.result_text = tk.Text(result_frame, wrap="word", font=("Microsoft YaHei UI", 10))
        self.result_text.grid(row=0, column=0, sticky="nsew")
        result_scrollbar = ttk.Scrollbar(result_frame, orient="vertical", command=self.result_text.yview)
        result_scrollbar.grid(row=0, column=1, sticky="ns")
        self.result_text.configure(yscrollcommand=result_scrollbar.set)

        self.trace_text = tk.Text(trace_frame, wrap="word", font=("Consolas", 10), width=32)
        self.trace_text.grid(row=0, column=0, sticky="nsew")
        trace_scrollbar = ttk.Scrollbar(trace_frame, orient="vertical", command=self.trace_text.yview)
        trace_scrollbar.grid(row=0, column=1, sticky="ns")
        self.trace_text.configure(yscrollcommand=trace_scrollbar.set)

        status_frame = ttk.Frame(self, padding=(16, 0, 16, 12))
        status_frame.grid(row=2, column=0, sticky="ew")
        status_frame.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

    def execute(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        query = self.query_var.get()
        self._set_running(True)
        self._replace_text(self.result_text, "正在分析...\n")
        self._replace_text(self.trace_text, "等待 Agent 执行...\n")

        self.worker = threading.Thread(target=self._run_agent, args=(query,), daemon=True)
        self.worker.start()

    def _run_agent(self, query: str) -> None:
        try:
            result = self.runtime.run(query)
            trace = "\n".join(f"{index}. {event}" for index, event in enumerate(self.runtime.agent.trace, start=1))
            self.events.put(("success", result, trace))
        except Exception as exc:
            self.events.put(("error", str(exc), ""))

    def _poll_events(self) -> None:
        try:
            kind, result, trace = self.events.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_events)
            return

        if kind == "success":
            self._replace_text(self.result_text, result)
            self._replace_text(self.trace_text, trace)
            self.status_var.set("分析完成")
        else:
            self._replace_text(self.result_text, f"执行失败：{result}")
            self._replace_text(self.trace_text, trace)
            self.status_var.set("执行失败")
        self._set_running(False)
        self.after(100, self._poll_events)

    def _set_running(self, running: bool) -> None:
        self.run_button.configure(state="disabled" if running else "normal")
        self.status_var.set("分析中..." if running else "就绪")

    def _replace_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, content)
        widget.configure(state="disabled")


def main() -> None:
    app = GuiAgentExecutor()
    app.mainloop()


if __name__ == "__main__":
    main()

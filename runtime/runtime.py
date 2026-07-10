from domain.diagnostics import Diagnosis, format_trace


class Runtime:
    def __init__(self, agent):
        self.agent = agent

    def run(self, query: str) -> str:
        diagnosis = self.run_structured(query)
        trace = format_trace(getattr(self.agent, "trace", []))
        return f"{diagnosis.to_text()}\n\n执行轨迹:\n{trace}"

    def run_structured(self, query: str) -> Diagnosis:
        query = query.strip()
        if not query:
            raise ValueError("请输入需要分析的无线网络问题。")
        return self.agent.run(query)

from duckduckgo_search import DDGS

class SearchEngine:
    def __init__(self, max_results: int = 3):
        self.max_results = max_results

    def search(self, query: str) -> str:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=self.max_results))
        except Exception as e:
            return f"搜索失败: {e}"
        if not results:
            return "未找到相关结果。"
        snippets = []
        for idx, item in enumerate(results, 1):
            title = item.get('title', '无标题')
            body = item.get('body', '')
            href = item.get('href', '')
            snippets.append(f"【{idx}】{title}\n{body}\n链接: {href}")
        return "\n\n".join(snippets)

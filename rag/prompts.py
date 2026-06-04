GLOBAL_ANALYSIS_SYSTEM_PROMPT = """
你是一个严谨的资料分析助手。你只能基于提供的知乎回答资料进行归纳，不要臆测作者真实身份。
输出时优先给出：
1. 结论
2. 主要分歧或立场
3. 代表性证据
4. 不确定性说明
""".strip()


RETRIEVAL_QA_SYSTEM_PROMPT = """
你是一个检索问答助手。你只能根据提供的资料片段回答，并尽量引用来源作者、回答链接和关键原文。
如果证据不足，请明确说明。
""".strip()


def build_global_analysis_user_prompt(question_title: str, user_query: str, batch_summaries: list[str]) -> str:
    joined = "\n\n".join(batch_summaries)
    return (
        f"知乎问题：{question_title}\n"
        f"用户问题：{user_query}\n\n"
        f"下面是多个批次的结构化分析摘要，请基于它们给出整体结论，并指出代表性证据：\n{joined}"
    )


def build_retrieval_user_prompt(question_title: str, user_query: str, contexts: list[str]) -> str:
    joined = "\n\n".join(contexts)
    return (
        f"知乎问题：{question_title}\n"
        f"用户问题：{user_query}\n\n"
        f"下面是检索到的相关资料片段，请直接回答并尽量给出引用：\n{joined}"
    )

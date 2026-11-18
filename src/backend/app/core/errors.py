"""带错误码的前置条件错误：这一步没法做，不是出了故障，是它需要的东西还没有。

例如「关联的文献库里一篇可用的论文都没有」。以前这类情况写成
``ValueError("IDEA_CONTEXT_EMPTY: …")``，错误码埋在文字里，任务停下来时记录的是
笼统的 ``INVALID_OUTPUT``，界面只能再从原始报错里把码抠出来。

放在 core 里而不是 ``agents/voyage/failures.py``：services 也要抛它，而 import
``app.agents.voyage`` 会把全部动作模块带进来（循环依赖）。failures.py 从这里转出。
"""


class PreconditionError(ValueError):
    """前置条件不满足。重试没有用，要人先把条件补上。

    - ``code``：稳定的错误码，界面按它给文案；
    - ``user_message``：能直接给用户看的一句话；
    - ``str(exc)`` 是 ``"CODE: 说明"``，原来按子串找错误码的地方照常工作。

    继承 ``ValueError``：这些地方原来抛的就是它，已有的 ``except ValueError`` 不用改。
    任务失败归类（``failures.classify_failure``）先看异常自己声明的 ``transient``，
    所以不会再落到 ``ValueError`` 的兜底码上。
    """

    transient: bool = False

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.user_message = message
        super().__init__(f"{code}: {message}")


__all__ = ["PreconditionError"]

"""对话智能体的循环内核：Navigator（定计划）· Helm（执行）· Sextant（验收）。

与 Voyage 那套同名同分工，但**不是同一套代码**：Voyage 的三件套跑在 worker 里、
遍历预先规划好的步骤行、失败语义是暂停重规划；对话没有预先计划（下一步由模型逐
token 决定）、必须在 HTTP 连接上流式输出、失败语义是「告诉用户，会话继续活着」。
硬套过来只会得到一个到处是 if 的四不像。

共享的是**纪律**，不是实现：

- Navigator 决定「下一步怎么走」——还剩几轮、这轮能用哪些工具、什么时候该收尾。
  它不碰模型也不碰工具，因此可以脱离一切 IO 单测。
- Helm 只管执行，**异常绝不外抛**：工具炸了、超时了、参数不是 JSON，全部转成结果
  回喂给模型，让它自己纠正。一次工具失败不该让整轮对话死掉。
- Sextant 做验收，**确定性检查优先**：能靠数数和对照得出的结论不花模型的钱。
  与 Voyage 的 Sextant 同一条原则。
"""

from app.agents.chat.core.helm import Helm
from app.agents.chat.core.navigator import Navigator, RoundPlan
from app.agents.chat.core.sextant import Sextant, Verdict

__all__ = ["Helm", "Navigator", "RoundPlan", "Sextant", "Verdict"]

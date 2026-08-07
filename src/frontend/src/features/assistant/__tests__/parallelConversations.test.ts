import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/* ============================================================
   对话之间的状态必须各自独立。

   这条测试的由来：运行状态原先是单个 `busy` 布尔加单个 `abortRef`，整个面板共用。
   于是 A 在跑时切到 B，B 也显示停止按钮；点下去掐掉的是 A 那条流。而切走之后
   A 在列表里的呼吸灯立刻灭了，尽管它还在跑。

   这些都不是类型错误——把表换回布尔，编译器一句话都不会说。所以在源码层面钉住。
   ============================================================ */

const panel = readFileSync(
  fileURLToPath(new URL('../AssistantPanel.tsx', import.meta.url)),
  'utf-8',
);
const rail = readFileSync(
  fileURLToPath(new URL('../ConversationRail.tsx', import.meta.url)),
  'utf-8',
);

describe('并行对话的状态', () => {
  it('在跑的是一组会话，不是一个布尔', () => {
    expect(panel).toContain('const [running, setRunning] = useState<Set<string>>');
    // 反面：别再回到面板级的单个开关
    expect(panel).not.toContain('const [busy, setBusy] = useState');
  });

  it('每场对话各有各的中止函数', () => {
    expect(panel).toContain('const abortById = useRef<Map<string, () => void>>');
    expect(panel).not.toContain('const abortRef = useRef<(() => void) | null>');
  });

  it('停止只停一场，要指名道姓', () => {
    // stopConv(id)：不接受"停掉当前那条流"这种含糊说法
    expect(panel).toMatch(/const stopConv = useCallback\(\(id: string \| null\)/);
    expect(panel).toContain('stopConv(convId)');
  });

  it('「当前这场在跑吗」是从那组里查出来的', () => {
    expect(panel).toContain('const busy = running.has(convId ?? NEW_CONV)');
  });

  it('列表里所有在跑的都亮灯，不只当前这场', () => {
    // 传集合而不是「当前 id」：切走之后那条还在跑，灯要一直亮到它结束
    expect(panel).toContain('runningIds={running}');
    expect(rail).toContain('runningIds?: ReadonlySet<string>');
    expect(rail).toContain('runningIds?.has(row.id)');
    expect(rail).not.toContain('row.id === runningId');
  });

  it('新会话拿到真 id 后要改名，否则它的灯不亮', () => {
    // 新会话起跑时 convId 还是 null，先用占位记着；建好之后必须换成真 id
    expect(panel).toContain("const NEW_CONV = '__new__'");
    expect(panel).toContain('next.delete(NEW_CONV)');
  });
});

describe('「换一个回答」', () => {
  it('往回找最近的用户轮，而不是假定就在前一格', () => {
    // 以前是 turns[i - 1]。新发起的一轮确实是「用户 + 助手」相邻，所以刚问完点是好的；
    // 但回放历史之后 tool_results 会并进上一轮，两者未必相邻，取到空串就什么都不发生。
    // 这个按钮于是变成「看起来完好、点了没反应」，是最难查的那种坏。
    expect(panel).toContain('const retryFrom = useCallback(');
    expect(panel).toContain('if (turn?.role !== \'user\') continue');
    expect(panel).toContain('onClick={() => void retryFrom(i)}');
    expect(panel).not.toContain('const question = turns[i - 1]');
  });
});
